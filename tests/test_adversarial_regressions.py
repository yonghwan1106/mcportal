# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Yong Park
"""적대 리뷰(W2)에서 재현된 결함들의 회귀 테스트.

각 테스트는 **결함이 살아 있으면 반드시 실패하도록** 반대 방향 표본으로 쓰였다.
원래 빌더 테스트들이 결함을 놓친 이유는 대부분 자기 선택 표본이었기 때문이다 —
문자열 타입 파라미터만 본 파라미터 테스트, 전 샘플이 XML 인 추론 테스트,
``respx`` 의 ``json=`` 만 쓴 스크러빙 테스트, ``await`` 를 순차로 건 async 예산
테스트가 그렇다. 여기서는 그 경계를 일부러 밟는다.

픽스처는 100% 합성이다. 실인증키·실네트워크·실데이터를 쓰지 않으며, 등장하는
기관명·서비스명·키 값은 모두 가상이다.
"""
from __future__ import annotations

import asyncio
import gc
import json
import shutil
import tempfile
import threading
import time
import tomllib
from dataclasses import replace
from itertools import permutations
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx

from mcportal.compiler.inference import infer_schema, infer_schema_with_report
from mcportal.compiler.openapi import CompileError, build_openapi, write_spec
from mcportal.compiler.sampler import (
    MAX_SAMPLES,
    SampleResult,
    infer_response_schemas,
    sample_source,
    write_samples,
)
from mcportal.compiler.sources import SourceSpecError, load_source
from mcportal.mcp import MCPortalAsyncTransport, server_from_spec
from mcportal.profiles.datago import DATA_GO_KR
from mcportal.quota.exceptions import QuotaExhausted
from mcportal.replay.cassette import Cassette, RecordingTransport
from mcportal.replay.scrub import SCRUB_PLACEHOLDER, scrub_text
from mcportal.transport import MCPortalTransport, _build_guard, create_client

# 가상 호스트·가상 인증키. 실제 존재하지 않는 도메인(.invalid)과 명백한 더미 값이다.
HOST = "apis.example.invalid"
BASE = f"https://{HOST}/9990000/demo"
FAKE_KEY = "FAKEKEYaaa+bbb/ccc=="
REPO_ROOT = Path(__file__).resolve().parents[1]


def _swagger(
    *,
    operation_id: str = "getRows",
    produces: str = "application/json",
    parameters: list[dict[str, Any]] | None = None,
    path: str = "/rows",
) -> dict[str, Any]:
    """합성 GW Swagger 2.0 문서를 만든다."""
    operation: dict[str, Any] = {
        "operationId": operation_id,
        "responses": {"200": {"description": "정상"}},
    }
    if parameters is not None:
        operation["parameters"] = parameters
    return {
        "swagger": "2.0",
        "host": HOST,
        "basePath": "/9990000/demo",
        "schemes": ["https"],
        "produces": [produces],
        "paths": {path: {"get": operation}},
    }


def _source(**kwargs: Any) -> Any:
    """합성 문서를 SourceSpec 으로 정규화한다."""
    return load_source(
        _swagger(**kwargs), service_id="9990000", service_name="가상 데모 서비스"
    )


def _sample(fmt: str, ok: bool, payload: dict[str, Any]) -> SampleResult:
    """합성 샘플 결과 1건."""
    return SampleResult(
        operation_id="getRows",
        status_code=200,
        ok=ok,
        result_code="00" if ok else "30",
        source_format=fmt,
        payload=payload,
    )


# ---------------------------------------------------------------------------
# CRITICAL — scrub.py: 본문 에코 변형 누락
# ---------------------------------------------------------------------------
def test_scrub_text_covers_json_escape_and_lowercase_percent() -> None:
    """PHP ``json_encode`` 의 ``\\/`` 표기와 소문자 퍼센트 인코딩까지 지운다.

    기존 테스트는 ``respx`` 의 ``json=``(``json.dumps`` 기본, ``/`` 미이스케이프)만
    써서 이미 아는 4가지 전송 변형만 검증했다. 실제 게이트웨이는 ``\\/`` 와
    ``%2b`` 를 되비추며, 그 표기가 남으면 ``json.loads``/``unquote`` 한 번으로
    디코딩키 원문이 복원된다.
    """
    secret = "ab12+CD/34=="
    bodies = {
        "원문": f'{{"echo": "{secret}"}}',
        "JSON 이스케이프": '{"echo": "ab12+CD\\/34=="}',
        "소문자 퍼센트": '{"echo": "ab12%2bCD%2f34%3d%3d"}',
        "대문자 퍼센트": '{"echo": "ab12%2BCD%2F34%3D%3D"}',
    }
    for label, body in bodies.items():
        out = scrub_text(body, [secret])
        assert SCRUB_PLACEHOLDER in out, f"{label}: 치환되지 않았다 — {out}"
        # 디코딩 한 번으로 키가 복원되지 않는지 반대 방향으로 확인한다.
        assert "ab12" not in out, f"{label}: 키 접두가 남았다 — {out}"


