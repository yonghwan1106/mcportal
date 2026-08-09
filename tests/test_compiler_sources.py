# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Yong Park
"""compiler.sources 테스트: 스펙 소스 4종 흡수 · IR 불변식 · 결정론.

픽스처는 100% 합성이다. 가상 기관·가상 데이터셋 ID·``.invalid`` 도메인만 쓰며
실인증키·실데이터·실네트워크는 어디에도 없다(이 모듈은 HTTP를 하지 않으므로
respx 모킹도 필요하지 않다).
"""

from __future__ import annotations

import dataclasses
import json
import re
from pathlib import Path
from typing import Any

import pytest

from mcportal.compiler.sources import (
    CatalogEntry,
    ODCLOUD_COMMON_PARAMS,
    OperationSpec,
    ParamSpec,
    STANDARD_COMMON_PARAMS,
    SourceKind,
    SourceSpec,
    SourceSpecError,
    catalog_entries_to_sources,
    catalog_entry_to_source,
    detect_source_kind,
    fingerprint_document,
    load_catalog_rows,
    load_gw_swagger,
    load_odcloud_swagger,
    load_rest_doc,
    load_source,
    unresolved_schema_operations,
)

_FIXTURES = Path(__file__).parent / "fixtures" / "compiler"


def _fixture(name: str) -> dict[str, Any]:
    """합성 픽스처를 매번 새 객체로 읽는다(테스트 간 오염 방지)."""
    return json.loads((_FIXTURES / name).read_text(encoding="utf-8"))


def _params(operation: OperationSpec) -> dict[str, ParamSpec]:
    """파라미터를 이름으로 인덱싱한다."""
    return {param.name: param for param in operation.parameters}


def _operations(source: SourceSpec) -> dict[str, OperationSpec]:
    """오퍼레이션을 id로 인덱싱한다."""
    return {operation.operation_id: operation for operation in source.operations}


def _reverse_keys(value: Any) -> Any:
    """dict 키 순서를 재귀적으로 뒤집는다(순서 비의존 검증용)."""
    if isinstance(value, dict):
        return {key: _reverse_keys(item) for key, item in reversed(list(value.items()))}
    if isinstance(value, list):
        return [_reverse_keys(item) for item in value]
    return value


# ---------------------------------------------------------------------------
# 1. odcloud swagger 어댑터
# ---------------------------------------------------------------------------
def test_odcloud_swagger_fields() -> None:
    """odcloud OAS 3.x → SourceSpec 필드 전수 검증."""
    source = load_odcloud_swagger(
        _fixture("sources_odcloud_swagger.json"),
        service_id="00000000",
        source_url="https://infuser.odcloud.invalid/oas/docs?namespace=00000000/v1",
        fetched_at="2026-08-05T09:00:00+09:00",
    )

    assert source.provider == "data.go.kr"
    assert source.service_id == "00000000"
    assert source.service_name == "가상 문화시설 목록 조회 서비스"
    assert source.base_url == "https://api.odcloud.invalid/api/00000000/v1"
    assert source.source_kind is SourceKind.ODCLOUD_SWAGGER
    assert source.key_param == "serviceKey"
    assert source.license_note == "KOGL 제1유형(예시)"
    assert source.fetched_at == "2026-08-05T09:00:00+09:00"
    assert len(source.operations) == 2

    detail, listing = source.operations  # (path, method) 오름차순
    assert listing.operation_id == "getCultureFacilityList"
    assert listing.method == "GET"
    assert listing.path == "/uddi:00000000-1111-2222-3333-444455556666"
    assert listing.summary == "문화시설 목록 조회"
    assert listing.response_media_type == "application/json"
    assert detail.operation_id == "getCultureFacilityDetail"

    facility_id = _params(detail)["facilityId"]
    assert (facility_id.required, facility_id.type, facility_id.example) == (
        True,
        "string",
        "F-0001",
    )


def test_odcloud_common_params_backfilled_and_enriched() -> None:
    """page·perPage·returnType 공통 파라미터를 보강하되 원 문서 선언을 존중한다."""
    source = load_odcloud_swagger(
        _fixture("sources_odcloud_swagger.json"), service_id="00000000"
    )
    operations = _operations(source)

    # (a) 문서가 선언하지 않은 오퍼레이션에는 셋을 모두 보강한다.
    detail = _params(operations["getCultureFacilityDetail"])
    assert {"page", "perPage", "returnType"} <= set(detail)
    assert detail["page"].type == "integer"
    assert detail["page"].required is False
    assert detail["returnType"].enum == ("json", "xml")

    # (b) 문서가 선언한 설명은 템플릿이 덮어쓰지 않는다(빈칸만 채운다).
    listing = _params(operations["getCultureFacilityList"])
    assert listing["page"].description == "페이지 번호(원 문서 표기)"
    assert listing["perPage"].description == ODCLOUD_COMMON_PARAMS[1].description
    assert listing["perPage"].example == "10"

    # (c) 페이징 파라미터에는 example을 주지 않는다(샘플러가 페이지를 늘려야 한다).
    assert listing["page"].example is None
    assert detail["page"].example is None


def test_odcloud_response_schema_inlined_and_missing_marked() -> None:
    """$ref는 인라인으로 펼치고, 스키마가 없는 자리는 None으로 명시한다."""
    source = load_odcloud_swagger(
        _fixture("sources_odcloud_swagger.json"), service_id="00000000"
    )
    operations = _operations(source)

    schema = operations["getCultureFacilityList"].response_schema
    assert schema is not None
    assert "$ref" not in json.dumps(schema, ensure_ascii=False)
    item = schema["properties"]["data"]["items"]
    assert item["properties"]["시설명"] == {"type": "string"}

    # 응답 스키마를 주지 않은 오퍼레이션은 None(=추론기가 채울 자리)으로 남는다.
    assert operations["getCultureFacilityDetail"].response_schema is None
    assert unresolved_schema_operations(source) == ("getCultureFacilityDetail",)


