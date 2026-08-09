# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Yong Park
"""compiler.openapi 테스트: OpenAPI 3.1 배치·키 비노출·결정론 직렬화.

네트워크·인증키·실데이터를 쓰지 않는다. ``SourceSpec`` 계열은 픽스처 파일 대신
이 파일 안에서 생성자로 직접 조립한다(가상 기관·``.invalid`` 도메인).
"""

from __future__ import annotations

import codecs
import importlib.util
import json
from pathlib import Path
from types import ModuleType
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
    infer_schema_with_report,
    load_source,
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
    # 파라미터 레벨 example 은 그대로 두되(OpenAPI 3.1 표준 위치), 스키마 안에도
    # JSON Schema 2020-12 의 `examples` 로 같은 값을 싣는다 — FastMCP 는
    # parameters[].schema 만 도구 입력 스키마로 옮기므로 이것이 없으면 큐레이션한
    # 예시값이 LLM 에게 0개 도달한다(적대 리뷰 F2).
    assert page["schema"] == {"type": "integer", "examples": [1]}
    # 선언 타입에 맞게 캐스팅되어 실린다(단수 키는 파라미터 레벨에만).
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


# ---------------------------------------------------------------------------
# ⑭ 자유문자열 게이트가 오퍼레이션·파라미터까지 (2026-08-09 Advisor 검증 A4)
# ---------------------------------------------------------------------------
_SYNTHETIC_LEAK = (
    "호출 예시: https://apis.example.invalid/demo/getDemoList"
    "?serviceKey=ab12%2BCD%2F34%3D%3D&pageNo=1"
)


@pytest.mark.parametrize(
    ("build_source", "expected"),
    [
        (
            lambda: _source((_operation(summary=_SYNTHETIC_LEAK),)),
            "summary",
        ),
        (
            lambda: _source((_operation(description=_SYNTHETIC_LEAK),)),
            "description",
        ),
        (
            lambda: _source(
                (_operation(parameters=(_param("pageNo", description=_SYNTHETIC_LEAK),)),)
            ),
            "pageNo",
        ),
    ],
)
def test_free_text_gate_covers_operation_and_parameter_text(
    build_source: Any, expected: str
) -> None:
    """오퍼레이션·파라미터 설명문의 인증키 대입도 산출 전에 막는다.

    게이트가 ``info.title`` · ``info.description`` · ``source_url`` ·
    ``license_note`` 4필드만 보던 시절에는, 큐레이션이 없는 경로
    (``curated=False`` · ``curation.json`` 부재)에서 원 스펙의 호출 예시가
    ``build_openapi`` · ``dumps`` 를 그대로 통과했다. 파일로 쓰는 CLI 경로만
    ``write_spec`` 이 막고 라이브러리 경로(문서 → FastMCP 도구 설명)는
    무방비였다.
    """
    with pytest.raises(CompileError) as excinfo:
        build_openapi(build_source())
    message = str(excinfo.value)
    assert "serviceKey" in message
    assert "getDemoList" in message
    assert expected in message


def test_free_text_gate_leaves_clean_operation_text_alone() -> None:
    """자격증명이 없는 설명문은 그대로 통과한다(과잉 차단 방지)."""
    clean = "조회 기간을 YYYYMM 6자리로 넣는다(예: 202601)."
    operation = _operation(
        summary="가상 자료 목록 조회",
        description=clean,
        parameters=(_param("pageNo", description=clean),),
    )
    document = build_openapi(_source((operation,))).document
    entry = document["paths"]["/getDemoList"]["get"]
    assert entry["description"] == clean
    assert entry["parameters"][0]["description"] == clean


# ---------------------------------------------------------------------------
# ⑮ 커밋된 데모 산출물 드리프트 (2026-08-09 Advisor 검증 A5)
# ---------------------------------------------------------------------------
REPO_ROOT = Path(__file__).resolve().parents[1]

#: ``examples/compile_demo.py`` 의 ``main()`` 이 ``load_source`` 에 넘기는 값.
#: 모듈 상수가 아니라 호출 인자라 여기서만 복제한다(아래 회귀가 값까지 대조한다).
_DEMO_SOURCE_URL = "https://example.invalid/demo/openapi.json"