# ---------------------------------------------------------------------------
# CRITICAL — cassette.py: 이름으로 지운 키가 본문 에코에는 평문으로 남는다
# ---------------------------------------------------------------------------
def _echo_transport() -> httpx.MockTransport:
    """요청 쿼리를 본문에 그대로 되비추는 합성 게이트웨이."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"cmmMsgHeader": {"requestUrl": str(request.url)}},
            headers={"content-type": "application/json"},
        )

    return httpx.MockTransport(handler)


def test_cassette_scrubs_caller_supplied_key_echoed_in_response_body() -> None:
    """호출자가 params 에 직접 실은 인증키도 본문 에코에서 지워진다.

    ``scrub_url``·``scrub_params`` 는 *이름*으로 지우고 본문은 *값*으로만 지운다.
    클라이언트가 보관한 시크릿 목록에는 호출자 키가 없으므로, 이름으로 식별한
    값을 값 기반 스크러빙에 합류시키지 않으면 URL 은 ``__SCRUBBED__`` 인데
    응답 본문에는 평문이 남는다. 카세트는 커밋 대상 파일이다.
    """
    caller_key = "CALLER-SUPPLIED-KEY-9999"
    cassette = Cassette()
    recording = RecordingTransport(
        _echo_transport(), cassette, secrets=["CLIENT-HELD-KEY-0001"]
    )
    with httpx.Client(transport=recording) as client:
        client.get(f"{BASE}/rows", params={"serviceKey": caller_key, "pageNo": "1"})

    interaction = cassette.interactions[0]
    assert SCRUB_PLACEHOLDER in interaction["request"]["url"]
    assert caller_key not in interaction["request"]["url"]
    assert caller_key not in json.dumps(interaction, ensure_ascii=False)
    assert SCRUB_PLACEHOLDER in interaction["response"]["body"]


def test_cassette_scrubs_profile_alias_key_echoed_in_response_body() -> None:
    """F10 별칭(``apiKey``)으로 선언한 키도 본문 에코에서 지워진다."""
    alias_key = "ALIASKEY-SECRET-VALUE"
    profile = replace(DATA_GO_KR, key_param_aliases=("apiKey", "authKey"))
    from mcportal.profiles.datago import key_params_of

    names = key_params_of(profile)
    assert "apiKey" in names  # 전제 확인: 별칭이 실제로 키 이름 목록에 든다.

    cassette = Cassette(key_params=names)
    recording = RecordingTransport(
        _echo_transport(), cassette, secrets=[], key_params=names
    )
    with httpx.Client(transport=recording) as client:
        client.get(f"{BASE}/rows", params={"apiKey": alias_key, "pageNo": "1"})

    dumped = json.dumps(cassette.interactions, ensure_ascii=False)
    assert alias_key not in dumped
    assert SCRUB_PLACEHOLDER in cassette.interactions[0]["response"]["body"]


# ---------------------------------------------------------------------------
# MAJOR — scrub.py: F10 매개변수화가 off-switch가 되면 안 된다
# ---------------------------------------------------------------------------
def test_empty_key_params_cannot_disable_cassette_scrubbing() -> None:
    """생성자에 빈 key_params 를 넘겨도 스크러빙은 살아 있다.

    ``Cassette``·``RecordingTransport`` 는 공개 API 로 재수출되므로 외부에서
    직접 도달할 수 있다. W1 에서는 물리적으로 불가능했던 "스크러빙 없는 카세트"
    상태가 F10 매개변수화로 만들어지면 안 된다.
    """
    cassette = Cassette(key_params=())
    assert cassette.key_params == ("serviceKey",)  # 기본값 폴백.

    recording = RecordingTransport(
        _echo_transport(), cassette, secrets=[], key_params=[]
    )
    with httpx.Client(transport=recording) as client:
        client.get(f"{BASE}/rows", params={"serviceKey": FAKE_KEY, "pageNo": "1"})

    dumped = json.dumps(cassette.interactions, ensure_ascii=False)
    assert FAKE_KEY not in dumped
    assert "FAKEKEYaaa" not in dumped


def test_cassette_key_params_roundtrip_is_symmetric(tmp_path: Path) -> None:
    """save→load 라운드트립에서 key_params 가 바뀌지 않는다(저장된 값 우선)."""
    aliased = Cassette(key_params=("serviceKey", "apiKey"))
    path = tmp_path / "aliased.json"
    aliased.save(path)
    assert Cassette.load(path).key_params == ("serviceKey", "apiKey")

    # 기본값과 같으면 필드를 쓰지 않는다(W1 카세트와 바이트 동일성 유지).
    default = Cassette()
    default_path = tmp_path / "default.json"
    default.save(default_path)
    stored = json.loads(default_path.read_text(encoding="utf-8"))
    assert "key_params" not in stored
    assert Cassette.load(default_path).key_params == ("serviceKey",)

    # 필드가 명시적으로 있으면 그 값을 그대로 복원한다("없음"과 구분한다).
    explicit = tmp_path / "explicit.json"
    explicit.write_text(
        json.dumps({"version": 1, "key_params": ["authKey"], "interactions": []}),
        encoding="utf-8",
    )
    assert Cassette.load(explicit).key_params == ("authKey",)

    # 손으로 빈 목록을 적어 넣은 파일도 스크러빙이 켜진 상태로 읽힌다 —
    # "스크러빙 없이 녹화된 카세트"인 척하는 파일이 존재할 수 없어야 한다.
    empty = tmp_path / "empty.json"
    empty.write_text(
        json.dumps({"version": 1, "key_params": [], "interactions": []}),
        encoding="utf-8",
    )
    assert Cassette.load(empty).key_params == ("serviceKey",)


# ---------------------------------------------------------------------------
# MAJOR — cassette.py: POST 재생이 다른 본문의 응답을 돌려준다
# ---------------------------------------------------------------------------
def test_post_replay_matches_on_request_body(tmp_path: Path) -> None:
    """같은 URL에 서로 다른 본문을 보낸 POST 가 서로의 응답을 받지 않는다.

    W1 까지는 GET 전용이라 드러나지 않았지만 W2 가 POST 경로를 실제로 열었다.
    본문이 매칭 키에 없으면 재생이 **오류 없이 조용히 틀린 데이터**를 돌려준다.
    """
    cassette_path = tmp_path / "post.json"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"forBody": request.content.decode("utf-8")})

    with respx.mock(assert_all_called=False) as mock:
        mock.route(host=HOST).mock(side_effect=handler)
        client = create_client(
            "K",
            mode="record",
            cassette_path=cassette_path,
            budget=100,
            ledger_path=tmp_path / "ledger.db",
        )
        client.post(f"{BASE}/submit", content=b'{"id":"AAA"}')
        client.post(f"{BASE}/submit", content=b'{"id":"BBB"}')
        client.close()

    stored = json.loads(cassette_path.read_text(encoding="utf-8"))
    assert len(stored["interactions"]) == 2
    assert "body" in stored["interactions"][0]["request"]

    replay = create_client(None, mode="replay", cassette_path=cassette_path)
    with replay:
        assert replay.post(f"{BASE}/submit", content=b'{"id":"AAA"}').json() == {
            "forBody": '{"id":"AAA"}'
        }
        assert replay.post(f"{BASE}/submit", content=b'{"id":"BBB"}').json() == {
            "forBody": '{"id":"BBB"}'
        }


def test_body_less_get_cassettes_stay_backward_compatible(tmp_path: Path) -> None:
    """``body`` 필드가 없는 W1 형식 카세트가 그대로 재생된다."""
    path = tmp_path / "w1.json"
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "interactions": [
                    {
                        "request": {
                            "method": "GET",
                            "url": f"{BASE}/rows?pageNo=1",
                            "params": {"pageNo": "1"},
                        },
                        "response": {
                            "status": 200,
                            "headers": {"content-type": "application/json"},
                            "body": '{"ok": true}',
                        },
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    with create_client(None, mode="replay", cassette_path=path) as client:
        assert client.get(f"{BASE}/rows", params={"pageNo": "1"}).json() == {"ok": True}


# ---------------------------------------------------------------------------
# CRITICAL — mcp.py: async 브리지가 하드 예산 상한을 무력화한다
# ---------------------------------------------------------------------------
def test_async_bridge_enforces_hard_budget_under_concurrency(tmp_path: Path) -> None:
    """동시 tool call 20건이 예산 2건을 넘기지 못한다.

    기존 async 예산 테스트는 ``await`` 를 순차로 걸어 이 경로를 통과시켰다.
    브리지가 요청마다 워커 스레드를 쓰므로 sync 경로에서는 불가능했던 동시
    in-flight 가 생기고, before_call(판정)과 after_call(기록) 사이가 원자적이지
    않으면 동시 요청 전부가 판정을 통과한다. MCP 서버는 정의상 동시 tool call 을
    받으므로 이것은 실사용 조건이다.
    """
    budget = 2
    concurrent = 20
    calls = {"n": 0}
    counter_lock = threading.Lock()

    def handler(request: httpx.Request) -> httpx.Response:
        with counter_lock:
            calls["n"] += 1
        # 상위 호출이 즉시 끝나면 in-flight 창이 너무 좁아 경합이 우연히 비껴간다.
        # 실제 네트워크 왕복만큼 창을 벌려, 예약 카운터가 유일한 방어선이 되게 한다.
        time.sleep(0.05)
        return httpx.Response(200, json={"ok": True})

    guard = _build_guard(budget, tmp_path / "ledger.db")
    sync_transport = MCPortalTransport(
        "K", inner=httpx.MockTransport(handler), guard=guard, cache=None
    )

    async def run() -> tuple[int, int]:
        transport = MCPortalAsyncTransport(sync_transport)
        async with httpx.AsyncClient(transport=transport) as client:

            async def one() -> str:
                try:
                    await client.get(f"{BASE}/rows")
                    return "ok"
                except QuotaExhausted:
                    return "blocked"

            outcomes = await asyncio.gather(*[one() for _ in range(concurrent)])
        return outcomes.count("ok"), outcomes.count("blocked")

    try:
        succeeded, blocked = asyncio.run(run())
    finally:
        guard.close()

    assert calls["n"] <= budget, f"실제 상위 호출이 하드 상한을 넘었다: {calls['n']}"
    assert succeeded == budget
    assert blocked == concurrent - budget


def test_quota_reservation_is_released_when_upstream_fails(tmp_path: Path) -> None:
    """상위 호출이 예외로 끝나도 in-flight 예약이 반납된다.

    반납되지 않으면 그 자리가 영구히 소비된 것으로 남아 예산이 실제보다 빨리
    소진된 것처럼 보인다.
    """
    attempts = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["n"] += 1
        raise httpx.ConnectError("합성 전송 오류", request=request)

    guard = _build_guard(10, tmp_path / "ledger.db")
    transport = MCPortalTransport(
        "K",
        inner=httpx.MockTransport(handler),
        guard=guard,
        cache=None,
        max_retries=0,
        sleep=lambda _seconds: None,
    )
    try:
        with httpx.Client(transport=transport) as client:
            for _ in range(3):
                with pytest.raises(httpx.ConnectError):
                    client.get(f"{BASE}/rows")
        # 예약이 새면 pending 이 3까지 쌓여 남은 예산을 잠식한다.
        assert guard._pending == 0
    finally:
        guard.close()


# ---------------------------------------------------------------------------
# CRITICAL — XML 응답 서비스의 MCP 도구가 호출되면 항상 실패한다
# ---------------------------------------------------------------------------
XML_BODY = (
    '<?xml version="1.0" encoding="UTF-8"?>'
    "<response><header><resultCode>00</resultCode></header>"
    "<body><totalCount>1</totalCount></body></response>"
)


def _xml_async_client(content_type: str) -> httpx.AsyncClient:
    """XML 본문을 돌려주는 합성 상위 API 위에 브리지를 얹는다."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=XML_BODY.encode("utf-8"),
            headers={"content-type": content_type},
        )

    sync_transport = MCPortalTransport(
        None, inner=httpx.MockTransport(handler), guard=None, cache=None
    )
    return httpx.AsyncClient(
        transport=MCPortalAsyncTransport(sync_transport), base_url=BASE
    )


