# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Yong Park
"""스펙 소스 어댑터 — 서로 다른 스펙 제공 방식을 단일 중간표현으로 흡수한다.

공공데이터포털(data.go.kr)은 같은 게이트웨이 위에서도 스펙을 세 갈래로 준다.

1. **odcloud JSON형** — ``infuser.odcloud.kr`` 가 돌려주는 OpenAPI 문서. ``page`` /
   ``perPage`` / ``returnType`` 이라는 자체 공통 질의 파라미터 규약을 쓴다.
2. **GW Swagger형** — 게이트웨이가 돌려주는 Swagger 2.0 또는 OpenAPI 3.x 문서.
   표준 REST 규약(``pageNo`` / ``numOfRows`` / ``type``)을 쓰고 XML·JSON 이중 응답이 흔하다.
3. **표준 REST 문서형** — 기계가 읽을 스펙이 아예 없고 활용가이드 문서(HWP/PDF)만
   있는 소스. 사람이 옮겨 적은 **수동 매핑 기술서**(JSON)를 입력으로 받는다(§5-2).

여기에 목록조회서비스(공공데이터 목록) **메타 행**을 더해 넷을 모두
:class:`SourceSpec` 하나로 정규화한다. 이 모듈이 컴파일러 파이프라인의 유일한
입구이며, 이후 단계(스키마 추론·OpenAPI 산출·MCP 연결)는 원 문서 형태를 다시
알 필요가 없다.

설계 원칙
---------
* **결정론** — 모든 dict 순회는 ``sorted()`` 를 거친다. 같은 문서를 키 순서만 바꿔
  넣어도 같은 :class:`SourceSpec` 이 나온다.
* **인증키 격리(I3)** — 소스에 ``serviceKey`` 류 파라미터가 있어도 결과에서 제거하고
  이름만 :attr:`SourceSpec.key_param` 에 남긴다. 인증키는 트랜스포트가 주입하며,
  MCP 도구 인자로 노출되면 LLM 프롬프트·스펙 파일로 유출될 수 있다.
* **빈 응답 스키마를 숨기지 않는다** — 소스가 응답 스키마를 주지 않으면
  :attr:`OperationSpec.response_schema` 를 ``None`` 으로 두어 "추론기가 채울 자리"임을
  명시한다. 빈 ``{}`` 로 얼버무리지 않는다(:func:`unresolved_schema_operations` 로 조회).
* **실패는 한국어로 구체적으로** — 어떤 필드가 왜 부족한지 :class:`SourceSpecError`
  메시지에 적는다.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, replace
from enum import StrEnum
from typing import Any
from urllib.parse import urlsplit

__all__ = [
    "CATALOG_COMMON_PARAMS",
    "CatalogEntry",
    "ODCLOUD_COMMON_PARAMS",
    "OperationSpec",
    "ParamSpec",
    "STANDARD_COMMON_PARAMS",
    "SourceKind",
    "SourceSpec",
    "SourceSpecError",
    "catalog_entries_to_sources",
    "catalog_entry_to_source",
    "detect_source_kind",
    "fingerprint_document",
    "load_catalog_rows",
    "load_gw_swagger",
    "load_odcloud_swagger",
    "load_rest_doc",
    "load_source",
    "unresolved_schema_operations",
]


# ---------------------------------------------------------------------------
# 예외
# ---------------------------------------------------------------------------
class SourceSpecError(ValueError):
    """스펙 소스를 :class:`SourceSpec` 으로 정규화할 수 없을 때 발생한다(한국어 메시지)."""


# ---------------------------------------------------------------------------
# 중간표현(IR)
# ---------------------------------------------------------------------------
class SourceKind(StrEnum):
    """스펙 소스의 제공 방식(W0 실사 3분류 + 목록조회 메타)."""

    ODCLOUD_SWAGGER = "odcloud_swagger"   # infuser.odcloud.kr OAS(JSON형)
    GW_SWAGGER = "gw_swagger"             # data.go.kr GW Swagger(2.0/3.x)
    REST_DOC_MANUAL = "rest_doc_manual"   # 활용가이드 문서 기반 수동 매핑 기술서
    CATALOG_META = "catalog_meta"         # 목록조회서비스 메타 행


@dataclass(frozen=True)
class ParamSpec:
    """오퍼레이션 파라미터 1개."""

    name: str
    location: str                      # "query" | "path" | "header"
    required: bool
    type: str                          # "string"|"integer"|"number"|"boolean"|"array"
    description: str | None = None
    example: str | None = None
    enum: tuple[str, ...] = ()
    default: str | None = None
    item_type: str | None = None       # type == "array" 일 때 원소 타입


@dataclass(frozen=True)
class OperationSpec:
    """단일 오퍼레이션(= MCP 도구 1개로 변환될 단위).

    ``response_schema`` 가 ``None`` 이면 **소스가 응답 스키마를 주지 않았다**는
    사실을 뜻한다. 그 자리는 라이브 샘플링 + 스키마 추론이 채운다(빈 ``{}`` 와
    구분하기 위해 일부러 ``None`` 을 쓴다).
    """

    operation_id: str                  # ASCII, [A-Za-z_][A-Za-z0-9_]* 보장
    method: str                        # 대문자 "GET"|"POST"
    path: str                          # base_url 이후 경로. 반드시 "/" 로 시작
    summary: str | None = None
    description: str | None = None
    parameters: tuple[ParamSpec, ...] = ()
    response_media_type: str = "application/json"
    response_schema: Mapping[str, Any] | None = None      # None이면 추론 대상
    request_body_schema: Mapping[str, Any] | None = None  # POST 본문(패스스루)
    tags: tuple[str, ...] = ()
    deprecated: bool = False


@dataclass(frozen=True)
class SourceSpec:
    """스펙 소스 1건을 정규화한 중간표현. 컴파일러의 유일한 입력."""

    provider: str                      # "data.go.kr"
    service_id: str                    # 포털 데이터셋 ID
    service_name: str                  # 한국어 서비스명(= 기본 info.title)
    base_url: str                      # 끝 "/" 없음
    source_kind: SourceKind
    operations: tuple[OperationSpec, ...]
    key_param: str = "serviceKey"      # 이 소스의 인증키 파라미터명(도구 인자에서 제외됨)
    source_url: str | None = None      # 스펙 원본 URL(없으면 None)
    fingerprint: str = ""              # "sha256:<64hex>" — 원본 문서 지문
    fetched_at: str | None = None       # ISO8601 KST. OpenAPI 문서에는 싣지 않는다
    description: str | None = None
    license_note: str | None = None    # KOGL 유형 등


@dataclass(frozen=True)
class CatalogEntry:
    """목록조회서비스 응답 행 1건의 정규화 형태."""

    service_id: str
    title: str
    end_point_url: str | None
    operation_name: str | None
    operation_url: str | None
    api_type: str | None               # "REST" | "LINK" | …
    data_format: str | None            # "JSON+XML" 등
    swagger_json_url: str | None
    guide_url: str | None
    org_name: str | None = None


# ---------------------------------------------------------------------------
# 상수
# ---------------------------------------------------------------------------
#: 기본 프로바이더 표기.
DEFAULT_PROVIDER = "data.go.kr"

#: W2가 흡수하는 HTTP 메서드(소문자). 그 밖의 메서드는 조용히 제외한다.
SUPPORTED_METHODS: tuple[str, ...] = ("get", "post")

#: W2가 흡수하는 파라미터 위치. ``formData``(2.0)·``cookie``(3.x)는 조용히 버린다.
SUPPORTED_LOCATIONS: tuple[str, ...] = ("query", "path", "header")

#: ParamSpec.type 으로 허용하는 값. 그 밖의 표기는 "string" 으로 접는다.
SUPPORTED_TYPES: tuple[str, ...] = ("string", "integer", "number", "boolean", "array")

#: 호스트에 이 라벨이 들어 있으면 odcloud 계열로 판정한다(infuser·api.odcloud 계열).
ODCLOUD_HOST_MARKER = "odcloud"

_JSON_MEDIA_TYPE = "application/json"
_XML_MEDIA_TYPE = "application/xml"

_OPERATION_ID_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_NON_IDENTIFIER_RE = re.compile(r"[^A-Za-z0-9_]")
_UNDERSCORE_RUN_RE = re.compile(r"_+")

#: 후보 식별자에 의미 있는 문자(ASCII 영숫자)가 하나라도 있는지 판정한다.
#: 비ASCII 치환 결과가 밑줄뿐인 경우를 "비어 있음"으로 취급하기 위해 쓴다(§4-2 규칙 2).
_ASCII_ALNUM_RE = re.compile(r"[A-Za-z0-9]")
_KEY_NORMALIZE_RE = re.compile(r"[^a-z0-9]")
_MAX_OPERATION_ID_LENGTH = 64

#: ``$ref`` 인라인 전개 횟수 상한. 공유 정의를 반복 참조하는 문서는 전개 노드 수가
#: 지수적으로 늘어나므로(순환이 아니어도) 상한 없이 펼치면 서비스 거부 경로가 된다.
#: 현실적인 정부 스펙 문서의 응답 스키마는 수백~수천 회면 충분히 펼쳐진다.
_MAX_REF_EXPANSIONS = 20_000

#: odcloud 계열이 전 오퍼레이션에서 공통으로 받는 질의 파라미터의 표준 표기.
#: 원 문서에 없으면 보강(backfill)하고, 있으면 설명·예시만 채운다.
#: 페이징 파라미터에 ``example`` 을 주지 않는 것은 의도적이다 — 샘플러가 페이지를
#: 1,2,3으로 늘려가며 서로 다른 표본을 얻어야 하기 때문이다(설계 §8-2).
ODCLOUD_COMMON_PARAMS: tuple[ParamSpec, ...] = (
    ParamSpec(
        name="page",
        location="query",
        required=False,
        type="integer",
        description="조회할 페이지 번호(1부터 시작). odcloud 공통 파라미터.",
    ),
    ParamSpec(
        name="perPage",
        location="query",
        required=False,
        type="integer",
        description="한 페이지에 담을 행 수. odcloud 공통 파라미터.",
        example="10",
    ),
    ParamSpec(
        name="returnType",
        location="query",
        required=False,
        type="string",
        description="응답 형식(JSON 기본, XML 선택). odcloud 공통 파라미터.",
        enum=("json", "xml"),
        example="json",
    ),
)

#: 표준 REST 규약(게이트웨이 Swagger·활용가이드 문서형)의 공통 질의 파라미터 표기.
STANDARD_COMMON_PARAMS: tuple[ParamSpec, ...] = (
    ParamSpec(
        name="pageNo",
        location="query",
        required=False,
        type="integer",
        description="조회할 페이지 번호(1부터 시작). 표준 REST 공통 파라미터.",
    ),
    ParamSpec(
        name="numOfRows",
        location="query",
        required=False,
        type="integer",
        description="한 페이지에 담을 결과 수. 표준 REST 공통 파라미터.",
        example="10",
    ),
    ParamSpec(
        name="type",
        location="query",
        required=False,
        type="string",
        description="응답 형식 선택(XML 기본, JSON 선택). XML·JSON 이중 제공 서비스에서만 의미가 있다.",
        enum=("xml", "json"),
        example="json",
    ),
)

#: 목록조회 메타에서 승격한 골격 오퍼레이션에 항상 붙는 공통 파라미터.
#: ``type`` 은 메타의 dataFormat 이 XML·JSON 이중 제공을 표기했을 때만 붙는다.
CATALOG_COMMON_PARAMS: tuple[ParamSpec, ...] = tuple(
    param for param in STANDARD_COMMON_PARAMS if param.name != "type"
)

_CATALOG_TYPE_PARAM: ParamSpec = next(
    param for param in STANDARD_COMMON_PARAMS if param.name == "type"
)

#: 목록조회서비스 응답 행의 키 표기 흔들림을 흡수하는 별칭표.
#: 비교 시 키는 소문자화 + 영숫자만 남긴 형태로 정규화하므로 camelCase·snake_case가
#: 같은 항목으로 접힌다(``endPoint`` / ``end_point`` → ``endpoint``).
_CATALOG_ALIASES: dict[str, tuple[str, ...]] = {
    "service_id": ("serviceid", "listid", "publicdatapk", "datasetid", "id"),
    "title": ("title", "listtitle", "servicenm", "servicename", "datasetnm", "name"),
    "end_point_url": ("endpoint", "endpointurl", "endpointaddress", "baseurl"),
    "operation_name": ("operationnm", "operationname", "opnm"),
    "operation_url": ("operationurl", "opurl", "callbackurl"),
    "api_type": ("apitype", "apitypenm", "servicetype"),
    "data_format": ("dataformat", "dataformatnm", "responseformat"),
    "swagger_json_url": ("swaggerjsonurl", "swaggerurl", "swaggerjson", "oasurl"),
    "guide_url": ("guideurl", "docurl", "documenturl", "detailurl"),
    "org_name": ("orgnm", "orgname", "organizationname", "instnm", "deptnm"),
}

#: 오류 메시지에 보여 줄 대표 키 표기(포털 실제 표기 형태). 비교 자체는
#: :data:`_CATALOG_ALIASES` 의 정규화 형태로 하므로 ``list_id`` 표기도 인식된다.
_CATALOG_ALIAS_HINTS: dict[str, tuple[str, ...]] = {
    "service_id": ("listId", "serviceId", "publicDataPk", "dataSetId", "id"),
    "title": ("listTitle", "title", "serviceNm", "serviceName", "dataSetNm"),
}


# ---------------------------------------------------------------------------
# 지문
# ---------------------------------------------------------------------------
def _json_default(value: Any) -> Any:
    """지문 계산용 JSON 직렬화 폴백(매핑·시퀀스를 표준 타입으로 접는다)."""
    if isinstance(value, Mapping):
        return dict(value)
    if isinstance(value, (tuple, list)):
        return list(value)
    if isinstance(value, (set, frozenset)):
        return sorted(value, key=repr)
    raise TypeError(
        f"지문을 계산할 수 없는 값이 포함되어 있습니다: {type(value).__name__}"
    )


def fingerprint_document(document: Mapping[str, Any] | str | bytes) -> str:
    """원본 문서의 sha256 지문을 ``"sha256:<64hex>"`` 형태로 돌려준다.

    Args:
        document: 매핑(JSON 객체)·문자열·바이트 중 하나. 매핑은
            ``json.dumps(sort_keys=True, ensure_ascii=False, separators=(",", ":"))``
            로 정규화한 UTF-8 바이트를, 문자열은 UTF-8 인코딩 바이트를, 바이트는
            그대로 해싱한다.

    Returns:
        ``"sha256:"`` 접두 + 소문자 64자리 16진수.

    Raises:
        SourceSpecError: 지원하지 않는 타입이거나 JSON 직렬화가 불가능할 때.
    """
    if isinstance(document, bytes):
        payload = document
    elif isinstance(document, str):
        payload = document.encode("utf-8")
    elif isinstance(document, Mapping):
        try:
            text = json.dumps(
                dict(document),
                sort_keys=True,
                ensure_ascii=False,
                separators=(",", ":"),
                default=_json_default,
            )
        except TypeError as exc:  # pragma: no cover - 방어적 분기
            raise SourceSpecError(
                f"문서 지문을 계산할 수 없습니다(JSON 직렬화 불가): {exc}"
            ) from exc
        payload = text.encode("utf-8")
    else:
        raise SourceSpecError(
            "지문 계산 입력은 매핑(JSON 객체)·문자열·바이트여야 합니다. "
            f"받은 타입: {type(document).__name__}"
        )
    return "sha256:" + hashlib.sha256(payload).hexdigest()


# ---------------------------------------------------------------------------
# 작은 도우미
# ---------------------------------------------------------------------------
def _require_mapping(value: Any, *, what: str) -> Mapping[str, Any]:
    """매핑임을 확인하고 돌려준다(아니면 :class:`SourceSpecError`)."""
    if not isinstance(value, Mapping):
        raise SourceSpecError(
            f"{what}은(는) 매핑(JSON 객체)이어야 합니다. 받은 타입: "
            f"{type(value).__name__}"
        )
    return value


def _text(value: Any) -> str | None:
    """문자열 값을 정리해 돌려준다(빈 값·None은 ``None``)."""
    if value is None:
        return None
    if isinstance(value, str):
        stripped = value.strip()
        return stripped or None
    return None


def _scalar_text(value: Any) -> str | None:
    """스칼라(문자열·수·불리언)를 API 질의값 표기의 문자열로 돌려준다."""
    if value is None:
        return None
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str):
        stripped = value.strip()
        return stripped or None
    if isinstance(value, (int, float)):
        return str(value)
    return None


def _lower_keys(mapping: Mapping[str, Any]) -> dict[str, Any]:
    """키를 소문자로 접은 사본을 돌려준다(대소문자 흔들림 흡수)."""
    return {str(key).lower(): value for key, value in mapping.items()}


def _normalize_key(key: Any) -> str:
    """행 키를 비교용으로 정규화한다(소문자 + 영숫자만)."""
    return _KEY_NORMALIZE_RE.sub("", str(key).lower())


def _param_type(raw: Any, *, fallback: str = "string") -> str:
    """스펙의 타입 표기를 :data:`SUPPORTED_TYPES` 중 하나로 접는다."""
    text = _text(raw)
    if text is None:
        return fallback
    lowered = text.lower()
    if lowered in SUPPORTED_TYPES:
        return lowered
    if lowered in ("int", "long", "int32", "int64"):
        return "integer"
    if lowered in ("float", "double", "decimal"):
        return "number"
    if lowered in ("bool",):
        return "boolean"
    # object·file 등 W2가 파라미터로 다루지 않는 표기는 문자열로 접는다.
    return fallback


def _enum_tuple(raw: Any) -> tuple[str, ...]:
    """열거값을 문자열 튜플로 만든다(원 순서 보존)."""
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)):
        return ()
    values: list[str] = []
    for item in raw:
        text = _scalar_text(item)
        if text is not None and text not in values:
            values.append(text)
    return tuple(values)


def _first_text(raw: Any) -> str | None:
    """문자열 목록(``produces`` · ``schemes`` 등)에서 첫 유효 값을 돌려준다."""
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)):
        return None
    for item in raw:
        text = _text(item)
        if text is not None:
            return text
    return None


def _normalize_base_url(raw: Any, *, where: str) -> str:
    """base_url을 I6(스킴 포함 · 끝 ``/`` 없음)에 맞게 정규화한다."""
    text = _text(raw)
    if text is None:
        raise SourceSpecError(
            f"{where}: base_url이 비어 있어 서버 주소를 정할 수 없습니다."
        )
    url = text.rstrip("/")
    if "://" not in url:
        raise SourceSpecError(
            f"{where}: base_url '{text}'에 스킴(https:// 등)이 없습니다. "
            "MCPortal은 절대 URL만 받습니다(불변식 I6)."
        )
    if not urlsplit(url).netloc:
        raise SourceSpecError(
            f"{where}: base_url '{text}'에서 호스트를 읽을 수 없습니다(불변식 I6)."
        )
    return url


def _normalize_path(raw: Any, *, where: str) -> str:
    """오퍼레이션 경로를 I2(``/`` 시작 · base_url 미포함)에 맞게 정규화한다."""
    text = _text(raw)
    if text is None:
        raise SourceSpecError(
            f"{where}: 'path' 필드가 비어 있습니다. path는 base_url 이후 경로이며 "
            "'/'로 시작해야 합니다(불변식 I2)."
        )
    if "://" in text:
        raise SourceSpecError(
            f"{where}: path '{text}'에 절대 URL이 들어 있습니다. path에는 base_url "
            "이후 경로만 적어야 합니다(불변식 I2)."
        )
    return text if text.startswith("/") else "/" + text


def _sorted_params(params: Sequence[ParamSpec]) -> tuple[ParamSpec, ...]:
    """I5 정렬(필수 먼저, 그 안에서 이름 오름차순)을 적용한다."""
    return tuple(sorted(params, key=lambda param: (not param.required, param.name)))


def unresolved_schema_operations(source: SourceSpec) -> tuple[str, ...]:
    """응답 스키마가 비어 있는(= 추론기가 채울) 오퍼레이션 id들을 돌려준다.

    소스가 응답 스키마를 주지 않은 자리는 :attr:`OperationSpec.response_schema` 가
    ``None`` 으로 남는다. 이 함수는 그 목록을 정렬해 돌려주므로, 샘플링·추론
    단계가 "무엇을 채워야 하는지"를 문서 형태에 관계없이 알 수 있다.

    Args:
        source: 정규화된 스펙 소스.

    Returns:
        ``operation_id`` 오름차순 튜플. 전부 스키마가 있으면 빈 튜플.
    """
    return tuple(
        sorted(
            operation.operation_id
            for operation in source.operations
            if operation.response_schema is None
        )
    )


# ---------------------------------------------------------------------------
# operation_id 정규화(§4-2, 불변식 I1)
# ---------------------------------------------------------------------------
def _path_slug(path: str) -> str:
    """경로에서 ASCII 영숫자만 남긴 슬러그를 만든다(연속 ``_`` 축약)."""
    slug = _UNDERSCORE_RUN_RE.sub("_", _NON_IDENTIFIER_RE.sub("_", path))
    return slug.strip("_")


def _cap_length(candidate: str, *, fingerprint: str) -> str:
    """식별자 길이 상한 64자를 적용한다(초과 시 앞 57자 + ``_`` + 지문 앞 6hex)."""
    if len(candidate) <= _MAX_OPERATION_ID_LENGTH:
        return candidate
    digest = fingerprint.split(":", 1)[-1][:6] or "000000"
    return f"{candidate[:57]}_{digest}"


def _normalize_operation_id(
    raw: Any,
    *,
    method: str,
    path: str,
    index: int,
    fingerprint: str,
    taken: set[str],
) -> str:
    """소스가 준 ``operationId``를 ASCII 식별자로 정규화한다(§4-2).

    Args:
        raw: 소스의 ``operationId``(없으면 None).
        method: 대문자 HTTP 메서드.
        path: 정규화된 경로.
        index: 정렬 후 0-기반 순번(최종 폴백에 쓰인다).
        fingerprint: 원본 문서 지문(길이 상한 초과 시 접미사로 쓰인다).
        taken: 이미 사용된 식별자 집합(충돌 시 ``_2``, ``_3``… 을 붙인다).

    Returns:
        ``^[A-Za-z_][A-Za-z0-9_]*$`` 를 만족하는 유일한 식별자.

    Raises:
        SourceSpecError: 어떤 규칙으로도 유효한 식별자를 만들 수 없을 때.
    """
    source_text = _text(raw) or ""
    candidate = _NON_IDENTIFIER_RE.sub("_", source_text)
    # 규칙 2 발동 조건은 "결과가 비었거나 숫자로 시작"이 아니라 "ASCII 영숫자가
    # 하나도 없거나 숫자로 시작"이다. 한글 전용 operationId 는 치환 후 '____'
    # 처럼 밑줄만 남아 '비어 있지 않다'고 판정되므로, 좁은 조건에서는 경로 기반
    # 폴백이 절대 발동하지 못하고 글자 수만 반영한 무의미한 도구명이 된다
    # (FastMCP 도구명이 빈 문자열이 되어 SEP-986 위반). 게다가 그 이름들은
    # 정렬 순서 기준 충돌 접미사(_2·_3)에 의존하므로 오퍼레이션이 하나만 늘어도
    # 기존 도구명이 전부 재배정된다 — MCP 클라이언트 allowlist·프롬프트가
    # 참조하는 안정 식별자가 조용히 깨진다.
    if not _ASCII_ALNUM_RE.search(candidate) or candidate[0] in "0123456789":
        slug = _path_slug(path)
        candidate = f"op_{method.lower()}_{slug}" if slug else ""
    if not _ASCII_ALNUM_RE.search(candidate):
        candidate = f"op_{index:02d}"
    candidate = _cap_length(candidate, fingerprint=fingerprint)

    unique = candidate
    suffix = 2
    while unique in taken:
        unique = _cap_length(f"{candidate}_{suffix}", fingerprint=fingerprint)
        suffix += 1
    if not _OPERATION_ID_RE.match(unique):
        raise SourceSpecError(
            f"오퍼레이션 식별자를 만들 수 없습니다(원본 operationId: '{source_text}', "
            f"경로: '{path}'). MCP 도구명은 ASCII 식별자여야 합니다(불변식 I1)."
        )
    taken.add(unique)
    return unique


# ---------------------------------------------------------------------------
# $ref 해석(§5-1)
# ---------------------------------------------------------------------------
def _pointer_tokens(ref: str) -> list[str]:
    """JSON 포인터 문자열을 토큰 목록으로 분해한다(``~1``·``~0`` 복원)."""
    body = ref[2:] if ref.startswith("#/") else ref.lstrip("#/")
    tokens: list[str] = []
    for token in body.split("/"):
        tokens.append(token.replace("~1", "/").replace("~0", "~"))
    return tokens


def _resolve_pointer(document: Mapping[str, Any], ref: str) -> Any:
    """문서 내부 JSON 포인터를 해석한다."""
    node: Any = document
    for token in _pointer_tokens(ref):
        if isinstance(node, Mapping) and token in node:
            node = node[token]
            continue
        raise SourceSpecError(
            f"문서 내부 참조 '{ref}'의 대상을 찾을 수 없습니다(막힌 토큰: '{token}'). "
            "components.schemas / definitions 에 해당 이름이 있는지 확인하세요."
        )
    return node


class _RefBudget:
    """``$ref`` 인라인 전개가 만들어 내는 노드 수를 세는 카운터.

    공유 정의를 여러 번 참조하는 문서는 인라인 전개 시 노드 수가 지수적으로
    늘어난다(``Dn`` 이 ``Dn-1`` 을 두 번 참조하면 2^n). 순환 참조가 아니어도
    깊이 20짜리 정상 문서 하나가 수십 MB 스키마와 수십 초 CPU 를 만들며, 그
    결과물은 그대로 ``components.schemas`` 에 실려 디스크에 쓰인다. 스펙 문서를
    원격에서 받아 온다는 점에서 이는 서비스 거부 경로이므로 상한으로 막는다.
    """

    __slots__ = ("remaining",)

    def __init__(self, limit: int) -> None:
        self.remaining = int(limit)

    def spend(self, ref: str) -> None:
        """노드 1개를 소비한다.

        Raises:
            SourceSpecError: 상한을 넘겼을 때(한국어 안내).
        """
        self.remaining -= 1
        if self.remaining < 0:
            raise SourceSpecError(
                f"$ref 인라인 전개가 상한({_MAX_REF_EXPANSIONS:,}회)을 넘었습니다"
                f"(마지막 참조: '{ref}'). 공유 정의를 여러 번 참조하는 문서는 "
                "인라인 전개 시 노드 수가 지수적으로 늘어납니다. 스키마를 단순화하거나 "
                "샘플링 추론(response_schema 없이 두기)을 사용하세요."
            )


def _resolve_refs(
    node: Any,
    document: Mapping[str, Any],
    *,
    stack: tuple[str, ...] = (),
    budget: "_RefBudget | None" = None,
) -> Any:
    """문서 내부 ``$ref``를 인라인으로 펼친다.

    외부 참조(다른 파일·HTTP URL)와 순환 참조는 :class:`SourceSpecError` 다.
    컴파일러가 컴포넌트 이름을 자기 규칙으로 다시 붙이므로, 해석 결과는 참조를
    남기지 않고 그 자리에 펼쳐 담는다.

    전개 노드 수는 :data:`_MAX_REF_EXPANSIONS` 로 제한한다. 순환이 아닌 정상 문서도
    공유 정의를 반복 참조하면 지수적으로 폭발하므로, 순환 검출만으로는 부족하다.
    """
    if budget is None:
        budget = _RefBudget(_MAX_REF_EXPANSIONS)
    if isinstance(node, Mapping):
        ref = node.get("$ref")
        if ref is not None:
            ref_text = _text(ref)
            if ref_text is None or not ref_text.startswith("#/"):
                raise SourceSpecError(
                    f"지원하지 않는 $ref입니다: '{ref}'. MCPortal은 문서 내부 참조"
                    "('#/components/schemas/...' · '#/definitions/...')만 해석합니다."
                )
            if ref_text in stack:
                chain = " → ".join((*stack, ref_text))
                raise SourceSpecError(
                    f"순환 $ref를 발견했습니다: {chain}. 순환 참조 스키마는 인라인으로 "
                    "펼칠 수 없으므로 지원하지 않습니다."
                )
            budget.spend(ref_text)
            target = _resolve_pointer(document, ref_text)
            resolved = _resolve_refs(
                target, document, stack=(*stack, ref_text), budget=budget
            )
            siblings = {
                key: _resolve_refs(value, document, stack=stack, budget=budget)
                for key, value in node.items()
                if key != "$ref"
            }
            if isinstance(resolved, Mapping):
                merged = dict(resolved)
                merged.update(siblings)
                return merged
            return resolved
        return {
            key: _resolve_refs(value, document, stack=stack, budget=budget)
            for key, value in node.items()
        }
    if isinstance(node, (list, tuple)):
        return [_resolve_refs(item, document, stack=stack, budget=budget) for item in node]
    return node


def _resolved_schema(raw: Any, document: Mapping[str, Any]) -> dict[str, Any] | None:
    """스키마 노드를 인라인 해석한다. 비었거나 매핑이 아니면 ``None``(추론 대상)."""
    if not isinstance(raw, Mapping) or not raw:
        return None
    resolved = _resolve_refs(raw, document)
    if not isinstance(resolved, Mapping) or not resolved:
        return None
    return dict(resolved)


# ---------------------------------------------------------------------------
# 공통 파라미터 보강
# ---------------------------------------------------------------------------
def _fill_from_template(param: ParamSpec, template: ParamSpec) -> ParamSpec:
    """공통 파라미터 템플릿으로 빈 메타(설명·예시·기본값·열거값)만 채운다.

    소스가 선언한 ``type``·``required``는 절대 덮어쓰지 않는다. 원 문서의 선언이
    항상 우선이며, 템플릿은 빈칸을 메우는 역할만 한다.
    """
    changes: dict[str, Any] = {}
    if param.description is None and template.description is not None:
        changes["description"] = template.description
    if param.example is None and template.example is not None:
        changes["example"] = template.example
    if param.default is None and template.default is not None:
        changes["default"] = template.default
    if not param.enum and template.enum:
        changes["enum"] = template.enum
    if param.type == "array" and param.item_type is None and template.item_type:
        changes["item_type"] = template.item_type
    return replace(param, **changes) if changes else param


def _merge_common_params(
    params: Sequence[ParamSpec],
    templates: Sequence[ParamSpec],
    *,
    key_param: str,
    backfill: bool,
) -> tuple[ParamSpec, ...]:
    """공통 질의 파라미터를 보강한다.

    Args:
        params: 소스에서 흡수한 파라미터들.
        templates: 공통 파라미터 표준 표기(:data:`ODCLOUD_COMMON_PARAMS` 등).
        key_param: 인증키 파라미터명. 같은 이름의 템플릿은 추가하지 않는다(I3).
        backfill: True면 소스에 없는 공통 파라미터를 추가한다. False면 이미 있는
            파라미터의 빈 메타만 채운다(사람이 쓴 기술서·소스 선언을 존중).

    Returns:
        I5 정렬이 적용된 파라미터 튜플.
    """
    by_name = {template.name.lower(): template for template in templates}
    merged: list[ParamSpec] = []
    present: set[str] = set()
    for param in params:
        lowered = param.name.lower()
        present.add(lowered)
        template = by_name.get(lowered)
        merged.append(_fill_from_template(param, template) if template else param)
    if backfill:
        for template in templates:
            lowered = template.name.lower()
            if lowered in present or lowered == key_param.lower():
                continue
            merged.append(template)
    return _sorted_params(merged)


# ---------------------------------------------------------------------------
# 불변식 검증(§4-1)
# ---------------------------------------------------------------------------
def _validate_source_spec(spec: SourceSpec) -> SourceSpec:
    """생성 시점에 IR 불변식 I1~I6을 강제한다."""
    if not spec.operations:
        raise SourceSpecError(
            f"'{spec.service_name}'(서비스 ID {spec.service_id})에서 오퍼레이션을 "
            "하나도 만들지 못했습니다. GET·POST 오퍼레이션이 있는지 확인하세요."
        )
    if spec.base_url != spec.base_url.rstrip("/") or "://" not in spec.base_url:
        raise SourceSpecError(
            f"base_url '{spec.base_url}'이 불변식 I6(스킴 포함·끝 '/' 없음)을 "
            "만족하지 않습니다."
        )
    seen: set[str] = set()
    key_lower = spec.key_param.lower()
    for operation in spec.operations:
        if not _OPERATION_ID_RE.match(operation.operation_id):
            raise SourceSpecError(
                f"operation_id '{operation.operation_id}'가 ASCII 식별자 규칙"
                "(^[A-Za-z_][A-Za-z0-9_]*$)을 만족하지 않습니다(불변식 I1)."
            )
        if operation.operation_id in seen:
            raise SourceSpecError(
                f"operation_id '{operation.operation_id}'가 소스 안에서 중복됩니다"
                "(불변식 I1)."
            )
        seen.add(operation.operation_id)
        if not operation.path.startswith("/") or spec.base_url in operation.path:
            raise SourceSpecError(
                f"path '{operation.path}'가 불변식 I2('/' 시작 · base_url 미포함)를 "
                "만족하지 않습니다."
            )
        for param in operation.parameters:
            if param.name.lower() == key_lower:
                raise SourceSpecError(
                    f"오퍼레이션 '{operation.operation_id}'의 파라미터에 인증키"
                    f"('{param.name}')가 남아 있습니다. 인증키는 트랜스포트가 주입하며 "
                    "MCP 도구 인자로 노출되면 안 됩니다(불변식 I3)."
                )
        expected_params = _sorted_params(operation.parameters)
        if tuple(operation.parameters) != expected_params:
            raise SourceSpecError(
                f"오퍼레이션 '{operation.operation_id}'의 parameters 정렬이 불변식 "
                "I5(필수 먼저·이름 오름차순)를 만족하지 않습니다."
            )
    order = [(operation.path, operation.method) for operation in spec.operations]
    if order != sorted(order):
        raise SourceSpecError(
            "operations가 (path, method) 오름차순으로 정렬되지 않았습니다(불변식 I4)."
        )
    return spec


# ---------------------------------------------------------------------------
# 판별
# ---------------------------------------------------------------------------
def _swagger_hosts(document: Mapping[str, Any]) -> tuple[str, ...]:
    """문서에서 호스트 후보를 모은다(2.0 ``host`` · 3.x ``servers[].url``)."""
    hosts: list[str] = []
    host = _text(document.get("host"))
    if host:
        hosts.append(host.lower())
    servers = document.get("servers")
    if isinstance(servers, Sequence) and not isinstance(servers, (str, bytes)):
        for server in servers:
            if isinstance(server, Mapping):
                url = _text(server.get("url"))
                if url:
                    hosts.append((urlsplit(url).netloc or url).lower())
    return tuple(hosts)


def _is_odcloud(document: Mapping[str, Any]) -> bool:
    """호스트 표기로 odcloud 계열인지 판정한다."""
    return any(ODCLOUD_HOST_MARKER in host for host in _swagger_hosts(document))


def detect_source_kind(document: Mapping[str, Any]) -> SourceKind:
    """문서 형태로 소스 종류를 판별한다.

    - ``mcportal_rest_doc`` 키 존재 → :attr:`SourceKind.REST_DOC_MANUAL`
    - ``swagger == "2.0"`` 또는 ``openapi`` 존재 → 호스트가 odcloud 계열이면
      :attr:`SourceKind.ODCLOUD_SWAGGER`, 아니면 :attr:`SourceKind.GW_SWAGGER`

    Args:
        document: 스펙 문서(JSON 객체).

    Returns:
        판별된 :class:`SourceKind`.

    Raises:
        SourceSpecError: 세 갈래 어디에도 해당하지 않을 때(어떤 키가 없어서
            판별에 실패했는지 메시지에 적는다).
    """
    doc = _require_mapping(document, what="스펙 문서")
    if "mcportal_rest_doc" in doc:
        return SourceKind.REST_DOC_MANUAL
    swagger = _text(doc.get("swagger"))
    openapi = _text(doc.get("openapi"))
    if swagger is not None or openapi is not None:
        _swagger_version(doc)  # 버전 표기 자체를 여기서 검증한다.
        return SourceKind.ODCLOUD_SWAGGER if _is_odcloud(doc) else SourceKind.GW_SWAGGER
    keys = ", ".join(sorted(str(key) for key in doc.keys())[:12]) or "(없음)"
    raise SourceSpecError(
        "스펙 소스 종류를 판별할 수 없습니다. 'swagger'(2.0) · 'openapi'(3.x) · "
        f"'mcportal_rest_doc' 중 하나가 필요합니다. 문서의 최상위 키: {keys}"
    )


def _swagger_version(document: Mapping[str, Any]) -> str:
    """Swagger/OpenAPI 버전 계열을 ``"2.0"`` 또는 ``"3.x"`` 로 판별한다."""
    swagger = _text(document.get("swagger"))
    openapi = _text(document.get("openapi"))
    if swagger is not None:
        if swagger != "2.0":
            raise SourceSpecError(
                f"지원하지 않는 swagger 버전입니다: '{swagger}'. MCPortal은 swagger "
                "2.0과 OpenAPI 3.x만 흡수합니다."
            )
        return "2.0"
    if openapi is not None:
        if not openapi.startswith("3."):
            raise SourceSpecError(
                f"지원하지 않는 openapi 버전입니다: '{openapi}'. MCPortal은 swagger "
                "2.0과 OpenAPI 3.x만 흡수합니다."
            )
        return "3.x"
    raise SourceSpecError(
        "스펙 문서에 'swagger'(2.0)도 'openapi'(3.x)도 없어 버전을 판별할 수 "
        "없습니다."
    )


# ---------------------------------------------------------------------------
# Swagger 흡수
# ---------------------------------------------------------------------------
def _swagger_base_url(document: Mapping[str, Any], version: str) -> str:
    """2.0은 ``schemes+host+basePath``, 3.x는 ``servers[0].url``로 base_url을 만든다."""
    if version == "2.0":
        host = _text(document.get("host"))
        if host is None:
            raise SourceSpecError(
                "swagger 2.0 문서에 'host' 필드가 없어 base_url을 복원할 수 없습니다. "
                "'schemes[0] + host + basePath' 조합이 필요합니다."
            )
        scheme = _first_text(document.get("schemes")) or "https"
        base_path = _text(document.get("basePath")) or ""
        return _normalize_base_url(
            f"{scheme}://{host}{base_path}", where="swagger 2.0 문서"
        )
    servers = document.get("servers")
    if not isinstance(servers, Sequence) or isinstance(servers, (str, bytes)) or not servers:
        raise SourceSpecError(
            "OpenAPI 3.x 문서에 'servers' 배열이 없어 base_url을 복원할 수 "
            "없습니다. servers[0].url 이 필요합니다."
        )
    first = servers[0]
    if not isinstance(first, Mapping) or _text(first.get("url")) is None:
        raise SourceSpecError(
            "OpenAPI 3.x 문서의 'servers[0].url'이 비어 있어 base_url을 복원할 수 "
            "없습니다."
        )
    return _normalize_base_url(first.get("url"), where="OpenAPI 3.x 문서")


def _param_from_swagger(
    raw: Any,
    *,
    version: str,
    key_param: str,
    document: Mapping[str, Any],
    where: str,
) -> tuple[ParamSpec | None, dict[str, Any] | None]:
    """파라미터 객체 1개를 :class:`ParamSpec` 으로 흡수한다.

    Returns:
        ``(파라미터 또는 None, 본문 스키마 또는 None)``. 인증키·미지원 위치는
        ``(None, None)`` 으로 조용히 버려지고, swagger 2.0 의 ``in: body`` 는
        ``(None, 본문 스키마)`` 로 돌아온다.
    """
    if not isinstance(raw, Mapping):
        raise SourceSpecError(
            f"{where}: parameters 항목이 매핑이 아닙니다(받은 타입: "
            f"{type(raw).__name__})."
        )
    param = raw
    if "$ref" in param:
        resolved = _resolve_refs(param, document)
        param = _require_mapping(resolved, what=f"{where}의 $ref 해석 결과")

    location = (_text(param.get("in")) or "").lower()
    if location == "body":
        return None, _resolved_schema(param.get("schema"), document)
    name = _text(param.get("name"))
    if name is None:
        raise SourceSpecError(
            f"{where}: parameters 항목에 'name' 필드가 없어 파라미터를 만들 수 "
            "없습니다."
        )
    if name.lower() == key_param.lower():
        # I3 — 인증키는 트랜스포트가 주입한다. 도구 인자로 노출하지 않는다.
        return None, None
    if location not in SUPPORTED_LOCATIONS:
        # formData(2.0)·cookie(3.x) 등 W2 미지원 위치는 조용히 버린다(§5-1).
        return None, None

    if version == "2.0":
        type_source: Mapping[str, Any] = param
        items = param.get("items")
    else:
        schema = param.get("schema")
        type_source = schema if isinstance(schema, Mapping) else {}
        items = type_source.get("items")

    param_type = _param_type(type_source.get("type"))
    item_type: str | None = None
    if param_type == "array" and isinstance(items, Mapping):
        item_type = _param_type(items.get("type"))

    example = _scalar_text(param.get("example"))
    if example is None:
        example = _scalar_text(param.get("x-example"))
    if example is None:
        example = _scalar_text(type_source.get("example"))

    required = bool(param.get("required", False)) or location == "path"

    return (
        ParamSpec(
            name=name,
            location=location,
            required=required,
            type=param_type,
            description=_text(param.get("description")),
            example=example,
            enum=_enum_tuple(type_source.get("enum")) or _enum_tuple(param.get("enum")),
            default=_scalar_text(type_source.get("default"))
            or _scalar_text(param.get("default")),
            item_type=item_type,
        ),
        None,
    )


def _swagger_parameters(
    operation: Mapping[str, Any],
    shared: Any,
    *,
    version: str,
    key_param: str,
    document: Mapping[str, Any],
    where: str,
) -> tuple[tuple[ParamSpec, ...], dict[str, Any] | None]:
    """경로 공통 파라미터와 오퍼레이션 파라미터를 합쳐 흡수한다."""
    collected: dict[tuple[str, str], ParamSpec] = {}
    body_schema: dict[str, Any] | None = None
    for group in (shared, operation.get("parameters")):
        if not isinstance(group, Sequence) or isinstance(group, (str, bytes)):
            continue
        for raw in group:
            param, body = _param_from_swagger(
                raw,
                version=version,
                key_param=key_param,
                document=document,
                where=where,
            )
            if body is not None and body_schema is None:
                body_schema = body
            if param is not None:
                collected[(param.name, param.location)] = param
    return _sorted_params(tuple(collected.values())), body_schema


def _swagger_response(
    operation: Mapping[str, Any],
    *,
    version: str,
    document: Mapping[str, Any],
    default_media_type: str | None,
) -> tuple[str, dict[str, Any] | None]:
    """정상 응답의 미디어타입과 스키마를 흡수한다(스키마 없으면 ``None``).

    출처는 **200 응답으로 한정한다**(§5-1 표). ``responses.default`` 로 폴백하지
    않는 이유: 정부 swagger 는 ``default`` 에 오류 봉투(``errorCode``/``errorMsg``)를
    적어 두는 경우가 있어, 폴백을 두면 그 오류 스키마가 MCP 도구의 200 응답
    설명이 되어 버린다. 200 이 없으면 ``None`` 을 돌려주어 샘플링 추론(§7-3)이
    자리를 채우게 하는 편이 설계 의도와 맞는다.
    """
    responses = operation.get("responses")
    entry: Any = None
    if isinstance(responses, Mapping):
        for key in ("200", 200):
            candidate = responses.get(key)
            if isinstance(candidate, Mapping):
                entry = candidate
                break

    if version == "2.0":
        media = (
            _first_text(operation.get("produces"))
            or default_media_type
            or _JSON_MEDIA_TYPE
        )
        schema = _resolved_schema(
            entry.get("schema") if isinstance(entry, Mapping) else None, document
        )
        return media, schema

    content = entry.get("content") if isinstance(entry, Mapping) else None
    if isinstance(content, Mapping) and content:
        media = sorted(str(key) for key in content.keys())[0]
        node = content.get(media)
        schema = _resolved_schema(
            node.get("schema") if isinstance(node, Mapping) else None, document
        )
        return media, schema
    return default_media_type or _JSON_MEDIA_TYPE, None


def _swagger_request_body(
    operation: Mapping[str, Any],
    *,
    document: Mapping[str, Any],
) -> dict[str, Any] | None:
    """OpenAPI 3.x ``requestBody``의 본문 스키마를 흡수한다."""
    body = operation.get("requestBody")
    if not isinstance(body, Mapping):
        return None
    if "$ref" in body:
        resolved = _resolve_refs(body, document)
        body = resolved if isinstance(resolved, Mapping) else {}
    content = body.get("content")
    if not isinstance(content, Mapping) or not content:
        return None
    media = sorted(str(key) for key in content.keys())[0]
    node = content.get(media)
    return _resolved_schema(
        node.get("schema") if isinstance(node, Mapping) else None, document
    )


def _tags_tuple(raw: Any) -> tuple[str, ...]:
    """태그 목록을 문자열 튜플로 만든다(원 순서 보존, 중복 제거)."""
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)):
        return ()
    tags: list[str] = []
    for item in raw:
        text = _text(item)
        if text is not None and text not in tags:
            tags.append(text)
    return tuple(tags)


def _swagger_operations(
    document: Mapping[str, Any],
    *,
    version: str,
    key_param: str,
    fingerprint: str,
) -> tuple[OperationSpec, ...]:
    """``paths``를 순회해 오퍼레이션들을 흡수한다(I4 정렬 후 식별자 부여)."""
    paths = document.get("paths")
    if not isinstance(paths, Mapping) or not paths:
        raise SourceSpecError(
            "스펙 문서의 'paths'가 없거나 비어 있어 오퍼레이션을 만들 수 없습니다. "
            "Swagger/OpenAPI 문서에는 경로가 최소 1개 필요합니다."
        )
    default_media_type = _first_text(document.get("produces"))

    collected: list[tuple[str, str, Mapping[str, Any], Any]] = []
    for path_key, path_item in sorted(
        ((str(key), value) for key, value in paths.items()), key=lambda kv: kv[0]
    ):
        item = _require_mapping(path_item, what=f"경로 '{path_key}'의 항목")
        lowered = _lower_keys(item)
        shared = lowered.get("parameters")
        path = _normalize_path(path_key, where=f"경로 '{path_key}'")
        for method in SUPPORTED_METHODS:
            operation = lowered.get(method)
            if isinstance(operation, Mapping):
                collected.append((path, method.upper(), operation, shared))
    if not collected:
        raise SourceSpecError(
            "스펙 문서의 'paths'에서 GET·POST 오퍼레이션을 찾지 못했습니다. "
            f"MCPortal W2가 흡수하는 메서드: {', '.join(SUPPORTED_METHODS)}."
        )
    collected.sort(key=lambda entry: (entry[0], entry[1]))

    taken: set[str] = set()
    operations: list[OperationSpec] = []
    for index, (path, method, operation, shared) in enumerate(collected):
        where = f"오퍼레이션 '{method} {path}'"
        operation_id = _normalize_operation_id(
            operation.get("operationId"),
            method=method,
            path=path,
            index=index,
            fingerprint=fingerprint,
            taken=taken,
        )
        parameters, body_from_params = _swagger_parameters(
            operation,
            shared,
            version=version,
            key_param=key_param,
            document=document,
            where=where,
        )
        media_type, response_schema = _swagger_response(
            operation,
            version=version,
            document=document,
            default_media_type=default_media_type,
        )
        request_body = (
            body_from_params
            if version == "2.0"
            else _swagger_request_body(operation, document=document)
        )
        operations.append(
            OperationSpec(
                operation_id=operation_id,
                method=method,
                path=path,
                summary=_text(operation.get("summary")),
                description=_text(operation.get("description")),
                parameters=parameters,
                response_media_type=media_type,
                response_schema=response_schema,
                request_body_schema=request_body,
                tags=_tags_tuple(operation.get("tags")),
                deprecated=bool(operation.get("deprecated", False)),
            )
        )
    return tuple(operations)


def _load_swagger(
    document: Mapping[str, Any],
    *,
    kind: SourceKind,
    service_id: str,
    service_name: str | None,
    source_url: str | None,
    fetched_at: str | None,
    key_param: str,
) -> SourceSpec:
    """Swagger 2.0 / OpenAPI 3.x 문서를 :class:`SourceSpec` 으로 흡수한다."""
    doc = _require_mapping(document, what="스펙 문서")
    version = _swagger_version(doc)
    fingerprint = fingerprint_document(doc)
    info = doc.get("info")
    info_map = info if isinstance(info, Mapping) else {}

    title = _text(service_name) or _text(info_map.get("title"))
    if title is None:
        raise SourceSpecError(
            "서비스명을 정할 수 없습니다. service_name 인자를 넘기거나 문서의 "
            "'info.title'을 채우세요."
        )
    base_url = _swagger_base_url(doc, version)
    operations = _swagger_operations(
        doc, version=version, key_param=key_param, fingerprint=fingerprint
    )

    if kind is SourceKind.ODCLOUD_SWAGGER:
        templates: tuple[ParamSpec, ...] = ODCLOUD_COMMON_PARAMS
        backfill = True
    else:
        templates = STANDARD_COMMON_PARAMS
        backfill = False
    operations = tuple(
        replace(
            operation,
            parameters=_merge_common_params(
                operation.parameters,
                templates,
                key_param=key_param,
                backfill=backfill,
            ),
        )
        for operation in operations
    )

    license_map = info_map.get("license")
    license_note = (
        _text(license_map.get("name")) if isinstance(license_map, Mapping) else None
    )

    return _validate_source_spec(
        SourceSpec(
            provider=DEFAULT_PROVIDER,
            service_id=str(service_id),
            service_name=title,
            base_url=base_url,
            source_kind=kind,
            operations=operations,
            key_param=key_param,
            source_url=_text(source_url),
            fingerprint=fingerprint,
            fetched_at=_text(fetched_at),
            description=_text(info_map.get("description")),
            license_note=license_note,
        )
    )


def load_odcloud_swagger(
    document: Mapping[str, Any],
    *,
    service_id: str,
    service_name: str | None = None,
    source_url: str | None = None,
    fetched_at: str | None = None,
    key_param: str = "serviceKey",
) -> SourceSpec:
    """odcloud 계열 OpenAPI 문서(``infuser.odcloud.kr``)를 흡수한다.

    odcloud 규약 처리
    -----------------
    * 공통 질의 파라미터 ``page`` · ``perPage`` · ``returnType`` 을
      :data:`ODCLOUD_COMMON_PARAMS` 표준 표기로 맞춘다. 원 문서에 있으면 빈
      메타(설명·예시·열거값)만 채우고, 없으면 선택 파라미터로 **보강**한다.
      odcloud 게이트웨이는 이 셋을 전 오퍼레이션에서 받으므로, 문서에 빠져 있어도
      MCP 도구가 페이지·응답형식을 다룰 수 있어야 한다.
    * 인증키(``serviceKey``, 대소문자 무시)는 파라미터에서 제거하고 이름만
      :attr:`SourceSpec.key_param` 에 남긴다(불변식 I3).
    * 응답 스키마가 없는 오퍼레이션은 ``response_schema=None`` 으로 남겨
      "추론기가 채울 자리"임을 명시한다.

    Args:
        document: odcloud OpenAPI 문서(JSON 객체).
        service_id: 포털 데이터셋 ID.
        service_name: 서비스명. None이면 ``info.title``.
        source_url: 스펙 원본 URL(없으면 None).
        fetched_at: 취득 시각(ISO8601 KST). OpenAPI 산출물에는 실리지 않는다.
        key_param: 인증키 파라미터명.

    Returns:
        정규화된 :class:`SourceSpec`.

    Raises:
        SourceSpecError: 버전 표기·``servers[0].url``·``paths``·``info.title`` 등
            필수 필드가 없거나, ``$ref`` 해석이 불가능할 때. 어떤 필드가 왜
            부족한지 메시지에 적는다.
    """
    return _load_swagger(
        document,
        kind=SourceKind.ODCLOUD_SWAGGER,
        service_id=service_id,
        service_name=service_name,
        source_url=source_url,
        fetched_at=fetched_at,
        key_param=key_param,
    )


def load_gw_swagger(
    document: Mapping[str, Any],
    *,
    service_id: str,
    service_name: str | None = None,
    source_url: str | None = None,
    fetched_at: str | None = None,
    key_param: str = "serviceKey",
) -> SourceSpec:
    """data.go.kr 게이트웨이 Swagger(2.0 / OpenAPI 3.x)를 흡수한다.

    2.0은 ``schemes[0] + host + basePath`` 로 base_url을 복원하고 파라미터 타입을
    ``parameters[].type`` 에서, 3.x는 ``servers[0].url`` 과
    ``parameters[].schema.type`` 에서 읽는다(§5-1). 표준 REST 공통 파라미터
    (``pageNo`` · ``numOfRows`` · ``type``)는 **이미 선언된 것의 빈 메타만** 채운다
    — 게이트웨이 문서가 선언하지 않은 파라미터를 임의로 추가하면 실제로 받지
    않는 인자를 MCP 도구에 노출할 위험이 있기 때문이다.

    Args:
        document: Swagger 2.0 또는 OpenAPI 3.x 문서.
        service_id: 포털 데이터셋 ID.
        service_name: 서비스명. None이면 ``info.title``.
        source_url: 스펙 원본 URL(없으면 None).
        fetched_at: 취득 시각(ISO8601 KST).
        key_param: 인증키 파라미터명.

    Returns:
        정규화된 :class:`SourceSpec`.

    Raises:
        SourceSpecError: 필수 필드 결손·미지원 버전·``$ref`` 해석 실패 시.
    """
    return _load_swagger(
        document,
        kind=SourceKind.GW_SWAGGER,
        service_id=service_id,
        service_name=service_name,
        source_url=source_url,
        fetched_at=fetched_at,
        key_param=key_param,
    )


# ---------------------------------------------------------------------------
# 수동 매핑 기술서(REST_DOC_MANUAL)
# ---------------------------------------------------------------------------
def _param_from_descriptor(raw: Any, *, key_param: str, where: str) -> ParamSpec | None:
    """기술서의 파라미터 항목 1개를 :class:`ParamSpec` 으로 옮긴다."""
    param = _require_mapping(raw, what=f"{where}의 parameters 항목")
    name = _text(param.get("name"))
    if name is None:
        raise SourceSpecError(
            f"{where}: parameters 항목에 'name' 필드가 없습니다. 기술서의 파라미터는 "
            "name·location·required·type을 갖춰야 합니다."
        )
    if name.lower() == key_param.lower():
        return None  # I3 — 인증키 제거.
    location = (_text(param.get("location")) or "query").lower()
    if location not in SUPPORTED_LOCATIONS:
        raise SourceSpecError(
            f"{where}: 파라미터 '{name}'의 location '{location}'은 지원하지 않습니다. "
            f"허용 값: {', '.join(SUPPORTED_LOCATIONS)}."
        )
    param_type = _param_type(param.get("type"))
    item_type = _param_type(param.get("item_type")) if param.get("item_type") else None
    return ParamSpec(
        name=name,
        location=location,
        required=bool(param.get("required", False)) or location == "path",
        type=param_type,
        description=_text(param.get("description")),
        example=_scalar_text(param.get("example")),
        enum=_enum_tuple(param.get("enum")),
        default=_scalar_text(param.get("default")),
        item_type=item_type,
    )


def load_rest_doc(
    descriptor: Mapping[str, Any],
    *,
    fetched_at: str | None = None,
) -> SourceSpec:
    """활용가이드 문서를 사람이 옮겨 적은 '수동 매핑 기술서'(JSON)를 읽는다.

    Swagger가 없는 표준 REST 문서형 소스(법제처 국가법령정보 등)를 위한 경로다.
    기술서 스키마는 설계 §5-2를 따른다. 기술서 자체가 리포에 커밋되므로 결과는
    결정론적이다.

    표준 공통 파라미터(``pageNo`` · ``numOfRows`` · ``type``)는 **기술서에 이미
    적힌 것의 빈 메타만** 채운다. 사람이 문서를 보고 적은 선언이 정본이므로
    없는 파라미터를 임의로 추가하지 않는다. XML·JSON 이중 응답은
    ``response_media_type`` 과 ``type`` 파라미터 열거값으로 표기된다.

    Args:
        descriptor: 기술서 JSON 객체.
        fetched_at: 취득·작성 시각(ISO8601 KST). OpenAPI 산출물에는 실리지 않는다.

    Returns:
        정규화된 :class:`SourceSpec`.

    Raises:
        SourceSpecError: ``mcportal_rest_doc`` 버전 미지원, 필수 필드
            (``service_id`` · ``service_name`` · ``base_url`` · ``operations``) 결손,
            오퍼레이션의 ``path`` 결손 등.
    """
    doc = _require_mapping(descriptor, what="수동 매핑 기술서")
    version = doc.get("mcportal_rest_doc")
    if version is None:
        raise SourceSpecError(
            "수동 매핑 기술서에 'mcportal_rest_doc' 버전 필드가 없습니다. "
            "표준 REST 문서형 기술서는 {\"mcportal_rest_doc\": 1, ...} 형태여야 합니다."
        )
    if version != 1:
        raise SourceSpecError(
            f"지원하지 않는 기술서 버전입니다: {version!r}. 현재 지원 버전은 1입니다."
        )

    fingerprint = fingerprint_document(doc)
    service_id = _text(doc.get("service_id"))
    service_name = _text(doc.get("service_name"))
    missing = [
        field
        for field, value in (("service_id", service_id), ("service_name", service_name))
        if value is None
    ]
    if missing:
        raise SourceSpecError(
            f"수동 매핑 기술서에 필수 필드가 없습니다: {', '.join(missing)}. "
            "포털 데이터셋 ID와 서비스명은 스펙 산출물의 식별 정보이므로 생략할 수 "
            "없습니다."
        )
    key_param = _text(doc.get("key_param")) or "serviceKey"
    base_url = _normalize_base_url(doc.get("base_url"), where="수동 매핑 기술서")

    raw_operations = doc.get("operations")
    if (
        not isinstance(raw_operations, Sequence)
        or isinstance(raw_operations, (str, bytes))
        or not raw_operations
    ):
        raise SourceSpecError(
            "수동 매핑 기술서의 'operations'가 비어 있습니다. 오퍼레이션이 최소 "
            "1개 필요합니다(각 항목은 method·path를 갖춰야 합니다)."
        )

    collected: list[tuple[str, str, Mapping[str, Any]]] = []
    for index, raw in enumerate(raw_operations):
        operation = _require_mapping(raw, what=f"기술서 operations[{index}]")
        method = (_text(operation.get("method")) or "GET").upper()
        if method.lower() not in SUPPORTED_METHODS:
            raise SourceSpecError(
                f"기술서 operations[{index}]의 method '{method}'는 지원하지 "
                f"않습니다. 허용 값: {', '.join(m.upper() for m in SUPPORTED_METHODS)}."
            )
        path = _normalize_path(
            operation.get("path"), where=f"기술서 operations[{index}]"
        )
        collected.append((path, method, operation))
    collected.sort(key=lambda entry: (entry[0], entry[1]))

    taken: set[str] = set()
    operations: list[OperationSpec] = []
    for index, (path, method, operation) in enumerate(collected):
        where = f"기술서 오퍼레이션 '{method} {path}'"
        operation_id = _normalize_operation_id(
            operation.get("operation_id"),
            method=method,
            path=path,
            index=index,
            fingerprint=fingerprint,
            taken=taken,
        )
        raw_params = operation.get("parameters")
        params: list[ParamSpec] = []
        if isinstance(raw_params, Sequence) and not isinstance(raw_params, (str, bytes)):
            for raw_param in raw_params:
                param = _param_from_descriptor(
                    raw_param, key_param=key_param, where=where
                )
                if param is not None:
                    params.append(param)
        response_schema = operation.get("response_schema")
        request_body = operation.get("request_body_schema")
        operations.append(
            OperationSpec(
                operation_id=operation_id,
                method=method,
                path=path,
                summary=_text(operation.get("summary")),
                description=_text(operation.get("description")),
                parameters=_merge_common_params(
                    params,
                    STANDARD_COMMON_PARAMS,
                    key_param=key_param,
                    backfill=False,
                ),
                response_media_type=(
                    _text(operation.get("response_media_type")) or _JSON_MEDIA_TYPE
                ),
                response_schema=(
                    dict(response_schema)
                    if isinstance(response_schema, Mapping) and response_schema
                    else None
                ),
                request_body_schema=(
                    dict(request_body)
                    if isinstance(request_body, Mapping) and request_body
                    else None
                ),
                tags=_tags_tuple(operation.get("tags")),
                deprecated=bool(operation.get("deprecated", False)),
            )
        )

    return _validate_source_spec(
        SourceSpec(
            provider=_text(doc.get("provider")) or DEFAULT_PROVIDER,
            service_id=str(service_id),
            service_name=str(service_name),
            base_url=base_url,
            source_kind=SourceKind.REST_DOC_MANUAL,
            operations=tuple(operations),
            key_param=key_param,
            source_url=_text(doc.get("source_url")),
            fingerprint=fingerprint,
            fetched_at=_text(fetched_at),
            description=_text(doc.get("description")),
            license_note=_text(doc.get("license_note")),
        )
    )


# ---------------------------------------------------------------------------
# 목록조회서비스 메타
# ---------------------------------------------------------------------------
def _pick(row: Mapping[str, Any], field: str) -> str | None:
    """별칭표를 훑어 행에서 필드 값을 뽑는다(정규화된 키로 비교)."""
    normalized = {_normalize_key(key): value for key, value in row.items()}
    for alias in _CATALOG_ALIASES[field]:
        value = normalized.get(alias)
        text = _text(value)
        if text is not None:
            return text
    return None


def load_catalog_rows(
    rows: Sequence[Mapping[str, Any]],
) -> tuple[CatalogEntry, ...]:
    """목록조회서비스 응답 행들을 :class:`CatalogEntry` 로 정규화한다.

    포털 응답의 키 표기는 camelCase·snake_case가 뒤섞여 오므로, 키를 소문자 +
    영숫자만 남긴 형태로 정규화한 뒤 별칭표(:data:`_CATALOG_ALIASES`)로 맞춘다.
    ``api_type`` 이 ``"LINK"`` 인 행도 그대로 담는다(필터링은
    :func:`catalog_entries_to_sources` 담당).

    Args:
        rows: 목록조회 응답의 행 시퀀스(보통 ``data`` 배열).

    Returns:
        입력 순서를 보존한 :class:`CatalogEntry` 튜플.

    Raises:
        SourceSpecError: 행이 매핑이 아니거나 서비스 ID·서비스명을 찾을 수 없을 때
            (몇 번째 행에서 어떤 키를 찾았는지 메시지에 적는다).
    """
    if isinstance(rows, Mapping) or isinstance(rows, (str, bytes)):
        raise SourceSpecError(
            "목록조회 메타 입력은 행(매핑)들의 시퀀스여야 합니다. 응답 본문을 "
            "넘겼다면 'data' 배열을 꺼내 넘기세요."
        )
    entries: list[CatalogEntry] = []
    for index, raw in enumerate(rows):
        row = _require_mapping(raw, what=f"목록조회 메타 행 {index}")
        service_id = _pick(row, "service_id")
        title = _pick(row, "title")
        for field, label, value in (
            ("service_id", "서비스 ID", service_id),
            ("title", "서비스명", title),
        ):
            if value is None:
                hints = ", ".join(_CATALOG_ALIAS_HINTS[field])
                present = ", ".join(sorted(str(key) for key in row.keys())) or "(없음)"
                raise SourceSpecError(
                    f"목록조회 메타 행 {index}에서 {label}({field})를 찾을 수 "
                    f"없습니다. 다음 중 하나의 키가 필요합니다: {hints} "
                    "(대소문자·밑줄 차이는 무시하므로 list_id 같은 표기도 인식합니다). "
                    f"행에 실제로 있는 키: {present}"
                )
        entries.append(
            CatalogEntry(
                service_id=str(service_id),
                title=str(title),
                end_point_url=_pick(row, "end_point_url"),
                operation_name=_pick(row, "operation_name"),
                operation_url=_pick(row, "operation_url"),
                api_type=_pick(row, "api_type"),
                data_format=_pick(row, "data_format"),
                swagger_json_url=_pick(row, "swagger_json_url"),
                guide_url=_pick(row, "guide_url"),
                org_name=_pick(row, "org_name"),
            )
        )
    return tuple(entries)


def _response_formats(data_format: str | None) -> tuple[str, ...]:
    """포털 dataFormat 표기에서 응답 형식 토큰(json·xml)을 뽑는다."""
    if data_format is None:
        return ()
    lowered = data_format.lower()
    formats: list[str] = []
    if "json" in lowered:
        formats.append("json")
    if "xml" in lowered:
        formats.append("xml")
    return tuple(formats)


def _catalog_path(entry: CatalogEntry, base_url: str) -> str:
    """``end_point_url`` 과 ``operation_url`` 로 오퍼레이션 경로를 복원한다."""
    operation_url = entry.operation_url
    if operation_url is None:
        name = entry.operation_name
        slug = _path_slug(name) if name else ""
        return f"/{slug}" if slug else "/"

    base_path = urlsplit(base_url).path.rstrip("/")
    if "://" in operation_url:
        operation_path = urlsplit(operation_url).path
        if base_path and operation_path.startswith(base_path):
            remainder = operation_path[len(base_path):]
        else:
            # 호스트·경로가 base_url 과 어긋나는 표기는 마지막 세그먼트만 취한다.
            remainder = "/" + operation_path.rstrip("/").rsplit("/", 1)[-1]
    else:
        remainder = operation_url
        if base_path and remainder.startswith(base_path):
            remainder = remainder[len(base_path):]
    remainder = remainder.strip()
    if not remainder.startswith("/"):
        remainder = "/" + remainder
    remainder = re.sub(r"/{2,}", "/", remainder)
    if len(remainder) > 1:
        remainder = remainder.rstrip("/")
    return remainder or "/"


def catalog_entry_to_source(
    entry: CatalogEntry,
    *,
    key_param: str = "serviceKey",
) -> SourceSpec:
    """카탈로그 행 1건을 '스키마 없는 골격 :class:`SourceSpec`'으로 승격한다.

    ``end_point_url`` + ``operation_url`` 로 base_url·path를 복원하고 오퍼레이션
    1개짜리 :class:`SourceSpec` 을 만든다. 목록조회 메타는 파라미터·응답 스키마를
    주지 않으므로 다음을 표준 표기로 채운다.

    * 표준 REST 공통 파라미터 ``pageNo`` · ``numOfRows`` (선택).
    * ``dataFormat`` 이 XML·JSON 이중 제공을 표기하면 ``type`` 파라미터
      (열거값 ``xml`` · ``json``)를 더하고, 그 사실을 오퍼레이션 설명에 적는다.
    * ``response_schema`` 는 **항상 ``None``** — 라이브 샘플링 추론이 채울 자리다.

    Args:
        entry: 정규화된 카탈로그 행.
        key_param: 인증키 파라미터명(도구 인자에서 제외된다).

    Returns:
        오퍼레이션 1개짜리 :class:`SourceSpec`.

    Raises:
        SourceSpecError: ``api_type`` 이 ``"LINK"`` 이거나(불변식 I7)
            ``end_point_url`` 이 없거나 스킴이 없을 때.
    """
    api_type = entry.api_type
    if api_type is not None and api_type.strip().upper() == "LINK":
        raise SourceSpecError(
            f"서비스 ID {entry.service_id}('{entry.title}')는 api_type이 'LINK'입니다. "
            "LINK형은 게이트웨이 API가 아니라 외부 페이지 연계이므로 스펙으로 "
            "승격하지 않습니다(불변식 I7)."
        )
    if entry.end_point_url is None:
        raise SourceSpecError(
            f"서비스 ID {entry.service_id}('{entry.title}')에 엔드포인트"
            "(endPoint/end_point_url)가 없어 base_url을 복원할 수 없습니다. "
            "포털 메타에서 이 값이 비어 있는 행은 호출 대상을 특정할 수 없습니다."
        )
    base_url = _normalize_base_url(
        entry.end_point_url,
        where=f"목록조회 메타 행(서비스 ID {entry.service_id})",
    )
    path = _catalog_path(entry, base_url)

    formats = _response_formats(entry.data_format)
    dual = len(formats) > 1
    if "json" in formats or not formats:
        media_type = _JSON_MEDIA_TYPE
    else:
        media_type = _XML_MEDIA_TYPE

    templates = list(CATALOG_COMMON_PARAMS)
    if dual:
        templates.append(_CATALOG_TYPE_PARAM)
    parameters = _merge_common_params(
        (), tuple(templates), key_param=key_param, backfill=True
    )

    notes = [
        "목록조회서비스 메타 1건에서 승격한 골격 오퍼레이션이다.",
        "포털 메타는 응답 스키마를 주지 않으므로 response_schema는 미확정(None)이며 "
        "라이브 샘플링 추론이 채운다.",
    ]
    if dual:
        notes.append(
            f"포털 표기 응답 형식: {entry.data_format} — XML·JSON 이중 응답이므로 "
            "type 파라미터로 형식을 고른다."
        )
    elif formats:
        notes.append(f"포털 표기 응답 형식: {entry.data_format}.")
    else:
        notes.append(
            "포털 메타에 응답 형식(dataFormat) 표기가 없어 JSON으로 가정한다."
        )
    if entry.operation_name is not None:
        notes.append(f"포털 표기 오퍼레이션명: {entry.operation_name}.")

    fingerprint = fingerprint_document(asdict(entry))
    operation_id = _normalize_operation_id(
        _path_slug(path) or entry.operation_name,
        method="GET",
        path=path,
        index=0,
        fingerprint=fingerprint,
        taken=set(),
    )
    operation = OperationSpec(
        operation_id=operation_id,
        method="GET",
        path=path,
        summary=entry.operation_name or entry.title,
        description=" ".join(notes),
        parameters=parameters,
        response_media_type=media_type,
        response_schema=None,
        tags=("catalog",),
    )

    description_parts = ["목록조회서비스 메타에서 승격한 스펙 골격이다."]
    if entry.org_name is not None:
        description_parts.append(f"제공기관: {entry.org_name}.")
    if api_type is not None:
        description_parts.append(f"API 유형: {api_type}.")

    return _validate_source_spec(
        SourceSpec(
            provider=DEFAULT_PROVIDER,
            service_id=entry.service_id,
            service_name=entry.title,
            base_url=base_url,
            source_kind=SourceKind.CATALOG_META,
            operations=(operation,),
            key_param=key_param,
            source_url=entry.swagger_json_url or entry.guide_url,
            fingerprint=fingerprint,
            fetched_at=None,
            description=" ".join(description_parts),
            license_note=None,
        )
    )


def catalog_entries_to_sources(
    entries: Sequence[CatalogEntry],
    *,
    key_param: str = "serviceKey",
) -> tuple[SourceSpec, ...]:
    """승격 가능한 행만 골라 :class:`SourceSpec` 튜플로 만든다.

    ``api_type == "LINK"`` 인 행(불변식 I7)과 엔드포인트가 결손된 행은 조용히
    제외한다. 입력 순서를 보존하므로 결과는 결정론적이다.

    Args:
        entries: 정규화된 카탈로그 행들.
        key_param: 인증키 파라미터명.

    Returns:
        승격된 :class:`SourceSpec` 튜플(제외된 행은 결과에 없다).
    """
    sources: list[SourceSpec] = []
    for entry in entries:
        try:
            sources.append(catalog_entry_to_source(entry, key_param=key_param))
        except SourceSpecError:
            continue
    return tuple(sources)


# ---------------------------------------------------------------------------
# 디스패처
# ---------------------------------------------------------------------------
def load_source(
    document: Mapping[str, Any],
    *,
    service_id: str,
    kind: SourceKind | None = None,
    service_name: str | None = None,
    source_url: str | None = None,
    fetched_at: str | None = None,
    key_param: str = "serviceKey",
) -> SourceSpec:
    """문서를 판별해 해당 어댑터에 위임한다.

    Args:
        document: 스펙 문서(swagger 2.0 · OpenAPI 3.x · 수동 매핑 기술서).
        service_id: 포털 데이터셋 ID. 기술형(REST_DOC_MANUAL)에서는 기술서에 적힌
            값과 일치해야 한다(불일치는 라벨링 사고이므로 예외로 막는다).
        kind: 소스 종류. None이면 :func:`detect_source_kind` 로 판별한다.
        service_name: 서비스명(None이면 문서에서 읽는다).
        source_url: 스펙 원본 URL.
        fetched_at: 취득 시각(ISO8601 KST).
        key_param: 인증키 파라미터명.

    Returns:
        정규화된 :class:`SourceSpec`.

    Raises:
        SourceSpecError: 판별 실패, 필수 필드 결손, ``CATALOG_META`` 를 이 함수로
            넘긴 경우(목록조회 메타는 :func:`load_catalog_rows` 경로를 쓴다).
    """
    doc = _require_mapping(document, what="스펙 문서")
    resolved_kind = kind if kind is not None else detect_source_kind(doc)

    if resolved_kind is SourceKind.REST_DOC_MANUAL:
        declared = _text(doc.get("service_id"))
        wanted = _text(service_id)
        if wanted is not None and declared is not None and declared != wanted:
            raise SourceSpecError(
                f"수동 매핑 기술서의 service_id('{declared}')와 인자로 받은 "
                f"service_id('{wanted}')가 다릅니다. 스펙 산출물이 잘못된 데이터셋 "
                "ID로 라벨링되는 것을 막기 위해 중단합니다."
            )
        return load_rest_doc(doc, fetched_at=fetched_at)

    if resolved_kind is SourceKind.CATALOG_META:
        raise SourceSpecError(
            "목록조회 메타(CATALOG_META)는 load_source로 처리하지 않습니다. "
            "load_catalog_rows(rows) → catalog_entries_to_sources(entries) 경로를 "
            "사용하세요."
        )

    loader = (
        load_odcloud_swagger
        if resolved_kind is SourceKind.ODCLOUD_SWAGGER
        else load_gw_swagger
    )
    return loader(
        doc,
        service_id=service_id,
        service_name=service_name,
        source_url=source_url,
        fetched_at=fetched_at,
        key_param=key_param,
    )
