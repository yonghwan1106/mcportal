# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Yong Park
"""큐레이션 오버레이 — 자동생성 스펙 위에 얹는 수작업 메타데이터 층.

MCPortal 의 2층 구조에서 아래층(엔진)은 공공 스펙을 기계적으로 OpenAPI 로
정규화하고, 위층(큐레이션)은 사람이 확인한 설명·예시·힌트를 얹는다. 위층은
**전부 데이터(JSON)** 이며, 이 모듈은 그 데이터를 읽고 검증하고 병합하는
**도메인 지식이 0인 일반 엔진**이다.

두 층이 만나는 자리는 프리셋 번들 디렉터리 하나다.

``<루트>/<preset_id>/``
    ``source.json``
        정찰이 취득한 스펙 원문 + 출처·취득일·지문(아래층 입력).
    ``curation.json``
        사람이 확인한 설명·예시·힌트(위층 입력). 없어도 컴파일된다.
    ``openapi.json``
        두 층을 병합해 산출한 OpenAPI 3.1 문서(커밋 대상).

설계 원칙
---------
* **도메인 로직 0줄** — 이 파일에는 특정 서비스의 식별자·기관명·파라미터명이
  하나도 등장하지 않는다. 도메인 지식은 전부 번들의 JSON 에 있다.
* **결정론** — 같은 ``source.json`` + ``curation.json`` 이면 **바이트 동일한**
  ``openapi.json`` 이 나온다. 매핑 순회는 전부 ``sorted()`` 를 거치므로 JSON 의
  키 순서를 뒤집어도 결과가 같다.
* **사실은 큐레이션이 바꾸지 않는다** — 타입·위치·필수 여부·경로·메서드는 원
  스펙 선언이 정본이다. 사실을 교정하는 통로는 근거를 요구하는 두 가지
  (``parameters_remove`` · ``response.unresolved``)뿐이다.
* **실패는 한국어로 구체적으로** — 어떤 키가 왜 틀렸고 무엇이 허용되는지를
  :class:`CurationError` 메시지에 적는다.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Union

from ..replay.scrub import CREDENTIAL_PARAM_NAMES, find_key_assignments
from .openapi import (
    CompileOptions,
    CompiledSpec,
    build_openapi,
    cast_scalar,
    dumps,
    write_spec,
)
from .sources import (
    OperationSpec,
    ParamSpec,
    SourceKind,
    SourceSpec,
    load_source,
    unresolved_schema_operations,
)

__all__ = [
    "CURATION_SCHEMA_VERSION",
    "Curation",
    "CurationError",
    "CurationReport",
    "ENV_PRESETS_ROOT",
    "OperationCuration",
    "PRESET_CURATION_FILENAME",
    "PRESET_OPENAPI_FILENAME",
    "PRESET_SOURCE_FILENAME",
    "PRESET_SOURCE_SCHEMA_VERSION",
    "PROVENANCE_KEYS",
    "ParamCuration",
    "ParamRemoval",
    "PresetInfo",
    "ResponseCuration",
    "ServiceCuration",
    "apply_curation",
    "apply_curation_with_report",
    "check_preset",
    "compile_preset",
    "default_presets_root",
    "iter_presets",
    "load_curation",
    "load_preset",
    "preset_info",
    "read_curation",
    "read_preset_source",
    "validate_curation",
    "write_preset",
]

#: 큐레이션 문서 스키마 버전.
CURATION_SCHEMA_VERSION: int = 1

#: 프리셋 소스 래퍼 스키마 버전.
PRESET_SOURCE_SCHEMA_VERSION: int = 1

#: 프리셋 루트를 지정하는 환경변수.
ENV_PRESETS_ROOT: str = "MCPORTAL_PRESETS"

#: 프리셋 번들의 고정 파일명.
PRESET_SOURCE_FILENAME: str = "source.json"
PRESET_CURATION_FILENAME: str = "curation.json"
PRESET_OPENAPI_FILENAME: str = "openapi.json"

PathLike = Union[str, Path]

#: ``service.version`` 이 만족해야 하는 형태.
_VERSION_RE = re.compile(r"^\d+\.\d+\.\d+$")

#: ``example_prompts`` 원소 길이 제한.
_PROMPT_MIN_LENGTH = 1
_PROMPT_MAX_LENGTH = 200

#: 큐레이션 자유문자열에서 걸러 낼 인증키 이름들. 소스가 선언한 ``key_param`` 은
#: 병합 시점에 여기에 더해진다. 사람이 손으로 채우는 문장에 실키가 붙은 예시
#: URL 을 그대로 옮겨 적는 것은 흔한 사고이므로, 커밋 전에 여기서 막는다.
#: 정본은 :data:`mcportal.replay.scrub.CREDENTIAL_PARAM_NAMES` 다.
_CURATION_KEY_PARAMS: tuple[str, ...] = CREDENTIAL_PARAM_NAMES

#: 큐레이션이 건드릴 수 없는 필드들(어느 계층에 있어도 거부한다).
#: 파라미터의 타입·위치·필수 여부, 오퍼레이션의 경로·메서드·식별자는 원 스펙
#: 선언이 정본이며, 그것이 틀렸다면 스펙 소스를 다시 받는 것이 옳다.
_FORBIDDEN_FIELDS: frozenset[str] = frozenset(
    {
        "type",
        "location",
        "required",
        "item_type",
        "response_schema",
        "request_body_schema",
        "path",
        "method",
        "operation_id",
    }
)

_TOP_KEYS: frozenset[str] = frozenset(
    {"mcportal_curation", "preset_id", "service", "operations"}
)
_SERVICE_KEYS: frozenset[str] = frozenset(
    {"group", "title", "version", "description", "license_note", "source_url", "notes"}
)
_OPERATION_KEYS: frozenset[str] = frozenset(
    {
        "summary",
        "description",
        "tags",
        "example_prompts",
        "response",
        "parameters",
        "parameters_remove",
    }
)
_PARAM_KEYS: frozenset[str] = frozenset(
    {"description", "example", "default", "enum", "enum_note"}
)
_RESPONSE_KEYS: frozenset[str] = frozenset({"unresolved", "reason"})
_REMOVAL_KEYS: frozenset[str] = frozenset({"name", "reason"})

_SOURCE_WRAPPER_KEYS: frozenset[str] = frozenset(
    {
        "mcportal_preset_source",
        "preset_id",
        "service_id",
        "service_name",
        "source_kind",
        "key_param",
        "source_url",
        "fetched_at",
        "license_note",
        "provenance",
        "document",
    }
)

#: ``source.json.provenance`` 가 채워야 하는 키들(내용은 자유 형식).
#:
#: 이것은 **리포에 커밋되는 프리셋의 내용 규약**이지 로더의 안전 조건이 아니다.
#: 그래서 여기서 하드 실패시키지 않는다 — 스키마 검증 규칙표(V1~V12)에도 이
#: 항목은 없고, 임시 디렉터리에 만든 합성 번들까지 출처 메타를 갖추라고 요구하면
#: 엔진이 데이터 정책을 강제하는 자리가 되어 버린다. 실제 커밋 번들이 이 여섯 키를
#: 모두 채웠는지는 ``tests/test_presets.py`` 가 검사한다.
PROVENANCE_KEYS: tuple[str, ...] = (
    "spec_origin",
    "spec_url",
    "raw_files",
    "raw_sha256",
    "acquisition",
    "personal_data_scan",
)

#: 오퍼레이션 식별자 형태(IR 불변식 I1).
_OPERATION_ID_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

#: E1 블록의 머리말.
_PROMPT_BLOCK_HEADING = "예시 프롬프트:"


class CurationError(ValueError):
    """큐레이션 오버레이를 읽거나 적용할 수 없을 때 발생한다(한국어 메시지)."""


# ---------------------------------------------------------------------------
# 오버레이 자료형
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class ParamCuration:
    """파라미터 1개의 오버레이."""

    description: str | None = None
    example: str | None = None
    default: str | None = None
    enum: tuple[str, ...] = ()
    enum_note: str | None = None


@dataclass(frozen=True)
class ParamRemoval:
    """파라미터 제거 지시(근거 필수)."""

    name: str
    reason: str


@dataclass(frozen=True)
class ResponseCuration:
    """응답 스키마 강등 지시(근거 필수)."""

    unresolved: bool
    reason: str


@dataclass(frozen=True)
class OperationCuration:
    """오퍼레이션 1개의 오버레이."""

    summary: str | None = None
    description: str | None = None
    tags: tuple[str, ...] = ()
    example_prompts: tuple[str, ...] = ()
    response: ResponseCuration | None = None
    parameters: Mapping[str, ParamCuration] = field(default_factory=dict)
    parameters_remove: tuple[ParamRemoval, ...] = ()


@dataclass(frozen=True)
class ServiceCuration:
    """서비스 수준 오버레이."""

    group: str | None = None
    title: str | None = None
    version: str = "0.1.0"
    description: str | None = None
    license_note: str | None = None
    source_url: str | None = None
    notes: tuple[str, ...] = ()


@dataclass(frozen=True)
class Curation:
    """큐레이션 문서 1건."""

    preset_id: str
    service: ServiceCuration
    operations: Mapping[str, OperationCuration] = field(default_factory=dict)


@dataclass(frozen=True)
class CurationReport:
    """병합 1회의 적용 요약(문서·테스트·CLI 표시용)."""

    operations_curated: int
    parameters_curated: int
    parameters_removed: int
    responses_unresolved: int
    example_prompt_count: int


@dataclass(frozen=True)
class PresetInfo:
    """프리셋 번들 1건의 요약."""

    preset_id: str
    service_id: str
    service_name: str
    group: str | None
    source_kind: str
    directory: Path
    source_path: Path
    curation_path: Path | None
    openapi_path: Path
    operation_count: int
    unresolved_count: int
    license_note: str | None
    notes: tuple[str, ...]


# ---------------------------------------------------------------------------
# 스키마 검증 도우미(V2·V9·V12)
# ---------------------------------------------------------------------------
def _type_name(value: Any) -> str:
    """오류 메시지에 쓸 파이썬 타입 이름."""
    return type(value).__name__


def _expect_mapping(value: Any, *, path: str) -> Mapping[str, Any]:
    """매핑(JSON 객체)임을 확인한다(V12)."""
    if not isinstance(value, Mapping):
        raise CurationError(
            f"{path}의 타입이 올바르지 않습니다. 기대: 객체(JSON object), "
            f"받은 타입: {_type_name(value)}."
        )
    return value


def _expect_text(value: Any, *, path: str, allow_none: bool = False) -> str | None:
    """문자열임을 확인한다(V12). ``allow_none`` 이면 ``None`` 을 그대로 통과시킨다."""
    if value is None:
        if allow_none:
            return None
        raise CurationError(
            f"{path}의 타입이 올바르지 않습니다. 기대: 문자열, 받은 타입: NoneType."
        )
    if not isinstance(value, str):
        raise CurationError(
            f"{path}의 타입이 올바르지 않습니다. 기대: 문자열, "
            f"받은 타입: {_type_name(value)}. 숫자·불리언을 그대로 적지 말고 "
            "질의 문자열 표기 그대로 따옴표로 감싸세요."
        )
    return value


def _expect_bool(value: Any, *, path: str) -> bool:
    """불리언임을 확인한다(V12). 정수 ``0``/``1`` 도 거부한다."""
    if not isinstance(value, bool):
        raise CurationError(
            f"{path}의 타입이 올바르지 않습니다. 기대: 불리언(true/false), "
            f"받은 타입: {_type_name(value)}."
        )
    return value


def _expect_text_list(value: Any, *, path: str) -> tuple[str, ...]:
    """문자열 목록임을 확인하고 원 순서를 보존한 채 중복을 제거한다(V12)."""
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise CurationError(
            f"{path}의 타입이 올바르지 않습니다. 기대: 문자열 배열, "
            f"받은 타입: {_type_name(value)}."
        )
    items: list[str] = []
    for index, raw in enumerate(value):
        text = _expect_text(raw, path=f"{path}[{index}]")
        assert text is not None  # _expect_text 가 None 을 거부했다.
        if text not in items:
            items.append(text)
    return tuple(items)


def _reject_forbidden_keys(mapping: Mapping[str, Any], *, path: str) -> None:
    """금지 필드가 있으면 막는다(V9)."""
    hits = sorted(str(key) for key in mapping if str(key) in _FORBIDDEN_FIELDS)
    if hits:
        raise CurationError(
            f"{path}에 금지된 필드가 있습니다: {', '.join(hits)}. "
            "큐레이션은 스펙 사실을 바꾸지 않습니다. 타입·위치·필수 여부·경로·"
            "메서드·식별자는 원 스펙 선언이 정본이며, 그것이 틀렸다면 스펙 소스를 "
            "다시 받으세요. 사실 교정 통로는 근거를 요구하는 parameters_remove 와 "
            "response.unresolved 둘뿐입니다."
        )


def _reject_unknown_keys(
    mapping: Mapping[str, Any], allowed: frozenset[str], *, path: str
) -> None:
    """허용되지 않은 키가 있으면 막는다(V2). 오타를 조기에 잡기 위한 게이트다."""
    unknown = sorted(str(key) for key in mapping if str(key) not in allowed)
    if unknown:
        raise CurationError(
            f"{path}에 알 수 없는 키가 있습니다: {', '.join(unknown)}. "
            f"이 계층에서 허용되는 키: {', '.join(sorted(allowed))}."
        )


def _check_keys(
    mapping: Mapping[str, Any], allowed: frozenset[str], *, path: str
) -> None:
    """금지 필드(V9)를 먼저 보고 그다음 미지 키(V2)를 본다."""
    _reject_forbidden_keys(mapping, path=path)
    _reject_unknown_keys(mapping, allowed, path=path)


def _iter_strings(node: Any, *, path: str) -> list[tuple[str, str]]:
    """문서를 훑어 ``(필드 경로, 문자열)`` 쌍을 모은다(순회 순서 결정론)."""
    found: list[tuple[str, str]] = []
    if isinstance(node, str):
        found.append((path, node))
    elif isinstance(node, Mapping):
        for key in sorted(str(item) for item in node.keys()):
            found.extend(_iter_strings(node[key], path=f"{path}.{key}"))
    elif isinstance(node, Sequence) and not isinstance(node, (str, bytes)):
        for index, item in enumerate(node):
            found.extend(_iter_strings(item, path=f"{path}[{index}]"))
    return found


def _scan_key_assignments(
    document: Mapping[str, Any], *, path: str, key_params: Sequence[str]
) -> None:
    """큐레이션 자유문자열에 인증키 대입이 있으면 막는다(V7)."""
    for field_path, text in _iter_strings(document, path=path):
        found = find_key_assignments(text, key_params)
        if found:
            raise CurationError(
                f"큐레이션 문자열에 인증키 대입이 있습니다"
                f"(탐지된 파라미터: {', '.join(found)}, 검사한 필드 경로: "
                f"{field_path}). 큐레이션 문서는 커밋 대상이므로 자격증명이 실린 "
                "채로 둘 수 없습니다. 예시 URL 에서 인증키 대입 구간을 지우고 "
                "다시 시도하세요."
            )


# ---------------------------------------------------------------------------
# 큐레이션 문서 파싱
# ---------------------------------------------------------------------------
def _load_service(raw: Any) -> ServiceCuration:
    """``service`` 블록을 :class:`ServiceCuration` 으로 읽는다."""
    path = "curation.service"
    mapping = _expect_mapping(raw, path=path)
    _check_keys(mapping, _SERVICE_KEYS, path=path)

    version = mapping.get("version")
    if version is None:
        version_text = ServiceCuration.version
    else:
        version_text = _expect_text(version, path=f"{path}.version") or ""
        if not _VERSION_RE.match(version_text):
            raise CurationError(
                f"{path}.version 표기가 올바르지 않습니다: {version_text!r}. "
                "'주.부.수' 세 자리 숫자 형태(예: 0.1.0)여야 합니다."
            )

    notes_raw = mapping.get("notes")
    notes = (
        ()
        if notes_raw is None
        else _expect_text_list(notes_raw, path=f"{path}.notes")
    )

    return ServiceCuration(
        group=_expect_text(mapping.get("group"), path=f"{path}.group", allow_none=True),
        title=_expect_text(mapping.get("title"), path=f"{path}.title", allow_none=True),
        version=version_text,
        description=_expect_text(
            mapping.get("description"), path=f"{path}.description", allow_none=True
        ),
        license_note=_expect_text(
            mapping.get("license_note"), path=f"{path}.license_note", allow_none=True
        ),
        source_url=_expect_text(
            mapping.get("source_url"), path=f"{path}.source_url", allow_none=True
        ),
        notes=notes,
    )


def _load_param(raw: Any, *, path: str) -> ParamCuration:
    """파라미터 오버레이 1개를 읽는다."""
    mapping = _expect_mapping(raw, path=path)
    _check_keys(mapping, _PARAM_KEYS, path=path)
    enum_raw = mapping.get("enum")
    enum = () if enum_raw is None else _expect_text_list(enum_raw, path=f"{path}.enum")
    return ParamCuration(
        description=_expect_text(
            mapping.get("description"), path=f"{path}.description", allow_none=True
        ),
        example=_expect_text(
            mapping.get("example"), path=f"{path}.example", allow_none=True
        ),
        default=_expect_text(
            mapping.get("default"), path=f"{path}.default", allow_none=True
        ),
        enum=enum,
        enum_note=_expect_text(
            mapping.get("enum_note"), path=f"{path}.enum_note", allow_none=True
        ),
    )


def _load_response(raw: Any, *, path: str) -> ResponseCuration:
    """응답 강등 지시를 읽는다(V10: ``unresolved`` 가 참이면 근거 필수)."""
    mapping = _expect_mapping(raw, path=path)
    _check_keys(mapping, _RESPONSE_KEYS, path=path)
    if "unresolved" not in mapping:
        raise CurationError(
            f"{path}에 'unresolved' 필드가 없습니다. 응답 강등 지시는 "
            "{\"unresolved\": true, \"reason\": \"...\"} 형태여야 합니다."
        )
    unresolved = _expect_bool(mapping.get("unresolved"), path=f"{path}.unresolved")
    reason = _expect_text(mapping.get("reason"), path=f"{path}.reason", allow_none=True)
    if unresolved and not (reason or "").strip():
        raise CurationError(
            f"{path}.reason 이 비어 있습니다. 응답 스키마를 미확정으로 강등하는 것은 "
            "스펙 사실을 뒤집는 조치이므로 근거를 남기세요."
        )
    return ResponseCuration(unresolved=unresolved, reason=reason or "")


def _load_removals(raw: Any, *, path: str) -> tuple[ParamRemoval, ...]:
    """파라미터 제거 지시 목록을 읽는다(V10: 근거 필수)."""
    if isinstance(raw, (str, bytes)) or not isinstance(raw, Sequence):
        raise CurationError(
            f"{path}의 타입이 올바르지 않습니다. 기대: 객체 배열, "
            f"받은 타입: {_type_name(raw)}."
        )
    removals: list[ParamRemoval] = []
    seen: set[str] = set()
    for index, item in enumerate(raw):
        item_path = f"{path}[{index}]"
        mapping = _expect_mapping(item, path=item_path)
        _check_keys(mapping, _REMOVAL_KEYS, path=item_path)
        name = _expect_text(mapping.get("name"), path=f"{item_path}.name")
        assert name is not None
        reason = _expect_text(
            mapping.get("reason"), path=f"{item_path}.reason", allow_none=True
        )
        if not (reason or "").strip():
            raise CurationError(
                f"{item_path}.reason 이 비어 있습니다. 파라미터 제거는 스펙 사실을 "
                "바꾸는 조치이므로 근거를 남기세요."
            )
        if name in seen:
            raise CurationError(
                f"{item_path}.name 이 중복됩니다: {name!r}. 같은 파라미터를 두 번 "
                "제거할 수 없습니다."
            )
        seen.add(name)
        removals.append(ParamRemoval(name=name, reason=reason or ""))
    return tuple(removals)


def _load_operation(raw: Any, *, path: str) -> OperationCuration:
    """오퍼레이션 오버레이 1개를 읽는다."""
    mapping = _expect_mapping(raw, path=path)
    _check_keys(mapping, _OPERATION_KEYS, path=path)

    tags_raw = mapping.get("tags")
    tags = () if tags_raw is None else _expect_text_list(tags_raw, path=f"{path}.tags")

    prompts_raw = mapping.get("example_prompts")
    prompts = (
        ()
        if prompts_raw is None
        else _expect_text_list(prompts_raw, path=f"{path}.example_prompts")
    )
    for index, prompt in enumerate(prompts):
        length = len(prompt.strip())
        if not _PROMPT_MIN_LENGTH <= length <= _PROMPT_MAX_LENGTH:
            raise CurationError(
                f"{path}.example_prompts[{index}] 의 길이가 범위를 벗어났습니다"
                f"(길이 {length}). 각 예시 프롬프트는 "
                f"{_PROMPT_MIN_LENGTH}~{_PROMPT_MAX_LENGTH}자여야 합니다."
            )

    response_raw = mapping.get("response")
    response = (
        None
        if response_raw is None
        else _load_response(response_raw, path=f"{path}.response")
    )

    parameters_raw = mapping.get("parameters")
    parameters: dict[str, ParamCuration] = {}
    if parameters_raw is not None:
        parameters_map = _expect_mapping(parameters_raw, path=f"{path}.parameters")
        for name in sorted(str(key) for key in parameters_map.keys()):
            parameters[name] = _load_param(
                parameters_map[name], path=f"{path}.parameters.{name}"
            )

    removals_raw = mapping.get("parameters_remove")
    removals = (
        ()
        if removals_raw is None
        else _load_removals(removals_raw, path=f"{path}.parameters_remove")
    )

    return OperationCuration(
        summary=_expect_text(
            mapping.get("summary"), path=f"{path}.summary", allow_none=True
        ),
        description=_expect_text(
            mapping.get("description"), path=f"{path}.description", allow_none=True
        ),
        tags=tags,
        example_prompts=prompts,
        response=response,
        parameters=parameters,
        parameters_remove=removals,
    )


def load_curation(document: Mapping[str, Any]) -> Curation:
    """큐레이션 문서(JSON 객체)를 :class:`Curation` 으로 읽는다.

    문서 단독으로 확인 가능한 규칙만 여기서 본다(버전·미지 키·금지 필드·근거
    누락·타입·인증키 대입). 소스와 대조해야 하는 참조 무결성은
    :func:`validate_curation` 이 담당한다.

    Args:
        document: ``curation.json`` 의 내용.

    Returns:
        검증을 통과한 :class:`Curation`.

    Raises:
        CurationError: 스키마 버전 불일치, 미지의 키, 금지 필드, 타입 불일치,
            근거 누락, 자유문자열의 인증키 대입 등.
    """
    doc = _expect_mapping(document, path="curation")
    version = doc.get("mcportal_curation")
    if not _is_schema_version(version, CURATION_SCHEMA_VERSION):
        raise CurationError(
            f"지원하지 않는 큐레이션 스키마 버전입니다: {version!r} "
            f"(받은 타입: {type(version).__name__}). "
            f"현재 지원 버전은 정수 {CURATION_SCHEMA_VERSION} 입니다"
            f'(문서 최상위에 {{"mcportal_curation": {CURATION_SCHEMA_VERSION}}} 이 '
            "필요합니다)."
        )
    _check_keys(doc, _TOP_KEYS, path="curation")

    preset_id = _expect_text(doc.get("preset_id"), path="curation.preset_id")
    assert preset_id is not None
    service = _load_service(doc.get("service"))

    operations_raw = doc.get("operations")
    operations: dict[str, OperationCuration] = {}
    if operations_raw is not None:
        operations_map = _expect_mapping(operations_raw, path="curation.operations")
        for operation_id in sorted(str(key) for key in operations_map.keys()):
            operations[operation_id] = _load_operation(
                operations_map[operation_id],
                path=f"curation.operations.{operation_id}",
            )

    _scan_key_assignments(doc, path="curation", key_params=_CURATION_KEY_PARAMS)
    return Curation(preset_id=preset_id, service=service, operations=operations)


def _no_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """같은 계층에 중복 키가 있으면 :class:`CurationError` 로 막는 JSON 훅.

    ``json.loads`` 의 표준 동작은 "마지막 값만 남긴다"라서, 손으로 쓰는 문서에서
    블록을 복붙해 늘리다 같은 이름을 두 번 적으면 **앞 블록이 통째로 조용히
    사라진다**(적대 리뷰 F11). §5-2 가 미지 키를 막는 이유가 "오타를 조기에
    잡기 위해"인데, 중복 키는 오타보다 더 조용히 내용을 지운다.

    Args:
        pairs: ``object_pairs_hook`` 이 넘겨주는 (키, 값) 목록.

    Returns:
        중복이 없을 때의 딕셔너리.

    Raises:
        CurationError: 같은 계층에 같은 키가 두 번 이상 나올 때.
    """
    seen: set[str] = set()
    duplicated: list[str] = []
    for key, _value in pairs:
        if key in seen and key not in duplicated:
            duplicated.append(key)
        seen.add(key)
    if duplicated:
        raise CurationError(
            f"같은 계층에 중복된 키가 있습니다: {', '.join(sorted(duplicated))}. "
            "JSON 표준 동작은 뒤엣것으로 덮어쓰기라 앞 블록이 조용히 사라집니다. "
            "중복을 지우고 하나로 합치세요."
        )
    return dict(pairs)


def _read_json(path: Path, *, what: str) -> Any:
    """UTF-8 JSON 파일을 읽는다(파손·중복 키는 :class:`CurationError`)."""
    text = path.read_text(encoding="utf-8")
    try:
        return json.loads(text, object_pairs_hook=_no_duplicate_keys)
    except json.JSONDecodeError as exc:
        raise CurationError(
            f"{what}을(를) JSON 으로 읽을 수 없습니다: {path} ({exc})."
        ) from exc
    except CurationError as exc:
        raise CurationError(f"{what}({path})에 문제가 있습니다: {exc}") from exc


def read_curation(path: PathLike) -> Curation:
    """``curation.json`` 파일을 읽어 :class:`Curation` 으로 돌려준다.

    파일 부재는 이 함수가 판단하지 않는다 — 번들에 큐레이션이 없을 수 있으므로
    존재 여부는 호출자가 먼저 확인한다(없으면 :class:`FileNotFoundError`).

    Args:
        path: 큐레이션 문서 경로.

    Returns:
        검증을 통과한 :class:`Curation`.

    Raises:
        CurationError: JSON 파손 또는 스키마 위반.
        OSError: 파일을 읽을 수 없을 때(부재 포함).
    """
    file_path = Path(path)
    return load_curation(_read_json(file_path, what="큐레이션 문서"))


# ---------------------------------------------------------------------------
# 소스 대조 검증(V4·V5·V6·V11)
# ---------------------------------------------------------------------------
def _is_schema_version(value: Any, expected: int) -> bool:
    """스키마 버전 필드가 **정수** ``expected`` 인지 엄격하게 판정한다.

    ``version != EXPECTED`` 는 파이썬 값 비교라 ``True == 1`` · ``1.0 == 1`` 이
    모두 참이 되어 ``true`` 와 ``1.0`` 이 버전 1 로 통과했다(적대 리뷰 F9).
    같은 모듈의 ``_expect_bool`` 은 "정수 0/1 도 거부한다"고 명시하며 엄격한데
    버전 필드만 느슨했던 비대칭을 없앤다(V1/V12 의 취지 = 오타 조기 검출).

    Args:
        value: 문서에서 읽은 버전 값.
        expected: 지원 버전(정수).

    Returns:
        정수이고(``bool`` 제외) 값이 같으면 True.
    """
    if isinstance(value, bool) or not isinstance(value, int):
        return False
    return value == expected


def _check_param_value_types(
    overlay: ParamCuration, param: ParamSpec, *, where: str
) -> None:
    """오버레이의 ``example`` · ``default`` · ``enum`` 이 선언 타입과 맞는지 본다(V14).

    큐레이션은 사실(``type``)을 바꿀 수 없다(§5-4). 그런데 병합은 타입을 보지
    않고 값을 얹고, 산출 단계의 캐스팅이 뒤에서 서로 다른 처리를 했다 —
    ``enum`` · ``default`` 는 **조용히 사라지고**(캐스팅 실패 시 키 생략)
    ``example`` 은 **스키마를 위반한 채 남았다**. 어느 쪽도 경고가 없어서
    큐레이션 작성자는 파일에 적은 값이 산출물에 없다는 것을 알 방법이 없었고,
    ``CurationReport.parameters_curated`` 는 "적용됨"처럼 1로 세어졌다
    (적대 리뷰 F8). 조용한 폐기 대신 커밋 전에 접는다.

    Args:
        overlay: 파라미터 오버레이.
        param: 소스가 선언한 파라미터.
        where: 오류 메시지에 실을 큐레이션 필드 경로.

    Raises:
        CurationError: 캐스팅할 수 없는 값이 있을 때(필드 경로 + 선언 타입 +
            받은 값을 함께 알려 준다). 타입이 맞아도 열거값에 없는 값이면
            :func:`_check_param_enum_membership` 이 이어서 막는다(V15).
    """
    declared = str(param.type)
    # 배열 파라미터의 열거값·예시는 원소 타입 기준으로 판정한다(산출 규칙과 동일).
    item_type = str(param.item_type or "string")
    scalar_type = item_type if declared == "array" else declared

    def fail(field: str, value: str, expected: str) -> None:
        raise CurationError(
            f"{where}.{field} 값이 소스가 선언한 타입과 맞지 않습니다: "
            f"{value!r} (선언 타입 {expected}). 큐레이션은 타입을 바꿀 수 "
            "없으므로(§5-4) 이 값은 산출물에서 조용히 버려지거나 스키마를 "
            "위반한 채 실립니다. 값을 고치거나 힌트를 지우세요."
        )

    if overlay.example is not None and cast_scalar(overlay.example, scalar_type) is None:
        fail("example", overlay.example, scalar_type)
    if overlay.default is not None and cast_scalar(overlay.default, scalar_type) is None:
        fail("default", overlay.default, scalar_type)
    for item in overlay.enum:
        if cast_scalar(item, scalar_type) is None:
            fail("enum", item, scalar_type)

    _check_param_enum_membership(overlay, param, where=where)


def _check_param_enum_membership(
    overlay: ParamCuration, param: ParamSpec, *, where: str
) -> None:
    """병합 후 유효 ``enum`` 과 ``example`` · ``default`` 가 모순되지 않는지 본다(V15).

    :func:`_check_param_value_types` 는 캐스팅 성공만 확인해서, 타입이 맞기만 하면
    **열거값에 없는 값**을 그대로 통과시켰다. 소스가 ``enum: [A, B]`` 를 선언한
    파라미터에 큐레이션이 ``example: "Z"`` 를 얹으면 산출 OpenAPI 가
    ``{"enum": ["A","B"], "examples": ["Z"]}`` 라는 자기모순 스키마가 되고, 그
    예시를 그대로 쓰는 LLM 툴콜은 100% 검증에 실패한다
    (2026-08-09 Advisor 검증 A1 — V14 확장).

    같은 원칙의 게이트가 이미 :mod:`~mcportal.compiler.sources` 쪽에 있다 —
    ``_apply_meta_examples`` 는 비표준 메타 블록의 예시값을 주입하기 전에
    ``value in param.enum`` 을 본다(적대 리뷰 F24). 소스에서 온 값은 막으면서
    사람이 손으로 쓴 큐레이션 값은 무검사로 통과시키던 비대칭을 없앤다.

    유효 ``enum`` 은 병합 규칙(:func:`_merge_param`)과 같다 — 오버레이가 비어
    있지 않으면 오버레이가 소스 열거값을 **교체**하므로, 검사 기준도 교체된
    새 열거값이다. ``example`` · ``default`` 도 병합 결과 기준으로 본다(오버레이가
    ``enum`` 만 갈아 끼우고 소스의 예시를 그대로 물려받는 조합도 같은 모순이다).

    Args:
        overlay: 파라미터 오버레이.
        param: 소스가 선언한 파라미터.
        where: 오류 메시지에 실을 큐레이션 필드 경로.

    Raises:
        CurationError: 병합 결과의 예시·기본값이 유효 열거값에 없을 때(문제
            파라미터 경로 · 값 · 허용값 목록을 함께 알려 준다).
    """
    effective_enum = tuple(overlay.enum) if overlay.enum else tuple(param.enum)
    if not effective_enum:
        return
    allowed = ", ".join(repr(item) for item in effective_enum)
    enum_origin = (
        "큐레이션이 교체한 enum" if overlay.enum else "소스가 선언한 enum"
    )
    for field_name in ("example", "default"):
        value = getattr(overlay, field_name)
        origin = f"{where}.{field_name}"
        if value is None:
            value = getattr(param, field_name)
            origin = f"소스 파라미터 {param.name!r} 의 {field_name}"
        if value is None or value in effective_enum:
            continue
        raise CurationError(
            f"{where}.{field_name} 이(가) 허용값 목록에 없습니다: {value!r} "
            f"(허용값 {allowed} — {enum_origin}). 값의 출처는 {origin} 입니다. "
            "이대로 두면 산출 OpenAPI 의 enum 과 examples/default 가 서로 어긋난 "
            "자기모순 스키마가 되어 그 예시를 따라 하는 호출이 항상 실패합니다. "
            "값을 허용값 중 하나로 고치거나 enum 을 함께 맞추세요."
        )


def validate_curation(curation: Curation, source: SourceSpec) -> None:
    """큐레이션이 참조하는 오퍼레이션·파라미터가 실제로 있는지 확인한다.

    Args:
        curation: 검증할 오버레이.
        source: 대조할 정규화 스펙 소스.

    Raises:
        CurationError: 없는 오퍼레이션·파라미터 참조(V4·V5), 인증키 이름을
            파라미터 키로 사용(V6), 필수 파라미터 제거 시도(V11).
    """
    by_id = {operation.operation_id: operation for operation in source.operations}
    key_lower = str(source.key_param).lower()

    for operation_id in sorted(curation.operations):
        operation = by_id.get(operation_id)
        if operation is None:
            available = ", ".join(sorted(by_id)) or "(없음)"
            raise CurationError(
                f"큐레이션이 참조한 오퍼레이션이 소스에 없습니다: {operation_id!r}. "
                f"소스가 실제로 가진 operation_id: {available}."
            )
        overlay = curation.operations[operation_id]
        actual = {param.name for param in operation.parameters}
        listed = ", ".join(sorted(actual)) or "(없음)"

        referenced: list[tuple[str, str]] = [
            (name, "parameters") for name in sorted(overlay.parameters)
        ]
        referenced.extend(
            (removal.name, "parameters_remove")
            for removal in overlay.parameters_remove
        )
        for name, where in referenced:
            if name.lower() == key_lower:
                raise CurationError(
                    f"오퍼레이션 {operation_id!r}의 {where} 가 인증키 이름"
                    f"({name!r})을 파라미터 키로 쓰고 있습니다. 인증키는 도구 인자가 "
                    "아니라 트랜스포트가 주입하므로 파라미터 목록에 존재하지 "
                    "않습니다(불변식 I3 위반)."
                )
            if name not in actual:
                raise CurationError(
                    f"큐레이션이 참조한 파라미터가 소스에 없습니다: {name!r} "
                    f"(오퍼레이션 {operation_id!r}, 위치 {where}). "
                    f"그 오퍼레이션의 실제 파라미터: {listed}."
                )

        # V13: 같은 파라미터를 힌트로 큐레이션하면서 제거까지 지정하는 모순 입력.
        # 병합은 제거를 먼저 적용하므로 오버레이가 조용히 증발한다(적대 리뷰 F12).
        contradictory = sorted(
            set(overlay.parameters) & {removal.name for removal in overlay.parameters_remove}
        )
        if contradictory:
            raise CurationError(
                f"오퍼레이션 {operation_id!r}에서 같은 파라미터를 제거와 힌트로 "
                f"동시에 지정할 수 없습니다: {', '.join(contradictory)}. "
                "제거가 항상 이기므로 `parameters` 에 적은 설명·예시는 산출물에 "
                "실리지 않습니다. 둘 중 하나만 남기세요."
            )

        required = {param.name for param in operation.parameters if param.required}
        for removal in overlay.parameters_remove:
            if removal.name in required:
                raise CurationError(
                    f"필수 파라미터를 제거할 수 없습니다: {removal.name!r} "
                    f"(오퍼레이션 {operation_id!r}). 필수 파라미터 제거는 스펙 사실 "
                    "변경입니다. 소스를 재취득하세요."
                )

        # V14: 오버레이 값이 소스가 선언한 타입으로 캐스팅되는지 확인한다.
        by_name = {param.name: param for param in operation.parameters}
        for name in sorted(overlay.parameters):
            _check_param_value_types(
                overlay.parameters[name],
                by_name[name],
                where=f"curation.operations.{operation_id}.parameters.{name}",
            )


# ---------------------------------------------------------------------------
# 병합(§5-3 텍스트 규칙 · §5-4 우선순위)
# ---------------------------------------------------------------------------
def _prompt_block(prompts: Sequence[str]) -> str:
    """E1 블록을 만든다(프롬프트가 없으면 빈 문자열)."""
    if not prompts:
        return ""
    lines = "\n".join(f"- {prompt}" for prompt in prompts)
    return f"{_PROMPT_BLOCK_HEADING}\n{lines}"


def _merge_description(base: str | None, prompts: Sequence[str]) -> str | None:
    """오퍼레이션 설명에 E1 블록을 붙인다(E3·E4 포함)."""
    block = _prompt_block(prompts)
    if base is None:
        merged = block
    elif block:
        merged = f"{base}\n\n{block}"
    else:
        merged = base
    merged = merged.strip()
    return merged or None


def _merge_enum_note(description: str | None, note: str | None) -> str | None:
    """파라미터 설명 말미에 ``enum_note`` 를 붙인다(E2·E4)."""
    if note is None:
        return description
    if description is None or not description.strip():
        merged = note
    else:
        merged = f"{description} {note}"
    merged = merged.strip()
    return merged or None


def _merge_param(param: ParamSpec, overlay: ParamCuration) -> ParamSpec:
    """파라미터 1개에 오버레이를 얹는다(타입·위치·필수 여부는 건드리지 않는다)."""
    description = (
        overlay.description if overlay.description is not None else param.description
    )
    return replace(
        param,
        description=_merge_enum_note(description, overlay.enum_note),
        example=overlay.example if overlay.example is not None else param.example,
        default=overlay.default if overlay.default is not None else param.default,
        enum=overlay.enum if overlay.enum else param.enum,
    )


def _sorted_params(params: Sequence[ParamSpec]) -> tuple[ParamSpec, ...]:
    """I5 정렬(필수 먼저, 그 안에서 이름 오름차순)을 다시 적용한다."""
    return tuple(sorted(params, key=lambda param: (not param.required, param.name)))


def _recheck_invariants(spec: SourceSpec) -> None:
    """병합 결과가 IR 불변식을 여전히 만족하는지 다시 본다.

    Raises:
        CurationError: 오퍼레이션 결손(I0), 식별자 형태·중복(I1), 경로 형태(I2),
            인증키 노출(I3), 오퍼레이션 정렬(I4), 파라미터 정렬(I5) 위반 시.
    """
    if not spec.operations:
        raise CurationError(
            "병합 결과에 오퍼레이션이 하나도 남지 않았습니다. 큐레이션은 "
            "오퍼레이션을 지울 수 없으므로 소스를 확인하세요."
        )
    key_lower = str(spec.key_param).lower()
    seen: set[str] = set()
    for operation in spec.operations:
        if not _OPERATION_ID_RE.match(operation.operation_id):
            raise CurationError(
                f"병합 결과의 operation_id 가 ASCII 식별자 규칙을 만족하지 "
                f"않습니다: {operation.operation_id!r} (불변식 I1)."
            )
        if operation.operation_id in seen:
            raise CurationError(
                f"병합 결과에서 operation_id 가 중복됩니다: "
                f"{operation.operation_id!r} (불변식 I1)."
            )
        seen.add(operation.operation_id)
        if not operation.path.startswith("/"):
            raise CurationError(
                f"병합 결과의 path 가 '/'로 시작하지 않습니다: {operation.path!r} "
                "(불변식 I2)."
            )
        for param in operation.parameters:
            if param.name.lower() == key_lower:
                raise CurationError(
                    f"병합 결과의 오퍼레이션 {operation.operation_id!r} 파라미터에 "
                    f"인증키({param.name!r})가 남아 있습니다(불변식 I3)."
                )
        if tuple(operation.parameters) != _sorted_params(operation.parameters):
            raise CurationError(
                f"병합 결과의 오퍼레이션 {operation.operation_id!r} 파라미터 정렬이 "
                "불변식 I5(필수 먼저·이름 오름차순)를 만족하지 않습니다."
            )
    order = [(operation.path, operation.method) for operation in spec.operations]
    if order != sorted(order):
        raise CurationError(
            "병합 결과의 operations 가 (path, method) 오름차순이 아닙니다"
            "(불변식 I4)."
        )


def _merged_free_text_targets(
    spec: SourceSpec,
) -> tuple[tuple[str, str, str], ...]:
    """V8 게이트가 훑을 (자리 이름, 교체 안내, 값) 목록을 만든다.

    서비스 메타 3필드뿐 아니라 **오퍼레이션·파라미터의 자유문자열까지** 담는다.
    큐레이션이 쓴 문자열은 V7(``_scan_key_assignments``)이 문서 전체를 깊이
    훑어 차단하는데, 소스에서 온 문자열은 3필드만 보던 비대칭이 있었다. 그 결과
    소스의 오퍼레이션/파라미터 설명에 ``serviceKey=<값>`` 이 있으면 병합 게이트·
    ``build_openapi`` · ``dumps`` 를 전부 통과해 **FastMCP 도구 설명까지 살아서
    도달**했다(적대 리뷰 F7). 파일로 쓰는 CLI 경로만 ``write_spec`` 이 막고
    라이브러리 경로(``compile_preset(...).document`` → ``server_from_spec``)는
    무방비였다.

    Args:
        spec: 병합 결과 스펙.

    Returns:
        ``(자리 이름, 큐레이션 교체 안내, 검사할 문자열)`` 튜플들.
    """
    targets: list[tuple[str, str, str]] = []

    def add(label: str, hint: str, value: Any) -> None:
        if isinstance(value, str) and value:
            targets.append((label, hint, value))

    add("SourceSpec.description", "`service.description`", spec.description)
    add("SourceSpec.license_note", "`service.license_note`", spec.license_note)
    add("SourceSpec.source_url", "`service.source_url`", spec.source_url)

    for operation in spec.operations:
        where = f"operations.{operation.operation_id}"
        add(f"{where}.summary", f"`{where}.summary`", operation.summary)
        add(f"{where}.description", f"`{where}.description`", operation.description)
        for tag in operation.tags:
            add(f"{where}.tags", f"`{where}.tags`", tag)
        for param in operation.parameters:
            field = f"{where}.parameters.{param.name}"
            add(f"{field}.description", f"`{field}.description`", param.description)
            add(f"{field}.example", f"`{field}.example`", param.example)
            add(f"{field}.default", f"`{field}.default`", param.default)
            for item in param.enum:
                add(f"{field}.enum", f"`{field}.enum`", item)
    return tuple(targets)


def _gate_merged_free_text(spec: SourceSpec) -> None:
    """병합 후 자유문자열에 인증키 대입이 남아 있으면 막는다(V8).

    소스 원문의 ``info.description`` 에 ``인증키=<자리표시자>`` 형태의 공식
    사용 안내가 들어 있는 것은 정부 스펙 문서의 관용 표기다. 그대로 두면
    산출 문서에 그 문자열이 실리므로, 큐레이션이 사람이 확인한 요약문으로
    **교체**하도록 여기서 조기에 차단한다.

    검사 범위는 서비스 메타 3필드 + **오퍼레이션(summary·description·tags)과
    파라미터(description·example·default·enum)** 다
    (:func:`_merged_free_text_targets`).

    Raises:
        CurationError: 병합 결과의 자유문자열에 인증키 대입이 남아 있을 때
            (어느 자리이고 어느 큐레이션 필드로 교체해야 하는지 함께 알려 준다).
    """
    names = (*_CURATION_KEY_PARAMS, str(spec.key_param))
    for label, curation_field, value in _merged_free_text_targets(spec):
        found = find_key_assignments(value, names)
        if found:
            raise CurationError(
                f"병합 후에도 {label} 에 인증키 대입이 남아 있습니다"
                f"(탐지된 파라미터: {', '.join(found)}). "
                f"{curation_field}을 큐레이션에서 교체하거나 소스를 다시 "
                "받으세요. 원 스펙 문서의 사용 안내 문구를 사람이 확인한 "
                "요약문으로 바꾸면 됩니다. 시크릿 게이트를 완화하는 방식으로는 "
                "해결하지 않습니다."
            )


def apply_curation_with_report(
    source: SourceSpec, curation: Curation
) -> tuple[SourceSpec, CurationReport]:
    """오버레이를 얹은 새 :class:`SourceSpec` 과 적용 요약을 함께 돌려준다.

    원본 :class:`SourceSpec` 은 변형하지 않는다(``dataclasses.replace`` 만 쓴다).

    Args:
        source: 자동생성된 정규화 스펙 소스.
        curation: 얹을 오버레이.

    Returns:
        ``(병합된 SourceSpec, CurationReport)``.

    Raises:
        CurationError: 참조 무결성 위반(V4~V6·V11), 병합 후 불변식 위반,
            병합 후 인증키 대입 잔존(V8).
    """
    validate_curation(curation, source)
    service = curation.service

    operations: list[OperationSpec] = []
    operations_curated = 0
    parameters_curated = 0
    parameters_removed = 0
    responses_unresolved = 0
    example_prompt_count = 0

    for operation in source.operations:
        overlay = curation.operations.get(operation.operation_id)
        if overlay is None:
            operations.append(operation)
            continue
        operations_curated += 1
        example_prompt_count += len(overlay.example_prompts)

        base_description = (
            overlay.description
            if overlay.description is not None
            else operation.description
        )
        response_schema = operation.response_schema
        if overlay.response is not None and overlay.response.unresolved:
            response_schema = None
            responses_unresolved += 1

        removed = {removal.name for removal in overlay.parameters_remove}
        parameters: list[ParamSpec] = []
        for param in operation.parameters:
            if param.name in removed:
                parameters_removed += 1
                continue
            param_overlay = overlay.parameters.get(param.name)
            if param_overlay is not None:
                param = _merge_param(param, param_overlay)
                parameters_curated += 1
            parameters.append(param)

        operations.append(
            replace(
                operation,
                summary=(
                    overlay.summary if overlay.summary is not None else operation.summary
                ),
                description=_merge_description(
                    base_description, overlay.example_prompts
                ),
                tags=overlay.tags if overlay.tags else operation.tags,
                parameters=_sorted_params(parameters),
                response_schema=response_schema,
            )
        )

    merged = replace(
        source,
        operations=tuple(operations),
        description=(
            service.description
            if service.description is not None
            else source.description
        ),
        license_note=(
            service.license_note
            if service.license_note is not None
            else source.license_note
        ),
        source_url=(
            service.source_url if service.source_url is not None else source.source_url
        ),
    )
    _recheck_invariants(merged)
    _gate_merged_free_text(merged)
    return merged, CurationReport(
        operations_curated=operations_curated,
        parameters_curated=parameters_curated,
        parameters_removed=parameters_removed,
        responses_unresolved=responses_unresolved,
        example_prompt_count=example_prompt_count,
    )


def apply_curation(source: SourceSpec, curation: Curation) -> SourceSpec:
    """오버레이를 얹은 새 :class:`SourceSpec` 을 돌려준다.

    Args:
        source: 자동생성된 정규화 스펙 소스.
        curation: 얹을 오버레이.

    Returns:
        병합된 :class:`SourceSpec`(원본은 변형되지 않는다).

    Raises:
        CurationError: :func:`apply_curation_with_report` 와 동일.
    """
    merged, _report = apply_curation_with_report(source, curation)
    return merged


# ---------------------------------------------------------------------------
# 프리셋 번들
# ---------------------------------------------------------------------------
def _wrapper_preset_id(wrapper: Mapping[str, Any], *, directory_name: str) -> str:
    """래퍼의 ``preset_id`` 를 읽고 디렉터리명과 대조한다(V3의 앞 두 자리)."""
    preset_id = _expect_text(wrapper.get("preset_id"), path="source.preset_id")
    assert preset_id is not None
    if directory_name and preset_id != directory_name:
        raise CurationError(
            f"프리셋 식별자가 어긋납니다. 디렉터리명: {directory_name!r}, "
            f"source.json.preset_id: {preset_id!r}. 산출물이 잘못된 데이터셋으로 "
            "라벨링되는 사고를 막기 위해 중단합니다."
        )
    return preset_id


def _read_preset_wrapper(path: Path) -> Mapping[str, Any]:
    """``source.json`` 래퍼를 읽고 스키마를 검증한다."""
    wrapper = _expect_mapping(
        _read_json(path, what="프리셋 소스 문서"), path="source"
    )
    version = wrapper.get("mcportal_preset_source")
    if not _is_schema_version(version, PRESET_SOURCE_SCHEMA_VERSION):
        raise CurationError(
            f"지원하지 않는 프리셋 소스 스키마 버전입니다: {version!r} "
            f"(받은 타입: {type(version).__name__}). "
            f"현재 지원 버전은 정수 {PRESET_SOURCE_SCHEMA_VERSION} 입니다."
        )
    _reject_unknown_keys(wrapper, _SOURCE_WRAPPER_KEYS, path="source")
    for name in ("service_id", "service_name", "fetched_at"):
        _expect_text(wrapper.get(name), path=f"source.{name}")
    for name in ("source_url", "license_note"):
        if name not in wrapper:
            raise CurationError(
                f"source.{name} 필드가 없습니다. 출처 URL 과 이용허락범위 표기는 "
                "정찰 규칙상 생략할 수 없습니다(값이 없으면 null 을 적으세요)."
            )
        _expect_text(wrapper.get(name), path=f"source.{name}", allow_none=True)
    _expect_mapping(wrapper.get("provenance"), path="source.provenance")
    _expect_mapping(wrapper.get("document"), path="source.document")
    return wrapper


def _wrapper_to_source(wrapper: Mapping[str, Any]) -> SourceSpec:
    """검증된 래퍼를 :class:`SourceSpec` 으로 흡수한다."""
    kind_text = _expect_text(
        wrapper.get("source_kind"), path="source.source_kind", allow_none=True
    )
    kind: SourceKind | None = None
    if kind_text is not None:
        try:
            kind = SourceKind(kind_text)
        except ValueError as exc:
            allowed = ", ".join(sorted(item.value for item in SourceKind))
            raise CurationError(
                f"알 수 없는 source_kind 입니다: {kind_text!r}. "
                f"허용 값: {allowed} (또는 null 로 두면 문서 형태로 판별합니다)."
            ) from exc

    key_param = (
        _expect_text(wrapper.get("key_param"), path="source.key_param", allow_none=True)
        or "serviceKey"
    )
    source_url = wrapper.get("source_url")
    license_note = wrapper.get("license_note")

    spec = load_source(
        wrapper["document"],
        service_id=str(wrapper["service_id"]),
        kind=kind,
        service_name=str(wrapper["service_name"]),
        source_url=source_url,
        fetched_at=str(wrapper["fetched_at"]),
        key_param=key_param,
    )
    # 우선순위(§5-4): source_url 은 래퍼가 정본, license_note 는 소스 선언이 우선.
    if source_url is not None and spec.source_url != source_url:
        spec = replace(spec, source_url=str(source_url))
    if spec.license_note is None and license_note is not None:
        spec = replace(spec, license_note=str(license_note))
    return spec


def read_preset_source(path: PathLike) -> tuple[SourceSpec, Mapping[str, Any]]:
    """``source.json`` 을 읽어 ``(SourceSpec, provenance)`` 로 돌려준다.

    래퍼는 원문 스펙(``document``)과 출처 메타를 함께 봉인한다. 스펙 본문은
    손대지 않고 :func:`~mcportal.compiler.sources.load_source` 에 그대로 넘긴다
    — 교정은 전부 ``curation.json`` 에서 한다.

    Args:
        path: ``source.json`` 경로.

    Returns:
        ``(정규화된 SourceSpec, provenance 매핑)``.

    Raises:
        CurationError: 래퍼 스키마 위반(버전·미지 키·필수 필드 결손·타입),
            디렉터리명과 ``preset_id`` 불일치.
        SourceSpecError: 스펙 본문 자체를 흡수할 수 없을 때.
    """
    file_path = Path(path)
    wrapper = _read_preset_wrapper(file_path)
    _wrapper_preset_id(wrapper, directory_name=file_path.parent.name)
    return _wrapper_to_source(wrapper), _expect_mapping(
        wrapper["provenance"], path="source.provenance"
    )


@dataclass(frozen=True)
class _Bundle:
    """프리셋 번들 1건을 읽어 들인 결과(내부용)."""

    directory: Path
    source_path: Path
    curation_path: Path | None
    openapi_path: Path
    preset_id: str
    source: SourceSpec
    curation: Curation | None
    report: CurationReport | None


def _load_bundle(directory: PathLike, *, curated: bool) -> _Bundle:
    """번들 디렉터리를 읽어 :class:`_Bundle` 로 만든다(V3 3자 일치 포함)."""
    base = Path(directory)
    source_path = base / PRESET_SOURCE_FILENAME
    if not source_path.is_file():
        raise CurationError(
            f"프리셋 번들에 {PRESET_SOURCE_FILENAME} 이 없습니다: {base}. "
            f"번들 디렉터리는 {PRESET_SOURCE_FILENAME} 을 반드시 포함해야 합니다."
        )
    wrapper = _read_preset_wrapper(source_path)
    preset_id = _wrapper_preset_id(wrapper, directory_name=base.name)
    spec = _wrapper_to_source(wrapper)

    candidate = base / PRESET_CURATION_FILENAME
    curation_path: Path | None = candidate if candidate.is_file() else None
    curation: Curation | None = None
    report: CurationReport | None = None
    if curation_path is not None:
        curation = read_curation(curation_path)
        if curation.preset_id != preset_id:
            raise CurationError(
                f"프리셋 식별자가 어긋납니다. 디렉터리명: {base.name!r}, "
                f"source.json.preset_id: {preset_id!r}, "
                f"curation.json.preset_id: {curation.preset_id!r}. "
                "세 값이 모두 같아야 합니다."
            )
        if curated:
            spec, report = apply_curation_with_report(spec, curation)

    return _Bundle(
        directory=base,
        source_path=source_path,
        curation_path=curation_path,
        openapi_path=base / PRESET_OPENAPI_FILENAME,
        preset_id=preset_id,
        source=spec,
        curation=curation,
        report=report,
    )


def _bundle_options(bundle: _Bundle, *, curated: bool) -> CompileOptions:
    """번들의 기본 컴파일 옵션을 만든다.

    큐레이션을 적용하지 않는 비교군(``curated=False``)에서는 제목·버전도
    자동생성 값을 쓴다 — 그래야 "큐레이션 유무" 하나만 달라진 A/B 가 된다.
    """
    if curated and bundle.curation is not None:
        service = bundle.curation.service
        return CompileOptions(
            title=service.title or bundle.source.service_name,
            version=service.version,
            generation_mode="offline",
        )
    return CompileOptions(
        title=bundle.source.service_name,
        version=ServiceCuration.version,
        generation_mode="offline",
    )


def load_preset(directory: PathLike, *, curated: bool = True) -> SourceSpec:
    """프리셋 번들을 읽어 :class:`SourceSpec` 으로 돌려준다.

    Args:
        directory: 번들 디렉터리.
        curated: True면 ``curation.json`` 이 있을 때 오버레이를 적용한다.
            False는 **벤치마크 비교군 전용**(자동생성 단독)이다.

    Returns:
        (선택적으로 큐레이션이 적용된) :class:`SourceSpec`.

    Raises:
        CurationError: 번들 구성·스키마·참조 무결성 위반.
    """
    return _load_bundle(directory, curated=curated).source


def preset_info(directory: PathLike) -> PresetInfo:
    """프리셋 번들 1건의 요약을 만든다(큐레이션 적용 기준).

    Args:
        directory: 번들 디렉터리.

    Returns:
        :class:`PresetInfo`.

    Raises:
        CurationError: 번들을 읽을 수 없을 때.
    """
    bundle = _load_bundle(directory, curated=True)
    service = bundle.curation.service if bundle.curation is not None else None
    return PresetInfo(
        preset_id=bundle.preset_id,
        service_id=bundle.source.service_id,
        service_name=(
            service.title
            if service is not None and service.title
            else bundle.source.service_name
        ),
        group=service.group if service is not None else None,
        source_kind=str(bundle.source.source_kind.value),
        directory=bundle.directory,
        source_path=bundle.source_path,
        curation_path=bundle.curation_path,
        openapi_path=bundle.openapi_path,
        operation_count=len(bundle.source.operations),
        unresolved_count=len(unresolved_schema_operations(bundle.source)),
        license_note=bundle.source.license_note,
        notes=service.notes if service is not None else (),
    )


def _has_bundle(candidate: Path) -> bool:
    """디렉터리가 프리셋 번들을 1개 이상 담고 있는지 본다."""
    try:
        if not candidate.is_dir():
            return False
        for child in candidate.iterdir():
            if child.is_dir() and (child / PRESET_SOURCE_FILENAME).is_file():
                return True
    except OSError:  # pragma: no cover - 권한·경로 이슈 방어
        return False
    return False


def default_presets_root() -> Path | None:
    """프리셋 루트를 탐색한다.

    탐색 순서는 ① 환경변수 :data:`ENV_PRESETS_ROOT` ② 패키지 데이터
    (``mcportal/presets``) ③ 리포 루트(``<repo>/presets``) ④ 현재 작업 디렉터리
    (``./presets``)다. **각 후보는 하위에 ``source.json`` 을 가진 디렉터리가
    1개 이상 있을 때만 채택**한다(빈 디렉터리를 잡고 "프리셋 0건"이라고
    보고하는 오해를 막는다).

    Returns:
        찾은 루트 경로. 어디에도 없으면 ``None``.
    """
    candidates: list[Path] = []
    configured = os.environ.get(ENV_PRESETS_ROOT)
    if configured:
        candidates.append(Path(configured))
    try:
        import mcportal

        package_file = getattr(mcportal, "__file__", None)
        if package_file:
            candidates.append(Path(package_file).resolve().parent / "presets")
    except ImportError:  # pragma: no cover - 패키지가 없을 수 없다
        pass
    candidates.append(Path(__file__).resolve().parents[3] / "presets")
    candidates.append(Path.cwd() / "presets")

    for candidate in candidates:
        if _has_bundle(candidate):
            return candidate
    return None


def iter_presets(root: PathLike | None = None) -> tuple[PresetInfo, ...]:
    """루트 아래 프리셋 번들들의 요약을 디렉터리명 오름차순으로 돌려준다.

    이름이 ``_`` 로 시작하는 디렉터리(원문·전사 보관소)와 ``source.json`` 이
    없는 디렉터리는 제외한다. 루트를 찾지 못하면 **예외가 아니라 빈 튜플**이다
    — 프리셋이 없는 상태는 실패가 아니다.

    Args:
        root: 프리셋 루트. ``None`` 이면 :func:`default_presets_root`.

    Returns:
        :class:`PresetInfo` 튜플(디렉터리명 오름차순).

    Raises:
        CurationError: 번들 하나라도 읽을 수 없을 때.
    """
    base = Path(root) if root is not None else default_presets_root()
    if base is None or not Path(base).is_dir():
        return ()
    directories = sorted(
        (child for child in Path(base).iterdir() if child.is_dir()),
        key=lambda child: child.name,
    )
    return tuple(
        preset_info(child)
        for child in directories
        if not child.name.startswith("_")
        and (child / PRESET_SOURCE_FILENAME).is_file()
    )


def compile_preset(
    directory: PathLike,
    *,
    curated: bool = True,
    options: CompileOptions | None = None,
) -> CompiledSpec:
    """프리셋 번들을 OpenAPI 3.1 문서로 컴파일한다.

    Args:
        directory: 번들 디렉터리.
        curated: False면 큐레이션을 적용하지 않는다(벤치마크 비교군).
        options: 컴파일 옵션. ``None`` 이면 큐레이션의 제목·버전으로 만든다.

    Returns:
        :class:`~mcportal.compiler.openapi.CompiledSpec`.

    Raises:
        CurationError: 번들을 읽을 수 없을 때.
        CompileError: OpenAPI 산출이 불가능할 때.
    """
    bundle = _load_bundle(directory, curated=curated)
    resolved = options if options is not None else _bundle_options(bundle, curated=curated)
    return build_openapi(bundle.source, options=resolved)


def write_preset(
    directory: PathLike, *, options: CompileOptions | None = None
) -> Path:
    """프리셋을 컴파일해 번들 안 ``openapi.json`` 에 기록한다.

    Args:
        directory: 번들 디렉터리.
        options: 컴파일 옵션(``None`` 이면 큐레이션 기준 기본값).

    Returns:
        기록한 파일 경로.

    Raises:
        CurationError: 번들을 읽을 수 없을 때.
        CompileError: 산출·저장 게이트에 걸렸을 때.
    """
    compiled = compile_preset(directory, options=options)
    return write_spec(compiled.document, Path(directory) / PRESET_OPENAPI_FILENAME)


def check_preset(directory: PathLike) -> bool:
    """커밋된 ``openapi.json`` 이 재생성 결과와 바이트 동일한지 본다.

    Args:
        directory: 번들 디렉터리.

    Returns:
        동일하면 ``True``. 파일이 없거나 1바이트라도 다르면 ``False``.

    Raises:
        CurationError: 번들을 읽을 수 없을 때.
        CompileError: 재생성 자체가 실패할 때.
    """
    target = Path(directory) / PRESET_OPENAPI_FILENAME
    if not target.is_file():
        return False
    expected = dumps(compile_preset(directory).document).encode("utf-8")
    return target.read_bytes() == expected