@pytest.mark.parametrize(
    ("produces", "content_type"),
    [
        # 소스가 XML 을 선언하고 실제로 XML 이 온다.
        ("application/xml", "application/xml"),
        # _type=json 이 무시돼 XML 이 오는 data.go.kr 실제 상황.
        ("application/json", "application/xml"),
    ],
)
def test_xml_response_tool_call_returns_structured_output(
    produces: str, content_type: str
) -> None:
    """XML 응답 서비스의 도구가 실제로 호출된다(정규화 JSON 으로 올라온다).

    컴파일러는 XML→dict 변환 결과에서 스키마를 추론해 ``outputSchema`` 로 싣는데
    런타임이 원본 XML 바이트를 그대로 넘기면 fastmcp 가 structured output 을
    만들지 못해 도구 호출이 100% 실패한다(목록에는 뜬다). 기존 배선 테스트는
    JSON 카세트 1건만 써서 이 경계를 밟지 않았다.
    """
    fastmcp = pytest.importorskip("fastmcp", reason="[mcp] extra 미설치")

    source = _source(produces=produces)
    samples = {
        "getRows": (
            _sample(
                "xml",
                True,
                {
                    "response": {
                        "header": {"resultCode": "00"},
                        "body": {"totalCount": "1"},
                    }
                },
            ),
        )
    }
    schemas, reports = infer_response_schemas(samples)
    document = build_openapi(source, schemas, reports=reports).document
    # 200 content 키는 선언과 무관하게 application/json 이다.
    assert set(document["paths"]["/rows"]["get"]["responses"]["200"]["content"]) == {
        "application/json"
    }

    async def run() -> Any:
        client = _xml_async_client(content_type)
        server = server_from_spec(document, client=client, name="가상 XML 서비스")
        async with fastmcp.Client(server) as session:
            return await session.call_tool("getRows", {})

    result = asyncio.run(run())
    assert result.structured_content == {
        "response": {"header": {"resultCode": "00"}, "body": {"totalCount": "1"}}
    }


