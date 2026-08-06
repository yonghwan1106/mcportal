# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Yong Park
"""compiler.openapi 테스트: OpenAPI 3.1 배치·키 비노출·결정론 직렬화.

네트워크·인증키·실데이터를 쓰지 않는다. ``SourceSpec`` 계열은 픽스처 파일 대신
이 파일 안에서 생성자로 직접 조립한다(가상 기관·``.invalid`` 도메인).
"""

from __future__ import annotations

import codecs
from pathlib import Path
from typing import Any

import pytest

from mcportal.compiler import (
    CompileError,
    CompileOptions,
    InferenceReport,
    OperationSpec,
    ParamSpec,
    SourceKind,
    SourceSpec,
    build_openapi,
    dumps,
    write_spec,
)
from mcportal.compiler.openapi import (
    OPENAPI_VERSION,
    X_MCPORTAL,
    DEFAULT_OPTIONS,
    schema_name_for,
)

BASE_URL = "https://apis.example.invalid/9990000/demo"
SERVICE_NAME = "가상행정연구원 공개자료 서비스"


def _param(
    name: str,
    *,
    location: str = "query",
    required: bool = True,
    type_: str = "string",
    description: str | None = None,
    example: str | None = None,
    enum: tuple[str, ...] = (),
    default: str | None = None,
    item_type: str | None = None,
) -> ParamSpec:
    """테스트용 ParamSpec 을 만든다."""
    return ParamSpec(
        name=name,
        location=location,
        required=required,
        type=type_,
        description=description,
        example=example,
        enum=enum,
        default=default,
        item_type=item_type,
    )


def _operation(
    operation_id: str = "getDemoList",
    *,
    method: str = "GET",
    path: str = "/getDemoList",
    summary: str | None = "가상 자료 목록 조회",
    description: str | None = None,
    parameters: tuple[ParamSpec, ...] = (),
    response_media_type: str = "application/json",
    response_schema: dict[str, Any] | None = None,
    request_body_schema: dict[str, Any] | None = None,
    tags: tuple[str, ...] = (),
    deprecated: bool = False,
) -> OperationSpec:
    """테스트용 OperationSpec 을 만든다."""
    return OperationSpec(
        operation_id=operation_id,
        method=method,
        path=path,
        summary=summary,
        description=description,
        parameters=parameters,
        response_media_type=response_media_type,
        response_schema=response_schema,
        request_body_schema=request_body_schema,
        tags=tags,
        deprecated=deprecated,
    )


def _source(
    operations: tuple[OperationSpec, ...] | None = None,
    **overrides: Any,
) -> SourceSpec:
    """테스트용 SourceSpec 을 만든다(합성 기관·.invalid 도메인)."""
    fields: dict[str, Any] = {
        "provider": "data.go.kr",
        "service_id": "99900001",
        "service_name": SERVICE_NAME,
        "base_url": BASE_URL,
        "source_kind": SourceKind.GW_SWAGGER,
        "operations": operations if operations is not None else (_operation(),),
        "key_param": "serviceKey",
        "source_url": "https://example.invalid/openapi.json",
        "fingerprint": "sha256:" + "ab" * 32,
        "fetched_at": None,
        "description": "가상 기관이 제공하는 합성 예시 서비스입니다.",
        "license_note": "KOGL 제1유형(예시)",
    }
    fields.update(overrides)
    return SourceSpec(**fields)


def _force_operations(
    source: SourceSpec, operations: tuple[OperationSpec, ...]
) -> SourceSpec:
    """IR 단계 방어를 우회해 operations 를 갈아끼운다.

    ``sources.py`` 가 생성 시점에 불변식을 강제하므로 '깨진 IR'은 정상 경로로는
    만들 수 없다. openapi.py 의 **2차 방어선**이 실제로 작동하는지 보려면 그
    1차 방어선을 우회해야 하므로, frozen dataclass 의 속성을 직접 덮어쓴다.
    """
    object.__setattr__(source, "operations", operations)
    return source


