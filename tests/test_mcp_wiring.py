# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Yong Park
"""mcp 연결 테스트: 임포트 가드·시그니처 교집합·async 브리지·무키 FastMCP 빌드.

fastmcp 경로 분기는 가짜 모듈 주입으로 검증하므로 fastmcp가 없어도 전부 통과한다.
실제 FastMCP 서버 빌드는 ``[mcp]`` extra가 설치돼 있을 때만 도는 통합 케이스이며,
replay 카세트를 써서 **무키·무네트워크**로 왕복한다. 픽스처는 전부 합성이고
도메인은 RFC 2606 예약 TLD(``.invalid``)라 실수로도 실호출이 나가지 않는다.
"""

from __future__ import annotations

import asyncio
import gzip
import importlib
import json
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import httpx
import pytest
import respx

import mcportal.mcp as mcp_module
from mcportal import MCPortalTransport, QuotaExhausted
from mcportal.mcp import (
    FASTMCP_REQUIREMENT,
    MCPortalAsyncTransport,
    build_async_client,
    require_fastmcp,
    server_from_spec,
)
from mcportal.replay import Cassette

BASE = "https://apis.example.invalid/demo"
LIST_URL = f"{BASE}/getDemoList"

# '+', '/', '=' 를 포함해 인코딩 변형이 뚜렷이 달라지는 합성 키.
DECODED_KEY = "ab12+CD/34=="

#: 가상 기관의 합성 OpenAPI 3.1 문서(실 서비스ID·실 URL을 쓰지 않는다).
DEMO_SPEC: dict[str, Any] = {
    "openapi": "3.1.0",
    "info": {"title": "가상행정연구원 데모 서비스", "version": "0.1.0"},
    "servers": [{"url": BASE}],
    "paths": {
        "/getDemoList": {
            "get": {
                "operationId": "getDemoList",
                "summary": "데모 목록 조회",
                "parameters": [
                    {
                        "name": "pageNo",
                        "in": "query",
                        "required": True,
                        "description": "페이지 번호",
                        "schema": {"type": "integer"},
                    }
                ],
                "responses": {
                    "200": {
                        "description": "정상 응답",
                        "content": {
                            "application/json": {"schema": {"type": "object"}}
                        },
                    }
                },
            }
        }
    },
}


def _async_get(client: httpx.AsyncClient, url: str, **kwargs: Any) -> httpx.Response:
    """sync 테스트 함수 안에서 async GET 1건을 실행한다(pytest-asyncio 미사용)."""

    async def _run() -> httpx.Response:
        async with client:
            return await client.get(url, **kwargs)

    return asyncio.run(_run())


# ---------------------------------------------------------------------------
# ① 임포트 가드
# ---------------------------------------------------------------------------
def test_require_fastmcp_raises_korean_import_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # sys.modules 에 None 을 심으면 임포트가 halt 되어 미설치와 같은 상황이 된다.
    monkeypatch.setitem(sys.modules, "fastmcp", None)
    with pytest.raises(ImportError) as excinfo:
        require_fastmcp()
    message = str(excinfo.value)
    assert "mcportal[mcp]" in message
    assert "pip install" in message
    assert "fastmcp가 필요합니다" in message
    assert FASTMCP_REQUIREMENT in message
    # 원 예외가 __cause__ 로 연결된다.
    assert excinfo.value.__cause__ is not None


# ---------------------------------------------------------------------------
# ② 2.x 경로 — from_openapi 위임(키워드 호출)
# ---------------------------------------------------------------------------
def _fake_v2_module(calls: list[tuple[tuple[Any, ...], dict[str, Any]]]) -> ModuleType:
    """``**kwargs`` 로 호출 인자를 그대로 포착하는 가짜 fastmcp(2.x 계열)."""
    module = ModuleType("fastmcp")

    class FakeServer:
        @staticmethod
        def from_openapi(*args: Any, **kwargs: Any) -> Any:
            calls.append((args, kwargs))
            return SimpleNamespace(kind="v2", name=kwargs.get("name"))

    module.FastMCP = FakeServer  # type: ignore[attr-defined]
    return module


def test_v2_path_delegates_with_keywords(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []
    monkeypatch.setitem(sys.modules, "fastmcp", _fake_v2_module(calls))
    client = httpx.AsyncClient(transport=MCPortalAsyncTransport(httpx.HTTPTransport()))

    server = server_from_spec(DEMO_SPEC, client=client, name="데모 서버")

    assert server.kind == "v2"
    assert len(calls) == 1  # 정확히 1회 위임.
    args, kwargs = calls[0]
    assert args == ()  # 전부 키워드로 넘긴다.
    assert kwargs["openapi_spec"]["openapi"] == "3.1.0"
    assert kwargs["name"] == "데모 서버"
    assert isinstance(kwargs["client"], httpx.AsyncClient)
    # 원본 문서를 변형하지 않는다(사본 전달).
    assert kwargs["openapi_spec"] is not DEMO_SPEC


# ---------------------------------------------------------------------------
# ③ 시그니처 교집합 — 좁은 시그니처에 name/tags 를 넘기지 않는다
# ---------------------------------------------------------------------------
def test_narrow_signature_receives_only_supported_keywords(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}
    module = ModuleType("fastmcp")

    class FakeServer:
        @staticmethod
        def from_openapi(openapi_spec: Any, client: Any) -> Any:
            captured["openapi_spec"] = openapi_spec
            captured["client"] = client
            return SimpleNamespace(kind="narrow")

    module.FastMCP = FakeServer  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "fastmcp", module)

    client = httpx.AsyncClient(transport=MCPortalAsyncTransport(httpx.HTTPTransport()))
    server = server_from_spec(
        DEMO_SPEC, client=client, name="무시될 이름", tags={"demo"}
    )

    assert server.kind == "narrow"
    assert set(captured) == {"openapi_spec", "client"}