# ---------------------------------------------------------------------------
# MAJOR — openapi.py: 만족 불가능한 파라미터 스키마
# ---------------------------------------------------------------------------
def test_typed_parameters_get_typed_enum_default_and_example() -> None:
    """정수·수·불리언 파라미터의 enum/default/example 이 선언 타입으로 실린다.

    기존 파라미터 테스트는 string 타입만 표본으로 써서 이 경계를 밟지 않았다.
    ``{"type":"integer","enum":["10","100"]}`` 는 어떤 값으로도 만족할 수 없다.
    """
    document = build_openapi(
        _source(
            parameters=[
                {
                    "name": "numOfRows",
                    "in": "query",
                    "required": True,
                    "type": "integer",
                    "enum": [10, 100, 1000],
                    "default": 10,
                    "x-example": "10",
                },
                {
                    "name": "flag",
                    "in": "query",
                    "required": False,
                    "type": "boolean",
                    "default": False,
                },
                {
                    "name": "ratio",
                    "in": "query",
                    "required": False,
                    "type": "number",
                    "enum": [0.5, 1.0],
                },
                {
                    "name": "codes",
                    "in": "query",
                    "required": False,
                    "type": "array",
                    "items": {"type": "integer"},
                    "enum": [1, 2],
                },
            ]
        )
    ).document
    listed = {
        item["name"]: item
        for item in document["paths"]["/rows"]["get"]["parameters"]
    }

    rows = listed["numOfRows"]["schema"]
    assert rows["enum"] == [10, 100, 1000]
    assert rows["default"] == 10
    # 만족 집합이 비지 않는지 직접 확인한다: default 는 enum 의 원소여야 한다.
    assert rows["default"] in rows["enum"]
    assert all(isinstance(value, int) for value in rows["enum"])

    assert listed["flag"]["schema"]["default"] is False
    assert listed["ratio"]["schema"]["enum"] == [0.5, 1.0]
    # 배열은 원소 허용값이라는 실제 의미대로 items.enum 으로 옮긴다.
    codes = listed["codes"]["schema"]
    assert codes["type"] == "array"
    assert codes["items"] == {"type": "integer", "enum": [1, 2]}
    assert "enum" not in codes


