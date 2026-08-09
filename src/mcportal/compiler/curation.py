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
    ``sampled_schemas.json``
        실키 샘플링이 **실측한** 응답 스키마(측정층 입력). 없어도 컴파일된다.
    ``openapi.json``
        세 층을 병합해 산출한 OpenAPI 3.1 문서(커밋 대상).

설계 원칙
---------
* **도메인 로직 0줄** — 이 파일에는 특정 서비스의 식별자·기관명·파라미터명이
  하나도 등장하지 않는다. 도메인 지식은 전부 번들의 JSON 에 있다.
* **결정론** — 같은 ``source.json`` + ``curation.json`` + ``sampled_schemas.json``
  이면 **바이트 동일한** ``openapi.json`` 이 나온다. 매핑 순회는 전부 ``sorted()``
  를 거치므로 JSON 의 키 순서를 뒤집어도 결과가 같다.
* **측정 결과도 데이터** — 라이브 표본에서 추론한 스키마는 산출물에만 남기지
  않고 ``sampled_schemas.json`` 으로 **영속화**한다. 그래야 키가 없는 사람이
  같은 번들을 다시 컴파일해도 같은 바이트가 나오고 ``compile --check`` 가
  드리프트를 내지 않는다. 재현할 수 없는 산출물은 커밋할 수 없다.
* **사실은 큐레이션이 바꾸지 않는다** — 타입·위치·필수 여부·경로·메서드는 원
  스펙 선언이 정본이다. 사실을 교정하는 통로는 근거를 요구하는 두 가지
  (``parameters_remove`` · ``response.unresolved``)뿐이다.
* **실패는 한국어로 구체적으로** — 어떤 키가 왜 틀렸고 무엇이 허용되는지를
  :class:`CurationError` 메시지에 적는다.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Union

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

if TYPE_CHECKING:  # pragma: no cover - 정적 분석 전용(런타임 임포트 아님)
    from ..profiles import ProviderProfile
    from .sampler import SampleResult

__all__ = [
    "CURATION_SCHEMA_VERSION",
    "Curation",
    "CurationError",
    "CurationReport",
    "ENV_PRESETS_ROOT",
    "OperationCuration",
    "PRESET_CASSETTE_DIRNAME",
    "PRESET_CURATION_FILENAME",
    "PRESET_OPENAPI_FILENAME",
    "PRESET_SAMPLED_FILENAME",
    "PRESET_SAMPLES_DIRNAME",
    "PRESET_SOURCE_FILENAME",
    "PRESET_SOURCE_SCHEMA_VERSION",
    "PROVENANCE_KEYS",
    "ParamCuration",
    "ParamRemoval",
    "PresetInfo",
    "PresetOperationSample",
    "PresetSampleReport",
    "ResponseCuration",
    "SAMPLED_SCHEMA_VERSION",
    "SampledInference",
    "SampledOperation",
    "SampledSchemas",
    "ServiceCuration",
    "apply_curation",
    "apply_curation_with_report",
    "apply_sampled_schemas",
    "check_preset",
    "compile_preset",
    "default_presets_root",
    "iter_presets",
    "load_curation",
    "load_preset",
    "load_sampled_schemas",
    "preset_info",
    "read_curation",
    "read_preset_source",
    "read_sampled_schemas",
    "sample_preset",
    "validate_curation",
    "write_preset",
    "write_sampled_schemas",
]

#: 큐레이션 문서 스키마 버전.
CURATION_SCHEMA_VERSION: int = 1

#: 프리셋 소스 래퍼 스키마 버전.
PRESET_SOURCE_SCHEMA_VERSION: int = 1

#: 샘플 스키마 문서 스키마 버전.
SAMPLED_SCHEMA_VERSION: int = 1

#: 프리셋 루트를 지정하는 환경변수.
ENV_PRESETS_ROOT: str = "MCPORTAL_PRESETS"

#: 프리셋 번들의 고정 파일명.
PRESET_SOURCE_FILENAME: str = "source.json"
PRESET_CURATION_FILENAME: str = "curation.json"
PRESET_OPENAPI_FILENAME: str = "openapi.json"

#: 실키 샘플링이 실측한 응답 스키마를 영속화하는 파일명(측정층).
#:
#: 이 파일이 있으면 **키가 없는 사람의 오프라인 컴파일도** 같은 바이트를 낸다.
#: 없으면 이 층은 통째로 없는 것이고 산출물은 소스+큐레이션만으로 결정된다.
PRESET_SAMPLED_FILENAME: str = "sampled_schemas.json"

#: 실키 샘플링 산출물이 놓이는 번들 하위 디렉터리명.
#: 카세트는 무키 재현(``mode="replay"``)의 입력이고, 샘플은 추론 근거의 증거물이다.
#: 둘 다 리포에는 남기되 **wheel 에는 싣지 않는다**(패키징 결정, W4 §6-1).
PRESET_CASSETTE_DIRNAME: str = "cassettes"
PRESET_SAMPLES_DIRNAME: str = "samples"

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

#: ``sampled_schemas.json`` 각 계층에서 허용되는 키들(V2 와 같은 취지의 게이트).
_SAMPLED_TOP_KEYS: frozenset[str] = frozenset(
    {"mcportal_sampled", "preset_id", "sampled_on", "provenance", "operations"}
)
_SAMPLED_PROVENANCE_KEYS: frozenset[str] = frozenset(
    {"cassette", "cassette_sha256", "sample_count", "call_count"}
)
_SAMPLED_OPERATION_KEYS: frozenset[str] = frozenset({"response_schema", "inference"})
_SAMPLED_INFERENCE_KEYS: frozenset[str] = frozenset(
    {"sample_count", "conflicts", "truncated"}
)