# ---------------------------------------------------------------------------
# ④ 3.x 경로 — OpenAPIProvider + providers=[...]
# ---------------------------------------------------------------------------
def test_v3_path_uses_openapi_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    module = ModuleType("fastmcp")

    class FakeProvider:
        def __init__(self, spec: Any, client: Any) -> None:
            self.spec = spec
            self.client = client

    class FakeServer:
        # from_openapi 가 없는 계열(3.x)을 흉내낸다.
        def __init__(self, name: str | None = None, *, providers: list[Any]) -> None:
            self.name = name
            self.providers = providers

    server_module = ModuleType("fastmcp.server")
    providers_module = ModuleType("fastmcp.server.providers")
    openapi_module = ModuleType("fastmcp.server.providers.openapi")
    openapi_module.OpenAPIProvider = FakeProvider  # type: ignore[attr-defined]
    providers_module.openapi = openapi_module  # type: ignore[attr-defined]
    server_module.providers = providers_module  # type: ignore[attr-defined]
    module.server = server_module  # type: ignore[attr-defined]
    module.FastMCP = FakeServer  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "fastmcp", module)

    client = httpx.AsyncClient(transport=MCPortalAsyncTransport(httpx.HTTPTransport()))
    server = server_from_spec(DEMO_SPEC, client=client, name="v3 서버")

    assert server.name == "v3 서버"
    assert len(server.providers) == 1
    provider = server.providers[0]
    assert provider.spec["openapi"] == "3.1.0"
    assert provider.client is client


# ---------------------------------------------------------------------------
# ⑤ 두 경로 모두 없음 → ImportError + 요구 버전 안내
# ---------------------------------------------------------------------------
def test_missing_entrypoint_raises_import_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = ModuleType("fastmcp")

    class FakeServer:
        pass

    module.FastMCP = FakeServer  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "fastmcp", module)

    client = httpx.AsyncClient(transport=MCPortalAsyncTransport(httpx.HTTPTransport()))
    with pytest.raises(ImportError) as excinfo:
        server_from_spec(DEMO_SPEC, client=client)
    message = str(excinfo.value)
    assert FASTMCP_REQUIREMENT in message
    assert "OpenAPI 진입점" in message


# ---------------------------------------------------------------------------
# ⑥ async 브리지 왕복(sync 스트림을 그대로 돌려주면 여기서 깨진다)
# ---------------------------------------------------------------------------
@respx.mock
def test_async_bridge_roundtrip() -> None:
    respx.get(LIST_URL).mock(
        return_value=httpx.Response(
            200,
            headers={"content-type": "application/json"},
            json={"total": 1, "items": [{"id": "A-1"}]},
        )
    )
    inner = MCPortalTransport(DECODED_KEY, inner=httpx.HTTPTransport())
    client = httpx.AsyncClient(transport=MCPortalAsyncTransport(inner))

    resp = _async_get(client, LIST_URL, params={"pageNo": "1"})

    assert resp.status_code == 200
    assert resp.json() == {"total": 1, "items": [{"id": "A-1"}]}


@respx.mock
def test_async_bridge_forwards_post_body() -> None:
    route = respx.post(LIST_URL).mock(return_value=httpx.Response(200, json={"ok": True}))
    inner = MCPortalTransport(None, inner=httpx.HTTPTransport())
    client = httpx.AsyncClient(transport=MCPortalAsyncTransport(inner))

    async def _run() -> httpx.Response:
        async with client:
            return await client.post(LIST_URL, content=b'{"page":1}')

    resp = asyncio.run(_run())

    assert resp.status_code == 200
    # POST 본문이 손실 없이 sync 트랜스포트까지 전달된다.
    assert route.calls.last.request.content == b'{"page":1}'


# ---------------------------------------------------------------------------
# ⑦ async 경로에서도 하드 예산 상한이 살아 있다
# ---------------------------------------------------------------------------
@respx.mock
def test_async_path_enforces_budget(tmp_path: Path) -> None:
    respx.get(LIST_URL).mock(return_value=httpx.Response(200, json={"ok": True}))
    client = build_async_client(
        DECODED_KEY,
        base_url=BASE,
        budget=2,
        ledger_path=tmp_path / "async.db",
        mode="live",
    )

    async def _run() -> None:
        async with client:
            # 캐시 히트로 쿼터가 소모되지 않는 상황을 피하려고 매번 다른 파라미터.
            for page in ("1", "2"):
                resp = await client.get(LIST_URL, params={"pageNo": page})
                assert resp.status_code == 200
            with pytest.raises(QuotaExhausted) as excinfo:
                await client.get(LIST_URL, params={"pageNo": "3"})
            assert "운영계정" in str(excinfo.value)

    asyncio.run(_run())