# ---------------------------------------------------------------------------
# 2. GW Swagger 2.0
# ---------------------------------------------------------------------------
def test_gw_swagger_v2_absorption() -> None:
    """swagger 2.0: schemes+host+basePath 복원, parameters[].type 흡수."""
    source = load_gw_swagger(
        _fixture("sources_gw_swagger_v2.json"), service_id="00000001"
    )

    assert source.source_kind is SourceKind.GW_SWAGGER
    assert source.base_url == "https://apis.example.invalid/0000000/demoStats"
    # GET·POST만 흡수한다(DELETE는 제외).
    assert [operation.method for operation in source.operations] == [
        "GET",
        "GET",
        "POST",
    ]

    region = source.operations[1]
    assert region.path == "/getRegionList"
    assert region.response_media_type == "application/xml"  # 문서 produces[0]
    assert region.tags == ("region", "list")

    params = _params(region)
    assert "attachedFile" not in params          # formData는 조용히 버린다
    assert params["sidoCodes"].type == "array"
    assert params["sidoCodes"].item_type == "string"
    assert params["type"].enum == ("xml", "json")  # 원 순서 보존
    assert params["numOfRows"].default == "10"
    assert params["pageNo"].required is True

    # 응답 스키마의 중첩 $ref가 2단으로 펼쳐진다.
    schema = region.response_schema
    assert schema is not None
    body = schema["properties"]["response"]["properties"]["body"]
    assert body["properties"]["totalCount"] == {"type": "integer"}

    report = source.operations[2]
    assert report.operation_id == "submitOrgReport"
    assert report.method == "POST"
    assert report.response_media_type == "application/json"  # 오퍼레이션 produces 우선
    assert report.request_body_schema is not None
    assert report.request_body_schema["properties"]["title"] == {"type": "string"}
    assert _params(report)["orgCode"].location == "path"
    assert _params(report)["orgCode"].required is True


# ---------------------------------------------------------------------------
# 3. GW Swagger 3.x
# ---------------------------------------------------------------------------
def test_gw_swagger_v3_absorption() -> None:
    """OpenAPI 3.x: servers[0].url, parameters[].schema.type 흡수."""
    source = load_gw_swagger(
        _fixture("sources_gw_swagger_v3.json"), service_id="00000003"
    )

    # 끝 "/"는 제거된다(불변식 I6).
    assert source.base_url == "https://apis.example.invalid/0000001/alertService"
    assert source.license_note is None

    operation = source.operations[0]
    assert operation.deprecated is True
    assert operation.tags == ("alert", "list")
    # content 키를 정렬해 첫 미디어타입을 고른다(text/xml보다 application/json이 앞).
    assert operation.response_media_type == "application/json"
    assert operation.response_schema is not None
    assert operation.response_schema["properties"]["totalCount"] == {"type": "integer"}

    params = _params(operation)
    assert "sessionHint" not in params            # cookie는 조용히 버린다
    assert params["X-Request-Trace"].location == "header"
    assert params["pageNo"].type == "integer"
    assert params["pageNo"].default == "1"        # schema.default
    assert params["pageNo"].example == "1"        # 파라미터 레벨 example
    assert params["regionCodes"].item_type == "integer"


# ---------------------------------------------------------------------------
# 4. 수동 매핑 기술서(표준 REST 문서형)
# ---------------------------------------------------------------------------
def test_rest_doc_absorption() -> None:
    """수동 매핑 기술서 → SourceSpec 변환과 표준 공통 파라미터 보강."""
    source = load_rest_doc(
        _fixture("sources_rest_doc.json"), fetched_at="2026-08-05T10:00:00+09:00"
    )

    assert source.source_kind is SourceKind.REST_DOC_MANUAL
    assert source.service_id == "00000002"
    assert source.base_url == "https://apis.example.invalid/0000002/lawInfo"
    assert source.license_note == "KOGL 제1유형(예시)"
    assert [operation.operation_id for operation in source.operations] == [
        "getLawDetail",
        "getLawList",
    ]

    operations = _operations(source)
    detail = operations["getLawDetail"]
    # 기술서가 "/" 없이 적어도 경로로 정규화된다(불변식 I2).
    assert detail.path == "/getLawDetail"
    assert detail.response_media_type == "application/xml"
    assert detail.response_schema is not None
    assert detail.response_schema["properties"]["lawId"] == {"type": "string"}

    listing = operations["getLawList"]
    assert listing.response_schema is None
    assert unresolved_schema_operations(source) == ("getLawList",)

    params = _params(listing)
    # XML·JSON 이중 응답은 type 파라미터의 열거값으로 표기된다.
    assert params["type"].enum == ("xml", "json")
    assert params["type"].default == "xml"
    # 기술서가 비워 둔 설명은 표준 표기로 채운다.
    assert params["numOfRows"].description == STANDARD_COMMON_PARAMS[1].description
    assert params["pageNo"].description == "페이지 번호"
    # 기술서에 없는 공통 파라미터를 임의로 추가하지 않는다(사람이 쓴 선언이 정본).
    assert set(params) == {"numOfRows", "pageNo", "query", "type"}


def test_rest_doc_missing_fields_raise_korean_error() -> None:
    """기술서 필수 필드 결손은 어떤 필드가 왜 부족한지 한국어로 알린다."""
    descriptor = _fixture("sources_rest_doc.json")
    del descriptor["service_name"]
    with pytest.raises(SourceSpecError) as excinfo:
        load_rest_doc(descriptor)
    message = str(excinfo.value)
    assert "service_name" in message
    assert "서비스명" in message

    broken = _fixture("sources_rest_doc.json")
    del broken["operations"][0]["path"]
    with pytest.raises(SourceSpecError) as excinfo:
        load_rest_doc(broken)
    assert "'path'" in str(excinfo.value)

    versioned = _fixture("sources_rest_doc.json")
    versioned["mcportal_rest_doc"] = 2
    with pytest.raises(SourceSpecError, match="지원하지 않는 기술서 버전"):
        load_rest_doc(versioned)


# ---------------------------------------------------------------------------
# 5. I3 — 인증키 격리
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("fixture_name", "loader"),
    [
        ("sources_odcloud_swagger.json", load_odcloud_swagger),
        ("sources_gw_swagger_v2.json", load_gw_swagger),
        ("sources_gw_swagger_v3.json", load_gw_swagger),
    ],
)
def test_key_param_is_never_exposed(fixture_name: str, loader: Any) -> None:
    """serviceKey는 대소문자 변형까지 전부 파라미터에서 제거된다(I3)."""
    source = loader(_fixture(fixture_name), service_id="00000000")
    for operation in source.operations:
        for param in operation.parameters:
            assert param.name.lower() != "servicekey"
    assert source.key_param == "serviceKey"


def test_rest_doc_removes_key_param() -> None:
    """기술서에 인증키가 남아 있어도 제거된다(I3)."""
    source = load_rest_doc(_fixture("sources_rest_doc.json"))
    names = {
        param.name for operation in source.operations for param in operation.parameters
    }
    assert "serviceKey" not in names