# ---------------------------------------------------------------------------
# ① 최소 SourceSpec → 문서 골격
# ---------------------------------------------------------------------------
def test_minimal_source_builds_openapi_31_skeleton() -> None:
    compiled = build_openapi(_source())
    document = compiled.document

    assert document["openapi"] == "3.1.0" == OPENAPI_VERSION
    assert document["info"]["title"] == SERVICE_NAME
    assert document["info"]["version"] == DEFAULT_OPTIONS.version
    assert document["servers"] == [{"url": BASE_URL}]
    assert compiled.operation_ids == ("getDemoList",)


def test_options_override_title_and_server_url() -> None:
    options = CompileOptions(
        title="다른 제목", version="9.9.9", server_url="https://other.invalid/svc"
    )
    document = build_openapi(_source(), options=options).document
    assert document["info"]["title"] == "다른 제목"
    assert document["info"]["version"] == "9.9.9"
    assert document["servers"] == [{"url": "https://other.invalid/svc"}]


# ---------------------------------------------------------------------------
# ② paths·operationId·소문자 메서드·200 응답 참조
# ---------------------------------------------------------------------------
def test_paths_and_response_reference_placement() -> None:
    operations = (
        _operation("getDemoItem", path="/getDemoItem", response_media_type="application/xml"),
        _operation("postDemoItem", method="POST", path="/getDemoItem"),
    )
    compiled = build_openapi(_source(operations))
    document = compiled.document

    item = document["paths"]["/getDemoItem"]
    assert set(item) == {"get", "post"}  # 같은 path 아래 메서드별로 담긴다.
    assert item["get"]["operationId"] == "getDemoItem"
    assert item["post"]["operationId"] == "postDemoItem"

    responses = item["get"]["responses"]
    assert responses["200"]["description"] == "정상 응답"
    # XML 을 선언한 소스라도 200 content 키는 application/json 이다. 런타임
    # 브리지가 정규화 JSON 으로 바꿔 돌려주기 때문이며, 원 선언은 메타에 남는다.
    assert set(responses["200"]["content"]) == {"application/json"}
    schema_ref = responses["200"]["content"]["application/json"]["schema"]
    assert schema_ref == {"$ref": "#/components/schemas/GetDemoItemResponse"}
    assert responses["200"]["x-mcportal"] == {"upstream_media_type": "application/xml"}
    # JSON 소스는 선언과 실제가 같으므로 메타를 덧붙이지 않는다.
    assert "x-mcportal" not in item["post"]["responses"]["200"]
    # 정렬 순서: 같은 path 안에서는 메서드 오름차순(GET < POST).
    assert compiled.operation_ids == ("getDemoItem", "postDemoItem")


def test_operations_are_ordered_by_path_then_method() -> None:
    operations = (
        _operation("zebra", path="/zebra"),
        _operation("alpha", path="/alpha"),
    )
    compiled = build_openapi(_source(operations))
    assert compiled.operation_ids == ("alpha", "zebra")


# ---------------------------------------------------------------------------
# ③ 참조 무결성: $ref 대상이 components.schemas 에 실재
# ---------------------------------------------------------------------------
def test_every_reference_resolves_in_components() -> None:
    operations = (
        _operation("getDemoList", path="/getDemoList"),
        _operation("getDemoItem", path="/getDemoItem"),
    )
    compiled = build_openapi(_source(operations))
    schemas = compiled.document["components"]["schemas"]

    refs: list[str] = []
    for path_item in compiled.document["paths"].values():
        for operation in path_item.values():
            content = operation["responses"]["200"]["content"]
            for media in content.values():
                refs.append(media["schema"]["$ref"])
    assert len(refs) == 2
    for ref in refs:
        name = ref.rsplit("/", 1)[-1]
        assert name in schemas, f"참조 대상이 없다: {ref}"
    assert compiled.schema_names == tuple(sorted(schemas))


