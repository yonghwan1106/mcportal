# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Yong Park
"""compiler.sampler 테스트: 하드캡·가드 경유·중복 제거·스크러빙·무키 재생.

respx 로 하위 HTTPTransport 호출만 가로채며 실네트워크 호출은 없다. 인증키는
``+``·``/``·``=`` 를 포함한 합성 문자열이고, 기관·서비스ID·도메인 전부 가상이다
(``.invalid`` 는 RFC 2606 예약 TLD 라 실수로도 실호출이 나가지 않는다).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from urllib.parse import quote, quote_plus

import httpx
import pytest
import respx

from mcportal import QuotaExhausted
from mcportal.compiler import (
    MAX_SAMPLES,
    InferenceConfig,
    OperationSpec,
    ParamSpec,
    SampleRequest,
    SampleResult,
    SourceKind,
    SourceSpec,
    build_sample_requests,
    compile_with_sampling,
    infer_response_schemas,
    sample_source,
)
from mcportal.compiler.openapi import X_MCPORTAL
from mcportal.compiler.sampler import (
    SampleKeyMissingError,
    SampleParamError,
    sample_operation,
    write_samples,
)

BASE_URL = "https://apis.example.invalid/9990000/demo"
LIST_URL = f"{BASE_URL}/getDemoList"

# '+', '/', '=' 를 포함해 인코딩 변형이 뚜렷이 달라지는 합성 키(실키 아님).
DECODED_KEY = "ab12+CD/34=="
# 위 합성 키의 인코딩키 형태.
ENCODED_KEY = "ab12%2BCD%2F34%3D%3D"


def _param(
    name: str,
    *,
    location: str = "query",
    required: bool = True,
    type_: str = "string",
    example: str | None = None,
    default: str | None = None,
    enum: tuple[str, ...] = (),
) -> ParamSpec:
    """테스트용 ParamSpec 을 만든다."""
    return ParamSpec(
        name=name,
        location=location,
        required=required,
        type=type_,
        description=None,
        example=example,
        enum=enum,
        default=default,
        item_type=None,
    )


def _operation(
    operation_id: str = "getDemoList",
    *,
    path: str = "/getDemoList",
    method: str = "GET",
    parameters: tuple[ParamSpec, ...] = (),
) -> OperationSpec:
    """테스트용 OperationSpec 을 만든다."""
    return OperationSpec(
        operation_id=operation_id,
        method=method,
        path=path,
        summary="가상 자료 목록 조회",
        parameters=parameters,
    )


def _source(operations: tuple[OperationSpec, ...] | None = None) -> SourceSpec:
    """테스트용 SourceSpec 을 만든다(합성 기관·.invalid 도메인)."""
    return SourceSpec(
        provider="data.go.kr",
        service_id="99900001",
        service_name="가상행정연구원 공개자료 서비스",
        base_url=BASE_URL,
        source_kind=SourceKind.GW_SWAGGER,
        operations=operations if operations is not None else (_operation(),),
        key_param="serviceKey",
        fingerprint="sha256:" + "cd" * 32,
    )


def _force_operations(
    source: SourceSpec, operations: tuple[OperationSpec, ...]
) -> SourceSpec:
    """IR 단계 방어를 우회해 operations 를 갈아끼운다(2차 방어선 검증용)."""
    object.__setattr__(source, "operations", operations)
    return source


def _paging_operation() -> OperationSpec:
    """페이징 파라미터가 있어 샘플마다 요청이 달라지는 오퍼레이션."""
    return _operation(
        parameters=(
            _param("numOfRows", type_="integer"),
            _param("pageNo", type_="integer"),
        )
    )


def _envelope(page: str, *, echo: str | None = None) -> dict[str, Any]:
    """표준형 JSON 응답 봉투(합성)."""
    body: dict[str, Any] = {
        "response": {
            "header": {"resultCode": "00", "resultMsg": "정상"},
            "body": {"pageNo": page, "items": {"item": [{"name": "가상항목"}]}},
        }
    }
    if echo is not None:
        body["response"]["echoKey"] = echo
    return body


def _json_route(*, echo: str | None = None) -> respx.Route:
    """목록 URL 을 표준형 JSON 으로 응답하는 respx 라우트를 깐다."""

    def handler(request: httpx.Request) -> httpx.Response:
        page = request.url.params.get("pageNo", "0")
        return httpx.Response(200, json=_envelope(page, echo=echo))

    return respx.get(LIST_URL).mock(side_effect=handler)


def _result(
    payload: dict[str, Any],
    *,
    operation_id: str = "getDemoList",
    ok: bool = True,
    result_code: str | None = "00",
    source_format: str = "json",
    status_code: int = 200,
) -> SampleResult:
    """테스트용 SampleResult 를 만든다(네트워크 없이 조립)."""
    return SampleResult(
        operation_id=operation_id,
        status_code=status_code,
        ok=ok,
        result_code=result_code,
        source_format=source_format,
        payload=payload,
    )


# ---------------------------------------------------------------------------
# ① 페이징 파라미터 → 샘플 순번이 증가한다
# ---------------------------------------------------------------------------
def test_paging_parameter_increments_per_sample() -> None:
    requests = build_sample_requests(_source((_paging_operation(),)), "getDemoList", count=3)
    assert len(requests) == 3
    assert [dict(request.params)["pageNo"] for request in requests] == ["1", "2", "3"]
    # 페이지 크기는 관용표 기본값으로 고정된다.
    assert {dict(request.params)["numOfRows"] for request in requests} == {"10"}
    assert all(request.method == "GET" for request in requests)
    assert all(request.path == "/getDemoList" for request in requests)


# ---------------------------------------------------------------------------
# ② 파라미터 값 결정 우선순위
# ---------------------------------------------------------------------------
def test_sample_value_priority_order() -> None:
    parameters = (
        _param("_type"),                                    # 관용표 → json
        _param("byOverride", example="예시무시"),            # overrides 우선
        _param("byExample", example="예시", default="기본"),  # example > default
        _param("byDefault", default="기본", enum=("첫값",)),  # default > enum
        _param("byEnum", enum=("첫값", "둘값")),              # enum[0]
        _param("cnt", type_="integer"),                     # 타입 기본 → 1
        _param("flag", type_="boolean"),                    # 타입 기본 → true
        _param("format"),                                   # 관용표 → json
        _param("numOfRows", type_="integer"),               # 관용표 → 10
        _param("optional", required=False, example="넣지않음"),
        _param("pageNo", type_="integer"),                  # 관용표 → 순번
    )
    requests = build_sample_requests(
        _source((_operation(parameters=parameters),)),
        "getDemoList",
        count=1,
        overrides={"byOverride": "강제값"},
    )
    params = dict(requests[0].params)
    assert params == {
        "_type": "json",
        "byOverride": "강제값",
        "byExample": "예시",
        "byDefault": "기본",
        "byEnum": "첫값",
        "cnt": "1",
        "flag": "true",
        "format": "json",
        "numOfRows": "10",
        "pageNo": "1",
    }
    # 선택 파라미터는 응답 형태를 흔들지 않기 위해 넣지 않는다.
    assert "optional" not in params
    # (name, value) 오름차순으로 고정된다(결정론).
    assert list(requests[0].params) == sorted(requests[0].params)


def test_path_parameter_is_substituted_into_path() -> None:
    operation = _operation(
        "getDemoItem",
        path="/item/{itemId}",
        parameters=(_param("itemId", location="path", example="A-001"),),
    )
    requests = build_sample_requests(_source((operation,)), "getDemoItem", count=1)
    assert requests[0].path == "/item/A-001"
    assert requests[0].params == ()


def test_service_key_never_enters_sample_requests() -> None:
    broken = _operation(parameters=(_param("serviceKey"), _param("pageNo", type_="integer")))
    source = _force_operations(_source(), (broken,))
    requests = build_sample_requests(source, "getDemoList", count=1)
    assert dict(requests[0].params) == {"pageNo": "1"}


# ---------------------------------------------------------------------------
# ③ 필수 파라미터 값 미결정 → SampleParamError
# ---------------------------------------------------------------------------
def test_undeterminable_required_parameter_raises() -> None:
    operation = _operation(parameters=(_param("keyword"),))
    with pytest.raises(SampleParamError) as excinfo:
        build_sample_requests(_source((operation,)), "getDemoList", count=1)
    message = str(excinfo.value)
    assert "getDemoList" in message
    assert "keyword" in message


def test_unknown_operation_id_raises_value_error() -> None:
    with pytest.raises(ValueError) as excinfo:
        build_sample_requests(_source(), "없는오퍼레이션", count=1)
    assert "없는오퍼레이션" in str(excinfo.value)


# ---------------------------------------------------------------------------
# ④ S1 하드캡
# ---------------------------------------------------------------------------
def test_hard_cap_rejects_count_above_max() -> None:
    source = _source((_paging_operation(),))
    with pytest.raises(ValueError) as excinfo:
        build_sample_requests(source, "getDemoList", count=MAX_SAMPLES + 1)
    assert str(MAX_SAMPLES) in str(excinfo.value)

    assert len(build_sample_requests(source, "getDemoList", count=MAX_SAMPLES)) == MAX_SAMPLES

    with pytest.raises(ValueError):
        build_sample_requests(source, "getDemoList", count=0)


def test_sample_operation_rejects_over_cap_request_list() -> None:
    # 하드캡을 넘기는 요청 목록을 억지로 만들어도 실행 단계에서 막힌다(우회 경로 없음).
    over_cap = tuple(
        SampleRequest(
            operation_id="getDemoList",
            method="GET",
            path="/getDemoList",
            params=(("pageNo", str(index)),),
        )
        for index in range(1, MAX_SAMPLES + 2)
    )
    with pytest.raises(ValueError) as excinfo:
        sample_operation(httpx.Client(), over_cap, base_url=BASE_URL)
    assert str(MAX_SAMPLES) in str(excinfo.value)


# ---------------------------------------------------------------------------
# ⑤ S3 중복 제거 — 페이징이 없으면 상위 호출 1회
# ---------------------------------------------------------------------------
@respx.mock
def test_duplicate_requests_are_collapsed_to_one_call(tmp_path: Path) -> None:
    route = _json_route()
    operation = _operation(parameters=(_param("numOfRows", type_="integer"),))
    requests = build_sample_requests(_source((operation,)), "getDemoList", count=3)
    assert len(requests) == 1  # 요청이 동일하므로 1건으로 접힌다.

    results = sample_source(
        _source((operation,)),
        service_key=DECODED_KEY,
        count=3,
        mode="live",
        budget=100,
        ledger_path=tmp_path / "dedup.db",
    )
    assert route.call_count == 1
    assert len(results["getDemoList"]) == 1


# ---------------------------------------------------------------------------
# ⑥ S2·S6 가드 경유 — 예산 소진은 삼키지 않는다
# ---------------------------------------------------------------------------
@respx.mock
def test_quota_guard_blocks_third_sample(tmp_path: Path) -> None:
    route = _json_route()
    with pytest.raises(QuotaExhausted) as excinfo:
        sample_source(
            _source((_paging_operation(),)),
            service_key=DECODED_KEY,
            count=3,
            mode="live",
            budget=2,
            ledger_path=tmp_path / "budget.db",
        )
    assert "운영계정" in str(excinfo.value)
    assert route.call_count == 2  # 3번째는 상위로 나가지 않는다.


# ---------------------------------------------------------------------------
# ⑦ S5 무키 안내
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("mode", ["record", "live"])
def test_missing_key_raises_korean_guidance(mode: str, tmp_path: Path) -> None:
    with pytest.raises(SampleKeyMissingError) as excinfo:
        sample_source(
            _source(),
            service_key=None,
            mode=mode,
            cassette_path=tmp_path / "c.json",
        )
    message = str(excinfo.value)
    assert "인증키" in message
    assert "replay" in message


def test_unknown_mode_and_missing_cassette_raise_value_error(tmp_path: Path) -> None:
    with pytest.raises(ValueError) as excinfo:
        sample_source(_source(), service_key=DECODED_KEY, mode="turbo")
    assert "mode" in str(excinfo.value)

    with pytest.raises(ValueError) as excinfo:
        sample_source(_source(), mode="replay")
    assert "cassette_path" in str(excinfo.value)

    with pytest.raises(ValueError) as excinfo:
        sample_source(_source(), service_key=DECODED_KEY, mode="record")
    assert "cassette_path" in str(excinfo.value)

    with pytest.raises(ValueError) as excinfo:
        sample_source(
            _source(),
            service_key=DECODED_KEY,
            mode="live",
            operation_ids=("없는것",),
        )
    assert "없는것" in str(excinfo.value)


# ---------------------------------------------------------------------------
# ⑧·⑩ record → 카세트 스크러빙 전문 스캔 → 무키 replay 왕복
# ---------------------------------------------------------------------------
@respx.mock
def test_record_scrubs_cassette_then_replays_without_key(tmp_path: Path) -> None:
    # 응답이 요청 키를 되비추는(echo) 최악의 경우까지 재현한다.
    _json_route(echo=DECODED_KEY)
    cassette_path = tmp_path / "cassette.json"

    recorded = sample_source(
        _source((_paging_operation(),)),
        service_key=ENCODED_KEY,
        count=2,
        mode="record",
        cassette_path=cassette_path,
        budget=100,
        ledger_path=tmp_path / "rec.db",
    )
    assert len(recorded["getDemoList"]) == 2

    raw = cassette_path.read_text(encoding="utf-8")
    for variant in (
        DECODED_KEY,
        ENCODED_KEY,
        quote(DECODED_KEY),
        quote(DECODED_KEY, safe=""),
        quote_plus(DECODED_KEY),
    ):
        assert variant not in raw, f"카세트에 키 변형이 남았다: {variant}"
    assert "__SCRUBBED__" in raw

    # 무키 재생: service_key 없이 같은 결과를 얻는다.
    replayed = sample_source(
        _source((_paging_operation(),)),
        count=2,
        mode="replay",
        cassette_path=cassette_path,
    )
    pages = [
        result.payload["response"]["body"]["pageNo"] for result in replayed["getDemoList"]
    ]
    assert pages == ["1", "2"]
    assert all(result.ok and result.result_code == "00" for result in replayed["getDemoList"])
    assert all(
        result.payload["response"]["echoKey"] == "__SCRUBBED__"
        for result in replayed["getDemoList"]
    )


# ---------------------------------------------------------------------------
# ⑨·⑭ S7 write_samples — 키 변형 0건 + 결정론
# ---------------------------------------------------------------------------
def test_write_samples_scrubs_echoed_key(tmp_path: Path) -> None:
    payload = _envelope("1", echo=DECODED_KEY)
    payload["response"]["encodedEcho"] = ENCODED_KEY
    results = {"getDemoList": (_result(payload),)}

    written = write_samples(results, tmp_path / "samples", secrets=[DECODED_KEY, ENCODED_KEY])
    assert [path.name for path in written] == ["getDemoList_01.json"]

    text = written[0].read_text(encoding="utf-8")
    for variant in (
        DECODED_KEY,
        ENCODED_KEY,
        quote(DECODED_KEY),
        quote(DECODED_KEY, safe=""),
        quote_plus(DECODED_KEY),
    ):
        assert variant not in text, f"샘플 파일에 키 변형이 남았다: {variant}"
    assert "__SCRUBBED__" in text


def test_write_samples_is_deterministic_and_lf_only(tmp_path: Path) -> None:
    results = {
        "getDemoList": (_result(_envelope("1")), _result(_envelope("2"))),
        "getDemoItem": (_result(_envelope("1"), operation_id="getDemoItem"),),
    }
    first_dir = tmp_path / "one"
    second_dir = tmp_path / "two"
    # secrets 는 키워드 필수 인자다(S7). 지울 시크릿이 없는 경로도 명시한다.
    first = write_samples(results, first_dir, secrets=[])
    second = write_samples(results, second_dir, secrets=[])

    assert [path.name for path in first] == [
        "getDemoItem_01.json",
        "getDemoList_01.json",
        "getDemoList_02.json",
    ]
    for left, right in zip(first, second):
        data = left.read_bytes()
        assert data == right.read_bytes()
        assert b"\r" not in data
        assert data.endswith(b"\n") and not data.endswith(b"\n\n")


# ---------------------------------------------------------------------------
# ⑪ XML(EUC-KR) 샘플 → source_format 판정 + R5 자동 선택
# ---------------------------------------------------------------------------
@respx.mock
def test_xml_samples_select_singleton_array_correction(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        page = request.url.params.get("pageNo", "0")
        # 1페이지는 item 1건(dict), 2페이지는 2건(list) → 같은 위치가 갈린다.
        items = (
            "<item><name>가상항목</name></item>"
            if page == "1"
            else "<item><name>가상항목</name></item><item><name>둘째항목</name></item>"
        )
        xml = (
            "<response><header><resultCode>00</resultCode>"
            "<resultMsg>정상</resultMsg></header>"
            f"<body><items>{items}</items></body></response>"
        )
        return httpx.Response(
            200,
            headers={"content-type": "application/xml; charset=euc-kr"},
            content=xml.encode("cp949"),
        )

    respx.get(LIST_URL).mock(side_effect=handler)
    results = sample_source(
        _source((_paging_operation(),)),
        service_key=DECODED_KEY,
        count=2,
        mode="live",
        budget=100,
        ledger_path=tmp_path / "xml.db",
    )
    samples = results["getDemoList"]
    assert [result.source_format for result in samples] == ["xml", "xml"]
    assert samples[0].payload["response"]["body"]["items"]["item"]["name"] == "가상항목"

    schemas, reports = infer_response_schemas(results)
    item = schemas["getDemoList"]["properties"]["response"]["properties"]["body"][
        "properties"
    ]["items"]["properties"]["item"]
    assert item["type"] == "array"  # R5 보정이 켜졌다.
    assert "items" in item
    assert reports["getDemoList"].sample_count == 2

    # config 를 명시하면 자동 선택을 하지 않으므로 진짜 충돌(anyOf)로 남는다.
    strict, _ = infer_response_schemas(
        results, config=InferenceConfig(xml_singleton_arrays=False)
    )
    strict_item = strict["getDemoList"]["properties"]["response"]["properties"]["body"][
        "properties"
    ]["items"]["properties"]["item"]
    assert "anyOf" in strict_item


# ---------------------------------------------------------------------------
# ⑫ ok=False 결과는 추론 입력에서 제외된다
# ---------------------------------------------------------------------------
def test_failed_results_are_excluded_from_inference() -> None:
    error_payload = {
        "response": {"header": {"resultCode": "03", "resultMsg": "NODATA_ERROR"}}
    }
    results = {
        "mixedOp": (
            _result(error_payload, operation_id="mixedOp", ok=False, result_code="03"),
            _result(_envelope("1"), operation_id="mixedOp"),
        ),
        "allFailedOp": (
            _result(error_payload, operation_id="allFailedOp", ok=False, result_code="03"),
        ),
    }
    schemas, reports = infer_response_schemas(results)

    assert "allFailedOp" not in schemas  # 전부 실패 → 폴백으로 넘긴다.
    assert "allFailedOp" not in reports
    assert reports["mixedOp"].sample_count == 1
    body = schemas["mixedOp"]["properties"]["response"]["properties"]
    assert "body" in body  # 성공 샘플만 반영됐다.


# ---------------------------------------------------------------------------
# ⑬ compile_with_sampling → generation_mode "sampled"
# ---------------------------------------------------------------------------
@respx.mock
def test_compile_with_sampling_marks_sampled_mode(tmp_path: Path) -> None:
    _json_route()
    compiled, results = compile_with_sampling(
        _source((_paging_operation(),)),
        service_key=DECODED_KEY,
        count=2,
        mode="live",
        budget=100,
        ledger_path=tmp_path / "compile.db",
    )
    meta = compiled.document["info"][X_MCPORTAL]
    assert meta["generation_mode"] == "sampled"
    assert meta["sample_count"] == 2
    assert "unresolved" not in meta["schema_inference"]
    assert len(results["getDemoList"]) == 2

    schema = compiled.document["components"]["schemas"]["GetDemoListResponse"]
    assert schema["type"] == "object"
    assert "response" in schema["properties"]
    # 샘플 값이 스키마로 새어 나가지 않는다(R8).
    assert "가상항목" not in str(schema)