def test_custom_key_param_is_removed_and_not_backfilled() -> None:
    """key_param을 바꾸면 그 이름의 파라미터가 제거되고 공통 보강도 피해 간다."""
    document = _fixture("sources_odcloud_swagger.json")
    operation = document["paths"]["/uddi:00000000-1111-2222-3333-444455550000"]["get"]
    operation["parameters"].append(
        {"name": "returnType", "in": "query", "required": True, "schema": {"type": "string"}}
    )
    source = load_odcloud_swagger(
        document, service_id="00000000", key_param="returnType"
    )
    for op in source.operations:
        assert all(param.name != "returnType" for param in op.parameters)
    assert source.key_param == "returnType"


# ---------------------------------------------------------------------------
# 6. I1 — operation_id 정규화
# ---------------------------------------------------------------------------
def test_operation_id_normalized_from_korean_names() -> None:
    """한글 operationId는 경로 기반 ASCII 식별자로 떨어진다(밑줄만 남지 않는다)."""
    source = load_gw_swagger(
        _fixture("sources_gw_swagger_v2.json"), service_id="00000001"
    )
    identifiers = [operation.operation_id for operation in source.operations]
    # 비ASCII 치환 결과가 밑줄뿐이면 "비어 있음"으로 보고 경로 슬러그로 폴백한다.
    # 글자 수만 반영한 '_________' 같은 이름은 MCP 도구명으로 쓸 수 없다.
    assert identifiers[0] == "op_get_getOrgList"
    assert identifiers[1] == "op_get_getRegionList"
    for identifier in identifiers:
        assert re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", identifier)
        assert identifier.strip("_"), f"밑줄뿐인 식별자가 남았다: {identifier!r}"
    assert len(set(identifiers)) == len(identifiers)


def test_operation_id_falls_back_to_path_slug() -> None:
    """operationId가 없거나 숫자로 시작하면 경로 슬러그로 폴백한다."""
    document = {
        "openapi": "3.0.0",
        "info": {"title": "가상 폴백 서비스"},
        "servers": [{"url": "https://apis.example.invalid/0000009/fallback"}],
        "paths": {
            "/getA": {"get": {"responses": {"200": {"description": "정상"}}}},
            "/getB": {
                "get": {
                    "operationId": "9invalid",
                    "responses": {"200": {"description": "정상"}},
                }
            },
        },
    }
    source = load_gw_swagger(document, service_id="00000009")
    assert [operation.operation_id for operation in source.operations] == [
        "op_get_getA",
        "op_get_getB",
    ]


def test_operation_id_length_cap() -> None:
    """64자를 넘으면 앞 57자 + '_' + 지문 앞 6hex로 접는다."""
    long_name = "a" * 90
    document = {
        "openapi": "3.0.0",
        "info": {"title": "가상 장문 서비스"},
        "servers": [{"url": "https://apis.example.invalid/0000008/long"}],
        "paths": {
            "/x": {
                "get": {
                    "operationId": long_name,
                    "responses": {"200": {"description": "정상"}},
                }
            }
        },
    }
    source = load_gw_swagger(document, service_id="00000008")
    identifier = source.operations[0].operation_id
    assert len(identifier) == 64
    assert identifier.startswith("a" * 57 + "_")
    assert identifier.endswith(source.fingerprint.split(":")[1][:6])


# ---------------------------------------------------------------------------
# 7. I4·I5 정렬과 dict 순서 비의존
# ---------------------------------------------------------------------------
def test_operations_and_parameters_are_sorted() -> None:
    """operations는 (path, method), parameters는 (필수 먼저, 이름) 순이다."""
    source = load_gw_swagger(
        _fixture("sources_gw_swagger_v2.json"), service_id="00000001"
    )
    order = [(operation.path, operation.method) for operation in source.operations]
    assert order == sorted(order)
    for operation in source.operations:
        keys = [(not param.required, param.name) for param in operation.parameters]
        assert keys == sorted(keys)


@pytest.mark.parametrize(
    "fixture_name",
    [
        "sources_odcloud_swagger.json",
        "sources_gw_swagger_v2.json",
        "sources_gw_swagger_v3.json",
        "sources_rest_doc.json",
    ],
)
def test_key_order_does_not_change_result(fixture_name: str) -> None:
    """문서의 dict 키 순서를 뒤집어도 같은 SourceSpec이 나온다(결정론)."""
    document = _fixture(fixture_name)
    straight = load_source(document, service_id=str(document.get("service_id", "00000000")))
    flipped = load_source(
        _reverse_keys(document), service_id=str(document.get("service_id", "00000000"))
    )
    assert straight == flipped


# ---------------------------------------------------------------------------
# 8. detect_source_kind
# ---------------------------------------------------------------------------
def test_detect_source_kind() -> None:
    """세 갈래를 판별하고, 미지 문서는 부족한 키를 알려 준다."""
    assert (
        detect_source_kind(_fixture("sources_odcloud_swagger.json"))
        is SourceKind.ODCLOUD_SWAGGER
    )
    assert (
        detect_source_kind(_fixture("sources_gw_swagger_v2.json"))
        is SourceKind.GW_SWAGGER
    )
    assert (
        detect_source_kind(_fixture("sources_gw_swagger_v3.json"))
        is SourceKind.GW_SWAGGER
    )
    assert (
        detect_source_kind(_fixture("sources_rest_doc.json"))
        is SourceKind.REST_DOC_MANUAL
    )

    with pytest.raises(SourceSpecError) as excinfo:
        detect_source_kind({"foo": 1, "bar": 2})
    message = str(excinfo.value)
    assert "swagger" in message and "openapi" in message and "mcportal_rest_doc" in message

    with pytest.raises(SourceSpecError, match="지원하지 않는 swagger 버전"):
        detect_source_kind({"swagger": "1.2"})


# ---------------------------------------------------------------------------
# 9. fingerprint_document
# ---------------------------------------------------------------------------
def test_fingerprint_document() -> None:
    """같은 내용이면 키 순서가 달라도 같은 지문, 한 글자 다르면 다른 지문."""
    document = _fixture("sources_gw_swagger_v3.json")
    digest = fingerprint_document(document)
    assert digest.startswith("sha256:")
    assert re.fullmatch(r"[0-9a-f]{64}", digest.split(":", 1)[1])
    assert fingerprint_document(_reverse_keys(document)) == digest

    changed = _fixture("sources_gw_swagger_v3.json")
    changed["info"]["title"] += "."
    assert fingerprint_document(changed) != digest

    assert fingerprint_document("가상 문서") == fingerprint_document("가상 문서".encode())
    with pytest.raises(SourceSpecError, match="매핑"):
        fingerprint_document(123)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# 10. $ref 해석