# ---------------------------------------------------------------------------
# ④ 파라미터 배치
# ---------------------------------------------------------------------------
def test_parameter_placement_covers_enum_default_example_and_array() -> None:
    parameters = (
        _param(
            "pageNo",
            required=True,
            type_="integer",
            description="페이지 번호",
            example="1",
        ),
        _param(
            "sido",
            required=False,
            type_="string",
            enum=("세종", "가상시", "무명군"),
            default="세종",
        ),
        _param("codes", required=False, type_="array", item_type="integer"),
    )
    document = build_openapi(_source((_operation(parameters=parameters),))).document
    listed = document["paths"]["/getDemoList"]["get"]["parameters"]

    assert [item["name"] for item in listed] == ["pageNo", "sido", "codes"]
    page = listed[0]
    assert page["in"] == "query"
    assert page["required"] is True
    assert page["schema"] == {"type": "integer"}
    # 스키마 안이 아니라 파라미터 레벨. 선언 타입에 맞게 캐스팅되어 실린다.
    assert page["example"] == 1
    assert "example" not in page["schema"]
    assert page["description"] == "페이지 번호"

    sido = listed[1]
    assert sido["required"] is False
    # enum 은 소스가 준 원 순서를 보존한다(알파벳 정렬하지 않는다).
    assert sido["schema"]["enum"] == ["세종", "가상시", "무명군"]
    assert sido["schema"]["default"] == "세종"

    codes = listed[2]
    assert codes["schema"] == {"type": "array", "items": {"type": "integer"}}


def test_array_parameter_without_item_type_falls_back_to_string() -> None:
    operation = _operation(parameters=(_param("codes", type_="array"),))
    document = build_openapi(_source((operation,))).document
    listed = document["paths"]["/getDemoList"]["get"]["parameters"]
    assert listed[0]["schema"] == {"type": "array", "items": {"type": "string"}}


def test_operation_without_parameters_omits_the_key() -> None:
    document = build_openapi(_source()).document
    assert "parameters" not in document["paths"]["/getDemoList"]["get"]


def test_tags_are_sorted_and_request_body_is_passed_through() -> None:
    operation = _operation(
        "postDemo",
        method="POST",
        path="/postDemo",
        tags=("zeta", "alpha"),
        request_body_schema={"type": "object", "properties": {"page": {"type": "integer"}}},
    )
    document = build_openapi(_source((operation,))).document
    entry = document["paths"]["/postDemo"]["post"]
    assert entry["tags"] == ["alpha", "zeta"]
    body = entry["requestBody"]["content"]["application/json"]["schema"]
    assert body == {"type": "object", "properties": {"page": {"type": "integer"}}}


# ---------------------------------------------------------------------------
# ⑤ 키 파라미터 방어(I3 2차 방어선)
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("name", ["serviceKey", "SERVICEKEY", "ServiceKey"])
def test_key_parameter_in_operation_raises_compile_error(name: str) -> None:
    broken = _operation(parameters=(_param(name),))
    source = _force_operations(_source(), (broken,))
    with pytest.raises(CompileError) as excinfo:
        build_openapi(source)
    message = str(excinfo.value)
    assert "인증키" in message
    assert "getDemoList" in message


# ---------------------------------------------------------------------------
# ⑥ security 미노출 + key_injection 사실 기록
# ---------------------------------------------------------------------------
def test_document_has_no_security_and_declares_transport_injection() -> None:
    compiled = build_openapi(_source())
    document = compiled.document

    assert "security" not in document
    assert "securitySchemes" not in document.get("components", {})
    assert document["info"][X_MCPORTAL]["key_injection"] == "transport"
    # 문서 전문에 인증키 파라미터 이름이 등장하지 않는다.
    assert "serviceKey" not in dumps(document)


