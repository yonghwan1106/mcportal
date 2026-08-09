# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Yong Park
"""정규화 스펙 소스(``SourceSpec``)를 OpenAPI 3.1 문서로 컴파일한다.

이 모듈은 컴파일러의 **출구**다. 입력은 :mod:`mcportal.compiler.sources` 가 만든
중간표현과 (선택적으로) :mod:`mcportal.compiler.inference` 가 추론한 응답 스키마이며,
출력은 FastMCP 가 그대로 삼킬 수 있는 표준 OpenAPI 3.1 문서다.

설계 원칙
---------
1. **결정론.** 같은 입력이면 같은 바이트가 나온다. 문서에는 생성 시각·호스트명·
   사용자명·``fetched_at`` 을 싣지 않으며, 직렬화는 ``sort_keys=True`` 로 전 객체
   키에 전순서를 준다. 남는 자유도는 배열 원소 순서뿐이고 그것은 생성 시점에
   정렬로 고정한다.
2. **인증키는 문서에 없다.** ``security`` · ``securitySchemes`` 를 만들지 않고,
   ``info.x-mcportal.key_injection = "transport"`` 로 사실만 남긴다. 키 파라미터가
   섞여 들어오면 :class:`CompileError` 로 막는다(IR 불변식 I3 의 2차 방어선).
3. **빈 스키마를 만들지 않는다.** 샘플링을 안 한 오퍼레이션도 폴백 스키마와
   ``unresolved`` 카운트를 남겨, MCP 도구 설명이 비는 것을 막는다.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Union

from ..replay.scrub import CREDENTIAL_PARAM_NAMES, find_key_assignments
from .inference import InferenceReport
from .sources import OperationSpec, ParamSpec, SourceSpec

__all__ = [
    "OPENAPI_VERSION",
    "X_MCPORTAL",
    "CompileError",
    "CompileOptions",
    "DEFAULT_OPTIONS",
    "CompiledSpec",
    "schema_name_for",
    "build_openapi",
    "cast_scalar",
    "dumps",
    "write_spec",
]

PathLike = Union[str, Path]

#: 산출 문서가 선언하는 OpenAPI 버전.
OPENAPI_VERSION: str = "3.1.0"

#: MCPortal 확장 메타가 실리는 벤더 확장 키.
X_MCPORTAL: str = "x-mcportal"

#: operation_id 로 허용되는 형태(IR 불변식 I1).
_OPERATION_ID_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

#: 응답 스키마를 확정하지 못했을 때 쓰는 폴백 스키마의 설명문.
_FALLBACK_DESCRIPTION = "응답 스키마 미확정(샘플링 미수행)"

#: 200 응답에 붙는 고정 설명문.
_RESPONSE_DESCRIPTION = "정상 응답"

#: 요청 본문(POST 패스스루)에 쓰는 미디어 타입.
_REQUEST_MEDIA_TYPE = "application/json"

#: 인증키 주입 주체를 밝히는 메타 값.
_KEY_INJECTION = "transport"

#: MCP 도구가 실제로 받게 되는 응답 미디어 타입. XML 을 선언한 소스라도 런타임
#: 브리지(:mod:`mcportal.mcp`)가 정규화 JSON 으로 바꿔 돌려주므로, 200 응답의
#: content 키는 이 값으로 고정한다. 원 선언은 ``x-mcportal.upstream_media_type``
#: 에 남긴다(사실을 지우지 않되 도구가 호출 가능하게 한다).
_TOOL_MEDIA_TYPE = "application/json"

#: 자유문자열 메타(스펙 원본 URL·라이선스 메모·설명)에서 걸러 낼 인증키 이름들.
#: 파라미터에는 2차 방어선이 있는데 사람이 손으로 채우는 문자열에는 없어서,
#: 실키가 붙은 URL을 source_url 로 기록하면 그대로 커밋 산출물이 된다.
#: 정본은 :data:`mcportal.replay.scrub.CREDENTIAL_PARAM_NAMES` 다 — 컴파일러의
#: 다른 게이트(메타 예시값 흡수·병합 게이트)와 같은 목록을 봐야 한다.
_FREE_TEXT_KEY_PARAMS: tuple[str, ...] = CREDENTIAL_PARAM_NAMES

#: path 템플릿 표현식(``{name}``)을 뽑는 정규식.
_PATH_TEMPLATE_RE = re.compile(r"\{([^{}]+)\}")


class CompileError(ValueError):
    """OpenAPI 산출이 불가능할 때 발생한다(한국어 메시지)."""


@dataclass(frozen=True)
class CompileOptions:
    """컴파일 산출 옵션.

    Attributes:
        title: ``info.title``. None 이면 ``SourceSpec.service_name`` 을 쓴다.
        version: ``info.version``.
        server_url: ``servers[0].url``. None 이면 ``SourceSpec.base_url`` 을 쓴다.
        schema_name_prefix: ``components.schemas`` 키 앞에 붙일 접두사.
        generation_mode: ``"offline"`` (오프라인 컴파일) 또는 ``"sampled"``
            (라이브 샘플링 추론 결과 반영).
        include_deprecated: False 면 ``deprecated`` 오퍼레이션을 통째로 제외한다.
    """

    title: str | None = None
    version: str = "0.1.0"
    server_url: str | None = None
    schema_name_prefix: str = ""
    generation_mode: str = "offline"
    include_deprecated: bool = True


#: 인자를 생략했을 때 적용되는 기본 컴파일 옵션.
DEFAULT_OPTIONS: CompileOptions = CompileOptions()


@dataclass(frozen=True)
class CompiledSpec:
    """산출된 OpenAPI 문서와 그 인덱스.

    Attributes:
        document: OpenAPI 3.1 문서(순수 dict/list/스칼라만 담긴다).
        operation_ids: ``paths`` 에 실린 순서(= ``(path, method)`` 오름차순)의 도구 후보들.
        schema_names: ``components.schemas`` 키(알파벳 오름차순).
    """

    document: dict[str, Any]
    operation_ids: tuple[str, ...]
    schema_names: tuple[str, ...]


def _tool_version() -> str:
    """MCPortal 패키지 버전을 지연 조회한다(순환 임포트 회피).

    ``mcportal`` 패키지가 아직 초기화 중이어도 ``import`` 자체는 부분 초기화된
    모듈을 돌려주므로 실패하지 않는다. 버전 속성이 없으면 ``"unknown"``.
    """
    import mcportal

    return str(getattr(mcportal, "__version__", "unknown"))


def _plain(value: Any, *, where: str) -> Any:
    """임의의 매핑/시퀀스를 순수 JSON 값(dict/list/스칼라)으로 복사한다.

    읽기 전용 매핑이나 튜플이 문서에 그대로 실리면 직렬화·비교가 흔들리므로
    깊은 복사로 소유권을 끊는다.

    Args:
        value: 변환할 값.
        where: 오류 메시지에 쓸 위치 설명.

    Returns:
        dict/list/스칼라만으로 이루어진 값.

    Raises:
        CompileError: JSON 으로 표현할 수 없는 값이 섞여 있을 때.
    """
    if value is None or isinstance(value, (str, bool, int, float)):
        return value
    if isinstance(value, Mapping):
        return {str(key): _plain(item, where=where) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(item, where=where) for item in value]
    raise CompileError(
        f"{where}에 JSON으로 직렬화할 수 없는 값이 있습니다: {type(value).__name__}"
    )


def schema_name_for(operation_id: str, prefix: str = "") -> str:
    """``operation_id`` 에서 ``components.schemas`` 키를 만든다.

    ``prefix`` + PascalCase(operation_id) + ``"Response"`` 형태이며 ASCII 영숫자만
    남긴다. 예: ``getDemoList`` → ``GetDemoListResponse``.

    Args:
        operation_id: 오퍼레이션 식별자.
        prefix: 스키마 이름 접두사(ASCII 영숫자 외 문자는 제거된다).

    Returns:
        스키마 이름 문자열.
    """
    parts = [part for part in re.split(r"[^A-Za-z0-9]+", str(operation_id)) if part]
    pascal = "".join(part[:1].upper() + part[1:] for part in parts)
    if not pascal:
        pascal = "Operation"
    clean_prefix = re.sub(r"[^A-Za-z0-9]", "", str(prefix))
    return f"{clean_prefix}{pascal}Response"


def _unique_schema_name(base: str, taken: set[str]) -> str:
    """이미 쓰인 이름과 충돌하면 ``_2``, ``_3`` … 을 붙여 유일하게 만든다."""
    if base not in taken:
        return base
    index = 2
    while f"{base}_{index}" in taken:
        index += 1
    return f"{base}_{index}"


def _validate_operation(operation: OperationSpec, source: SourceSpec) -> None:
    """오퍼레이션이 IR 불변식(§4-1)을 지키는지 확인한다.

    Raises:
        CompileError: operation_id 형태(I1)·path 형태(I2)·메서드 결손 위반 시.
    """
    if not _OPERATION_ID_RE.match(str(operation.operation_id)):
        raise CompileError(
            "operation_id가 ASCII 식별자 규칙(^[A-Za-z_][A-Za-z0-9_]*$)을 "
            f"만족하지 않습니다: {operation.operation_id!r} "
            f"(서비스 {source.service_id})"
        )
    if not str(operation.path).startswith("/"):
        raise CompileError(
            f"path는 '/'로 시작해야 합니다: {operation.path!r} "
            f"(오퍼레이션 {operation.operation_id})"
        )
    if not str(operation.method).strip():
        raise CompileError(f"HTTP 메서드가 비었습니다(오퍼레이션 {operation.operation_id}).")

    # OpenAPI 3.1: path 의 모든 템플릿 표현식에는 대응하는 `in: path` 파라미터가
    # 있어야 한다. 없으면 도구는 채울 수 없는 자리를 남긴 채 호출되고, 리터럴
    # "{name}" 이 퍼센트 인코딩되어 경로에 박혀 상시 404 + 매 호출 쿼터 소모가 된다.
    # 정부 swagger 가 path 파라미터 선언을 빠뜨리거나, 그 파라미터가 인증키라서
    # I3 로 제거된 경우에 실제로 발생한다.
    template = {name.strip() for name in _PATH_TEMPLATE_RE.findall(str(operation.path))}
    declared = {
        str(param.name)
        for param in operation.parameters
        if str(param.location) == "path"
    }
    missing = sorted(template - declared)
    if missing:
        raise CompileError(
            f"path 템플릿에 대응하는 path 파라미터가 없습니다: {', '.join(missing)} "
            f"(경로 {operation.path}, 오퍼레이션 {operation.operation_id}). "
            "OpenAPI 3.1은 모든 템플릿 표현식에 in='path' 파라미터를 요구합니다. "
            "인증키 파라미터였다면 경로 자체를 바꿔야 합니다(인증키는 도구 인자가 아닙니다)."
        )
    orphan = sorted(declared - template)
    if orphan:
        raise CompileError(
            f"path 파라미터가 경로 템플릿에 없습니다: {', '.join(orphan)} "
            f"(경로 {operation.path}, 오퍼레이션 {operation.operation_id}). "
            "in='path' 파라미터는 반드시 '{이름}' 형태로 경로에 등장해야 합니다."
        )


def cast_scalar(text: str, declared_type: str) -> Any:
    """문자열 스칼라를 선언된 JSON Schema 타입으로 캐스팅한다(불가하면 ``None``).

    ``ParamSpec.enum`` · ``default`` · ``example`` 은 :mod:`~mcportal.compiler.sources`
    가 항상 ``str`` 로 고정한다(원 문서가 정수를 줘도 마찬가지). 그 문자열을
    캐스팅 없이 ``{"type": "integer"}`` 스키마에 실으면
    ``{"type":"integer","enum":["10","100"]}`` 처럼 **어떤 값으로도 만족할 수 없는
    스키마**가 나온다(정수는 enum 불일치, 문자열은 type 불일치). 입력 스키마로
    인자를 검증하는 MCP 클라이언트에서는 그 도구를 영구히 호출할 수 없다.

    Args:
        text: 캐스팅할 문자열.
        declared_type: 파라미터가 선언한 타입.

    Returns:
        캐스팅된 값. 타입이 문자열 계열이면 원문 그대로, 캐스팅 실패면 ``None``.
    """
    value = str(text)
    if declared_type == "integer":
        try:
            return int(value.strip())
        except (TypeError, ValueError):
            return None
    if declared_type == "number":
        try:
            return float(value.strip())
        except (TypeError, ValueError):
            return None
    if declared_type == "boolean":
        lowered = value.strip().lower()
        if lowered in ("true", "1"):
            return True
        if lowered in ("false", "0"):
            return False
        return None
    return value


def _cast_all(values: tuple[str, ...], declared_type: str) -> list[Any] | None:
    """열거값 전체를 캐스팅한다. 하나라도 실패하면 ``None``(= 통째로 생략)."""
    cast: list[Any] = []
    for item in values:
        converted = cast_scalar(item, declared_type)
        if converted is None:
            return None
        cast.append(converted)
    return cast


def _parameter_object(
    param: ParamSpec, *, operation_id: str, key_param: str
) -> dict[str, Any]:
    """``ParamSpec`` 을 OpenAPI 파라미터 객체로 옮긴다(§7-4).

    ``example`` 은 스키마 안이 아니라 **파라미터 레벨**에 싣고, ``enum`` 은 소스가
    준 원 순서를 그대로 보존한다.

    ``enum``·``default``·``example`` 은 선언된 타입으로 캐스팅해서 싣는다
    (:func:`cast_scalar`). 캐스팅할 수 없는 값은 **그 키를 통째로 생략**한다 —
    만족 불가능한 스키마를 만드느니 제약을 하나 덜 싣는 편이 도구를 살린다.
    ``array`` 타입의 ``enum`` 은 원소 허용값이라는 실제 의미대로 ``items.enum``
    으로 옮긴다(배열 스키마 레벨에 스칼라 열거값을 두면 어떤 값도 만족하지 못한다).

    Raises:
        CompileError: 인증키 파라미터가 섞여 있을 때(IR 불변식 I3 의 2차 방어선).
    """
    if str(param.name).lower() == str(key_param).lower():
        raise CompileError(
            f"인증키 파라미터가 오퍼레이션 {operation_id}의 parameters에 남아 있습니다: "
            f"{param.name!r}. 인증키는 트랜스포트가 주입하므로 스펙에 노출하지 않습니다."
        )
    declared = str(param.type)
    schema: dict[str, Any] = {"type": declared}
    if declared == "array":
        item_type = str(param.item_type or "string")
        items: dict[str, Any] = {"type": item_type}
        if param.enum:
            cast_items = _cast_all(param.enum, item_type)
            if cast_items is not None:
                items["enum"] = cast_items
        schema["items"] = items
    elif param.enum:
        cast_enum = _cast_all(param.enum, declared)
        if cast_enum is not None:
            schema["enum"] = cast_enum
    if param.default is not None:
        default = cast_scalar(str(param.default), declared)
        if default is not None:
            schema["default"] = default
    obj: dict[str, Any] = {
        "name": str(param.name),
        "in": str(param.location),
        "required": bool(param.required),
        "schema": schema,
    }
    if param.description is not None:
        obj["description"] = param.description
    if param.example is not None:
        example = cast_scalar(str(param.example), declared)
        value = str(param.example) if example is None else example
        obj["example"] = value
        # 파라미터 레벨 example 은 MCP 도구까지 도달하지 않는다. FastMCP 의
        # OpenAPI->tool 변환은 `parameters[].schema` 만 도구 입력 스키마로 옮기고
        # `parameters[].example` 은 버린다(2.14.7 실측). 큐레이션한 예시값이 LLM
        # 에게 0개 도달하던 원인이므로(적대 리뷰 F2), JSON Schema 2020-12 의 표준
        # 주석 키워드인 `examples` 로 **스키마 안에도** 같은 값을 싣는다.
        # OpenAPI 3.1 은 스키마 방언이 2020-12 이므로 문서 유효성도 유지된다.
        schema["examples"] = [value]
    return obj


def _operation_object(
    operation: OperationSpec,
    *,
    source: SourceSpec,
    schema_name: str,
) -> dict[str, Any]:
    """오퍼레이션 1개를 OpenAPI 오퍼레이션 객체로 옮긴다(§7-2).

    값이 없는 선택 항목(summary·description·tags·parameters·deprecated)은 키 자체를
    생략해 문서에 빈 자리가 남지 않게 한다.
    """
    obj: dict[str, Any] = {"operationId": str(operation.operation_id)}
    if operation.summary is not None:
        obj["summary"] = operation.summary
    if operation.description is not None:
        obj["description"] = operation.description
    if operation.tags:
        obj["tags"] = sorted(str(tag) for tag in operation.tags)
    if operation.deprecated:
        obj["deprecated"] = True
    parameters = [
        _parameter_object(
            param,
            operation_id=str(operation.operation_id),
            key_param=str(source.key_param),
        )
        for param in operation.parameters
    ]
    if parameters:
        obj["parameters"] = parameters
    if operation.request_body_schema is not None:
        obj["requestBody"] = {
            "required": True,
            "content": {
                _REQUEST_MEDIA_TYPE: {
                    "schema": _plain(
                        operation.request_body_schema,
                        where=f"오퍼레이션 {operation.operation_id}의 request_body_schema",
                    )
                }
            },
        }
    # 200 content 키는 항상 application/json 이다. 소스가 XML 을 선언했더라도
    # 런타임 브리지가 정규화 JSON 으로 바꿔 돌려주기 때문이다. 선언과 실제가
    # 어긋나면 fastmcp 가 outputSchema 는 있는데 structured output 을 만들지 못해
    # **도구 호출이 100% 실패한다**(XML 응답 서비스가 목록에는 뜨는데 못 부르는 상태).
    upstream_media = str(operation.response_media_type or _TOOL_MEDIA_TYPE)
    response_object: dict[str, Any] = {
        "description": _RESPONSE_DESCRIPTION,
        "content": {
            _TOOL_MEDIA_TYPE: {"schema": {"$ref": f"#/components/schemas/{schema_name}"}}
        },
    }
    if upstream_media != _TOOL_MEDIA_TYPE:
        # 사실을 지우지 않는다: 상위 API 가 실제로 무엇을 보내는지는 남긴다.
        response_object[X_MCPORTAL] = {"upstream_media_type": upstream_media}
    obj["responses"] = {"200": response_object}
    return obj


def _guard_free_text(value: Any, *, where: str, key_param: str) -> str:
    """자유문자열 메타에 인증키가 붙어 있으면 :class:`CompileError` 로 막는다.

    ``parameters`` 에는 인증키 2차 방어선이 있는데 사람이 손으로 채우는 문자열
    (``source_url``·``license_note``·``description``)에는 아무 게이트가 없었다.
    스펙 문서를 실키가 붙은 URL로 받아 온 뒤 그 URL을 ``source_url`` 로 기록하는
    것은 자연스러운 사용법이고, 산출 문서는 ``specs/`` 아래 **커밋 대상**이므로
    현실적인 유출 경로다. parameters 게이트와 대칭이 되도록 여기서도 거부한다.

    Args:
        value: 검사할 자유문자열.
        where: 오류 메시지에 쓸 위치 설명.
        key_param: 이 소스의 인증키 파라미터 이름(기본 목록에 더해 검사한다).

    Returns:
        검사를 통과한 문자열.

    Raises:
        CompileError: 인증키에 값이 붙어 있을 때.
    """
    text = str(value)
    names = (*_FREE_TEXT_KEY_PARAMS, str(key_param))
    found = find_key_assignments(text, names)
    if found:
        raise CompileError(
            f"{where}에 인증키 값이 남아 있습니다(발견된 파라미터: {', '.join(found)}). "
            "산출 문서는 커밋 대상이므로 자격증명이 실린 채로 만들 수 없습니다. "
            "해당 값을 제거하거나 스크러빙한 뒤 다시 컴파일하세요."
        )
    return text


def _guard_operation_free_text(
    operations: Sequence[OperationSpec], *, key_param: str
) -> None:
    """오퍼레이션·파라미터 설명문에 남은 인증키 대입을 막는다(자유문자열 게이트 확장).

    :func:`_guard_free_text` 는 ``info.title`` · ``info.description`` ·
    ``source_url`` · ``license_note`` 4필드만 봤다. 큐레이션을 거치는 경로는
    :mod:`~mcportal.compiler.curation` 의 병합 게이트(V8)가 오퍼레이션·파라미터
    문자열까지 훑지만, **큐레이션이 없는 경로**(``curated=False``, ``curation.json``
    부재)에는 그 게이트가 아예 실행되지 않는다. 그래서 원 스펙의 오퍼레이션
    설명에 ``인증키=<값>`` 형태의 호출 예시가 있으면 :func:`build_openapi` 와
    :func:`dumps` 를 통과해, 파일로 쓰지 않는 라이브러리 경로
    (``document`` → FastMCP 도구 설명)로 그대로 흘러갔다. 파일 경로만 막는
    :func:`write_spec` 과 대칭이 되도록 산출 직전에 접는다
    (2026-08-09 Advisor 검증 A4).

    자격증명 이름 목록은 F4 처방대로 :data:`_FREE_TEXT_KEY_PARAMS` 정본을 쓴다.

    Args:
        operations: 산출 대상 오퍼레이션들.
        key_param: 이 소스의 인증키 파라미터 이름.

    Raises:
        CompileError: 어느 오퍼레이션·파라미터의 어느 필드인지 밝히며 막는다.
    """
    for operation in operations:
        operation_id = str(operation.operation_id)
        if operation.summary is not None:
            _guard_free_text(
                operation.summary,
                where=f"오퍼레이션 {operation_id}의 summary",
                key_param=key_param,
            )
        if operation.description is not None:
            _guard_free_text(
                operation.description,
                where=f"오퍼레이션 {operation_id}의 description",
                key_param=key_param,
            )
        for param in operation.parameters:
            if param.description is None:
                continue
            _guard_free_text(
                param.description,
                where=(
                    f"오퍼레이션 {operation_id}의 파라미터 "
                    f"{param.name}의 description"
                ),
                key_param=key_param,
            )


def build_openapi(
    source: SourceSpec,
    response_schemas: Mapping[str, Mapping[str, Any]] | None = None,
    *,
    options: CompileOptions = DEFAULT_OPTIONS,
    reports: Mapping[str, InferenceReport] | None = None,
) -> CompiledSpec:
    """``SourceSpec``(+추론 스키마)을 OpenAPI 3.1 문서로 컴파일한다.

    Args:
        source: 정규화된 스펙 소스.
        response_schemas: operation_id → 응답 JSON Schema. 여기 있으면 최우선,
            없으면 ``OperationSpec.response_schema``, 그것도 없으면 폴백 스키마(§7-3).
        options: 산출 옵션.
        reports: operation_id → :class:`InferenceReport`. 있으면 ``x-mcportal`` 메타의
            ``sample_count`` · ``schema_inference`` 요약에 반영한다(문서에 실리는 것은
            **집계 수치뿐**이며 샘플 값은 실리지 않는다).

    Returns:
        :class:`CompiledSpec` — 문서와 그 인덱스.

    Raises:
        CompileError: operations 가 비었거나, IR 불변식(§4-1)이 깨졌거나,
            인증키 파라미터가 남아 있거나, 자유문자열(메타·오퍼레이션·파라미터
            설명)에 인증키 대입이 남아 있거나, 필수 메타(title·server_url)를
            결정할 수 없을 때.
    """
    if not source.operations:
        raise CompileError(
            "컴파일할 오퍼레이션이 없습니다. SourceSpec.operations가 비어 있습니다 "
            f"(서비스 {source.service_id})."
        )

    selected = [
        operation
        for operation in source.operations
        if options.include_deprecated or not operation.deprecated
    ]
    if not selected:
        raise CompileError(
            "컴파일할 오퍼레이션이 없습니다. include_deprecated=False 로 모든 "
            f"오퍼레이션이 제외되었습니다(서비스 {source.service_id})."
        )
    # (path, method) 오름차순 — 결정론(I4). IR이 이미 정렬돼 있어도 방어적으로 고정한다.
    selected.sort(key=lambda operation: (str(operation.path), str(operation.method).upper()))

    key_param = str(source.key_param)
    # 큐레이션 없는 경로에는 병합 게이트가 없다. 자유문자열 검사를 산출 전에 건다.
    _guard_operation_free_text(selected, key_param=key_param)

    title = options.title or source.service_name
    if not title:
        raise CompileError(
            "info.title을 결정할 수 없습니다. CompileOptions.title 또는 "
            "SourceSpec.service_name 중 하나가 필요합니다."
        )
    server_url = options.server_url or source.base_url
    if not server_url:
        raise CompileError(
            "servers[0].url을 결정할 수 없습니다. CompileOptions.server_url 또는 "
            "SourceSpec.base_url 중 하나가 필요합니다."
        )

    schemas: dict[str, Any] = {}
    paths: dict[str, Any] = {}
    operation_ids: list[str] = []
    taken_names: set[str] = set()
    seen_ids: set[str] = set()
    unresolved = 0
    sample_count = 0
    conflict_count = 0
    truncated = False

    for operation in selected:
        _validate_operation(operation, source)
        operation_id = str(operation.operation_id)
        if operation_id in seen_ids:
            raise CompileError(
                f"operation_id가 중복됩니다: {operation_id!r} (서비스 {source.service_id})."
            )
        seen_ids.add(operation_id)

        schema_name = _unique_schema_name(
            schema_name_for(operation_id, options.schema_name_prefix), taken_names
        )
        taken_names.add(schema_name)

        # ① 추론 스키마 > ② 소스가 준 스키마 > ③ 폴백(§7-3).
        provided = response_schemas.get(operation_id) if response_schemas else None
        if provided is not None:
            body = _plain(provided, where=f"{operation_id}의 추론 응답 스키마")
        elif operation.response_schema is not None:
            body = _plain(operation.response_schema, where=f"{operation_id}의 소스 응답 스키마")
        else:
            body = {"type": "object", "description": _FALLBACK_DESCRIPTION}
            unresolved += 1
        schemas[schema_name] = body

        report = reports.get(operation_id) if reports else None
        if report is not None:
            sample_count += int(report.sample_count)
            conflict_count += len(report.conflicts)
            truncated = truncated or bool(report.truncated)

        method = str(operation.method).lower()
        path_item = paths.setdefault(str(operation.path), {})
        if method in path_item:
            raise CompileError(
                f"같은 path에 같은 메서드가 중복됩니다: {operation.method} {operation.path} "
                f"(오퍼레이션 {operation_id})."
            )
        path_item[method] = _operation_object(
            operation, source=source, schema_name=schema_name
        )
        operation_ids.append(operation_id)

    meta: dict[str, Any] = {
        "tool_version": _tool_version(),
        "provider": str(source.provider),
        "service_id": str(source.service_id),
        "source_kind": str(getattr(source.source_kind, "value", source.source_kind)),
        "source_fingerprint": str(source.fingerprint),
        "generation_mode": str(options.generation_mode),
        "sample_count": sample_count,
        "key_injection": _KEY_INJECTION,
        "schema_inference": {"conflicts": conflict_count, "truncated": truncated},
    }
    if source.source_url is not None:
        meta["source_url"] = _guard_free_text(
            source.source_url, where="SourceSpec.source_url", key_param=key_param
        )
    if source.license_note is not None:
        meta["license_note"] = _guard_free_text(
            source.license_note, where="SourceSpec.license_note", key_param=key_param
        )
    if unresolved:
        meta["schema_inference"]["unresolved"] = unresolved

    info: dict[str, Any] = {
        "title": _guard_free_text(title, where="info.title", key_param=key_param),
        "version": str(options.version),
    }
    if source.description:
        info["description"] = _guard_free_text(
            source.description, where="SourceSpec.description", key_param=key_param
        )
    info[X_MCPORTAL] = meta

    document: dict[str, Any] = {
        "openapi": OPENAPI_VERSION,
        "info": info,
        "servers": [{"url": str(server_url)}],
        "paths": paths,
        "components": {"schemas": schemas},
    }
    return CompiledSpec(
        document=document,
        operation_ids=tuple(operation_ids),
        schema_names=tuple(sorted(schemas)),
    )


def dumps(document: Mapping[str, Any]) -> str:
    """문서를 결정론 JSON 문자열로 직렬화한다.

    ``sort_keys=True`` 가 전 객체 키에 전순서를 주고, ``ensure_ascii=False`` 가
    한국어 설명을 ``\\uXXXX`` 로 부풀리지 않게 한다. 끝에 개행 1개를 붙인다.

    Args:
        document: 직렬화할 문서.

    Returns:
        UTF-8 로 그대로 쓸 수 있는 JSON 문자열(끝 개행 포함).
    """
    return (
        json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    )


def write_spec(document: Mapping[str, Any], path: PathLike) -> Path:
    """:func:`dumps` 결과를 UTF-8 · LF 개행으로 파일에 쓴다.

    Windows 기본 개행(CRLF)이 섞이면 같은 내용이 다른 바이트가 되어 결정론이
    깨지므로 ``newline="\\n"`` 을 명시한다. 부모 디렉터리는 자동 생성한다.

    쓰기 직전 문서 전체를 훑어 인증키 값이 남아 있는지 확인한다
    (:func:`~mcportal.replay.scrub.find_key_assignments`). ``specs/`` 아래 산출물은
    커밋 대상이므로, 어떤 경로로 만들어진 문서든 이 게이트를 통과해야 디스크에
    닿는다. :func:`build_openapi` 의 메타 게이트와 이중 방어를 이룬다.

    Args:
        document: 저장할 문서.
        path: 저장 경로.

    Returns:
        저장된 파일 경로.

    Raises:
        CompileError: 문서 어딘가에 인증키 값이 남아 있을 때.
    """
    text = dumps(document)
    found = find_key_assignments(text, _FREE_TEXT_KEY_PARAMS)
    if found:
        raise CompileError(
            f"저장하려는 문서에 인증키 값이 남아 있습니다(발견된 파라미터: "
            f"{', '.join(found)}). specs/ 산출물은 커밋 대상이므로 자격증명이 실린 "
            "채로 저장할 수 없습니다."
        )
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    with open(target, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)
    return target