# ---------------------------------------------------------------------------
def _ref_document(ref_target: str, extra_schema: dict[str, Any]) -> dict[str, Any]:
    """$ref 검증용 최소 합성 문서."""
    return {
        "openapi": "3.0.0",
        "info": {"title": "가상 참조 서비스"},
        "servers": [{"url": "https://apis.example.invalid/0000004/refs"}],
        "paths": {
            "/get": {
                "get": {
                    "operationId": "getRefs",
                    "responses": {
                        "200": {
                            "description": "정상",
                            "content": {
                                "application/json": {"schema": {"$ref": ref_target}}
                            },
                        }
                    },
                }
            }
        },
        "components": {"schemas": extra_schema},
    }


def test_internal_ref_is_inlined() -> None:
    """문서 내부 참조는 인라인으로 펼쳐진다."""
    document = _ref_document(
        "#/components/schemas/A",
        {
            "A": {"type": "object", "properties": {"child": {"$ref": "#/components/schemas/B"}}},
            "B": {"type": "object", "properties": {"value": {"type": "integer"}}},
        },
    )
    source = load_gw_swagger(document, service_id="00000004")
    schema = source.operations[0].response_schema
    assert schema is not None
    assert schema["properties"]["child"]["properties"]["value"] == {"type": "integer"}


def test_circular_and_external_refs_raise() -> None:
    """순환 참조·외부 참조는 SourceSpecError."""
    circular = _ref_document(
        "#/components/schemas/A",
        {
            "A": {"type": "object", "properties": {"child": {"$ref": "#/components/schemas/B"}}},
            "B": {"type": "object", "properties": {"back": {"$ref": "#/components/schemas/A"}}},
        },
    )
    with pytest.raises(SourceSpecError, match="순환"):
        load_gw_swagger(circular, service_id="00000004")

    external = _ref_document("https://other.example.invalid/spec.json#/A", {})
    with pytest.raises(SourceSpecError, match="지원하지 않는 \\$ref"):
        load_gw_swagger(external, service_id="00000004")

    missing = _ref_document("#/components/schemas/Nope", {"A": {"type": "object"}})
    with pytest.raises(SourceSpecError, match="찾을 수 없습니다"):
        load_gw_swagger(missing, service_id="00000004")


# ---------------------------------------------------------------------------
# 11~12. 목록조회 메타
# ---------------------------------------------------------------------------
def _catalog_entries() -> tuple[CatalogEntry, ...]:
    """합성 목록조회 응답의 data 배열을 정규화한다."""
    return load_catalog_rows(_fixture("sources_catalog_rows.json")["data"])


def test_load_catalog_rows_absorbs_key_variants() -> None:
    """camelCase·snake_case 키 표기를 모두 흡수하고 LINK 행도 그대로 담는다."""
    entries = _catalog_entries()
    assert len(entries) == 5
    assert entries[0].service_id == "00000010"
    assert entries[0].swagger_json_url is not None
    # 두 번째 행은 snake_case 키만 쓴다.
    assert entries[1].service_id == "00000011"
    assert entries[1].end_point_url == "https://apis.example.invalid/0000011/sportsService/"
    assert entries[1].operation_url == "getFacilityList"
    assert entries[2].api_type == "LINK"
    assert entries[3].end_point_url is None
    assert entries[4].data_format is None


def test_load_catalog_rows_missing_identity_raises() -> None:
    """서비스 ID·서비스명을 못 찾으면 어떤 키가 필요한지 알려 준다."""
    with pytest.raises(SourceSpecError) as excinfo:
        load_catalog_rows([{"orgNm": "가상행정연구원"}])
    message = str(excinfo.value)
    assert "서비스 ID" in message
    assert "listId" in message
    assert "orgNm" in message  # 행에 실제로 있던 키를 알려 준다


def test_catalog_entry_to_source_skeleton() -> None:
    """카탈로그 행 승격: 경로 복원 · 표준 공통 파라미터 · 빈 응답 스키마 명시."""
    entry = _catalog_entries()[0]
    source = catalog_entry_to_source(entry)

    assert source.source_kind is SourceKind.CATALOG_META
    assert source.base_url == "https://apis.example.invalid/0000010/libraryService"
    assert source.source_url == entry.swagger_json_url
    assert source.fingerprint.startswith("sha256:")

    operation = source.operations[0]
    assert operation.operation_id == "getLibraryList"
    assert operation.path == "/getLibraryList"
    # JSON+XML 이중 제공 → type 파라미터 + JSON 우선 미디어타입.
    assert operation.response_media_type == "application/json"
    assert set(_params(operation)) == {"numOfRows", "pageNo", "type"}
    assert _params(operation)["type"].enum == ("xml", "json")
    assert "이중 응답" in (operation.description or "")
    # 응답 스키마는 항상 미확정(추론 대상)이다.
    assert operation.response_schema is None
    assert unresolved_schema_operations(source) == ("getLibraryList",)


def test_catalog_entry_single_format_and_unknown_format() -> None:
    """단일 XML 표기는 type 파라미터를 붙이지 않고, 표기 결손은 JSON으로 가정한다."""
    entries = _catalog_entries()
    xml_only = catalog_entry_to_source(entries[1])
    operation = xml_only.operations[0]
    assert operation.response_media_type == "application/xml"
    assert set(_params(operation)) == {"numOfRows", "pageNo"}
    # 상대 경로 operation_url + 끝 "/" base_url도 정확히 복원된다.
    assert xml_only.base_url == "https://apis.example.invalid/0000011/sportsService"
    assert operation.path == "/getFacilityList"

    unknown = catalog_entry_to_source(entries[4])
    assert unknown.operations[0].response_media_type == "application/json"
    assert "JSON으로 가정" in (unknown.operations[0].description or "")
    # base_url과 어긋나는 절대 operation_url은 마지막 세그먼트로 복원한다.
    assert unknown.operations[0].path == "/getTrafficList"