def test_uncastable_enum_is_dropped_rather_than_made_unsatisfiable() -> None:
    """캐스팅할 수 없는 열거값은 통째로 생략한다(만족 불가능 스키마를 안 만든다)."""
    document = build_openapi(
        _source(
            parameters=[
                {
                    "name": "limit",
                    "in": "query",
                    "required": False,
                    "type": "integer",
                    "enum": ["all", "10"],
                }
            ]
        )
    ).document
    schema = document["paths"]["/rows"]["get"]["parameters"][0]["schema"]
    assert schema == {"type": "integer"}


# ---------------------------------------------------------------------------
# MAJOR — openapi.py: path 템플릿과 파라미터 선언 불일치
# ---------------------------------------------------------------------------
def test_path_template_without_declared_parameter_is_rejected() -> None:
    """대응 파라미터가 없는 path 템플릿은 CompileError 로 막는다.

    통과시키면 리터럴 ``{missing}`` 이 퍼센트 인코딩되어 경로에 박히고, 도구는
    상시 404 를 내면서 매 호출마다 쿼터를 태운다.
    """
    document = {
        "openapi": "3.0.3",
        "info": {"title": "가상 서비스"},
        "servers": [{"url": BASE}],
        "paths": {
            "/orphan/{missing}": {
                "get": {"operationId": "orphan", "responses": {"200": {"description": "정상"}}}
            }
        },
    }
    source = load_source(document, service_id="9990000", service_name="가상 서비스")
    with pytest.raises(CompileError) as excinfo:
        build_openapi(source)
    assert "missing" in str(excinfo.value)


def test_declared_path_parameter_matching_template_compiles() -> None:
    """정상 짝이 맞으면 그대로 컴파일된다(게이트가 과잉 차단하지 않는다)."""
    document = {
        "openapi": "3.0.3",
        "info": {"title": "가상 서비스"},
        "servers": [{"url": BASE}],
        "paths": {
            "/orgs/{orgCode}": {
                "get": {
                    "operationId": "getOrg",
                    "parameters": [
                        {
                            "name": "orgCode",
                            "in": "path",
                            "required": True,
                            "schema": {"type": "string"},
                        }
                    ],
                    "responses": {"200": {"description": "정상"}},
                }
            }
        },
    }
    source = load_source(document, service_id="9990000", service_name="가상 서비스")
    compiled = build_openapi(source)
    assert compiled.operation_ids == ("getOrg",)


# ---------------------------------------------------------------------------
# MAJOR — openapi.py: 자유문자열 메타의 인증키
# ---------------------------------------------------------------------------
def test_free_text_metadata_carrying_a_key_is_rejected() -> None:
    """실키가 붙은 source_url·license_note·description 은 CompileError 다.

    parameters 에는 2차 방어선이 있는데 사람이 손으로 채우는 문자열에는 없었다.
    산출 문서는 ``specs/`` 아래 커밋 대상이므로 현실적인 유출 경로다.
    """
    base = _source()
    leaky_url = replace(
        base, source_url=f"https://{HOST}/demo/swagger.json?serviceKey=FAKEKEYaaa"
    )
    with pytest.raises(CompileError):
        build_openapi(leaky_url)

    with pytest.raises(CompileError):
        build_openapi(replace(base, license_note="apiKey=FAKEKEYaaa 로 받았음"))

    with pytest.raises(CompileError):
        build_openapi(replace(base, description="authKey=FAKEKEYaaa"))

    # 스크러빙된 URL 은 통과한다(과잉 차단 방지).
    clean = replace(
        base, source_url=f"https://{HOST}/demo/swagger.json?serviceKey={SCRUB_PLACEHOLDER}"
    )
    assert build_openapi(clean).document["info"]["x-mcportal"]["source_url"]


def test_write_spec_refuses_to_persist_a_leaked_key(tmp_path: Path) -> None:
    """어떤 경로로 만들어진 문서든 실키가 있으면 디스크에 닿지 않는다."""
    target = tmp_path / "openapi.json"
    with pytest.raises(CompileError):
        write_spec({"info": {"x": f"https://{HOST}/s?serviceKey=FAKEKEYaaa"}}, target)
    assert not target.exists()

    written = write_spec({"info": {"title": "가상 서비스"}}, tmp_path / "clean.json")
    assert written.exists()


