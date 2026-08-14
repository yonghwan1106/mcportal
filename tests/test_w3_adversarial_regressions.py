# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Yong Park
"""적대 리뷰(W3, 2026-08-06)에서 재현된 결함들의 회귀 테스트.

W2 회귀는 :mod:`tests.test_adversarial_regressions` 가 담당하고, 이 모듈은 W3
산출물(스펙 소스 G2/G3 · 큐레이션 게이트 · CLI · 배포 설정 · 원문 증거물)에서
나온 발견만 고정한다. 각 테스트는 **수정을 되돌리면 반드시 실패하도록** 반대
방향 표본으로 썼다 — 기존 테스트들이 결함을 놓친 이유는 대부분 자기 선택 표본
이었기 때문이다(``type`` 이 항상 붙은 스키마, 오퍼레이션 1개짜리 GW 문서,
``curated=False`` 를 15000115 하나로만 확인한 프리셋 테스트, 형식만 보는
provenance 검사가 그렇다).

픽스처는 100% 합성이다. 가상 기관·``.invalid`` 도메인·명백한 더미 문자열만 쓰며
실인증키·실네트워크·실응답 데이터는 어디에도 없다. 실제 커밋된 프리셋 번들을
읽는 테스트는 파일을 읽기만 한다(네트워크 0건).
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import tempfile
import tomllib
import zipfile
from pathlib import Path
from typing import Any

import pytest

from mcportal import cli
from mcportal.compiler.curation import (
    CURATION_SCHEMA_VERSION,
    CurationError,
    apply_curation_with_report,
    compile_preset,
    load_curation,
    read_curation,
)
from mcportal.compiler.openapi import CompileError, build_openapi, dumps
from mcportal.compiler.sources import (
    OperationSpec,
    ParamSpec,
    SourceKind,
    SourceSpec,
    load_source,
    unresolved_schema_operations,
)
from mcportal.replay.scrub import (
    CREDENTIAL_PARAM_NAMES,
    find_key_assignments,
    is_credential_param,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
PRESETS_ROOT = REPO_ROOT / "presets"
PRESET_IDS = ("15000115", "15081808", "15101612", "15102108")

#: 합성 픽스처가 쓰는 가상 호스트(존재하지 않는 TLD).
HOST = "apis.example.invalid"


# ---------------------------------------------------------------------------
# 합성 픽스처
# ---------------------------------------------------------------------------
def _gw_document(
    paths: dict[str, Any], meta: list[dict[str, Any]] | None = None
) -> dict[str, Any]:
    """게이트웨이 Swagger 2.0 합성 문서(가상 기관 · .invalid 도메인)."""
    document: dict[str, Any] = {
        "swagger": "2.0",
        "info": {"title": "가상 통계 서비스"},
        "host": f"{HOST}/0000000/demo",
        "basePath": "",
        "schemes": ["https"],
        "paths": paths,
    }
    if meta is not None:
        document["swaggerOprtinVOs"] = meta
    return document


def _get_operation(operation_id: str, parameters: list[dict[str, Any]]) -> dict[str, Any]:
    """200 응답 스키마를 갖춘 GET 오퍼레이션 하나."""
    return {
        "get": {
            "operationId": operation_id,
            "parameters": parameters,
            "responses": {
                "200": {
                    "description": "성공",
                    "schema": {"type": "object", "properties": {"n": {"type": "integer"}}},
                }
            },
        }
    }


def _params(source: SourceSpec, index: int = 0) -> dict[str, ParamSpec]:
    """오퍼레이션 하나의 파라미터를 이름으로 인덱싱한다."""
    return {param.name: param for param in source.operations[index].parameters}


def _synthetic_source(
    *,
    operation_description: str | None = None,
    param_description: str | None = None,
) -> SourceSpec:
    """큐레이션 병합 테스트용 합성 :class:`SourceSpec`."""
    return SourceSpec(
        provider="data.go.kr",
        service_id="00000000",
        service_name="가상 합성 서비스",
        base_url=f"https://{HOST}/api",
        source_kind=SourceKind.GW_SWAGGER,
        key_param="serviceKey",
        operations=(
            OperationSpec(
                operation_id="getSynthList",
                method="GET",
                path="/getSynthList",
                description=operation_description,
                parameters=(
                    ParamSpec(name="flag", location="query", required=False, type="boolean"),
                    ParamSpec(
                        name="mode",
                        location="query",
                        required=False,
                        type="string",
                        description=param_description,
                    ),
                    ParamSpec(
                        name="rows",
                        location="query",
                        required=False,
                        type="integer",
                        description="행 수",
                    ),
                ),
                response_schema={"type": "object", "properties": {"n": {"type": "integer"}}},
            ),
        ),
    )


def _curation(operations: dict[str, Any] | None = None) -> Any:
    """합성 소스에 얹을 최소 큐레이션."""
    document: dict[str, Any] = {
        "mcportal_curation": CURATION_SCHEMA_VERSION,
        "preset_id": "00000000",
        "service": {"title": "가상 합성 서비스", "version": "1.0.0"},
    }
    if operations is not None:
        document["operations"] = operations
    return load_curation(document)


# ---------------------------------------------------------------------------
# F1(critical) — G2 정보량 판정이 `type` 없는 정상 스키마를 폐기했다
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("schema", "expect_kept"),
    [
        # `type` 생략 = JSON Schema/OpenAPI 3.x 의 정상 형태. 보존해야 한다.
        (
            {
                "properties": {
                    "resultCode": {"type": "string"},
                    "totalCount": {"type": "integer"},
                    "items": {
                        "type": "array",
                        "items": {"type": "object", "properties": {"id": {"type": "string"}}},
                    },
                }
            },
            True,
        ),
        # `type` 생략 + items 만 선언 = 배열
        ({"items": {"type": "string"}}, True),
        # 필드 이름만 선언(타입 없음) — 이름 자체가 선언이다
        ({"type": "object", "properties": {"totalCount": {}, "items": {}}}, True),
        ({"type": "object", "properties": {"a": {"description": "이름은 있다"}}}, True),
        # 선언된 필드가 0개인 껍데기 = 폐기(정찰 F-03 회귀는 유지된다)
        ({"type": "object", "properties": {}}, False),
        ({"type": "array"}, False),
        ({"description": "설명만 있다"}, False),
    ],
)
def test_schema_without_declared_type_is_not_discarded(
    schema: dict[str, Any], expect_kept: bool
) -> None:
    """``type`` 생략 스키마를 통째로 버리던 W2 대비 회귀를 고정한다.

    ``_declared_schema_type()`` 이 None 이면 object/array 분기를 모두 건너뛰고
    0점으로 떨어져 ``_resolved_schema`` 가 None 을 돌려줬다. v0.1.0 에서는
    보존되던 응답 구조가 W3 이후 사라졌던 자리다.

    폐기 판정이 남는 기준은 **선언된 필드가 0개**라는 사실 하나뿐이다.
    """
    document = _gw_document(
        {
            "/x": {
                "get": {
                    "operationId": "getX",
                    "responses": {"200": {"description": "성공", "schema": schema}},
                }
            }
        }
    )
    source = load_source(
        document, kind=SourceKind.GW_SWAGGER, service_id="00000000", key_param="serviceKey"
    )
    kept = source.operations[0].response_schema is not None
    assert kept is expect_kept
    assert unresolved_schema_operations(source) == (() if expect_kept else ("getX",))


# ---------------------------------------------------------------------------
# F2(major) — 큐레이션한 example 이 MCP 도구에 하나도 전달되지 않았다
# ---------------------------------------------------------------------------
def test_parameter_example_survives_into_the_mcp_tool_schema() -> None:
    """FastMCP 가 버리는 파라미터 레벨 example 을 스키마 레벨에도 싣는다.

    FastMCP 2.x 의 OpenAPI->tool 변환은 ``parameters[].schema`` 만 도구 입력
    스키마로 옮기므로 ``parameters[].example`` 은 전부 소실된다. 기존
    test_presets 7번은 openapi.json 만 보고 도구 스키마는 보지 않아 이 손실을
    놓쳤다(표본선택 편향). 여기서는 **왕복**을 확인한다.
    """
    fastmcp = pytest.importorskip("fastmcp", reason="fastmcp 미설치(선택 의존성)")
    assert fastmcp is not None
    anyio = pytest.importorskip("anyio")
    import httpx

    from mcportal.mcp import server_from_spec

    document = compile_preset(PRESETS_ROOT / "15101612").document
    listed = document["paths"]["/getNationtradeList"]["get"]["parameters"]
    curated = {
        str(item["name"]): item["example"] for item in listed if "example" in item
    }
    assert curated, "이 프리셋은 큐레이션한 example 을 갖고 있어야 한다"

    client = httpx.AsyncClient(base_url=f"https://{HOST}")
    try:
        server = server_from_spec(document, client=client)
        tools = anyio.run(server.get_tools)
    finally:
        anyio.run(client.aclose)

    properties = tools["getNationtradeList"].parameters["properties"]
    for name, expected in curated.items():
        assert properties[name].get("examples") == [expected], name


def test_parameter_example_is_cast_before_being_put_in_schema_examples() -> None:
    """``schema.examples`` 도 선언 타입으로 캐스팅된 값을 싣는다(문자열 재유입 금지)."""
    source = SourceSpec(
        provider="data.go.kr",
        service_id="00000000",
        service_name="가상 합성 서비스",
        base_url=f"https://{HOST}/api",
        source_kind=SourceKind.GW_SWAGGER,
        operations=(
            OperationSpec(
                operation_id="getSynthList",
                method="GET",
                path="/getSynthList",
                parameters=(
                    ParamSpec(
                        name="pageNo",
                        location="query",
                        required=True,
                        type="integer",
                        example="1",
                    ),
                ),
            ),
        ),
    )
    listed = build_openapi(source).document["paths"]["/getSynthList"]["get"]["parameters"]
    assert listed[0]["schema"]["examples"] == [1]
    assert listed[0]["example"] == 1


# ---------------------------------------------------------------------------
# F3(major) — 메타 블록 1개가 다른 오퍼레이션까지 오염시켰다
# ---------------------------------------------------------------------------
def _meta_block(
    entries: list[dict[str, str]],
    *,
    operation_id: str | None = None,
    url: str | None = None,
) -> dict[str, Any]:
    """비표준 메타 블록 1개(있는 키만 싣는다)."""
    block: dict[str, Any] = {"reqList": entries}
    if operation_id is not None:
        block["operationId"] = operation_id
    if url is not None:
        block["oprtinUrl"] = url
    return block


def _two_version_document(meta: list[dict[str, Any]]) -> dict[str, Any]:
    """경로 마지막 세그먼트가 같은 오퍼레이션 2개(v1/v2 병존)."""
    parameters = [
        {"name": "serviceKey", "in": "query", "type": "string", "required": True},
        {"name": "ym", "in": "query", "type": "string", "required": False},
    ]
    return _gw_document(
        {
            "/v1/getList": _get_operation("getListV1", parameters),
            "/v2/getList": _get_operation("getListV2", parameters),
        },
        meta,
    )


def test_meta_block_does_not_bleed_into_a_same_suffix_operation() -> None:
    """블록이 명시적으로 v1 을 가리키면 v2 는 예시값을 상속하지 않는다.

    기존 방어는 "블록 2개 -> 오퍼레이션 1개"뿐이었고 반대 방향에는 가드가 없었다.
    매칭 후보에 **경로 마지막 세그먼트**가 섞여 있어 ``/v1/getList`` 와
    ``/v2/getList`` 가 같은 키(``getlist``)로 축약된 것이 원인이다.
    """
    document = _two_version_document(
        [
            _meta_block(
                [{"paramtrNm": "ym", "paramtrBassValue": "201601"}],
                operation_id="getListV1",
                url="https://origin.invalid/v1/getList",
            )
        ]
    )
    source = load_source(
        document, kind=SourceKind.GW_SWAGGER, service_id="00000000", key_param="serviceKey"
    )
    by_id = {operation.operation_id: operation for operation in source.operations}
    v1 = {param.name: param for param in by_id["getListV1"].parameters}
    v2 = {param.name: param for param in by_id["getListV2"].parameters}
    assert v1["ym"].example == "201601"
    assert v2["ym"].example is None


def test_meta_block_matching_two_operations_is_discarded_entirely() -> None:
    """어느 쪽인지 가릴 수 없으면 그 블록은 통째로 버린다(양쪽 모두 값 없음).

    ``oprtinUrl`` 도 ``operationId`` 정확 일치도 없어 세그먼트로만 붙는 경우다.
    "추측으로 값을 만드는 것보다 값이 없는 편이 낫다"를 반대 방향에서 지킨다.
    """
    document = _two_version_document(
        [_meta_block([{"paramtrNm": "ym", "paramtrBassValue": "201601"}], operation_id="getList")]
    )
    source = load_source(
        document, kind=SourceKind.GW_SWAGGER, service_id="00000000", key_param="serviceKey"
    )
    for operation in source.operations:
        params = {param.name: param for param in operation.parameters}
        assert params["ym"].example is None, operation.operation_id


def test_two_meta_blocks_matching_one_operation_still_apply_nothing() -> None:
    """기존 방어(블록 2개 -> 오퍼레이션 1개 = 무동작)가 유지되는지 확인한다."""
    parameters = [
        {"name": "serviceKey", "in": "query", "type": "string", "required": True},
        {"name": "ym", "in": "query", "type": "string", "required": False},
    ]
    document = _gw_document(
        {"/getList": _get_operation("getList", parameters)},
        [
            _meta_block([{"paramtrNm": "ym", "paramtrBassValue": "201601"}], operation_id="getList"),
            _meta_block([{"paramtrNm": "ym", "paramtrBassValue": "199001"}], operation_id="getList"),
        ],
    )
    source = load_source(
        document, kind=SourceKind.GW_SWAGGER, service_id="00000000", key_param="serviceKey"
    )
    assert _params(source)["ym"].example is None


# ---------------------------------------------------------------------------
# F4(major) — key_param 정확 일치만 제외해 authKey 류 자격증명이 새어 나갔다
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "name", ["authKey", "auth_key", "apiKey", "api_key", "accessKey", "secretKey", "AUTH-KEY"]
)
def test_credential_named_meta_values_are_never_promoted_to_examples(name: str) -> None:
    """``serviceKey`` 외의 이름으로 선언된 인증 파라미터도 example 이 되지 않는다.

    ``find_key_assignments`` 는 ``이름=값`` 형태만 잡는데 메타 흡수는
    ``"example": "<값>"`` 이라는 **맨값**을 만들므로 커밋 직전 게이트가 원리상
    못 잡는다. 그래서 흡수 단계에서 막아야 한다.
    """
    secret = "SYNTHETICSECRETVALUE0000000"
    parameters = [
        {"name": "serviceKey", "in": "query", "type": "string", "required": True},
        {"name": name, "in": "query", "type": "string", "required": False},
    ]
    document = _gw_document(
        {"/getX": _get_operation("getX", parameters)},
        [_meta_block([{"paramtrNm": name, "paramtrBassValue": secret}], operation_id="getX")],
    )
    source = load_source(
        document, kind=SourceKind.GW_SWAGGER, service_id="00000000", key_param="serviceKey"
    )
    assert _params(source)[name].example is None

    serialized = dumps(build_openapi(source).document)
    assert secret not in serialized


def test_credential_name_set_is_shared_by_every_gate() -> None:
    """자격증명 이름 집합은 한 곳(:mod:`mcportal.replay.scrub`)에서 공유한다."""
    from mcportal.compiler import curation as curation_module
    from mcportal.compiler import openapi as openapi_module

    assert openapi_module._FREE_TEXT_KEY_PARAMS == CREDENTIAL_PARAM_NAMES
    assert curation_module._CURATION_KEY_PARAMS == CREDENTIAL_PARAM_NAMES
    assert is_credential_param("AUTH-KEY") is True
    assert is_credential_param("cntyCd") is False
    assert is_credential_param("myKey", extra=("myKey",)) is True


# ---------------------------------------------------------------------------
# F24(minor) — 메타 예시값이 소스 선언 enum 을 위반한 채 실렸다
# ---------------------------------------------------------------------------
def test_meta_example_violating_declared_enum_is_rejected() -> None:
    """``enum`` 밖의 값은 주입하지 않는다(자기모순 OpenAPI 방지)."""
    parameters = [
        {"name": "serviceKey", "in": "query", "type": "string", "required": True},
        {
            "name": "mode",
            "in": "query",
            "type": "string",
            "required": False,
            "enum": ["A", "B"],
        },
    ]
    bad = _gw_document(
        {"/getY": _get_operation("getY", parameters)},
        [_meta_block([{"paramtrNm": "mode", "paramtrBassValue": "ZZZ"}], operation_id="getY")],
    )
    source = load_source(
        bad, kind=SourceKind.GW_SWAGGER, service_id="00000000", key_param="serviceKey"
    )
    assert _params(source)["mode"].example is None

    good = _gw_document(
        {"/getY": _get_operation("getY", parameters)},
        [_meta_block([{"paramtrNm": "mode", "paramtrBassValue": "B"}], operation_id="getY")],
    )
    source = load_source(
        good, kind=SourceKind.GW_SWAGGER, service_id="00000000", key_param="serviceKey"
    )
    assert _params(source)["mode"].example == "B"


# ---------------------------------------------------------------------------
# F5(major) — 빈 루트에서 미존재 ID 가 조용히 무시되고 exit 0 이 나왔다
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("argv_extra", [[], ["--check"], ["--json"], ["--check", "--json"]])
def test_compile_rejects_unknown_id_even_on_an_empty_root(
    tmp_path: Path, argv_extra: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    """``compile [--check] <ID>`` 는 번들이 0건이어도 ID 를 검증한다.

    ``if not infos:`` 단축 반환이 ``_select_presets`` 보다 먼저 수행돼, 프리셋을
    아직 체크아웃하지 않은 CI 잡에서 드리프트 게이트가 **아무것도 검사하지 않고
    초록**을 냈다. 게이트의 침묵 통과가 가장 나쁜 실패 모드다.
    """
    empty = tmp_path / "empty"
    empty.mkdir()
    code = cli.main(["compile", *argv_extra, "99999999", "--presets-root", str(empty)])
    assert code == cli.EXIT_ERROR
    captured = capsys.readouterr()
    assert "99999999" in captured.err
    assert "(없음)" in captured.err


def test_compile_without_id_on_an_empty_root_is_still_success(tmp_path: Path) -> None:
    """ID 를 지정하지 않은 호출은 여전히 "빈 상태는 실패가 아니다"를 지킨다."""
    empty = tmp_path / "empty"
    empty.mkdir()
    assert cli.main(["compile", "--presets-root", str(empty)]) == cli.EXIT_OK


# ---------------------------------------------------------------------------
# F6(major) — 15081808 은 curated=False 컴파일이 불가능하다(문서화된 한계)
# ---------------------------------------------------------------------------
def test_uncurated_compile_status_is_pinned_for_every_preset() -> None:
    """4프리셋 전수로 ``curated=False`` 를 돌려 K2 대조군 가용성을 고정한다.

    기존 테스트는 합성 번들(test_curation 24번)과 15000115 하나(test_presets)
    로만 확인해, **문제가 있는 15081808 로는 아무도 시도하지 않았다**(표본선택
    편향). 이 프리셋은 원 ``info.description`` 에 인증키 사용 안내가 있어
    큐레이션을 끄면 시크릿 게이트에 걸린다 — PROTOCOL.md §8-2 **L8** 로 등재된
    한계이며, 게이트를 완화하는 방식으로 해결하지 않는다.
    """
    blocked = {"15081808"}
    for preset_id in PRESET_IDS:
        directory = PRESETS_ROOT / preset_id
        if preset_id in blocked:
            with pytest.raises(CompileError) as excinfo:
                compile_preset(directory, curated=False)
            assert "인증키" in str(excinfo.value)
        else:
            compiled = compile_preset(directory, curated=False)
            assert compiled.operation_ids


def test_l8_limitation_is_documented_where_it_matters() -> None:
    """한계는 코드에만 있으면 안 된다 — 프로토콜과 프리셋 README 양쪽에 적혀 있다."""
    protocol = (REPO_ROOT / "benchmarks" / "PROTOCOL.md").read_text(encoding="utf-8")
    assert "| L8 |" in protocol
    assert "15081808" in protocol.split("| L8 |", 1)[1].split("\n", 1)[0]

    readme = (PRESETS_ROOT / "15081808" / "README.md").read_text(encoding="utf-8")
    assert "curated=False" in readme
    assert "L8" in readme


# ---------------------------------------------------------------------------
# F7(major) — V8 시크릿 게이트가 SourceSpec 3필드만 검사했다
# ---------------------------------------------------------------------------
_LEAK = f"호출 예시: https://{HOST}/api/getSynthList?serviceKey=SYNTHETICLEAK1234&rows=10"


@pytest.mark.parametrize("where", ["operation", "parameter"])
def test_merge_gate_blocks_key_assignments_in_operation_and_parameter_text(
    where: str,
) -> None:
    """소스가 오퍼레이션/파라미터 설명에 담은 인증키 대입도 병합에서 막는다.

    큐레이션이 쓴 문자열은 V7 이 문서 전체를 깊이 훑어 차단하는데 소스에서 온
    문자열은 3필드만 봤다. 그 비대칭 때문에 라이브러리 경로
    (``compile_preset(...).document`` -> ``server_from_spec``)가 무방비였고,
    누출 문자열이 **FastMCP 도구 설명까지 살아서 도달**했다.
    """
    kwargs = (
        {"operation_description": _LEAK}
        if where == "operation"
        else {"param_description": _LEAK}
    )
    with pytest.raises(CurationError) as excinfo:
        apply_curation_with_report(_synthetic_source(**kwargs), _curation())
    message = str(excinfo.value)
    assert "serviceKey" in message
    assert "getSynthList" in message


def test_merge_gate_leaves_clean_free_text_alone() -> None:
    """자격증명이 없는 자유문자열은 그대로 통과한다(과잉 차단 방지)."""
    clean = "조회 기간을 YYYYMM 6자리로 넣는다(예: 201601)."
    merged, _report = apply_curation_with_report(
        _synthetic_source(operation_description=clean), _curation()
    )
    assert merged.operations[0].description == clean


# ---------------------------------------------------------------------------
# F8(major) — 타입 불일치 큐레이션 힌트가 조용히 사라지거나 스키마를 위반했다
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("overlay", "field"),
    [
        ({"rows": {"enum": ["열", "스물"]}}, "enum"),
        ({"rows": {"example": "abc"}}, "example"),
        ({"rows": {"default": "없음"}}, "default"),
        ({"flag": {"example": "예"}}, "example"),
        ({"flag": {"default": "없음"}}, "default"),
    ],
)
def test_type_mismatched_curation_hint_is_rejected(
    overlay: dict[str, Any], field: str
) -> None:
    """선언 타입으로 캐스팅되지 않는 힌트는 커밋 전에 접는다.

    이전에는 방향에 따라 결과가 갈렸다 — ``enum``·``default`` 는 조용히 폐기,
    ``example`` 은 스키마를 위반한 채 잔존. 둘 다 무고지였고
    ``CurationReport.parameters_curated`` 는 "적용됨"처럼 세어졌다.
    """
    with pytest.raises(CurationError) as excinfo:
        apply_curation_with_report(
            _synthetic_source(),
            _curation({"getSynthList": {"parameters": overlay}}),
        )
    message = str(excinfo.value)
    assert field in message
    assert next(iter(overlay)) in message


def test_type_matching_curation_hint_still_lands_in_the_document() -> None:
    """타입이 맞는 힌트는 그대로 산출물에 실린다(과잉 차단 방지)."""
    merged, report = apply_curation_with_report(
        _synthetic_source(),
        _curation(
            {
                "getSynthList": {
                    "parameters": {
                        "rows": {"example": "10", "default": "10", "enum": ["10", "100"]},
                        "flag": {"example": "true"},
                    }
                }
            }
        ),
    )
    assert report.parameters_curated == 2
    listed = build_openapi(merged).document["paths"]["/getSynthList"]["get"]["parameters"]
    by_name = {item["name"]: item for item in listed}
    assert by_name["rows"]["schema"]["enum"] == [10, 100]
    assert by_name["rows"]["schema"]["default"] == 10
    assert by_name["rows"]["schema"]["examples"] == [10]
    assert by_name["flag"]["schema"]["examples"] == [True]


# ---------------------------------------------------------------------------
# F12(minor) — parameters 와 parameters_remove 의 모순 입력이 무경고 통과했다
# ---------------------------------------------------------------------------
def test_curating_and_removing_the_same_parameter_is_rejected() -> None:
    """제거가 항상 이기므로 같은 이름을 양쪽에 적는 것은 오류다."""
    with pytest.raises(CurationError) as excinfo:
        apply_curation_with_report(
            _synthetic_source(),
            _curation(
                {
                    "getSynthList": {
                        "parameters": {"mode": {"description": "이 설명은 어디로 가는가"}},
                        "parameters_remove": [{"name": "mode", "reason": "유령 인자"}],
                    }
                }
            ),
        )
    assert "mode" in str(excinfo.value)


def test_removing_a_parameter_alone_still_works() -> None:
    """제거만 지정하면 정상 동작한다(과잉 차단 방지)."""
    merged, report = apply_curation_with_report(
        _synthetic_source(),
        _curation(
            {"getSynthList": {"parameters_remove": [{"name": "mode", "reason": "유령 인자"}]}}
        ),
    )
    assert [param.name for param in merged.operations[0].parameters] == ["flag", "rows"]
    assert report.parameters_removed == 1


# ---------------------------------------------------------------------------
# F9(minor) — 스키마 버전 검사의 타입 구멍(True / 1.0 이 통과)
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("version", [True, 1.0, "1", 0, 2, None])
def test_schema_version_must_be_a_true_integer(version: Any) -> None:
    """``True == 1`` · ``1.0 == 1`` 로 통과하던 구멍을 막는다(curation 쪽)."""
    with pytest.raises(CurationError) as excinfo:
        load_curation(
            {
                "mcportal_curation": version,
                "preset_id": "00000000",
                "service": {"title": "가상", "version": "1.0.0"},
            }
        )
    assert "스키마 버전" in str(excinfo.value)


@pytest.mark.parametrize("version", [True, 1.0, "1"])
def test_preset_source_schema_version_must_be_a_true_integer(
    tmp_path: Path, version: Any
) -> None:
    """같은 구멍이 ``source.json`` 로더에도 있었다(양쪽 동일 처리)."""
    from mcportal.compiler.curation import read_preset_source

    original = json.loads(
        (PRESETS_ROOT / "15101612" / "source.json").read_text(encoding="utf-8")
    )
    original["mcportal_preset_source"] = version
    target = tmp_path / "source.json"
    target.write_text(json.dumps(original, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(CurationError) as excinfo:
        read_preset_source(target)
    assert "스키마 버전" in str(excinfo.value)


# ---------------------------------------------------------------------------
# F11(minor) — curation.json 의 중복 키가 조용히 덮였다
# ---------------------------------------------------------------------------
def test_duplicate_json_keys_are_rejected(tmp_path: Path) -> None:
    """복붙으로 블록을 늘리다 생기는 중복 키를 조용히 삼키지 않는다."""
    text = (
        "{\n"
        f'  "mcportal_curation": {CURATION_SCHEMA_VERSION},\n'
        '  "preset_id": "00000000",\n'
        '  "service": {"title": "가상", "version": "1.0.0"},\n'
        '  "operations": {\n'
        '    "getSynthList": {"summary": "첫번째"},\n'
        '    "getSynthList": {"summary": "두번째"}\n'
        "  }\n"
        "}\n"
    )
    target = tmp_path / "curation.json"
    target.write_text(text, encoding="utf-8")
    with pytest.raises(CurationError) as excinfo:
        read_curation(target)
    assert "getSynthList" in str(excinfo.value)


# ---------------------------------------------------------------------------
# F10(minor) — CALL_BUDGET 이 범위 검증 없이 --json 계약에 실렸다
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("value", ["-5", "0"])
def test_env_budget_out_of_range_is_rejected(
    value: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """``--budget`` 과 같은 규칙(1 이상)을 환경변수 경로에도 적용한다.

    ``limit: -5`` 는 ``mcportal.quota.status/1`` 소비자가 방어할 수 없는 값이고,
    호출 0건인데 ``EXHAUSTED`` 로 보고됐다.
    """
    monkeypatch.setenv("CALL_BUDGET", value)
    assert cli.main(["quota", "status", "--json"]) == cli.EXIT_ERROR
    assert "1 이상" in capsys.readouterr().err


def test_env_budget_in_range_still_works(monkeypatch: pytest.MonkeyPatch) -> None:
    """정상 범위 값은 그대로 통과한다(과잉 차단 방지)."""
    monkeypatch.setenv("CALL_BUDGET", "7")
    assert cli.main(["quota", "status", "--json"]) == cli.EXIT_OK


# ---------------------------------------------------------------------------
# F13(minor) — MCPORTAL_PRESETS 가 조용히 무시됐다
# ---------------------------------------------------------------------------
def test_ignored_env_presets_root_is_reported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """환경변수가 채택되지 않으면 stderr 경고와 ``root_source`` 로 밝힌다.

    ``presets`` 는 읽기 전용이라 혼동에 그치지만 ``compile`` 은 쓰기 명령이라
    사용자가 의도하지 않은 트리의 openapi.json 을 재생성한다.
    """
    empty = tmp_path / "empty"
    empty.mkdir()
    monkeypatch.setenv("MCPORTAL_PRESETS", str(empty))
    assert cli.main(["presets", "--json"]) == cli.EXIT_OK
    captured = capsys.readouterr()
    assert "MCPORTAL_PRESETS" in captured.err
    assert json.loads(captured.out)["root_source"] != f"env:MCPORTAL_PRESETS"


def test_adopted_env_presets_root_is_not_warned(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """채택된 환경변수에는 경고를 내지 않는다(소음 방지)."""
    monkeypatch.setenv("MCPORTAL_PRESETS", str(PRESETS_ROOT))
    assert cli.main(["presets", "--json"]) == cli.EXIT_OK
    captured = capsys.readouterr()
    assert "MCPORTAL_PRESETS" not in captured.err
    assert json.loads(captured.out)["root_source"] == "env:MCPORTAL_PRESETS"


# ---------------------------------------------------------------------------
# F28(minor) — East Asian Ambiguous 를 1폭으로 세어 표가 어긋났다
# ---------------------------------------------------------------------------
def test_ambiguous_width_characters_keep_columns_aligned() -> None:
    """``·``(U+00B7, Ambiguous)이 든 셀도 열이 어긋나지 않는다."""
    assert cli.display_width("·") == 2
    headers = ["ID", "서비스명", "종류"]
    rows = [
        ["15081808", "국세청 사업자등록정보 진위확인·상태조회", "odcloud_swagger"],
        ["15101612", "관세청 국가별 수출입실적", "gw_swagger"],
    ]
    lines = cli.render_table(headers, rows)
    # 터미널의 열 위치는 문자 인덱스가 아니라 **접두부의 표시폭**이다. 전각 문자
    # 수가 다른 행들은 표시폭이 정렬돼도 문자 인덱스는 서로 다르다.
    starts = {
        cli.display_width(line[: line.index(marker)])
        for line in lines
        for marker in ("odcloud_swagger", "gw_swagger")
        if marker in line
    }
    assert len(starts) == 1


# ---------------------------------------------------------------------------
# F15(minor) — median_ns 가 은행가 반올림을 써 선등록 문면과 어긋났다
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("samples", "expected"),
    [([1, 2, 3, 4], 3), ([3, 4], 4), ([1, 2], 2), ([1, 2, 3, 4, 5], 3), ([2, 4], 3)],
)
def test_median_ns_rounds_half_up(samples: list[int], expected: int) -> None:
    """PROTOCOL.md §4-4 의 "반올림" 문면대로 half-up 으로 계산한다."""
    harness = _import_harness()
    assert harness.median_ns(samples) == expected


def test_protocol_documents_the_rounding_rule() -> None:
    """문면과 구현의 일치가 이 프로젝트의 가치이므로 계산식을 문서에 적어 둔다."""
    protocol = (REPO_ROOT / "benchmarks" / "PROTOCOL.md").read_text(encoding="utf-8")
    assert "half-up" in protocol
    assert "floor(median + 0.5)" in protocol


def _import_harness() -> Any:
    """``benchmarks/harness.py`` 를 파일 경로로 임포트한다(패키지가 아니다).

    ``exec_module`` **전에** ``sys.modules`` 에 등록한다. 하네스는
    ``from __future__ import annotations`` 아래에서 dataclass 를 정의하는데,
    ``dataclasses`` 는 ``ClassVar``/``InitVar`` 판정을 위해 문자열 애노테이션을
    ``sys.modules[cls.__module__].__dict__`` 로 되짚는다. 등록이 없으면 그 조회가
    ``None`` 이라 ``AttributeError: 'NoneType' object has no attribute '__dict__'``
    로 죽는다.

    전체 실행에서는 :mod:`tests.test_benchmark_harness` 가 같은 이름으로 먼저
    등록해 둔 덕에 우연히 통과했다 — 이 파일만 단독으로 돌리면 5건이 죽는
    **실행 순서 의존 잠복 버그**였고, 부분 실행이 안 되는 테스트 파일은 회귀를
    좁혀 볼 수 없게 만든다.
    """
    import importlib.util
    import sys

    path = REPO_ROOT / "benchmarks" / "harness.py"
    spec = importlib.util.spec_from_file_location("mcportal_bench_harness", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        sys.modules.pop(spec.name, None)
        raise
    return module


# ---------------------------------------------------------------------------
# F17(major) — .gitattributes 정규화가 매니페스트 해시를 커밋 순간 거짓으로 만들었다
# ---------------------------------------------------------------------------
_MANIFEST_PATH = PRESETS_ROOT / "_MANIFEST_20260806.json"


def _manifest_entries() -> list[dict[str, Any]]:
    return json.loads(_MANIFEST_PATH.read_text(encoding="utf-8"))


def test_manifest_hashes_match_the_files_on_disk() -> None:
    """매니페스트 24항목의 sha256·bytes 를 실제로 재계산한다.

    기존 검사는 ``provenance["raw_sha256"].startswith("sha256:")`` 처럼 **형식만**
    봤고, 매니페스트 해시를 재계산하는 테스트는 리포 전체에 0건이었다. 그래서
    543 passed 로도 통과했다.
    """
    mismatched: list[str] = []
    for entry in _manifest_entries():
        path = PRESETS_ROOT / entry["폴더"] / entry["파일"]
        data = path.read_bytes()
        if hashlib.sha256(data).hexdigest() != entry["sha256"]:
            mismatched.append(f"{path} (sha256)")
        if len(data) != int(entry["bytes"]):
            mismatched.append(f"{path} (bytes)")
    assert mismatched == []


def test_raw_evidence_is_exempt_from_eol_normalization() -> None:
    """원문 증거물은 커밋 시 CRLF->LF 정규화를 거치지 않는다.

    ``.gitattributes`` 의 ``* text=auto eol=lf`` 가 clean 필터로 LF 정규화를 하면,
    CRLF 를 가진 9개 파일의 커밋 바이트가 작업트리와 달라져 **클론한 사람에게는
    매니페스트 해시가 전부 거짓**이 된다. git 자체에 물어 확인한다.
    """
    attributes = (REPO_ROOT / ".gitattributes").read_text(encoding="utf-8")
    assert "presets/_raw/** -text" in attributes
    assert "presets/_transcribed/** -text" in attributes

    crlf = [
        PRESETS_ROOT / entry["폴더"] / entry["파일"]
        for entry in _manifest_entries()
        if b"\r\n" in (PRESETS_ROOT / entry["폴더"] / entry["파일"]).read_bytes()
    ]
    assert crlf, "CRLF 를 가진 원문이 하나도 없다면 이 회귀는 의미가 없다"

    for path in crlf:
        relative = path.relative_to(REPO_ROOT).as_posix()
        filtered = _git("hash-object", "--path", relative, relative)
        raw = _git("hash-object", "--no-filters", relative)
        assert filtered == raw, relative


def _git(*args: str) -> str:
    """리포 루트에서 git 을 돌려 표준출력을 돌려준다."""
    result = subprocess.run(
        ["git", *args],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    if result.returncode != 0:  # pragma: no cover - git 부재 환경 방어
        pytest.skip(f"git 을 실행할 수 없습니다: {result.stderr.strip()}")
    return result.stdout.strip()


# ---------------------------------------------------------------------------
# F19(major) — sdist 제외 규칙이 없어 presets/ 가 PyPI 로 재배포될 참이었다
# ---------------------------------------------------------------------------
def test_sdist_excludes_raw_evidence_and_benchmark_results() -> None:
    """``[tool.hatch.build.targets.sdist]`` 로 원문·결과물을 명시 차단한다.

    hatchling 의 sdist 기본값은 ``.gitignore`` 만 존중하고 리포 전체를 담는다.
    실제로 커밋된 v0.1.0 sdist 에는 ``specs/`` · ``tests/`` · ``examples/`` 까지
    들어 있어 화이트리스트가 없다는 사실이 입증된다.
    """
    config = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    excluded = config["tool"]["hatch"]["build"]["targets"]["sdist"]["exclude"]
    for required in ("presets/_raw", "presets/_transcribed", "benchmarks/results"):
        assert required in excluded


# ---------------------------------------------------------------------------
# F16 · F20(minor) — 개인정보 스캔 범위가 선언보다 좁았다
# ---------------------------------------------------------------------------
_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_RRN_RE = re.compile(r"(?<!\d)\d{6}-\d{7}(?!\d)")
_BIZ_RE = re.compile(r"(?<!\d)\d{3}-\d{2}-\d{5}(?!\d)")

#: `_raw/` 스냅샷에 남아 있고 NOTICE-DATA §2-1 에 등재된 기관 창구 주소.
_REGISTERED_EMAILS = frozenset({"opendata_help@nia.or.kr"})


def _presets_text_files() -> list[Path]:
    """``presets/**`` 의 텍스트 파일 목록(바이너리는 별도 검사)."""
    return [
        path
        for path in sorted(PRESETS_ROOT.rglob("*"))
        if path.is_file() and path.suffix.lower() not in {".xlsx", ".bin"}
    ]


def test_no_personal_data_in_presets_tree() -> None:
    """검사 범위를 ``presets/**`` 전체로 넓힌다(선언과 검사 범위의 일치).

    기존 회귀는 번들 4종의 json 만 읽어서, 커밋 대상인 ``presets/_raw/**`` 의
    원문 HTML 약 800 KB 를 한 번도 보지 않았다. 앞으로 ``_raw/`` 에 무엇이
    추가돼도 이 테스트가 먼저 잡는다.
    """
    unregistered: list[str] = []
    for path in _presets_text_files():
        text = path.read_text(encoding="utf-8", errors="ignore")
        where = path.relative_to(PRESETS_ROOT).as_posix()
        assert _RRN_RE.findall(text) == [], where
        assert _BIZ_RE.findall(text) == [], where
        for address in set(_EMAIL_RE.findall(text)):
            if address not in _REGISTERED_EMAILS:
                unregistered.append(f"{where}: {address}")
    assert unregistered == [], (
        "NOTICE-DATA.md §2-1 에 등재되지 않은 이메일 주소가 있습니다: " + ", ".join(unregistered)
    )


def test_bundle_outputs_have_no_contact_information() -> None:
    """번들 산출물(4종)에는 이메일·전화가 0건이라는 좁은 선언을 따로 고정한다."""
    for preset_id in PRESET_IDS:
        for name in ("source.json", "curation.json", "openapi.json", "README.md"):
            path = PRESETS_ROOT / preset_id / name
            text = path.read_text(encoding="utf-8")
            where = f"{preset_id}/{name}"
            assert _EMAIL_RE.findall(text) == [], where
            assert "apiTelNo" not in text, where
            assert "1566-0025" not in text, where


def test_notice_data_registers_what_the_raw_snapshots_actually_contain() -> None:
    """`_raw/` 스냅샷의 관리부서 대표번호가 문서에 등재돼 있는지 확인한다.

    정찰과 NOTICE-DATA 가 "전화 실값 0건"을 선언했는데 커밋 예정 파일에는 10자리
    전화 실값이 있었다. 선언을 사실에 맞춘 뒤, 그 사실이 문서에서 사라지지
    않도록 고정한다.
    """
    notice = (PRESETS_ROOT / "NOTICE-DATA.md").read_text(encoding="utf-8")
    found: set[str] = set()
    for path in PRESETS_ROOT.rglob("datagokr_*_openapi.html"):
        text = path.read_text(encoding="utf-8", errors="ignore")
        found.update(re.findall(r'apiTelNo\s*=\s*"(\d{9,11})"', text))
    assert found, "포털 스냅샷에서 관리부서 번호를 찾지 못했다(픽스처가 바뀌었는가?)"
    for number in found:
        assert number in notice, number


def test_binary_reference_document_has_no_personal_data() -> None:
    """유일한 바이너리(xlsx)는 OOXML 파트를 풀어 검사한다(zip 안은 정규식이 못 본다)."""
    path = PRESETS_ROOT / "_raw" / "15101612" / "refdoc_관세청조회코드_v1.2.xlsx"
    if not path.is_file():  # pragma: no cover - 파일 축소를 택한 경우
        pytest.skip("참고문서 xlsx 가 리포에 없습니다.")
    with zipfile.ZipFile(path) as archive:
        text = "".join(
            archive.read(name).decode("utf-8", "ignore")
            for name in archive.namelist()
            if name.endswith((".xml", ".rels"))
        )
    assert _RRN_RE.findall(text) == []
    assert _BIZ_RE.findall(text) == []
    assert _EMAIL_RE.findall(text) == []

    notice = (PRESETS_ROOT / "NOTICE-DATA.md").read_text(encoding="utf-8")
    # 문서가 등재한 제작 환경 식별자가 실제로 그 파일에 있는지(=문서가 사실인지).
    assert "lastModifiedBy" in text and "lastModifiedBy" in notice
    assert "A2A0CA8A-956D-47A0-8E5A-4A6F0423A6CE" in text
    assert "A2A0CA8A-956D-47A0-8E5A-4A6F0423A6CE" in notice


# ---------------------------------------------------------------------------
# F21 · F25 · F27(문서) — 선언과 사실의 범위를 맞춘다
# ---------------------------------------------------------------------------
def test_readme_states_what_the_wheel_does_and_does_not_carry() -> None:
    """루트 README 의 wheel 동봉 서술이 빌드 설정과 일치한다.

    W4 에서 루트 ``README.md`` 가 영문 정본으로 바뀌고 한국어판이 ``README.ko.md``
    로 이관됐다. 검사 대상만 두 파일로 옮기고 **검사 강도는 그대로 둔다** — 정본이
    영어가 됐다는 이유로 한국어 독자가 보는 문서에서 이 고지가 빠지면, 이 회귀가
    막으려던 사고(설치본의 실제 내용과 다른 광고, 개인정보 0건 주장의 범위 확대)가
    한국어 쪽에서 그대로 재현된다.

    v0.2.0 에서 사실이 **뒤집혔다.** 프리셋 4종이 wheel 에 동봉되기 시작했으므로
    "동봉되지 않는다"를 강제하던 검사를 그대로 두면 이 테스트가 거짓을 지키는 셈이
    된다. 그래서 방향만 반전하고 **검사할 축은 늘린다** — 이제 위험한 거짓말은
    반대쪽이다. "다 들어 있다"고 적어 놓고 샘플링 증거(실응답 본문·카세트)까지 같이
    나가면 배포하지 않기로 한 것을 배포하게 된다. 그래서 문서가 **동봉하지 않는
    것**도 함께 적도록 요구하고, 그 주장을 ``pyproject.toml`` 의 실제 force-include
    목록과 대조한다(문서만 고치고 설정이 따라오지 않는 경우를 잡는다).

    v0.2.2 에서 금지목록이 **한 칸 줄었다** — ``sampled_schemas`` 만 뺀다.
    W6 클린 재현(2026-08-15)에서 pip 설치본의 ``mcportal compile --check`` 가
    ``4건 중 3건 드리프트 / exit 3`` 으로 죽는 것이 실측됐다. ``--check`` 는
    파일을 비교하는 게 아니라 source + curation + sampled 3층에서
    ``openapi.json`` 을 **재합성해** 대조하기 때문이다. sampled 층이 wheel 에
    없으면 설치본은 자기가 실어 나른 ``openapi.json`` 을 스스로 재현하지 못한다.
    즉 빠져 있던 것은 증거가 아니라 산출물을 만든 **빌드 입력**이었다. 노출
    관점에서도 이 파일은 응답에서 추론한 **필드명·타입 구조만** 담고 실응답 값이
    **0건**이며, 그 구조는 이미 ``openapi.json`` 으로 wheel 에 나가고 있어
    **추가로 새어 나가는 정보가 없다**. 그래서 이 항목만 금지에서 뺀다.

    **경계는 그대로다.** 여전히 금지인 것은 **실응답 본문(``samples/``)과
    카세트(``cassettes/``)** 다 — data.go.kr 원문이라 재배포 조건·스크러빙 검토가
    선행되어야 한다. 이 경계가 이 테스트의 존재 이유이므로 검사 강도는 낮추지 않고
    **반대 방향으로 축을 늘린다**: sampled 층 3건이 force-include 에 실제로 들어
    있는지, 그리고 샘플링하지 않은 ``15081808`` 에는 파일도 항목도 **없는지**까지
    못박는다(개인정보 축이라 실호출하지 않은 번들이다 — 이 사실이 조용히 뒤집히면
    그게 사고다). 얻은 것은 pip 설치본 ``compile --check`` 의
    ``4건 중 4건 일치 / exit 0`` 복구다.

    ⚠️ 부분문자열 함정이 아니다: ``"samples"`` 는 ``"sampled_schemas"`` 의
    부분문자열이 **아니다**(``sampled`` 는 ``d`` 로 끝난다). 그래서 ``samples``
    금지를 그대로 남겨 둬도 오탐이 나지 않는다 — 아래 마지막 두 줄이 그 성질
    자체를 고정한다.
    """
    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    assert "the published wheel carries the four preset bundles" in readme
    assert "MCPORTAL_PRESETS" in readme
    # 동봉하지 않는 것(실응답 본문·카세트)을 같은 자리에서 밝힌다.
    assert "stay in the" in readme
    # 0.2.2 에서 늘어난 동봉 범위(sampled 층)도 같은 자리에 적는다.
    assert "Since 0.2.2" in readme
    assert "sampled_schemas.json" in readme
    # "개인정보 0건"은 하위 문서(§2-1)와 같은 범위로 좁혀 서술한다.
    assert "The scope of that statement is the bundle artifacts" in readme
    assert "opendata_help@nia.or.kr" in readme

    korean = (REPO_ROOT / "README.ko.md").read_text(encoding="utf-8")
    assert "wheel 에 동봉된다" in korean
    assert "MCPORTAL_PRESETS" in korean
    assert "0.2.2 부터는" in korean
    assert "sampled_schemas.json" in korean
    # 한국어판도 "리포에만 남는 것"을 같은 자리에서 밝혀야 한다.
    assert "리포에만" in korean
    assert "번들 산출물" in korean
    assert "opendata_help@nia.or.kr" in korean

    # 문서의 주장을 빌드 설정으로 확인한다: 번들 4종의 파일과 sampled 층은 동봉
    # 목록에 있고, 실응답 본문·카세트는 한 줄도 없다.
    pyproject = (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    force_include = pyproject.split(
        "[tool.hatch.build.targets.wheel.force-include]"
    )[1].split("\n[", 1)[0]
    for preset_id in PRESET_IDS:
        for name in ("source.json", "curation.json", "openapi.json", "README.md"):
            assert f'"presets/{preset_id}/{name}"' in force_include, (
                f"{preset_id}/{name} 가 wheel 동봉 목록에 없다"
            )
    # 반대 방향의 단언. sampled 층이 조용히 빠지면 설치본의 compile --check 가
    # 다시 exit 3 으로 돌아간다(2026-08-15 실측). 파일 존재와 동봉 항목을 함께
    # 못박아 "설정만 지우고 파일은 남는" 경우도 잡는다.
    for preset_id in ("15000115", "15101612", "15102108"):
        assert f'"presets/{preset_id}/sampled_schemas.json"' in force_include, (
            f"{preset_id}/sampled_schemas.json 가 wheel 동봉 목록에서 빠졌다 — "
            "설치본이 openapi.json 을 재합성하지 못한다"
        )
        assert (PRESETS_ROOT / preset_id / "sampled_schemas.json").is_file(), (
            f"{preset_id}/sampled_schemas.json 파일이 리포에 없다"
        )
    # 15081808 은 개인정보 축이라 실호출하지 않았다 — sampled 층이 애초에 없다.
    # 파일 단위 매핑이라 없는 경로를 적으면 빌드가 FileNotFoundError 로 죽으므로
    # 동봉 목록에도 없어야 한다. 둘 다 못박는다(한쪽만 뒤집혀도 사고다).
    assert '"presets/15081808/sampled_schemas.json"' not in force_include, (
        "15081808 에는 sampled_schemas.json 이 없다 — 동봉 목록에 적으면 빌드가 죽는다"
    )
    assert not (PRESETS_ROOT / "15081808" / "sampled_schemas.json").exists(), (
        "15081808 에 sampled_schemas.json 이 생겼다 — 샘플링 제외 전제가 뒤집혔다"
    )

    for forbidden in ("cassettes", "samples", "_raw", "_transcribed"):
        assert forbidden not in force_include, (
            f"wheel 동봉 목록에 {forbidden} 가 들어갔다 — 배포 대상이 아니다"
        )
    # 위 금지 목록이 sampled 층을 오탐하지 않는다는 성질 자체를 고정한다:
    # "samples" 는 "sampled_schemas" 의 부분문자열이 아니다.
    assert "sampled_schemas" in force_include
    assert "samples" not in "sampled_schemas"


def test_presets_readme_headline_matches_its_own_numbers() -> None:
    """절 제목의 수치가 본문·CLI 출력과 일치한다(격차를 축소해 말하지 않는다)."""
    readme = (PRESETS_ROOT / "README.md").read_text(encoding="utf-8")
    assert "## 4. 응답 스키마 12건 중 10건을 실키 샘플링으로 채웠다" in readme
    assert "절반이 비어 있다" not in readme

    total = unresolved = 0
    for preset_id in PRESET_IDS:
        document = json.loads(
            (PRESETS_ROOT / preset_id / "openapi.json").read_text(encoding="utf-8")
        )
        inference = document["info"]["x-mcportal"]["schema_inference"]
        unresolved += int(inference.get("unresolved", 0))
        total += sum(
            1
            for item in document["paths"].values()
            for method in item
            if method in ("get", "post", "put", "patch", "delete")
        )
    # 절 제목이 말하는 "12건 중 10건" 은 이제 **해소한 개수**다. 그러므로 지금
    # 남아 있어야 하는 미확정은 0 이고, 오퍼레이션 총계는 그대로 12 다.
    assert (unresolved, total) == (0, 12)

    # 제목의 10 이 어디서 온 숫자인지 본문이 근거를 대고 있다(측정일 · 호출 수).
    assert "2026-08-09" in readme
    assert "10회 호출" in readme


def test_notice_data_separates_portal_page_snapshots_from_datasets() -> None:
    """포털 페이지 스냅샷을 데이터셋 이용허락범위로 귀속하지 않는다."""
    notice = (PRESETS_ROOT / "NOTICE-DATA.md").read_text(encoding="utf-8")
    assert "## 1-1." in notice
    assert "all rights reserved" in notice.lower()
    assert "데이터셋 이용허락범위와 별개" in notice

    # 문서가 주장하는 저작권 표시가 실제 파일에 있는지 확인한다.
    for path in PRESETS_ROOT.rglob("datagokr_*_openapi.html"):
        text = path.read_text(encoding="utf-8", errors="ignore")
        assert "Ministry of the Interior and Safety" in text, path.name


# ---------------------------------------------------------------------------
# 프리셋 산출물 자체가 시크릿·모순을 담지 않는지 최종 확인
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("preset_id", PRESET_IDS)
def test_committed_presets_stay_free_of_key_assignments(preset_id: str) -> None:
    """커밋된 산출물 전체를 자격증명 이름 집합으로 다시 훑는다."""
    text = (PRESETS_ROOT / preset_id / "openapi.json").read_text(encoding="utf-8")
    assert find_key_assignments(text, CREDENTIAL_PARAM_NAMES) == ()


@pytest.mark.parametrize("preset_id", PRESET_IDS)
def test_committed_presets_have_no_enum_violating_examples(preset_id: str) -> None:
    """산출 문서의 example 은 같은 스키마의 enum 을 위반하지 않는다."""
    document = json.loads(
        (PRESETS_ROOT / preset_id / "openapi.json").read_text(encoding="utf-8")
    )
    for path, item in document["paths"].items():
        for method, operation in item.items():
            for parameter in operation.get("parameters", []):
                schema = parameter.get("schema", {})
                if "enum" in schema and "example" in parameter:
                    assert parameter["example"] in schema["enum"], f"{path} {method}"
                if "enum" in schema and "examples" in schema:
                    for value in schema["examples"]:
                        assert value in schema["enum"], f"{path} {method}"