def test_catalog_link_and_missing_endpoint_are_rejected() -> None:
    """LINK형(I7)과 엔드포인트 결손 행은 승격되지 않는다."""
    entries = _catalog_entries()
    with pytest.raises(SourceSpecError) as excinfo:
        catalog_entry_to_source(entries[2])
    assert "LINK" in str(excinfo.value)

    with pytest.raises(SourceSpecError) as excinfo:
        catalog_entry_to_source(entries[3])
    assert "endPoint" in str(excinfo.value)

    sources = catalog_entries_to_sources(entries)
    assert [source.service_id for source in sources] == [
        "00000010",
        "00000011",
        "00000014",
    ]


def test_catalog_source_is_not_loadable_via_load_source() -> None:
    """목록조회 메타는 load_source 경로로 처리하지 않는다."""
    with pytest.raises(SourceSpecError, match="load_catalog_rows"):
        load_source(
            {"openapi": "3.0.0"},
            service_id="00000010",
            kind=SourceKind.CATALOG_META,
        )


# ---------------------------------------------------------------------------
# 13. IR 불변성
# ---------------------------------------------------------------------------
def test_dataclasses_are_frozen_with_tuple_collections() -> None:
    """모든 IR dataclass는 frozen이고 컬렉션 필드는 tuple이다."""
    source = load_rest_doc(_fixture("sources_rest_doc.json"))
    operation = source.operations[0]
    param = operation.parameters[0]
    entry = _catalog_entries()[0]

    assert isinstance(source.operations, tuple)
    assert isinstance(operation.parameters, tuple)
    assert isinstance(operation.tags, tuple)
    assert isinstance(param.enum, tuple)

    for instance, field_name, value in (
        (source, "service_id", "x"),
        (operation, "path", "/x"),
        (param, "name", "x"),
        (entry, "title", "x"),
    ):
        with pytest.raises(dataclasses.FrozenInstanceError):
            setattr(instance, field_name, value)


# ---------------------------------------------------------------------------
# 14. 디스패처와 방어선
# ---------------------------------------------------------------------------
def test_load_source_dispatch() -> None:
    """kind를 생략하면 판별해서 위임하고, 명시하면 그대로 따른다."""
    document = _fixture("sources_odcloud_swagger.json")
    auto = load_source(document, service_id="00000000")
    explicit = load_source(
        document, service_id="00000000", kind=SourceKind.ODCLOUD_SWAGGER
    )
    assert auto == explicit
    assert auto.source_kind is SourceKind.ODCLOUD_SWAGGER

    forced = load_source(document, service_id="00000000", kind=SourceKind.GW_SWAGGER)
    assert forced.source_kind is SourceKind.GW_SWAGGER
    # GW 경로에서는 odcloud 공통 파라미터를 보강하지 않는다.
    detail = _operations(forced)["getCultureFacilityDetail"]
    assert set(_params(detail)) == {"facilityId"}


def test_load_source_rejects_service_id_mismatch_for_rest_doc() -> None:
    """기술서의 service_id와 인자가 다르면 라벨링 사고를 막기 위해 중단한다."""
    with pytest.raises(SourceSpecError, match="service_id"):
        load_source(_fixture("sources_rest_doc.json"), service_id="99999999")
    source = load_source(_fixture("sources_rest_doc.json"), service_id="00000002")
    assert source.service_id == "00000002"


def test_empty_paths_and_unsupported_methods_raise() -> None:
    """오퍼레이션을 하나도 만들 수 없으면 이유를 밝히고 중단한다."""
    document = {
        "openapi": "3.0.0",
        "info": {"title": "가상 빈 서비스"},
        "servers": [{"url": "https://apis.example.invalid/0000005/empty"}],
        "paths": {},
    }
    with pytest.raises(SourceSpecError, match="'paths'"):
        load_gw_swagger(document, service_id="00000005")

    document["paths"] = {"/only": {"delete": {"operationId": "dropIt"}}}
    with pytest.raises(SourceSpecError, match="GET·POST"):
        load_gw_swagger(document, service_id="00000005")


def test_missing_service_name_raises() -> None:
    """info.title도 service_name도 없으면 서비스명을 만들지 않는다."""
    document = {
        "openapi": "3.0.0",
        "servers": [{"url": "https://apis.example.invalid/0000006/anon"}],
        "paths": {"/p": {"get": {"responses": {"200": {"description": "정상"}}}}},
    }
    with pytest.raises(SourceSpecError, match="info.title"):
        load_gw_swagger(document, service_id="00000006")
    named = load_gw_swagger(
        document, service_id="00000006", service_name="가상 무명 서비스"
    )
    assert named.service_name == "가상 무명 서비스"


def test_no_secret_material_in_specs() -> None:
    """어떤 어댑터 산출물에도 인증키 값이 실리지 않는다."""
    sources = [
        load_odcloud_swagger(_fixture("sources_odcloud_swagger.json"), service_id="00000000"),
        load_gw_swagger(_fixture("sources_gw_swagger_v2.json"), service_id="00000001"),
        load_gw_swagger(_fixture("sources_gw_swagger_v3.json"), service_id="00000003"),
        load_rest_doc(_fixture("sources_rest_doc.json")),
        *catalog_entries_to_sources(_catalog_entries()),
    ]
    for source in sources:
        dumped = json.dumps(dataclasses.asdict(source), ensure_ascii=False, default=str)
        assert "serviceKey=" not in dumped
        for operation in source.operations:
            assert all(
                param.name.lower() != source.key_param.lower()
                for param in operation.parameters
            )


# ---------------------------------------------------------------------------
# 15. 어댑터 일반화 회귀 (W3: 실전 형식 차이 F-02·F-03·F-04·F-11 흡수)
#
# 아래 네 절은 전부 "포털 문서의 일반 형태"에 대한 규칙이며 특정 서비스 전용
# 분기가 아니다. 픽스처는 여기서도 100% 합성이다.
# ---------------------------------------------------------------------------
def _swagger2(paths: dict[str, Any], *, host: str, **extra: Any) -> dict[str, Any]:
    """합성 swagger 2.0 문서 골격."""
    document: dict[str, Any] = {
        "swagger": "2.0",
        "info": {"title": "가상 회귀 서비스"},
        "host": host,
        "basePath": "/0000007/regress",
        "schemes": ["https"],
        "paths": paths,
    }
    document.update(extra)
    return document


def _ok_response(schema: dict[str, Any] | None = None) -> dict[str, Any]:
    """200 응답 1개짜리 responses 블록."""
    entry: dict[str, Any] = {"description": "정상"}
    if schema is not None:
        entry["schema"] = schema
    return {"200": entry}