# ---------------------------------------------------------------------------
# MAJOR — sources.py: $ref 인라인 전개 폭발
# ---------------------------------------------------------------------------
def test_shared_ref_expansion_is_bounded() -> None:
    """공유 정의를 반복 참조하는 정상(비순환) 문서가 상한에서 거부된다.

    순환 참조만 막고 폭발을 막지 않으면, 깊이 20짜리 문서 하나가 수십 MB 스키마와
    수십 초 CPU 를 만들어 그대로 디스크에 쓰인다. 스펙 문서를 원격에서 받아 온다는
    점에서 서비스 거부 경로다.
    """
    depth = 20
    definitions: dict[str, Any] = {
        "D0": {"type": "object", "properties": {"v": {"type": "string"}}}
    }
    for index in range(1, depth + 1):
        definitions[f"D{index}"] = {
            "type": "object",
            "properties": {
                "a": {"$ref": f"#/definitions/D{index - 1}"},
                "b": {"$ref": f"#/definitions/D{index - 1}"},
            },
        }
    document = {
        "swagger": "2.0",
        "host": HOST,
        "basePath": "/9990000/demo",
        "schemes": ["https"],
        "definitions": definitions,
        "paths": {
            "/rows": {
                "get": {
                    "operationId": "getRows",
                    "responses": {
                        "200": {
                            "description": "정상",
                            "schema": {"$ref": f"#/definitions/D{depth}"},
                        }
                    },
                }
            }
        },
    }
    with pytest.raises(SourceSpecError) as excinfo:
        load_source(document, service_id="9990000", service_name="가상 서비스")
    assert "$ref" in str(excinfo.value)


def test_modest_shared_refs_still_resolve() -> None:
    """상한 안쪽의 공유 참조는 정상적으로 펼쳐진다(과잉 차단 방지)."""
    document = {
        "swagger": "2.0",
        "host": HOST,
        "basePath": "/9990000/demo",
        "schemes": ["https"],
        "definitions": {
            "Item": {"type": "object", "properties": {"id": {"type": "string"}}},
            "Page": {
                "type": "object",
                "properties": {
                    "first": {"$ref": "#/definitions/Item"},
                    "second": {"$ref": "#/definitions/Item"},
                },
            },
        },
        "paths": {
            "/rows": {
                "get": {
                    "operationId": "getRows",
                    "responses": {
                        "200": {
                            "description": "정상",
                            "schema": {"$ref": "#/definitions/Page"},
                        }
                    },
                }
            }
        },
    }
    source = load_source(document, service_id="9990000", service_name="가상 서비스")
    schema = source.operations[0].response_schema
    assert schema is not None
    assert schema["properties"]["first"]["properties"]["id"] == {"type": "string"}


# ---------------------------------------------------------------------------
# MINOR — sources.py: responses.default 오류 봉투가 200 스키마로 채택된다
# ---------------------------------------------------------------------------
def test_default_error_envelope_is_not_adopted_as_the_200_schema() -> None:
    """``responses.default`` 는 정상 응답 스키마 출처가 아니다.

    정부 swagger 는 ``default`` 에 오류 봉투(errorCode/errorMsg)를 적어 두는
    경우가 있다. 폴백을 두면 그 오류 스키마가 MCP 도구의 200 응답 설명이 되고,
    "샘플링으로 채울 자리(None)" 표시도 사라져 추론 대상에서 빠진다.
    """
    document = {
        "swagger": "2.0",
        "host": HOST,
        "basePath": "/9990000/demo",
        "schemes": ["https"],
        "paths": {
            "/rows": {
                "get": {
                    "operationId": "getRows",
                    "responses": {
                        "default": {
                            "description": "오류",
                            "schema": {
                                "type": "object",
                                "properties": {
                                    "errorCode": {"type": "string"},
                                    "errorMsg": {"type": "string"},
                                },
                            },
                        }
                    },
                }
            }
        },
    }
    source = load_source(document, service_id="9990000", service_name="가상 서비스")
    assert source.operations[0].response_schema is None

    # 폴백 스키마가 실리고 unresolved 카운트가 선다(추론 대상으로 넘어간다).
    compiled = build_openapi(source).document
    schema = compiled["components"]["schemas"]["GetRowsResponse"]
    assert "errorCode" not in json.dumps(schema, ensure_ascii=False)
    assert compiled["info"]["x-mcportal"]["schema_inference"]["unresolved"] == 1

    # 대조군: 200 이 스키마를 주면 그대로 채택된다.
    document["paths"]["/rows"]["get"]["responses"]["200"] = {
        "description": "정상",
        "schema": {"type": "object", "properties": {"total": {"type": "integer"}}},
    }
    ok_source = load_source(document, service_id="9990000", service_name="가상 서비스")
    assert ok_source.operations[0].response_schema == {
        "type": "object",
        "properties": {"total": {"type": "integer"}},
    }