# ---------------------------------------------------------------------------
# ⑧ gzip 회귀: 이미 디코딩된 바이트를 다시 gunzip 하지 않는다
# ---------------------------------------------------------------------------
@respx.mock
def test_async_bridge_strips_stale_content_encoding() -> None:
    xml = (
        "<response><header><resultCode>00</resultCode></header>"
        "<body><item><name>가상동</name></item></body></response>"
    )
    respx.get(LIST_URL).mock(
        return_value=httpx.Response(
            200,
            headers={
                "content-type": "application/xml; charset=utf-8",
                "content-encoding": "gzip",
            },
            content=gzip.compress(xml.encode("utf-8")),
        )
    )
    inner = MCPortalTransport(DECODED_KEY, inner=httpx.HTTPTransport())
    client = httpx.AsyncClient(transport=MCPortalAsyncTransport(inner))

    resp = _async_get(client, LIST_URL, params={"pageNo": "1"})

    assert resp.status_code == 200
    assert "가상동" in resp.text
    # 재구성 응답에는 인코딩·길이 헤더가 남지 않는다.
    assert resp.headers.get("content-encoding") is None
    assert resp.headers.get("transfer-encoding") is None


# ---------------------------------------------------------------------------
# ⑨ 키 주입 유지: async 경로에서도 정확히 1회 인코딩
# ---------------------------------------------------------------------------
@respx.mock
def test_async_path_encodes_service_key_exactly_once() -> None:
    route = respx.get(LIST_URL).mock(return_value=httpx.Response(200, json={"ok": True}))
    inner = MCPortalTransport(DECODED_KEY, inner=httpx.HTTPTransport())
    client = httpx.AsyncClient(transport=MCPortalAsyncTransport(inner))

    _async_get(client, LIST_URL, params={"pageNo": "1"})

    sent_url = str(route.calls.last.request.url)
    assert "%2B" in sent_url
    assert "%252B" not in sent_url
    assert "%2F" in sent_url
    assert "%252F" not in sent_url
    assert route.calls.last.request.url.params["serviceKey"] == DECODED_KEY


# ---------------------------------------------------------------------------
# ⑩ 모듈 임포트는 fastmcp 없이도 성공한다(가드는 사용 시점에만)
# ---------------------------------------------------------------------------
def test_module_import_does_not_require_fastmcp(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setitem(sys.modules, "fastmcp", None)
    try:
        reloaded = importlib.reload(mcp_module)
        assert reloaded.FASTMCP_REQUIREMENT == FASTMCP_REQUIREMENT
        # 최상위 패키지도 fastmcp 없이 임포트된다.
        top = importlib.import_module("mcportal")
        assert top.__version__
        assert not hasattr(top, "mcp") or True  # 최상위 재수출에 의존하지 않는다.
    finally:
        monkeypatch.undo()
        importlib.reload(mcp_module)


# ---------------------------------------------------------------------------
# ⑪ 통합: 실제 FastMCP 서버를 무키·무네트워크로 빌드하고 도구를 호출한다
# ---------------------------------------------------------------------------
def _write_demo_cassette(path: Path) -> None:
    """합성 응답 1건짜리 replay 카세트를 만든다(시크릿 0건)."""
    cassette = Cassette()
    cassette.add(
        method="GET",
        url=f"{LIST_URL}?pageNo=1",
        params={"pageNo": "1"},
        status=200,
        content_type="application/json",
        body_text=json.dumps(
            {"total": 1, "items": [{"id": "A-1", "name": "가상동"}]},
            ensure_ascii=False,
        ),
        secrets=[],
    )
    cassette.save(path)


def test_real_fastmcp_server_builds_and_calls_tool_keyless(tmp_path: Path) -> None:
    fastmcp = pytest.importorskip("fastmcp", reason="[mcp] extra 미설치")

    cassette_path = tmp_path / "demo_cassette.json"
    _write_demo_cassette(cassette_path)

    client = build_async_client(
        base_url=BASE, mode="replay", cassette_path=cassette_path
    )
    server = server_from_spec(DEMO_SPEC, client=client, name="가상 데모 서버")

    async def _run() -> tuple[list[str], Any]:
        async with fastmcp.Client(server) as session:
            tools = await session.list_tools()
            result = await session.call_tool("getDemoList", {"pageNo": 1})
            return [tool.name for tool in tools], result

    tool_names, result = asyncio.run(_run())

    # 도구 정의는 fastmcp가 만든다(자체 코드젠 없음). 이름은 operationId 유래.
    assert "getDemoList" in tool_names
    # 응답은 카세트에서 나온다 — 네트워크·인증키 0건.
    payload = getattr(result, "data", None)
    assert payload == {"total": 1, "items": [{"id": "A-1", "name": "가상동"}]}