#: ``sampled_on`` 이 만족해야 하는 형태(날짜만 — 시각·타임존은 적지 않는다).
_SAMPLED_ON_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

#: 측정일 기준 시간대(KST). 측정 주체가 어디에 있든 같은 날짜 표기를 쓴다.
_KST = timezone(timedelta(hours=9))

_SOURCE_WRAPPER_KEYS: frozenset[str] = frozenset(
    {
        "mcportal_preset_source",
        "preset_id",
        "service_id",
        "service_name",
        "source_kind",
        "key_param",
        "key_location",
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
class SampledInference:
    """영속화된 추론 요약 1건(집계 수치만 — 표본 값은 하나도 담지 않는다).

    :func:`~mcportal.compiler.openapi.build_openapi` 는 리포트에서
    ``sample_count`` · ``len(conflicts)`` · ``truncated`` **세 가지만** 읽어
    ``x-mcportal`` 메타를 만든다. 그래서 커밋 대상 파일에는 그 세 수치만 적고,
    :attr:`conflicts` 는 개수를 길이로 갖는 자리표시자 튜플로 되돌려 준다 —
    :class:`~mcportal.compiler.inference.InferenceReport` 자리에 그대로 끼워도
    같은 문서가 나오게 하기 위한 형태 맞춤이며, 충돌의 위치·타입 같은 상세는
    이 층의 관심사가 아니다.

    Attributes:
        sample_count: 추론에 실제로 쓰인 표본 수.
        conflict_count: 타입 충돌 기록 수.
        truncated: 깊이·속성 수 상한으로 잘라낸 곳이 있었는지 여부.
    """

    sample_count: int
    conflict_count: int
    truncated: bool

    @property
    def conflicts(self) -> tuple[None, ...]:
        """충돌 **개수**를 길이로 갖는 자리표시자(내용은 비어 있다)."""
        return (None,) * self.conflict_count


@dataclass(frozen=True)
class SampledOperation:
    """오퍼레이션 1건의 실측 응답 스키마와 그 추론 요약."""

    response_schema: Mapping[str, Any]
    inference: SampledInference


@dataclass(frozen=True)
class SampledSchemas:
    """``sampled_schemas.json`` 1건(측정층 문서).

    Attributes:
        preset_id: 대상 프리셋 식별자(디렉터리명·``source.json`` 과 일치해야 한다).
        sampled_on: 측정일(KST 날짜). 시각은 적지 않는다.
        provenance: 측정 출처(카세트 경로·지문·표본 수·호출 수).
        operations: ``operation_id`` → :class:`SampledOperation`.
    """

    preset_id: str
    sampled_on: str
    provenance: Mapping[str, Any]
    operations: Mapping[str, SampledOperation] = field(default_factory=dict)


@dataclass(frozen=True)
class CurationReport:
    """병합 1회의 적용 요약(문서·테스트·CLI 표시용)."""

    operations_curated: int
    parameters_curated: int
    parameters_removed: int
    responses_unresolved: int
    example_prompt_count: int


@dataclass(frozen=True)
class PresetOperationSample:
    """실키 샘플링에서 오퍼레이션 1건이 남긴 요약(값은 하나도 담지 않는다).

    응답 본문·인증키는 **이 자료형에 절대 담기지 않는다**. 사람이 보는 출력과
    ``--json`` 계약이 모두 이 요약만 읽으므로, 여기에 값이 없으면 화면·로그·
    파이프 어디로도 새지 않는다. 실제 페이로드는 스크러빙을 거쳐
    ``samples/`` 파일로만 남는다.

    Attributes:
        operation_id: 대상 오퍼레이션.
        calls: 실제로 나간 상위 호출 수(중복 제거 후).
        ok: 전송·업무 응답이 모두 정상이었던 호출 수(추론 입력 자격).
        failed: 그 밖의 호출 수.
        status_codes: 관측된 HTTP 상태 코드(오름차순·중복 제거).
        result_codes: 관측된 data.go.kr ``resultCode``(오름차순·중복 제거).
        schema_inferred: 이 오퍼레이션의 응답 스키마를 확정했는지 여부.
        sample_count: 추론에 실제로 쓰인 표본 수.
        property_count: 추론 스키마의 ``properties`` 총 개수(중첩 포함).
        max_depth: 관측된 최대 깊이(루트가 0).
        truncated: 깊이·속성 수 상한으로 잘라낸 곳이 있으면 True.
        conflicts: 타입 충돌 기록 수.
    """

    operation_id: str
    calls: int
    ok: int
    failed: int
    status_codes: tuple[int, ...] = ()
    result_codes: tuple[str, ...] = ()
    schema_inferred: bool = False
    sample_count: int = 0
    property_count: int = 0
    max_depth: int = 0
    truncated: bool = False
    conflicts: int = 0


@dataclass(frozen=True)
class PresetSampleReport:
    """프리셋 1건의 실키 샘플링 1회 요약.

    Attributes:
        preset_id: 대상 프리셋 식별자.
        directory: 번들 디렉터리.
        target_operations: 샘플링 대상으로 고른 미확정 오퍼레이션들(오름차순).
        call_count: 나간 상위 호출 총합.
        ok_count: 정상 응답 호출 수.
        failed_count: 실패 응답 호출 수.
        operations: 오퍼레이션별 요약(:class:`PresetOperationSample`).
        resolved_operations: 응답 스키마를 확정한 오퍼레이션들(오름차순).
        cassette_path: 녹화된 카세트 경로.
        samples_dir: 샘플 파일이 놓인 디렉터리.
        sample_paths: 기록된 샘플 파일 경로들.
        openapi_path: 갱신한 ``openapi.json`` 경로. 대상이 없거나 확정한 스키마가
            하나도 없어 갱신하지 않았으면 ``None``.
        sampled_path: 기록한 ``sampled_schemas.json`` 경로(갱신하지 않았으면
            ``None``). 이 파일이 있어야 오프라인 재컴파일이 같은 바이트를 낸다.
    """

    preset_id: str
    directory: Path
    target_operations: tuple[str, ...]
    call_count: int
    ok_count: int
    failed_count: int
    operations: tuple[PresetOperationSample, ...]
    resolved_operations: tuple[str, ...]
    cassette_path: Path
    samples_dir: Path
    sample_paths: tuple[Path, ...] = ()
    openapi_path: Path | None = None
    sampled_path: Path | None = None


@dataclass(frozen=True)
class PresetInfo:
    """프리셋 번들 1건의 요약.

    Attributes:
        unresolved_count: **측정층까지 반영한** 미확정 응답 스키마 수. 산출
            ``openapi.json`` 의 ``x-mcportal.schema_inference.unresolved`` 와 같은
            값이다 — 샘플링으로 채워진 자리를 여기서 여전히 "미확정"이라고 세면
            CLI 표가 산출물과 다른 사실을 말하게 된다.
        sampled_path: ``sampled_schemas.json`` 경로(없으면 ``None``).
        resolved_by_sampling: 소스·큐레이션 기준으로는 미확정이었는데 실측
            스키마가 채운 오퍼레이션 수.
    """

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
    sampled_path: Path | None = None
    resolved_by_sampling: int = 0


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


def _expect_int(value: Any, *, path: str, minimum: int | None = None) -> int:
    """정수임을 확인한다(V12). ``_expect_bool`` 과 대칭으로 불리언을 거부한다."""
    if isinstance(value, bool) or not isinstance(value, int):
        raise CurationError(
            f"{path}의 타입이 올바르지 않습니다. 기대: 정수, "
            f"받은 타입: {_type_name(value)}."
        )
    if minimum is not None and value < minimum:
        raise CurationError(
            f"{path} 값이 범위를 벗어났습니다: {value} (최소 {minimum})."
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
    # F-08: 인증키 주입 위치는 **선택 필드**다. 생략하면 질의문자열(기본값)이므로
    # 이 필드를 쓰지 않는 기존 번들 4종은 산출물이 1바이트도 바뀌지 않는다.
    # 허용값 검증은 SourceSpec.__post_init__ 이 한다(SourceSpecError).
    key_location = (
        _expect_text(
            wrapper.get("key_location"), path="source.key_location", allow_none=True
        )
        or SourceSpec.key_location
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
    if key_location != spec.key_location:
        spec = replace(spec, key_location=key_location)
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


# ---------------------------------------------------------------------------
# 측정층 — sampled_schemas.json (W4 후속: 실측 결과의 영속화)
# ---------------------------------------------------------------------------
def _load_sampled_inference(raw: Any, *, path: str) -> SampledInference:
    """추론 요약 블록을 읽는다."""
    mapping = _expect_mapping(raw, path=path)
    _reject_unknown_keys(mapping, _SAMPLED_INFERENCE_KEYS, path=path)
    # 표본 0건으로 확정된 스키마는 존재할 수 없다(추론기가 그런 결과를 만들지
    # 않는다). 0 을 허용하면 "측정했다"는 거짓 기록이 통과한다.
    sample_count = _expect_int(
        mapping.get("sample_count"), path=f"{path}.sample_count", minimum=1
    )
    conflicts = _expect_int(mapping.get("conflicts"), path=f"{path}.conflicts", minimum=0)
    truncated = _expect_bool(mapping.get("truncated"), path=f"{path}.truncated")
    return SampledInference(
        sample_count=sample_count, conflict_count=conflicts, truncated=truncated
    )


def _load_sampled_operation(raw: Any, *, path: str) -> SampledOperation:
    """오퍼레이션 1건의 실측 스키마 블록을 읽는다.

    금지 필드(V9) 게이트는 여기에 걸지 않는다. ``response_schema`` 는 큐레이션
    계층에서는 금지 필드지만(사람이 사실을 뒤집는 통로가 되면 안 되므로) 이 층은
    **사람의 의견이 아니라 실측**이라 그 값을 적는 것이 존재 이유다. 대신 근거를
    ``provenance`` 로 요구해 "무엇을 재생하면 같은 값이 나오는지"를 남긴다.
    """
    mapping = _expect_mapping(raw, path=path)
    _reject_unknown_keys(mapping, _SAMPLED_OPERATION_KEYS, path=path)
    schema = _expect_mapping(
        mapping.get("response_schema"), path=f"{path}.response_schema"
    )
    if not schema:
        raise CurationError(
            f"{path}.response_schema 가 비어 있습니다. 빈 객체는 '아무 제약도 없음'"
            "이라 폴백 스키마보다도 못한 확정 선언이 됩니다. 표본에서 아무것도 "
            "얻지 못했다면 그 오퍼레이션 항목을 통째로 지우세요(폴백으로 돌아갑니다)."
        )
    inference = _load_sampled_inference(mapping.get("inference"), path=f"{path}.inference")
    return SampledOperation(response_schema=schema, inference=inference)


def _load_sampled_provenance(raw: Any, *, path: str) -> Mapping[str, Any]:
    """측정 출처 블록을 읽는다(네 키 모두 필수, 카세트 정보는 null 허용)."""
    mapping = _expect_mapping(raw, path=path)
    _reject_unknown_keys(mapping, _SAMPLED_PROVENANCE_KEYS, path=path)
    for name in ("cassette", "cassette_sha256"):
        if name not in mapping:
            raise CurationError(
                f"{path}.{name} 필드가 없습니다. 실측 결과는 무엇을 재생하면 같은 "
                "값이 나오는지를 함께 적어야 합니다(카세트가 없으면 null 을 적으세요)."
            )
        _expect_text(mapping.get(name), path=f"{path}.{name}", allow_none=True)
    counts = {
        name: _expect_int(mapping.get(name), path=f"{path}.{name}", minimum=0)
        for name in ("sample_count", "call_count")
    }
    return {
        "cassette": mapping.get("cassette"),
        "cassette_sha256": mapping.get("cassette_sha256"),
        "sample_count": counts["sample_count"],
        "call_count": counts["call_count"],
    }


def load_sampled_schemas(
    document: Mapping[str, Any], *, preset_id: str | None = None
) -> SampledSchemas:
    """실측 스키마 문서(JSON 객체)를 :class:`SampledSchemas` 로 읽는다.

    Args:
        document: ``sampled_schemas.json`` 의 내용.
        preset_id: 대조할 프리셋 식별자(``None`` 이면 대조하지 않는다).

    Returns:
        검증을 통과한 :class:`SampledSchemas`.

    Raises:
        CurationError: 스키마 버전 불일치, 미지의 키, ``preset_id`` 불일치,
            측정일 형태 오류, 빈 ``operations`` · 빈 ``response_schema``,
            타입 불일치, 문자열의 인증키 대입.
    """
    doc = _expect_mapping(document, path="sampled")
    version = doc.get("mcportal_sampled")
    if not _is_schema_version(version, SAMPLED_SCHEMA_VERSION):
        raise CurationError(
            f"지원하지 않는 실측 스키마 문서 버전입니다: {version!r} "
            f"(받은 타입: {type(version).__name__}). "
            f"현재 지원 버전은 정수 {SAMPLED_SCHEMA_VERSION} 입니다"
            f'(문서 최상위에 {{"mcportal_sampled": {SAMPLED_SCHEMA_VERSION}}} 이 '
            "필요합니다)."
        )
    _reject_unknown_keys(doc, _SAMPLED_TOP_KEYS, path="sampled")

    document_preset_id = _expect_text(doc.get("preset_id"), path="sampled.preset_id")
    assert document_preset_id is not None
    if preset_id is not None and document_preset_id != preset_id:
        raise CurationError(
            f"프리셋 식별자가 어긋납니다. 번들: {preset_id!r}, "
            f"{PRESET_SAMPLED_FILENAME}.preset_id: {document_preset_id!r}. "
            "다른 데이터셋에서 측정한 스키마가 섞이면 산출물이 통째로 거짓이 "
            "되므로 중단합니다."
        )

    sampled_on = _expect_text(doc.get("sampled_on"), path="sampled.sampled_on")
    assert sampled_on is not None
    if not _SAMPLED_ON_RE.match(sampled_on):
        raise CurationError(
            f"sampled.sampled_on 표기가 올바르지 않습니다: {sampled_on!r}. "
            "'YYYY-MM-DD'(KST 날짜)여야 합니다. 시각·타임존은 적지 않습니다 — "
            "측정 시각은 산출물에 필요하지 않고 측정자의 생활 시간대를 드러냅니다."
        )
    try:
        date.fromisoformat(sampled_on)
    except ValueError as exc:
        raise CurationError(
            f"sampled.sampled_on 이 실재하는 날짜가 아닙니다: {sampled_on!r}."
        ) from exc

    provenance = _load_sampled_provenance(
        doc.get("provenance"), path="sampled.provenance"
    )

    operations_map = _expect_mapping(doc.get("operations"), path="sampled.operations")
    if not operations_map:
        raise CurationError(
            "sampled.operations 가 비어 있습니다. 채운 오퍼레이션이 하나도 없으면 "
            f"{PRESET_SAMPLED_FILENAME} 자체를 두지 마세요 — 빈 측정층은 산출물을 "
            "'sampled' 로 라벨링하면서 실제로는 아무것도 측정하지 않았다고 말합니다."
        )
    operations: dict[str, SampledOperation] = {}
    for operation_id in sorted(str(key) for key in operations_map.keys()):
        if not _OPERATION_ID_RE.match(operation_id):
            raise CurationError(
                f"sampled.operations 의 키가 ASCII 식별자 규칙을 만족하지 "
                f"않습니다: {operation_id!r} (불변식 I1)."
            )
        operations[operation_id] = _load_sampled_operation(
            operations_map[operation_id], path=f"sampled.operations.{operation_id}"
        )

    _gate_sampled_document(doc)
    return SampledSchemas(
        preset_id=document_preset_id,
        sampled_on=sampled_on,
        provenance=provenance,
        operations=operations,
    )


def read_sampled_schemas(
    path: PathLike, *, preset_id: str | None = None
) -> SampledSchemas:
    """``sampled_schemas.json`` 파일을 읽어 :class:`SampledSchemas` 로 돌려준다.

    Args:
        path: 실측 스키마 문서 경로.
        preset_id: 대조할 프리셋 식별자.

    Returns:
        검증을 통과한 :class:`SampledSchemas`.

    Raises:
        CurationError: JSON 파손 또는 스키마 위반.
        OSError: 파일을 읽을 수 없을 때(부재 포함).
    """
    file_path = Path(path)
    return load_sampled_schemas(
        _read_json(file_path, what="실측 스키마 문서"), preset_id=preset_id
    )


def _sampled_document(
    preset_id: str,
    schemas: Mapping[str, Mapping[str, Any]],
    reports: Mapping[str, Any],
    *,
    sampled_on: str,
    cassette: str | None,
    cassette_sha256: str | None,
    call_count: int,
) -> dict[str, Any]:
    """영속화할 문서를 조립한다(순회는 전부 ``sorted()`` — 결정론)."""
    operations: dict[str, Any] = {}
    total_samples = 0
    for operation_id in sorted(schemas):
        report = reports[operation_id]
        sample_count = int(report.sample_count)
        total_samples += sample_count
        operations[operation_id] = {
            "response_schema": dict(schemas[operation_id]),
            "inference": {
                "sample_count": sample_count,
                "conflicts": len(report.conflicts),
                "truncated": bool(report.truncated),
            },
        }
    return {
        "mcportal_sampled": SAMPLED_SCHEMA_VERSION,
        "preset_id": preset_id,
        "sampled_on": sampled_on,
        "provenance": {
            "cassette": cassette,
            "cassette_sha256": cassette_sha256,
            "sample_count": total_samples,
            "call_count": int(call_count),
        },
        "operations": operations,
    }


def _cassette_provenance(
    directory: Path, cassette_path: PathLike | None
) -> tuple[str | None, str | None]:
    """카세트의 **상대 경로**와 지문을 만든다.

    절대 경로는 적지 않는다 — 커밋 대상 파일에 측정자의 홈 디렉터리 이름을
    남기지 않기 위해서다. 번들 밖 카세트는 파일명만 적는다.
    """
    if cassette_path is None:
        return None, None
    target = Path(cassette_path)
    try:
        relative = target.resolve().relative_to(directory.resolve()).as_posix()
    except (OSError, ValueError):
        relative = target.name
    digest: str | None = None
    if target.is_file():
        digest = "sha256:" + hashlib.sha256(target.read_bytes()).hexdigest()
    return relative, digest


def _gate_sampled_document(document: Mapping[str, Any]) -> None:
    """기록 직전 문서 전체에서 인증키 대입을 찾는다(V7 과 같은 게이트).

    응답 본문이 요청 인증키를 되비추는 사례가 있어 **추론 스키마의 예시·설명
    문자열**로도 키가 흘러들 수 있다. 카세트·샘플 파일과 별개로 여기서도 막는다.
    """
    for field_path, text in _iter_strings(document, path="sampled"):
        found = find_key_assignments(text, _CURATION_KEY_PARAMS)
        if found:
            raise CurationError(
                f"실측 스키마 문서에 인증키 대입이 있습니다"
                f"(탐지된 파라미터: {', '.join(found)}, 검사한 필드 경로: "
                f"{field_path}). 이 파일은 커밋 대상이므로 자격증명이 실린 채로 "
                "저장할 수 없습니다. 시크릿 스크러빙을 거친 표본으로 다시 "
                "추론하세요."
            )


def write_sampled_schemas(
    directory: PathLike,
    schemas: Mapping[str, Mapping[str, Any]],
    reports: Mapping[str, Any],
    *,
    preset_id: str,
    sampled_on: str | None = None,
    cassette_path: PathLike | None = None,
    call_count: int = 0,
) -> Path:
    """실측 스키마를 번들의 ``sampled_schemas.json`` 에 기록한다.

    기록 전에 ① 문서를 조립하고 ② :func:`load_sampled_schemas` 로 **자기 자신을
    다시 읽어** 스키마 검증을 통과시키고 ③ 인증키 게이트를 지난다. 읽을 수 없는
    파일을 쓰는 경로를 남기지 않기 위해서다.

    Args:
        directory: 프리셋 번들 디렉터리.
        schemas: ``operation_id`` → 추론 응답 스키마.
        reports: ``operation_id`` → 추론 리포트(``sample_count`` · ``conflicts`` ·
            ``truncated`` 를 읽는다).
        preset_id: 번들 식별자.
        sampled_on: 측정일(생략하면 오늘 KST 날짜).
        cassette_path: 녹화 카세트 경로(있으면 상대 경로·sha256 을 남긴다).
        call_count: 이 측정에서 나간 상위 호출 총합.

    Returns:
        기록한 파일 경로.

    Raises:
        CurationError: 채울 스키마가 없거나, 리포트가 빠졌거나, 조립한 문서가
            스키마 검증·인증키 게이트를 통과하지 못할 때.
    """
    base = Path(directory)
    if not schemas:
        raise CurationError(
            "영속화할 실측 스키마가 없습니다. 정상 응답이 0건이면 산출물을 "
            "'sampled' 로 라벨링하지 않습니다."
        )
    missing = sorted(set(schemas) - set(reports))
    if missing:
        raise CurationError(
            f"추론 리포트가 없는 오퍼레이션이 있습니다: {', '.join(missing)}. "
            "스키마와 리포트는 같은 추론 1회의 두 산출물이므로 짝이 맞아야 합니다."
        )
    cassette, digest = _cassette_provenance(base, cassette_path)
    document = _sampled_document(
        preset_id,
        schemas,
        reports,
        sampled_on=sampled_on or datetime.now(_KST).date().isoformat(),
        cassette=cassette,
        cassette_sha256=digest,
        call_count=call_count,
    )
    load_sampled_schemas(document, preset_id=preset_id)
    _gate_sampled_document(document)

    target = base / PRESET_SAMPLED_FILENAME
    target.parent.mkdir(parents=True, exist_ok=True)
    with open(target, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(dumps(document))
    return target


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
    sampled_path: Path | None = None
    sampled: SampledSchemas | None = None


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

    sampled_candidate = base / PRESET_SAMPLED_FILENAME
    sampled_path: Path | None = sampled_candidate if sampled_candidate.is_file() else None
    sampled: SampledSchemas | None = None
    if sampled_path is not None:
        # 적용 여부(curated)와 무관하게 **읽고 검증한다**. 비교군 경로에서만 통과하는
        # 깨진 측정층을 남기지 않기 위해서다.
        sampled = read_sampled_schemas(sampled_path, preset_id=preset_id)
        known = {operation.operation_id for operation in spec.operations}
        unknown = sorted(set(sampled.operations) - known)
        if unknown:
            available = ", ".join(sorted(known)) or "(없음)"
            raise CurationError(
                f"{PRESET_SAMPLED_FILENAME} 이 소스에 없는 오퍼레이션을 채우고 "
                f"있습니다: {', '.join(unknown)}. 소스가 실제로 가진 "
                f"operation_id: {available}. 그대로 두면 그 항목은 조용히 버려져 "
                "'측정했다'는 기록만 남습니다."
            )

    return _Bundle(
        directory=base,
        source_path=source_path,
        curation_path=curation_path,
        openapi_path=base / PRESET_OPENAPI_FILENAME,
        preset_id=preset_id,
        source=spec,
        curation=curation,
        report=report,
        sampled_path=sampled_path,
        sampled=sampled,
    )


def _sampled_layer(
    bundle: _Bundle, *, curated: bool
) -> tuple[dict[str, Mapping[str, Any]], dict[str, SampledInference]]:
    """번들의 측정층을 ``build_openapi`` 인자 두 개로 편다.

    주입 순서는 **소스 → 큐레이션(강등 포함) → 측정**이다. ``build_openapi`` 가
    ``response_schemas`` 를 최우선으로 보므로 실측이 마지막에 이긴다 — 큐레이션의
    ``response.unresolved`` 강등이 한 말은 "소스 선언을 믿지 마라"였고, 실측
    표본이 바로 그 물음의 답이기 때문이다.

    ``curated=False`` (벤치마크 비교군)에서는 측정층을 얹지 않는다. 그 경로의
    정의가 "자동생성 단독"이고, 실측 스키마는 자동생성 산물이 아니다.
    """
    if not curated or bundle.sampled is None:
        return {}, {}
    schemas: dict[str, Mapping[str, Any]] = {}
    reports: dict[str, SampledInference] = {}
    for operation_id in sorted(bundle.sampled.operations):
        entry = bundle.sampled.operations[operation_id]
        schemas[operation_id] = entry.response_schema
        reports[operation_id] = entry.inference
    return schemas, reports


def _bundle_options(bundle: _Bundle, *, curated: bool) -> CompileOptions:
    """번들의 기본 컴파일 옵션을 만든다.

    큐레이션을 적용하지 않는 비교군(``curated=False``)에서는 제목·버전도
    자동생성 값을 쓴다 — 그래야 "큐레이션 유무" 하나만 달라진 A/B 가 된다.

    ``generation_mode`` 는 측정층 유무가 정한다. 번들에 실측 스키마가 있으면
    산출물은 라이브 표본에 근거한 것이므로 ``"sampled"`` 다 — 그 사실은 키가 없는
    사람이 다시 컴파일해도 변하지 않는다(근거가 파일로 남아 있으므로).
    """
    mode = "sampled" if curated and bundle.sampled is not None else "offline"
    if curated and bundle.curation is not None:
        service = bundle.curation.service
        return CompileOptions(
            title=service.title or bundle.source.service_name,
            version=service.version,
            generation_mode=mode,
        )
    return CompileOptions(
        title=bundle.source.service_name,
        version=ServiceCuration.version,
        generation_mode=mode,
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
    unresolved = unresolved_schema_operations(bundle.source)
    sampled_ids = set(bundle.sampled.operations) if bundle.sampled is not None else set()
    remaining = tuple(
        operation_id for operation_id in unresolved if operation_id not in sampled_ids
    )
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
        unresolved_count=len(remaining),
        license_note=bundle.source.license_note,
        notes=service.notes if service is not None else (),
        sampled_path=bundle.sampled_path,
        resolved_by_sampling=len(unresolved) - len(remaining),
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

    번들에 ``sampled_schemas.json`` 이 있으면 실측 응답 스키마를 마지막 층으로
    얹는다(:func:`_sampled_layer`). 네트워크는 어느 경로에서도 쓰지 않는다 —
    측정 결과가 파일로 남아 있으므로 키 없이도 같은 문서가 나온다.

    Args:
        directory: 번들 디렉터리.
        curated: False면 큐레이션도 측정층도 적용하지 않는다(벤치마크 비교군).
        options: 컴파일 옵션. ``None`` 이면 큐레이션의 제목·버전으로 만든다.

    Returns:
        :class:`~mcportal.compiler.openapi.CompiledSpec`.

    Raises:
        CurationError: 번들을 읽을 수 없을 때.
        CompileError: OpenAPI 산출이 불가능할 때.
    """
    bundle = _load_bundle(directory, curated=curated)
    resolved = options if options is not None else _bundle_options(bundle, curated=curated)
    schemas, reports = _sampled_layer(bundle, curated=curated)
    return build_openapi(
        bundle.source,
        schemas or None,
        options=resolved,
        reports=reports or None,
    )


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


# ---------------------------------------------------------------------------
# 실키 샘플링 글루(W4 §3-2)
# ---------------------------------------------------------------------------
def _sampling_targets(source: SourceSpec) -> tuple[str, ...]:
    """샘플링해야 할 미확정 응답 스키마 오퍼레이션들을 고른다.

    판정 기준은 **큐레이션을 적용한 소스**다. 게이트웨이 스웨거 2종은 원 문서가
    응답 스키마를 주긴 하지만 사람이 확인한 결과 그 선언이 실제 응답과 다르다는
    것이 밝혀져 ``curation.json`` 의 ``response.unresolved`` 로 강등돼 있다. 원
    소스(``curated=False``) 기준으로 고르면 그 2종이 대상에서 빠져 "미확정 10건"
    중 2건이 영원히 채워지지 않는다.

    Args:
        source: 큐레이션이 적용된 :class:`SourceSpec`.

    Returns:
        ``operation_id`` 오름차순 튜플.
    """
    return unresolved_schema_operations(source)


def _operation_summary(
    operation_id: str,
    results: Sequence["SampleResult"],
    report: Any | None,
) -> PresetOperationSample:
    """오퍼레이션 1건의 샘플 결과·추론 리포트를 값 없는 요약으로 접는다."""
    ok = sum(1 for result in results if result.ok)
    return PresetOperationSample(
        operation_id=operation_id,
        calls=len(results),
        ok=ok,
        failed=len(results) - ok,
        status_codes=tuple(sorted({int(result.status_code) for result in results})),
        result_codes=tuple(
            sorted(
                {
                    str(result.result_code)
                    for result in results
                    if result.result_code is not None
                }
            )
        ),
        schema_inferred=report is not None,
        sample_count=int(getattr(report, "sample_count", 0) or 0),
        property_count=int(getattr(report, "property_count", 0) or 0),
        max_depth=int(getattr(report, "max_depth_seen", 0) or 0),
        truncated=bool(getattr(report, "truncated", False)),
        conflicts=len(getattr(report, "conflicts", ()) or ()),
    )


def apply_sampled_schemas(
    directory: PathLike,
    results: Mapping[str, Sequence["SampleResult"]],
    *,
    cassette_path: PathLike | None = None,
    sampled_on: str | None = None,
) -> Path:
    """샘플 결과에서 응답 스키마를 추론해 번들에 **영속화**하고 산출물을 갱신한다.

    :func:`~mcportal.compiler.sampler.infer_response_schemas` →
    :func:`write_sampled_schemas` → :func:`write_preset` 순서다. 마지막 단계가
    핵심이다 — 산출물은 방금 쓴 ``sampled_schemas.json`` 을 읽는 **오프라인
    경로로** 만든다. 산출 경로가 하나뿐이면 "라이브에서 만든 문서"와 "키 없이
    재컴파일한 문서"가 갈라질 자리가 원천적으로 없다. 그래서 이 함수를 쓴 뒤에도
    ``mcportal compile --check`` 는 일치를 보고한다.

    번들은 다시 읽으므로 이 함수만 따로 불러도 동작한다(샘플링과 산출을 분리해
    두면 같은 카세트로 재현 산출이 가능하다).

    Args:
        directory: 프리셋 번들 디렉터리.
        results: ``operation_id`` → 샘플 결과들
            (:func:`~mcportal.compiler.sampler.sample_source` 의 반환값).
        cassette_path: 녹화 카세트 경로(측정 출처로 남긴다).
        sampled_on: 측정일(생략하면 오늘 KST 날짜).

    Returns:
        기록한 ``openapi.json`` 경로.

    Raises:
        CurationError: 번들을 읽을 수 없거나, 정상 응답이 0건이라 채울 스키마가
            없거나, 소스에 없는 오퍼레이션을 채우려 하거나, 이번 측정이 이미
            영속화된 실측을 지우게 될 때(부분 실패 덮어쓰기 방지).
        CompileError: 산출·저장 게이트에 걸렸을 때(인증키 잔존 포함).
    """
    # 지연 임포트: 샘플러는 httpx·트랜스포트·쿼터 계층을 끌어온다. 큐레이션 모듈은
    # `mcportal presets` 같은 순수 오프라인 경로에서도 임포트되므로, 라이브 샘플링을
    # 실제로 쓰는 함수 안에서만 끌어와 오프라인 경로의 임포트 비용을 0으로 둔다.
    from .sampler import infer_response_schemas

    bundle = _load_bundle(directory, curated=True)
    schemas, reports = infer_response_schemas(results)
    if not schemas:
        raise CurationError(
            "표본에서 확정한 응답 스키마가 없습니다(정상 응답 0건). 채울 것이 "
            "없는데 산출물을 다시 쓰면 'sampled' 라벨만 붙은 거짓 기록이 됩니다."
        )
    known = {operation.operation_id for operation in bundle.source.operations}
    unknown = sorted(set(schemas) - known)
    if unknown:
        available = ", ".join(sorted(known)) or "(없음)"
        raise CurationError(
            f"샘플 결과가 소스에 없는 오퍼레이션을 담고 있습니다: "
            f"{', '.join(unknown)}. 소스가 실제로 가진 operation_id: {available}."
        )

    # 측정 파일은 **측정 1회**를 통째로 서술한다(한 날짜·한 카세트). 그래서 새
    # 측정은 이전 파일을 덮어쓰는데, 재실행에서 일부 오퍼레이션만 실패하면 지난번에
    # 확정한 스키마가 조용히 사라지고 산출물이 폴백으로 되돌아간다 — 보고서에는
    # "N건 확정"만 남아 성공처럼 보인다. 그 조용한 손실을 여기서 접는다.
    if bundle.sampled is not None:
        lost = sorted(set(bundle.sampled.operations) - set(schemas))
        if lost:
            raise CurationError(
                f"이번 측정은 이미 확정된 스키마 {len(lost)}건을 지웁니다: "
                f"{', '.join(lost)}. 그 오퍼레이션들이 이번에 정상 응답을 하나도 "
                f"주지 않았습니다. {PRESET_SAMPLED_FILENAME} 은 측정 1회를 통째로 "
                "서술하므로 부분 실패를 덮어쓰면 지난 측정이 사라집니다. 실패 원인을 "
                "해결해 같은 대상을 다시 채우거나, 처음부터 다시 측정할 생각이라면 "
                f"{PRESET_SAMPLED_FILENAME} 을 지우고 실행하세요."
            )

    write_sampled_schemas(
        bundle.directory,
        schemas,
        reports,
        preset_id=bundle.preset_id,
        sampled_on=sampled_on,
        cassette_path=cassette_path,
        call_count=sum(len(tuple(results[operation_id])) for operation_id in results),
    )
    return write_preset(bundle.directory)


def sample_preset(
    directory: PathLike,
    *,
    service_key: str,
    count: int = 3,
    budget: int | None = None,
    ledger_path: PathLike | None = None,
    cassette_dir: PathLike | None = None,
    samples_dir: PathLike | None = None,
    overrides: Mapping[str, str] | None = None,
    profile: "ProviderProfile | None" = None,
    apply_schemas: bool = True,
) -> PresetSampleReport:
    """프리셋 번들의 **미확정 응답 스키마만** 실키 샘플링으로 채운다(W4 §3-2).

    경로는 다음 순서로 고정돼 있다.

    1. :func:`load_preset` (``curated=True``) — 큐레이션 적용 소스를 얻는다.
    2. :func:`_sampling_targets` — 미확정 오퍼레이션만 고른다(전수 호출 금지).
    3. :func:`~mcportal.compiler.sampler.sample_source` (``mode="record"``) —
       쿼터가드 경유·카세트 녹화·스크러빙이 구조적으로 강제된다.
    4. :func:`~mcportal.compiler.sampler.write_samples` — 인증키를 시크릿으로
       **명시**해 샘플 페이로드를 저장한다(응답이 키를 되비추는 사례 방어).
    5. :func:`apply_sampled_schemas` — 추론 → ``sampled_schemas.json`` 영속화 →
       그 파일을 읽는 오프라인 경로로 ``openapi.json`` 재생성.

    대상이 0건이면 **네트워크에 나가지 않는다**. 호출 0회 보고서를 그대로 돌려
    주므로, 이미 전부 확정된 프리셋에 예산을 태우는 경로가 없다.

    쿼터 소진(:class:`~mcportal.quota.QuotaExhausted`)은 잡지 않고 전파한다 —
    예산이 끝났다는 사실은 호출자가 반드시 알아야 하는 사건이다.

    Args:
        directory: 프리셋 번들 디렉터리.
        service_key: data.go.kr 인증키. **키워드 필수**이며 어떤 산출물에도
            평문으로 남지 않는다(카세트·샘플 양쪽에서 스크러빙된다).
        count: 오퍼레이션당 샘플 수(1 이상 하드캡 이하).
        budget: 일일 예산 상한(``None`` 이면 환경변수 → 프로파일 기본값).
        ledger_path: 사용량 원장 경로. 실호출 경로이므로 기록은 정상 동작이다.
        cassette_dir: 카세트 디렉터리. 생략하면 ``<번들>/cassettes``.
        samples_dir: 샘플 디렉터리. 생략하면 ``<번들>/samples``.
        overrides: 필수 파라미터의 강제 값(이름 → 값).
        profile: 프로바이더 프로파일(``None`` 이면 data.go.kr 정본).
        apply_schemas: False 면 ``openapi.json`` 을 갱신하지 않는다(측정만 하고
            산출은 사람이 결정하는 운용을 위해 둔다).

    Returns:
        :class:`PresetSampleReport` — 값이 아니라 **수치만** 담은 요약.

    Raises:
        CurationError: 번들을 읽을 수 없을 때.
        SamplingError: 인증키가 없거나 필수 파라미터 값을 정할 수 없을 때.
        QuotaExhausted: 일일 예산이 소진됐을 때(삼키지 않는다).
    """
    from ..profiles import DATA_GO_KR
    from ..runtime.keys import prepare_service_key
    from .sampler import infer_response_schemas, sample_source, write_samples

    bundle = _load_bundle(directory, curated=True)
    base = bundle.directory
    cassette_root = Path(cassette_dir) if cassette_dir else base / PRESET_CASSETTE_DIRNAME
    samples_root = Path(samples_dir) if samples_dir else base / PRESET_SAMPLES_DIRNAME
    cassette_path = cassette_root / f"{bundle.preset_id}.json"
    targets = _sampling_targets(bundle.source)

    if not targets:
        return PresetSampleReport(
            preset_id=bundle.preset_id,
            directory=base,
            target_operations=(),
            call_count=0,
            ok_count=0,
            failed_count=0,
            operations=(),
            resolved_operations=(),
            cassette_path=cassette_path,
            samples_dir=samples_root,
        )

    cassette_root.mkdir(parents=True, exist_ok=True)
    samples_root.mkdir(parents=True, exist_ok=True)

    results = sample_source(
        bundle.source,
        service_key=service_key,
        count=count,
        mode="record",
        cassette_path=cassette_path,
        budget=budget,
        ledger_path=ledger_path,
        profile=profile if profile is not None else DATA_GO_KR,
        operation_ids=targets,
        overrides=overrides,
    )

    # 시크릿은 입력 원문과 준비된(디코딩) 키 둘 다다. 전송에 실제로 쓰이는 것은
    # 준비된 키지만, 응답이 인코딩키 형태를 되비추는 사례가 있어 양쪽을 넘긴다.
    secrets = [
        secret
        for secret in dict.fromkeys([service_key, prepare_service_key(service_key)])
        if secret
    ]
    sample_paths = write_samples(results, samples_root, secrets=secrets)

    _schemas, reports = infer_response_schemas(results)
    operations = tuple(
        _operation_summary(operation_id, results.get(operation_id, ()), reports.get(operation_id))
        for operation_id in sorted(results)
    )
    openapi_path: Path | None = None
    sampled_path: Path | None = None
    if apply_schemas and reports:
        openapi_path = apply_sampled_schemas(base, results, cassette_path=cassette_path)
        sampled_path = base / PRESET_SAMPLED_FILENAME

    return PresetSampleReport(
        preset_id=bundle.preset_id,
        directory=base,
        target_operations=tuple(targets),
        call_count=sum(summary.calls for summary in operations),
        ok_count=sum(summary.ok for summary in operations),
        failed_count=sum(summary.failed for summary in operations),
        operations=operations,
        resolved_operations=tuple(sorted(reports)),
        cassette_path=cassette_path,
        samples_dir=samples_root,
        sample_paths=tuple(sample_paths),
        openapi_path=openapi_path,
        sampled_path=sampled_path,
    )