# ---------------------------------------------------------------------------
# MAJOR — sources.py: 한글 operationId → 쓸 수 없는 도구명
# ---------------------------------------------------------------------------
def test_korean_operation_id_becomes_a_usable_mcp_tool_name() -> None:
    """한글 전용 operationId 가 빈 MCP 도구명을 만들지 않는다(SEP-986)."""
    fastmcp = pytest.importorskip("fastmcp", reason="[mcp] extra 미설치")

    source = _source(operation_id="목록조회")
    assert source.operations[0].operation_id.strip("_"), "밑줄뿐인 식별자가 남았다"

    document = build_openapi(source).document
    # 스키마 이름도 숫자로 시작하지 않는다.
    for name in document["components"]["schemas"]:
        assert name[0].isalpha(), f"스키마 이름이 식별자로 부적절하다: {name}"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": True})

    sync_transport = MCPortalTransport(
        None, inner=httpx.MockTransport(handler), guard=None, cache=None
    )
    client = httpx.AsyncClient(
        transport=MCPortalAsyncTransport(sync_transport), base_url=BASE
    )

    async def run() -> list[str]:
        server = server_from_spec(document, client=client, name="가상 서비스")
        async with fastmcp.Client(server) as session:
            return [tool.name for tool in await session.list_tools()]

    names = asyncio.run(run())
    assert names, "도구가 하나도 등록되지 않았다"
    for name in names:
        assert name, "MCP 도구명이 빈 문자열이다(SEP-986 위반)"
        assert name.strip("_"), f"밑줄뿐인 도구명이다: {name!r}"


def test_tool_names_are_stable_when_an_operation_is_added() -> None:
    """오퍼레이션이 하나 늘어도 기존 도구명이 재배정되지 않는다.

    정렬 순서 기준 충돌 접미사(_2·_3)에 의존하면 오퍼레이션 추가만으로 기존
    MCP 도구 식별자가 전부 이동한다 — 클라이언트 allowlist·프롬프트가 참조하는
    안정 식별자가 조용히 깨지는 파괴적 변경이다.
    """
    paths = {
        "/law/detail": {
            "get": {"operationId": "상세", "responses": {"200": {"description": "정상"}}}
        },
        "/law/search": {
            "get": {"operationId": "조회", "responses": {"200": {"description": "정상"}}}
        },
    }
    base_document = {
        "openapi": "3.0.3",
        "info": {"title": "가상 법령 서비스"},
        "servers": [{"url": BASE}],
        "paths": paths,
    }
    before = {
        operation.path: operation.operation_id
        for operation in load_source(
            base_document, service_id="9990000", service_name="가상 법령 서비스"
        ).operations
    }

    grown = json.loads(json.dumps(base_document))
    grown["paths"]["/law/all"] = {
        "get": {"operationId": "전체", "responses": {"200": {"description": "정상"}}}
    }
    after = {
        operation.path: operation.operation_id
        for operation in load_source(
            grown, service_id="9990000", service_name="가상 법령 서비스"
        ).operations
    }

    for path, identifier in before.items():
        assert after[path] == identifier, (
            f"{path} 의 도구명이 {identifier!r} → {after[path]!r} 로 이동했다"
        )
    assert len(set(after.values())) == len(after)


# ---------------------------------------------------------------------------
# MAJOR — sampler.py: 하드캡 우회 · XML 자동 감지 오작동 · 2중 방어 기본값
# ---------------------------------------------------------------------------
def test_duplicate_operation_ids_do_not_multiply_upstream_calls(
    tmp_path: Path,
) -> None:
    """operation_ids 를 중복 지정해도 실제 상위 호출은 count 회를 넘지 않는다."""
    count = 3
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(200, json={"a": 1})

    source = _source(
        parameters=[
            {"name": "pageNo", "in": "query", "required": True, "type": "integer"}
        ]
    )
    with respx.mock(assert_all_called=False) as mock:
        mock.route(host=HOST).mock(side_effect=handler)
        results = sample_source(
            source,
            service_key="K",
            count=count,
            mode="record",
            cassette_path=tmp_path / "c.json",
            budget=10_000,
            ledger_path=tmp_path / "ledger.db",
            operation_ids=["getRows"] * 4,
        )

    assert calls["n"] == count
    assert calls["n"] <= MAX_SAMPLES
    assert len(results["getRows"]) == count


def test_failed_gateway_xml_sample_does_not_flip_json_inference() -> None:
    """게이트웨이 XML 오류 1건이 JSON 오퍼레이션의 R5 보정을 켜지 못한다.

    ``ok=False`` 샘플은 추론 입력에서 제외되므로 XML 판정 모수에도 들어가면
    안 된다. 켜지면 진짜 스키마 충돌(anyOf)이 배열로 접혀, 추론기가 만든
    스키마가 추론에 쓴 샘플을 거부하는 상태가 된다.
    """
    ok_samples = (
        _sample("json", True, {"items": {"x": 1}}),
        _sample("json", True, {"items": [{"x": 1}, {"x": 2}]}),
    )
    gateway_error = _sample(
        "xml", False, {"OpenAPI_ServiceResponse": {"cmmMsgHeader": {"returnReasonCode": "30"}}}
    )

    pure, _ = infer_response_schemas({"getRows": ok_samples})
    mixed, _ = infer_response_schemas({"getRows": (*ok_samples, gateway_error)})

    assert pure == mixed
    # 진짜 충돌은 anyOf 로 남는다(배열로 접히지 않는다).
    assert "anyOf" in mixed["getRows"]["properties"]["items"]

    # 대조군: 성공 샘플이 전부 XML 이면 R5 보정이 켜진다.
    xml_only = (
        _sample("xml", True, {"items": {"x": 1}}),
        _sample("xml", True, {"items": [{"x": 1}, {"x": 2}]}),
    )
    corrected, _ = infer_response_schemas({"getRows": xml_only})
    assert corrected["getRows"]["properties"]["items"]["type"] == "array"