# ---------------------------------------------------------------------------
# ⑦ x-mcportal 메타 전 필드
# ---------------------------------------------------------------------------
def test_x_mcportal_metadata_fields() -> None:
    report = InferenceReport(
        sample_count=3,
        property_count=7,
        max_depth_seen=3,
        truncated=True,
        conflicts=(),
    )
    options = CompileOptions(generation_mode="sampled")
    compiled = build_openapi(
        _source(),
        {"getDemoList": {"type": "object"}},
        options=options,
        reports={"getDemoList": report},
    )
    meta = compiled.document["info"][X_MCPORTAL]

    assert meta["tool_version"]  # mcportal.__version__ 이 실린다.
    assert meta["provider"] == "data.go.kr"
    assert meta["service_id"] == "99900001"
    assert meta["source_kind"] == SourceKind.GW_SWAGGER.value
    assert meta["source_fingerprint"] == "sha256:" + "ab" * 32
    assert meta["source_url"] == "https://example.invalid/openapi.json"
    assert meta["license_note"] == "KOGL 제1유형(예시)"
    assert meta["generation_mode"] == "sampled"
    assert meta["sample_count"] == 3
    assert meta["schema_inference"] == {"conflicts": 0, "truncated": True}
    assert "unresolved" not in meta["schema_inference"]


def test_optional_metadata_keys_are_omitted_when_absent() -> None:
    compiled = build_openapi(
        _source(source_url=None, license_note=None, description=None)
    )
    meta = compiled.document["info"][X_MCPORTAL]
    assert "source_url" not in meta
    assert "license_note" not in meta
    assert "description" not in compiled.document["info"]
    assert meta["generation_mode"] == "offline"
    assert meta["sample_count"] == 0


def test_reports_aggregate_conflicts_and_sample_counts() -> None:
    from mcportal.compiler import TypeConflict

    operations = (
        _operation("opA", path="/a"),
        _operation("opB", path="/b"),
    )
    reports = {
        "opA": InferenceReport(
            sample_count=3,
            property_count=2,
            max_depth_seen=1,
            truncated=False,
            conflicts=(TypeConflict(pointer="#", types=("integer", "number"), resolution="promoted:number"),),
        ),
        "opB": InferenceReport(
            sample_count=2,
            property_count=1,
            max_depth_seen=1,
            truncated=False,
            conflicts=(),
        ),
    }
    compiled = build_openapi(
        _source(operations),
        {"opA": {"type": "object"}, "opB": {"type": "object"}},
        reports=reports,
    )
    meta = compiled.document["info"][X_MCPORTAL]
    assert meta["sample_count"] == 5  # 전 오퍼레이션 합.
    assert meta["schema_inference"]["conflicts"] == 1


# ---------------------------------------------------------------------------
# ⑧ 시각 비유출
# ---------------------------------------------------------------------------
def test_fetched_at_never_leaks_into_document() -> None:
    fetched = "2091-12-31T23:59:59+09:00"
    compiled = build_openapi(_source(fetched_at=fetched))
    serialized = dumps(compiled.document)
    assert fetched not in serialized
    assert "2091" not in serialized
    assert "23:59:59" not in serialized


# ---------------------------------------------------------------------------
# ⑨ 결정론 ① — 같은 입력 2회 컴파일 → 동일 문자열
# ---------------------------------------------------------------------------
def test_compilation_is_deterministic() -> None:
    operations = (
        _operation("getDemoList", path="/getDemoList", parameters=(_param("pageNo", type_="integer"),)),
        _operation("getDemoItem", path="/getDemoItem", tags=("b", "a")),
    )
    first = build_openapi(_source(operations), reports=None)
    second = build_openapi(_source(operations), reports=None)
    assert dumps(first.document) == dumps(second.document)
    assert first.operation_ids == second.operation_ids
    assert first.schema_names == second.schema_names


def test_dumps_is_sorted_and_ends_with_single_newline() -> None:
    text = dumps(build_openapi(_source()).document)
    assert text.endswith("\n")
    assert not text.endswith("\n\n")
    # sort_keys=True 이므로 최상위 키가 알파벳 순서로 나온다.
    top_keys = [
        line.strip().split('"')[1]
        for line in text.splitlines()
        if line.startswith('  "')
    ]
    assert top_keys == sorted(top_keys)