# --- F-02: 본문형 오퍼레이션의 페이징 유령 인자 ----------------------------
def test_pagination_backfill_skips_body_operations() -> None:
    """요청 본문형 오퍼레이션에는 페이징 공통 파라미터를 보강하지 않는다(F-02).

    조회 대상을 본문이 정하므로 질의문자열 페이징은 의미가 없다. 응답 형식
    역할(returnType)은 그대로 보강한다 — odcloud 공식 안내가 질의 문자열 전달을
    명시하기 때문이다.
    """
    document = _swagger2(
        {
            "/bodyOp": {
                "post": {
                    "operationId": "bodyOp",
                    "parameters": [
                        {
                            "in": "body",
                            "name": "body",
                            "required": True,
                            "schema": {
                                "type": "object",
                                "required": ["ids"],
                                "properties": {
                                    "ids": {"type": "array", "items": {"type": "string"}}
                                },
                            },
                        }
                    ],
                    "responses": _ok_response({"type": "object", "properties": {"n": {"type": "integer"}}}),
                }
            },
            "/queryOp": {
                "get": {
                    "operationId": "queryOp",
                    "responses": _ok_response({"type": "object", "properties": {"n": {"type": "integer"}}}),
                }
            },
        },
        host="api.odcloud.invalid",
    )
    source = load_odcloud_swagger(document, service_id="00000007")
    operations = _operations(source)

    body_params = _params(operations["bodyOp"])
    assert operations["bodyOp"].request_body_schema is not None
    assert "page" not in body_params
    assert "perPage" not in body_params
    assert "returnType" in body_params  # 형식 역할은 유지된다

    # 질의형 오퍼레이션의 기존 동작은 그대로다(회귀 방지).
    assert {"page", "perPage", "returnType"} <= set(_params(operations["queryOp"]))


def test_declared_pagination_params_are_still_enriched_on_body_operations() -> None:
    """소스가 직접 선언한 페이징 파라미터는 본문형이어도 지우지 않는다.

    보강(backfill)만 건너뛴다 — 소스 선언이 정본이라는 원칙은 그대로다.
    """
    document = _swagger2(
        {
            "/bodyOp": {
                "post": {
                    "operationId": "bodyOp",
                    "parameters": [
                        {
                            "in": "body",
                            "name": "body",
                            "schema": {
                                "type": "object",
                                "properties": {"ids": {"type": "string"}},
                            },
                        },
                        {"name": "page", "in": "query", "type": "integer"},
                    ],
                    "responses": _ok_response({"type": "object", "properties": {"n": {"type": "integer"}}}),
                }
            }
        },
        host="api.odcloud.invalid",
    )
    params = _params(load_odcloud_swagger(document, service_id="00000007").operations[0])
    assert "page" in params
    assert params["page"].description == ODCLOUD_COMMON_PARAMS[0].description
    assert "perPage" not in params  # 보강은 여전히 건너뛴다


# --- F-03/F-11: 정보량 0인 껍데기 스키마 강등과 빈 properties 제거 --------
@pytest.mark.parametrize(
    "schema",
    [
        {"type": "object", "properties": {}},
        {"type": "array", "items": {"type": "object", "properties": {}}},
        {"type": "array"},
        {"description": "설명만 있다"},
    ],
)
def test_empty_shell_schema_is_demoted_to_none(schema: dict[str, Any]) -> None:
    """정보량 0인 스키마는 None으로 강등돼 추론 대상이 된다(정찰 F-03).

    빈 ``{}`` 는 이전부터 막고 있었지만 ``{"type":"object","properties":{}}`` 같은
    껍데기는 통과해, unresolved_schema_operations 가 빈 튜플을 돌려주고 샘플링·
    추론이 영원히 트리거되지 않았다.

    ⚠️ 2026-08-06 적대 리뷰(설계 §7 G2 개정)로 두 사례가 이 목록에서 **빠졌다**:
    ``{"properties": {...}}``(``type`` 생략)와 ``properties`` 에 이름만 선언한
    객체는 이제 보존된다. 근거는 :func:`test_named_fields_survive_without_type`.
    강등이 남는 기준은 **선언된 필드가 0개**라는 사실 하나뿐이다.
    """
    document = _swagger2(
        {"/x": {"get": {"operationId": "getX", "responses": _ok_response(schema)}}},
        host="apis.example.invalid",
    )
    source = load_gw_swagger(document, service_id="00000007")
    assert source.operations[0].response_schema is None
    assert unresolved_schema_operations(source) == ("getX",)


@pytest.mark.parametrize(
    "schema",
    [
        {"type": "object", "properties": {"a": {"type": "string"}}},
        {"type": "array", "items": {"type": "integer"}},
        {"type": "object", "properties": {}, "required": ["a"]},
        {"type": "object", "properties": {"a": {"type": "object", "properties": {"b": {"type": "integer"}}}}},
    ],
)
def test_informative_schema_survives(schema: dict[str, Any]) -> None:
    """실제 선언이 하나라도 있으면 강등하지 않는다(과잉 강등 방지)."""
    document = _swagger2(
        {"/x": {"get": {"operationId": "getX", "responses": _ok_response(schema)}}},
        host="apis.example.invalid",
    )
    source = load_gw_swagger(document, service_id="00000007")
    assert source.operations[0].response_schema is not None
    assert unresolved_schema_operations(source) == ()


@pytest.mark.parametrize(
    "schema",
    [
        # `type` 생략 + 전부 타입 선언된 하위 필드(JSON Schema 표준 형태)
        {
            "properties": {
                "resultCode": {"type": "string"},
                "items": {"type": "array", "items": {"type": "string"}},
            }
        },
        # `type` 생략 + 배열(items 만 선언)
        {"items": {"type": "object", "properties": {"id": {"type": "string"}}}},
        # 필드 이름만 선언(타입 없음) — 이름 자체가 선언이다
        {"type": "object", "properties": {"totalCount": {}, "items": {}}},
        {"type": "object", "properties": {"a": {"description": "이름은 있다"}}},
        {"type": "object", "properties": {"a": {"type": "object", "properties": {}}}},
    ],
)
def test_named_fields_survive_without_type(schema: dict[str, Any]) -> None:
    """``type`` 을 생략했거나 리프에 타입이 없어도 선언된 필드는 살아남는다.

    적대 리뷰 F1(critical): ``type`` 은 JSON Schema/OpenAPI 3.x 의 **선택**
    키워드인데 G2 판정이 ``type`` 없는 정상 스키마를 통째로 폐기해, v0.1.0 에서는
    보존되던 응답 구조가 사라졌다(W2 대비 회귀). 같은 리뷰의 부수 발견대로
    "필드 이름은 선언했지만 타입이 없다"까지 함께 지우면 미확정 건수가 부풀려져
    **정직 고지의 방향이 반대로 어긋난다**.
    """
    document = _swagger2(
        {"/x": {"get": {"operationId": "getX", "responses": _ok_response(schema)}}},
        host="apis.example.invalid",
    )
    source = load_gw_swagger(document, service_id="00000007")
    assert source.operations[0].response_schema is not None
    assert unresolved_schema_operations(source) == ()