def test_write_samples_requires_explicit_secrets(tmp_path: Path) -> None:
    """``write_samples`` 는 secrets 없이 호출할 수 없다(S7 2중 방어 강제).

    기본값 ``()`` 를 두면 문서가 안내하는 기본 호출이 곧 무방비가 되어, 응답이
    요청 키를 되비출 때 인증키 평문이 커밋 대상 샘플 파일에 남는다.
    """
    echoed = {"echoedKey": FAKE_KEY}
    results = {"getRows": (_sample("json", True, echoed),)}

    with pytest.raises(TypeError):
        write_samples(results, tmp_path / "nokw")  # type: ignore[call-arg]

    written = write_samples(results, tmp_path / "scrubbed", secrets=[FAKE_KEY])
    text = written[0].read_text(encoding="utf-8")
    assert FAKE_KEY not in text
    assert "FAKEKEYaaa" not in text
    assert SCRUB_PLACEHOLDER in text


# ---------------------------------------------------------------------------
# MAJOR — transport.py: 원장 핸들 누수
# ---------------------------------------------------------------------------
def test_client_close_releases_the_ledger_handle() -> None:
    """``client.close()`` 뒤에 원장 디렉터리를 지울 수 있다.

    ``create_client`` 사용자에게는 원장 핸들이 노출되지 않으므로, 트랜스포트가
    정리를 연쇄시키지 않으면 회수 수단이 없다. Windows 는 열린 SQLite 파일이
    있는 디렉터리를 지우지 못하므로 임시 디렉터리 정리가 조용히 실패한다.
    """
    workspace = Path(tempfile.mkdtemp())
    try:
        with respx.mock(assert_all_called=False) as mock:
            mock.route(host=HOST).mock(return_value=httpx.Response(200, json={"a": 1}))
            client = create_client(
                "K", budget=5, ledger_path=workspace / "ledger.db", mode="live"
            )
            client.get(f"{BASE}/rows")
            client.close()
        gc.collect()
        shutil.rmtree(workspace)
        assert not workspace.exists()
    finally:
        shutil.rmtree(workspace, ignore_errors=True)


def test_async_client_close_releases_the_ledger_handle() -> None:
    """async 경로도 같은 정리를 연쇄시킨다."""
    from mcportal.mcp import build_async_client

    workspace = Path(tempfile.mkdtemp())
    try:
        with respx.mock(assert_all_called=False) as mock:
            mock.route(host=HOST).mock(return_value=httpx.Response(200, json={"a": 1}))

            async def run() -> None:
                client = build_async_client(
                    "K",
                    base_url=BASE,
                    budget=5,
                    ledger_path=workspace / "ledger.db",
                    mode="live",
                )
                await client.get("/rows")
                await client.aclose()

            asyncio.run(run())
        gc.collect()
        shutil.rmtree(workspace)
        assert not workspace.exists()
    finally:
        shutil.rmtree(workspace, ignore_errors=True)


# ---------------------------------------------------------------------------
# MINOR — inference.py: 절단 경로의 순서 의존성
# ---------------------------------------------------------------------------
def test_inference_stays_order_independent_beyond_max_samples() -> None:
    """샘플이 상한을 넘어도 순열 무관이 성립하고, 절단 사실이 리포트에 남는다.

    "앞에서부터 N개"를 쓰면 6개 샘플의 720개 순열이 서로 다른 스키마를 낳는데
    ``truncated`` 도 False 라 호출자가 절단 사실조차 알 수 없었다.
    """
    samples: list[Any] = [{"a": index} for index in range(1, 6)] + [{"b": "late"}]
    outputs = {
        json.dumps(infer_schema(list(perm)), sort_keys=True, ensure_ascii=False)
        for perm in permutations(samples)
    }
    assert len(outputs) == 1, f"순열마다 다른 스키마가 나왔다: {len(outputs)}종"

    _, report = infer_schema_with_report(samples)
    assert report.truncated is True
    assert report.sample_count == 5

    # 상한 이내면 절단 표시가 서지 않는다(과잉 표기 방지).
    _, small = infer_schema_with_report(samples[:3])
    assert small.truncated is False


# ---------------------------------------------------------------------------
# MINOR — pyproject.toml: 직접 임포트한 anyio 미선언
# ---------------------------------------------------------------------------
def test_anyio_is_declared_in_the_mcp_extra() -> None:
    """``mcp.py`` 가 직접 임포트하는 anyio 가 [mcp] extra 에 선언돼 있다."""
    metadata = tomllib.loads(
        (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    )
    extras = metadata["project"]["optional-dependencies"]
    assert any(item.startswith("anyio") for item in extras["mcp"])
    # 코어 의존성은 httpx 단일을 유지한다.
    assert metadata["project"]["dependencies"] == ["httpx>=0.27"]