# ---------------------------------------------------------------------------
# ⑩ 결정론 ② — write_spec 2회 → 파일 바이트 동일
# ---------------------------------------------------------------------------
def test_write_spec_is_byte_identical_and_lf_only(tmp_path: Path) -> None:
    document = build_openapi(_source()).document
    target = tmp_path / "nested" / "openapi.json"

    path_one = write_spec(document, target)
    first = target.read_bytes()
    path_two = write_spec(build_openapi(_source()).document, target)
    second = target.read_bytes()

    assert path_one == path_two == target
    assert first == second
    assert b"\r" not in first  # CRLF 혼입 없음.
    assert first.endswith(b"\n") and not first.endswith(b"\n\n")
    assert not first.startswith(codecs.BOM_UTF8)
    assert first.decode("utf-8") == dumps(document)


# ---------------------------------------------------------------------------
# ⑪ 스키마 우선순위 3단 + unresolved 카운트
# ---------------------------------------------------------------------------
def test_schema_priority_inferred_then_source_then_fallback() -> None:
    operations = (
        _operation("opInferred", path="/a"),
        _operation("opFromSource", path="/b", response_schema={"type": "object", "title": "소스"}),
        _operation("opFallback", path="/c"),
    )
    compiled = build_openapi(
        _source(operations),
        {"opInferred": {"type": "object", "properties": {"x": {"type": "string"}}}},
    )
    schemas = compiled.document["components"]["schemas"]

    assert schemas["OpInferredResponse"] == {
        "type": "object",
        "properties": {"x": {"type": "string"}},
    }
    assert schemas["OpFromSourceResponse"] == {"type": "object", "title": "소스"}
    assert schemas["OpFallbackResponse"] == {
        "type": "object",
        "description": "응답 스키마 미확정(샘플링 미수행)",
    }
    assert compiled.document["info"][X_MCPORTAL]["schema_inference"]["unresolved"] == 1


def test_inferred_schema_wins_over_source_schema() -> None:
    operation = _operation(response_schema={"type": "object", "title": "소스"})
    compiled = build_openapi(
        _source((operation,)), {"getDemoList": {"type": "array"}}
    )
    schemas = compiled.document["components"]["schemas"]
    assert schemas["GetDemoListResponse"] == {"type": "array"}
    assert "unresolved" not in compiled.document["info"][X_MCPORTAL]["schema_inference"]


def test_provided_schema_is_copied_not_aliased() -> None:
    provided: dict[str, Any] = {"type": "object", "properties": {}}
    compiled = build_openapi(_source(), {"getDemoList": provided})
    provided["properties"]["오염"] = {"type": "string"}
    assert compiled.document["components"]["schemas"]["GetDemoListResponse"] == {
        "type": "object",
        "properties": {},
    }


# ---------------------------------------------------------------------------
# ⑫ schema_name_for 충돌 → _2
# ---------------------------------------------------------------------------
def test_schema_name_for_basic_and_prefix() -> None:
    assert schema_name_for("getDemoList") == "GetDemoListResponse"
    assert schema_name_for("get_demo_list") == "GetDemoListResponse"
    assert schema_name_for("getDemoList", "Demo") == "DemoGetDemoListResponse"
    assert schema_name_for("getDemoList", "de-mo!") == "demoGetDemoListResponse"


def test_schema_name_collision_gets_numeric_suffix() -> None:
    operations = (
        _operation("getDemoList", path="/a"),
        _operation("get_demo_list", path="/b"),
    )
    compiled = build_openapi(_source(operations))
    assert compiled.schema_names == ("GetDemoListResponse", "GetDemoListResponse_2")
    refs = {
        operation["operationId"]: operation["responses"]["200"]["content"][
            "application/json"
        ]["schema"]["$ref"]
        for path_item in compiled.document["paths"].values()
        for operation in path_item.values()
    }
    assert refs["getDemoList"].endswith("/GetDemoListResponse")
    assert refs["get_demo_list"].endswith("/GetDemoListResponse_2")