def test_empty_properties_noise_is_stripped_from_scalar_leaves() -> None:
    """스칼라 리프에 덧붙은 빈 properties 는 산출물로 새어 나가지 않는다(F-11).

    선언 타입이 object 인 노드의 빈 properties 는 "필드가 없다"는 사실이므로
    남긴다 — 그 사실이 정보량 판정의 근거가 된다.
    """
    document = _swagger2(
        {
            "/x": {
                "get": {
                    "operationId": "getX",
                    "responses": _ok_response(
                        {
                            "type": "object",
                            "properties": {
                                "code": {"type": "string", "properties": {}},
                                "items": {
                                    "type": "array",
                                    "properties": {},
                                    "items": {"type": "integer", "properties": {}},
                                },
                                "hollow": {"type": "object", "properties": {}},
                            },
                        }
                    ),
                }
            }
        },
        host="apis.example.invalid",
    )
    schema = load_gw_swagger(document, service_id="00000007").operations[0].response_schema
    assert schema is not None
    assert schema["properties"]["code"] == {"type": "string"}
    assert "properties" not in schema["properties"]["items"]
    assert schema["properties"]["items"]["items"] == {"type": "integer"}
    assert schema["properties"]["hollow"] == {"type": "object", "properties": {}}


# --- F-04: 비표준 메타 블록의 예시값 흡수 ---------------------------------
def _meta_block(operation_id: str, entries: list[dict[str, Any]]) -> dict[str, Any]:
    """게이트웨이 비표준 메타 블록 1개."""
    return {
        "operationId": operation_id,
        "gwSvcNm": operation_id,
        "oprtinUrl": f"https://origin.example.invalid/rest/{operation_id}",
        "reqList": entries,
    }


def _meta_document(blocks: list[dict[str, Any]]) -> dict[str, Any]:
    """메타 블록을 곁들인 합성 게이트웨이 문서."""
    return _swagger2(
        {
            "/getMetaList": {
                "get": {
                    "operationId": "getMetaList",
                    "responses": _ok_response(
                        {"type": "object", "properties": {"n": {"type": "integer"}}}
                    ),
                },
                "parameters": [
                    {"name": "serviceKey", "in": "query", "type": "string", "required": True},
                    {"name": "startYm", "in": "query", "type": "string", "required": True},
                    {"name": "endYm", "in": "query", "type": "string", "required": True},
                    {
                        "name": "unit",
                        "in": "query",
                        "type": "string",
                        "required": False,
                        "example": "원 문서 예시",
                    },
                    {"name": "blank", "in": "query", "type": "string", "required": False},
                ],
            }
        },
        host="apis.example.invalid",
        swaggerOprtinVOs=blocks,
    )


def test_meta_block_fills_missing_examples_only() -> None:
    """표준 필드에 example 이 없을 때만 비표준 메타 블록 값으로 채운다(F-04)."""
    document = _meta_document(
        [
            _meta_block(
                "getMetaList",
                [
                    {"paramtrNm": "serviceKey", "paramtrBassValue": "인증키"},
                    {"paramtrNm": "startYm", "paramtrBassValue": "202601"},
                    {"paramtrNm": "endYm", "paramtrBassValue": "202612"},
                    {"paramtrNm": "unit", "paramtrBassValue": "메타 예시"},
                    {"paramtrNm": "blank", "paramtrBassValue": "-"},
                ],
            )
        ]
    )
    params = _params(load_gw_swagger(document, service_id="00000007").operations[0])
    assert params["startYm"].example == "202601"
    assert params["endYm"].example == "202612"
    # 이미 있던 예시값은 덮어쓰지 않는다.
    assert params["unit"].example == "원 문서 예시"
    # "-" 는 "값 없음" 표기이므로 예시로 쓰지 않는다.
    assert params["blank"].example is None
    # 인증키는 애초에 파라미터에서 제거된다(I3).
    assert "serviceKey" not in params


def test_meta_block_ambiguous_match_applies_nothing() -> None:
    """두 개 이상의 메타 블록이 한 오퍼레이션에 매칭되면 아무것도 적용하지 않는다."""
    document = _meta_document(
        [
            _meta_block("getMetaList", [{"paramtrNm": "startYm", "paramtrBassValue": "202601"}]),
            _meta_block("getMetaList", [{"paramtrNm": "startYm", "paramtrBassValue": "199001"}]),
        ]
    )
    params = _params(load_gw_swagger(document, service_id="00000007").operations[0])
    assert params["startYm"].example is None


def test_meta_block_absence_is_a_no_op() -> None:
    """메타 블록이 없는 문서(대다수 odcloud 문서)는 아무 영향도 받지 않는다."""
    without = load_gw_swagger(_meta_document([]), service_id="00000007")
    assert _params(without.operations[0])["startYm"].example is None
    # 메타 블록 자리에 엉뚱한 타입이 와도 조용히 무시한다.
    broken = _meta_document([])
    broken["swaggerOprtinVOs"] = "배열이 아님"
    assert load_gw_swagger(broken, service_id="00000007").operations[0].parameters


def test_meta_block_matches_by_path_segment_when_operation_id_is_korean() -> None:
    """한글 operationId 라 식별자가 경로 슬러그로 떨어져도 경로로 매칭된다."""
    document = _meta_document(
        [_meta_block("getMetaList", [{"paramtrNm": "startYm", "paramtrBassValue": "202601"}])]
    )
    document["paths"]["/getMetaList"]["get"]["operationId"] = "목록조회"
    source = load_gw_swagger(document, service_id="00000007")
    assert source.operations[0].operation_id == "op_get_getMetaList"
    assert _params(source.operations[0])["startYm"].example == "202601"