def _load_compile_demo() -> ModuleType:
    """``examples/compile_demo.py`` 를 경로 기반 스펙 로드로 읽는다.

    ``examples/`` 는 패키지가 아니라 ``sys.path`` 에 없고, ``sys.path`` 를 건드리면
    다른 테스트에 부작용이 남는다. 스크립트는 ``__main__`` 가드가 있어 임포트만
    으로는 아무 파일도 쓰지 않으므로, 상수·함수만 안전하게 가져올 수 있다.
    """
    path = REPO_ROOT / "examples" / "compile_demo.py"
    spec = importlib.util.spec_from_file_location("mcportal_compile_demo", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_committed_demo_spec_matches_current_compiler_output() -> None:
    """커밋된 ``specs/demo/openapi.json`` 이 현행 컴파일러 산출과 바이트까지 같다.

    데모 산출물은 사람이 여는 유일한 '완성품 예시'인데, 컴파일러가 바뀌어도
    아무도 재생성을 강제하지 않아 낡은 채로 커밋에 남았다(F2 처방 이전 산출물).
    이 회귀는 파일을 쓰지 않고 인메모리 재생성 결과와만 대조하므로, 드리프트가
    생기면 ``examples/compile_demo.py`` 를 다시 돌리라는 신호가 된다.
    """
    demo = _load_compile_demo()
    committed_path = REPO_ROOT / "specs" / "demo" / "openapi.json"
    committed = committed_path.read_bytes()

    source = load_source(
        demo.DEMO_SWAGGER,
        service_id=demo.SERVICE_ID,
        service_name=demo.SERVICE_NAME,
        source_url=_DEMO_SOURCE_URL,
        fetched_at=demo.FETCHED_AT,
    )
    schema, report = infer_schema_with_report(demo.DEMO_SAMPLES)
    compiled = build_openapi(
        source,
        {"getDemoList": schema},
        options=CompileOptions(generation_mode="sampled"),
        reports={"getDemoList": report},
    )

    assert dumps(compiled.document).encode("utf-8") == committed
    # 복제한 source_url 이 실제 산출물의 값과 어긋나면 위 대조가 무의미해진다.
    assert compiled.document["info"][X_MCPORTAL]["source_url"] == _DEMO_SOURCE_URL


def test_committed_demo_spec_carries_schema_level_examples() -> None:
    """데모 산출물이 F2 처방(스키마 안 examples)을 반영한 최신본인지 못 박는다."""
    document = json.loads(
        (REPO_ROOT / "specs" / "demo" / "openapi.json").read_text(encoding="utf-8")
    )
    parameters = document["paths"]["/getDemoList"]["get"]["parameters"]
    by_name = {param["name"]: param for param in parameters}
    assert by_name["pageNo"]["schema"]["examples"] == [1]
    assert by_name["numOfRows"]["schema"]["examples"] == [10]


# ---------------------------------------------------------------------------
# W4 §5 - 큐레이션 없는 경로의 파라미터 '값' 자유문자열 게이트
# ---------------------------------------------------------------------------
_LEAKY = "https://apis.example.invalid/demo?serviceKey=ab12%2BCD%2F34%3D%3D&pageNo=1"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("description", _LEAKY),
        ("example", _LEAKY),
        ("default", _LEAKY),
    ],
)
def test_parameter_value_fields_with_key_assignment_are_rejected(
    field: str, value: str
) -> None:
    """파라미터의 설명뿐 아니라 example·default 도 인증키 대입을 통과시키지 않는다.

    큐레이션 병합 게이트(V8)는 처음부터 값 필드까지 훑었는데 큐레이션 없는
    경로의 게이트는 설명문 3자리만 봤다. 그 비대칭 때문에 원 스펙이 호출 예시
    URL 을 파라미터 예시로 들고 있으면 산출 문서의 ``schema.examples`` 까지
    그대로 실렸다(W4 §5).
    """
    param = _param("targetYm", **{field: value})
    source = _source((_operation(parameters=(param,)),))
    with pytest.raises(CompileError) as excinfo:
        build_openapi(source)
    message = str(excinfo.value)
    assert "serviceKey" in message
    assert field in message
    assert "targetYm" in message


def test_parameter_enum_with_key_assignment_is_rejected() -> None:
    """열거값에 숨은 인증키 대입도 막는다(enum 은 도구 인자 후보로 그대로 노출된다)."""
    param = _param("mode", required=False, enum=("json", _LEAKY))
    source = _source((_operation(parameters=(param,)),))
    with pytest.raises(CompileError, match="enum"):
        build_openapi(source)


def test_operation_tag_with_key_assignment_is_rejected() -> None:
    """태그도 도구 설명에 실리는 자유문자열이므로 같은 게이트를 지난다."""
    source = _source((_operation(tags=("정상태그", _LEAKY)),))
    with pytest.raises(CompileError, match="tags"):
        build_openapi(source)


def test_clean_parameter_values_still_compile() -> None:
    """게이트 확장이 정상 값(예시·기본값·열거값)을 잡아채지 않는다.

    게이트를 넓히면 오탐으로 기존 프리셋 컴파일을 깨뜨릴 수 있다. 자격증명
    이름이 **값 없이** 문장에 등장하는 경우까지 막으면 안 된다는 것도 함께 본다
    (``find_key_assignments`` 는 값이 붙은 대입만 탐지한다).
    """
    param = _param(
        "pageNo",
        required=False,
        type_="integer",
        description="serviceKey 는 트랜스포트가 주입하므로 인자가 아니다.",
        example="1",
        default="1",
        enum=("1", "2"),
    )
    compiled = build_openapi(_source((_operation(parameters=(param,)),)))
    listed = compiled.document["paths"]["/getDemoList"]["get"]["parameters"]
    assert listed[0]["schema"]["examples"] == [1]