def test_schema_name_prefix_option_is_applied() -> None:
    compiled = build_openapi(
        _source(), options=CompileOptions(schema_name_prefix="Demo")
    )
    assert compiled.schema_names == ("DemoGetDemoListResponse",)


# ---------------------------------------------------------------------------
# ⑬ include_deprecated=False
# ---------------------------------------------------------------------------
def test_include_deprecated_false_drops_operation_and_schema() -> None:
    operations = (
        _operation("getDemoList", path="/a"),
        _operation("getOldList", path="/b", deprecated=True),
    )
    kept = build_openapi(_source(operations), options=CompileOptions(include_deprecated=False))
    assert kept.operation_ids == ("getDemoList",)
    assert kept.schema_names == ("GetDemoListResponse",)
    assert "/b" not in kept.document["paths"]

    both = build_openapi(_source(operations))
    assert both.operation_ids == ("getDemoList", "getOldList")
    assert both.document["paths"]["/b"]["get"]["deprecated"] is True
    assert "deprecated" not in both.document["paths"]["/a"]["get"]


def test_all_deprecated_with_exclusion_raises_compile_error() -> None:
    operations = (_operation("getOldList", path="/b", deprecated=True),)
    with pytest.raises(CompileError) as excinfo:
        build_openapi(_source(operations), options=CompileOptions(include_deprecated=False))
    assert "오퍼레이션이 없습니다" in str(excinfo.value)


# ---------------------------------------------------------------------------
# ⑭ 한국어가 \uXXXX 로 이스케이프되지 않는다
# ---------------------------------------------------------------------------
def test_korean_text_is_not_escaped(tmp_path: Path) -> None:
    operation = _operation(description="세종특별자치시 가상 자료를 돌려준다.")
    document = build_openapi(_source((operation,))).document
    text = dumps(document)
    assert "세종특별자치시" in text
    assert "\\u" not in text

    target = write_spec(document, tmp_path / "openapi.json")
    assert "세종특별자치시" in target.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# ⑮ 불변식 위반 → CompileError
# ---------------------------------------------------------------------------
def test_empty_operations_raise_compile_error() -> None:
    source = _force_operations(_source(), ())
    with pytest.raises(CompileError) as excinfo:
        build_openapi(source)
    assert "오퍼레이션이 없습니다" in str(excinfo.value)


def test_non_ascii_operation_id_raises_compile_error() -> None:
    source = _force_operations(_source(), (_operation("목록조회"),))
    with pytest.raises(CompileError) as excinfo:
        build_openapi(source)
    assert "operation_id" in str(excinfo.value)


def test_path_without_leading_slash_raises_compile_error() -> None:
    source = _force_operations(_source(), (_operation(path="getDemoList"),))
    with pytest.raises(CompileError) as excinfo:
        build_openapi(source)
    assert "'/'" in str(excinfo.value)


def test_duplicate_operation_id_raises_compile_error() -> None:
    operations = (
        _operation("getDemoList", path="/a"),
        _operation("getDemoList", path="/b"),
    )
    source = _force_operations(_source(), operations)
    with pytest.raises(CompileError) as excinfo:
        build_openapi(source)
    assert "중복" in str(excinfo.value)


def test_missing_title_raises_compile_error() -> None:
    with pytest.raises(CompileError) as excinfo:
        build_openapi(_source(service_name=""))
    assert "info.title" in str(excinfo.value)


def test_missing_server_url_raises_compile_error() -> None:
    with pytest.raises(CompileError) as excinfo:
        build_openapi(_source(base_url=""))
    assert "servers" in str(excinfo.value)


def test_unserializable_schema_raises_compile_error() -> None:
    operation = _operation(response_schema={"type": object()})  # type: ignore[dict-item]
    with pytest.raises(CompileError) as excinfo:
        build_openapi(_source((operation,)))
    assert "JSON" in str(excinfo.value)