# ---------------------------------------------------------------------------
# 16. 정보량 판정 보강 (2026-08-09 Advisor 검증 A2·A3)
# ---------------------------------------------------------------------------
def _response_schema(schema: dict[str, Any]) -> dict[str, Any] | None:
    """합성 문서 1개를 흡수해 확정된 응답 스키마를 돌려준다(없으면 None)."""
    document = _swagger2(
        {"/x": {"get": {"operationId": "getX", "responses": _ok_response(schema)}}},
        host="apis.example.invalid",
    )
    return load_gw_swagger(document, service_id="00000007").operations[0].response_schema


@pytest.mark.parametrize(
    "schema",
    [
        # A2: 시그널 키가 '있기만' 하면 1점을 주던 탓에 빈 목록 껍데기가 강등을 피했다.
        {"type": "object", "properties": {}, "required": []},
        {"type": "object", "properties": {}, "enum": []},
        {"type": "object", "properties": {}, "allOf": []},
        {"type": "object", "properties": {}, "additionalProperties": {}},
        {"type": "object", "properties": {}, "format": ""},
    ],
)
def test_empty_signal_keys_do_not_rescue_a_shell_schema(schema: dict[str, Any]) -> None:
    """값이 빈 시그널 키는 선언이 아니다 — 껍데기는 그대로 강등된다(A2).

    ``required: []`` 는 "필수 필드가 하나도 없다"가 아니라 대개 도구가 붙인
    빈 자리다. 그것으로 1점을 주면 :func:`unresolved_schema_operations` 가
    빈 튜플을 돌려주고 샘플링·추론이 영원히 트리거되지 않아, 정찰 F-03 이
    고치려던 상태로 되돌아간다.
    """
    assert _response_schema(schema) is None


@pytest.mark.parametrize(
    "schema",
    [
        # additionalProperties: false 는 "추가 필드 금지"라는 유의미한 선언이다.
        {"type": "object", "properties": {}, "additionalProperties": False},
        # 값이 있는 시그널 키는 기존대로 인정한다(F-03 케이스 회귀 유지).
        {"type": "object", "properties": {}, "required": ["a"]},
    ],
)
def test_meaningful_signal_keys_still_count(schema: dict[str, Any]) -> None:
    """False·비어 있지 않은 값은 여전히 '선언'으로 센다(과잉 강등 방지)."""
    assert _response_schema(schema) is not None


@pytest.mark.parametrize(
    "schema",
    [
        # A3: draft-04 튜플 표기. 원소 타입을 선언했는데 0점으로 폐기됐다.
        {"type": "array", "items": [{"type": "string"}, {"type": "integer"}]},
        # type 생략 + 튜플 표기(실효 타입 폴백까지 같은 인정 기준이어야 한다).
        {"items": [{"type": "object", "properties": {"id": {"type": "string"}}}]},
    ],
)
def test_tuple_form_items_are_recognized(schema: dict[str, Any]) -> None:
    """배열 ``items``(draft-04 튜플 표기)도 원소 선언으로 인정한다(A3).

    Swagger 2.0 은 draft-04 기반이라 ``"items": [ ... ]`` 가 유효한 표기다.
    매핑만 보던 판정은 원소 타입을 또박또박 선언한 배열 스키마를 통째로
    버렸다(``response_schema=None``).
    """
    resolved = _response_schema(schema)
    assert resolved is not None
    assert isinstance(resolved["items"], list)


@pytest.mark.parametrize(
    "schema",
    [
        {"type": "array", "items": []},
        {"type": "array", "items": [{}, {"properties": {}}]},
    ],
)
def test_tuple_form_items_without_declarations_still_demote(
    schema: dict[str, Any],
) -> None:
    """튜플 표기여도 원소가 아무것도 선언하지 않으면 0점 그대로다."""
    assert _response_schema(schema) is None


# ---------------------------------------------------------------------------
# F-08 - SourceSpec.key_location
# ---------------------------------------------------------------------------
def _minimal_source(**overrides: Any) -> SourceSpec:
    """불변식을 만족하는 최소 SourceSpec 을 만든다(합성 기관·.invalid 도메인)."""
    fields: dict[str, Any] = {
        "provider": "data.go.kr",
        "service_id": "99900001",
        "service_name": "가상행정연구원 공개자료 서비스",
        "base_url": "https://apis.example.invalid/9990000/demo",
        "source_kind": SourceKind.GW_SWAGGER,
        "operations": (
            OperationSpec(operation_id="getDemoList", method="GET", path="/getDemoList"),
        ),
    }
    fields.update(overrides)
    return SourceSpec(**fields)


def test_key_location_defaults_to_query() -> None:
    """인증키 주입 위치의 기본값은 질의문자열이다(기존 소스 전부 그대로)."""
    assert _minimal_source().key_location == "query"


def test_swagger_adapter_yields_query_key_location() -> None:
    """어댑터가 만든 소스도 기본값을 그대로 물려받는다(자동 승격 없음).

    스펙 문서의 ``securityDefinitions`` 를 읽어 헤더로 바꾸는 경로는 만들지
    않는다 - 인증 방식을 문서 선언이 바꾸게 두면 I3(인증키 격리)의 전제가 흔들린다.
    """
    source = load_gw_swagger(
        _fixture("sources_gw_swagger_v2.json"), service_id="99900002", fetched_at=None
    )
    assert source.key_location == "query"


def test_key_location_accepts_header() -> None:
    """헤더 인증 제공자를 흡수할 수 있게 'header' 는 허용값이다."""
    assert _minimal_source(key_location="header").key_location == "header"


@pytest.mark.parametrize("value", ["headers", "Header", "", "cookie", "QUERY"])
def test_unknown_key_location_is_rejected(value: str) -> None:
    """허용값 밖의 위치는 생성 시점에 막는다(허용값 목록을 함께 알린다)."""
    with pytest.raises(SourceSpecError) as excinfo:
        _minimal_source(key_location=value)
    assert "key_location" in str(excinfo.value)
    assert "query, header" in str(excinfo.value)


def test_key_location_is_validated_on_replace() -> None:
    """``dataclasses.replace`` 로 나중에 바꾸는 경로도 같은 규칙을 지난다.

    이 값은 어댑터가 아니라 프리셋 래퍼·프로파일이 나중에 명시하므로, 검사가
    어댑터의 불변식 검증에만 있으면 실제 설정 경로가 통째로 검사를 비껴간다.
    """
    source = _minimal_source()
    assert dataclasses.replace(source, key_location="header").key_location == "header"
    with pytest.raises(SourceSpecError):
        dataclasses.replace(source, key_location="body")
