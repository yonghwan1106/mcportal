# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Yong Park
"""FastMCP 연결 계층: OpenAPI 문서 → MCP 서버(변환 로직 100% 위임).

이 모듈이 하는 일은 정확히 세 가지뿐이다.

1. **임포트 가드** — fastmcp는 선택적 추가 의존성(``[mcp]`` extra)이므로,
   미설치 시 한국어 안내를 담은 :class:`ImportError` 로 막는다. 모듈 최상단에서
   ``import fastmcp`` 를 하지 않으므로 ``import mcportal.mcp`` 자체는 fastmcp
   없이도 성공한다(가드는 사용 시점에만 발동한다).
2. **async 클라이언트 배선** — FastMCP는 ``httpx.AsyncClient`` 를 요구하지만
   MCPortal 런타임(쿼터가드·캐시·정규화·record/replay)은 sync 트랜스포트다.
   :class:`MCPortalAsyncTransport` 가 워커 스레드로 위임하는 얇은 어댑터로
   그 간극을 메운다. 쿼터 로직을 async로 재구현하지 않는다.
3. **fastmcp 진입점 호출** — ``FastMCP.from_openapi()``(2.x) 또는
   ``OpenAPIProvider`` + ``providers=[...]``(3.x)를 런타임 시그니처
   introspection으로 골라 호출한다.

도구 정의 생성, JSON Schema → 도구 인자 변환, 핸들러 코드 생성 같은 **자체
코드젠은 일절 하지 않는다.** 스펙→도구 변환의 정확도·유지보수 책임은 fastmcp에
있고, MCPortal의 기여는 그 앞단(스펙 정규화)과 뒷단(쿼터·위생)에 있다.

인증키는 트랜스포트가 주입하므로 MCP 도구 인자로 노출되지 않는다. 컴파일러가
``security``/``securitySchemes`` 를 문서에 싣지 않는 것과 짝을 이루는 규약이다.
"""

from __future__ import annotations

import inspect
import json
from collections.abc import Mapping
from importlib import import_module, metadata
from pathlib import Path
from types import ModuleType
from typing import Any, Optional, Union

import anyio.to_thread
import httpx

from .profiles import DATA_GO_KR, ProviderProfile
from .runtime.normalize import normalize_response
from .transport import _build_transport

PathLike = Union[str, Path]

#: [mcp] extra가 요구하는 fastmcp 버전 범위. W4에서 == 정확 핀으로 승격한다.
FASTMCP_REQUIREMENT: str = "fastmcp>=2.10,<3"

#: 미설치 안내(한국어). ImportError 메시지 본문.
FASTMCP_IMPORT_HINT: str = (
    "MCP 변환에는 fastmcp가 필요합니다. MCPortal 코어는 httpx만 의존하며 "
    "fastmcp는 선택적 추가 의존성입니다. 다음으로 설치하세요:\n"
    '    pip install "mcportal[mcp]"\n'
    f"(요구 버전: {FASTMCP_REQUIREMENT})"
)

#: 응답 재구성 시 반드시 벗겨야 하는 헤더들. 이미 디코딩된 바이트에 이 헤더가
#: 남아 있으면 httpx가 다시 gunzip을 시도하다 DecodingError로 죽는다.
_STALE_RESPONSE_HEADERS = ("content-encoding", "content-length", "transfer-encoding")

#: 브리지가 상위로 올려 보내는 본문의 Content-Type. XML 응답도 정규화 JSON 으로
#: 바꿔 돌려주므로 MCP 도구는 항상 JSON 만 보게 된다.
_JSON_CONTENT_TYPE = "application/json; charset=utf-8"

__all__ = [
    "FASTMCP_REQUIREMENT",
    "FASTMCP_IMPORT_HINT",
    "MCPortalAsyncTransport",
    "build_async_client",
    "build_server",
    "fastmcp_version",
    "require_fastmcp",
    "server_from_spec",
]


# ---------------------------------------------------------------------------
# 임포트 가드
# ---------------------------------------------------------------------------
def require_fastmcp() -> ModuleType:
    """fastmcp 모듈을 임포트해 돌려준다.

    Returns:
        임포트된 ``fastmcp`` 모듈.

    Raises:
        ImportError: 미설치 시 :data:`FASTMCP_IMPORT_HINT` 를 메시지로 하는
            ImportError. 원 예외를 ``__cause__`` 로 연결한다.
    """
    try:
        module = import_module("fastmcp")
    except ImportError as exc:
        raise ImportError(FASTMCP_IMPORT_HINT) from exc
    if module is None:  # pragma: no cover - 방어(sys.modules 오염)
        raise ImportError(FASTMCP_IMPORT_HINT)
    return module


def fastmcp_version() -> str:
    """설치된 fastmcp 버전 문자열을 돌려준다.

    ``__version__`` 이 없으면 :mod:`importlib.metadata` 로 조회하고, 그래도
    없으면 ``"unknown"`` 을 돌려준다.

    Raises:
        ImportError: fastmcp가 설치돼 있지 않을 때(:func:`require_fastmcp`).
    """
    module = require_fastmcp()
    version = getattr(module, "__version__", None)
    if isinstance(version, str) and version:
        return version
    try:
        return metadata.version("fastmcp")
    except Exception:  # pragma: no cover - 배포 메타데이터 부재 방어
        return "unknown"


def _accepted_keywords(func: Any) -> Optional[frozenset[str]]:
    """호출 가능 객체가 받아들이는 키워드 이름 집합을 돌려준다.

    ``**kwargs``(VAR_KEYWORD)가 있으면 임의 키워드를 받는다는 뜻이므로 None을
    돌려준다(= 제한 없음). 시그니처를 읽을 수 없는 객체도 None으로 본다.

    fastmcp 2.x와 3.x의 진입점 시그니처가 서로 다르고 마이너 릴리스에서도
    파라미터가 늘고 줄기 때문에, 고정 키워드 목록 대신 런타임 introspection으로
    교집합만 넘긴다.
    """
    try:
        signature = inspect.signature(func)
    except (TypeError, ValueError):  # pragma: no cover - C 확장 등 방어
        return None
    names: set[str] = set()
    for parameter in signature.parameters.values():
        if parameter.kind is inspect.Parameter.VAR_KEYWORD:
            return None
        if parameter.kind in (
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
            inspect.Parameter.KEYWORD_ONLY,
        ):
            names.add(parameter.name)
    return frozenset(names)


def _openapi_provider(module: ModuleType) -> Any:
    """fastmcp 3.x 계열의 ``OpenAPIProvider`` 를 찾는다(없으면 None).

    속성 체인(``fastmcp.server.providers.openapi.OpenAPIProvider``)을 먼저
    보고, 없으면 서브모듈 임포트를 시도한다. 2.x에는 존재하지 않으므로 None이
    정상 결과다.
    """
    node: Any = module
    for attribute in ("server", "providers", "openapi", "OpenAPIProvider"):
        node = getattr(node, attribute, None)
        if node is None:
            break
    if node is not None:
        return node
    try:
        submodule = import_module("fastmcp.server.providers.openapi")
    except ImportError:
        return None
    return getattr(submodule, "OpenAPIProvider", None)


def _normalize_xml_payload(
    content: bytes, content_type: str | None
) -> tuple[bytes, bool]:
    """XML 본문이면 정규화 JSON 바이트로 바꾼다.

    판정은 Content-Type 이 아니라 :func:`~mcportal.runtime.normalize.normalize_response`
    가 본 **실제 본문 형태**로 한다(게이트웨이 오류는 JSON 을 요청해도 XML 로
    오므로 헤더를 믿을 수 없다). JSON 이거나 파싱할 수 없으면 원본을 그대로 둔다.

    Args:
        content: 상위 응답 본문 바이트.
        content_type: 상위 응답 Content-Type(디코딩 힌트로만 쓴다).

    Returns:
        ``(본문 바이트, 변환 여부)`` 쌍.
    """
    if not content:
        return content, False
    try:
        normalized = normalize_response(content, content_type)
    except Exception:  # pragma: no cover - 비정형 본문 방어(원본 통과)
        return content, False
    if normalized.source_format != "xml":
        return content, False
    try:
        payload = json.dumps(normalized.data, ensure_ascii=False).encode("utf-8")
    except (TypeError, ValueError):  # pragma: no cover - 직렬화 불가 방어
        return content, False
    return payload, True


# ---------------------------------------------------------------------------
# sync → async 트랜스포트 브리지
# ---------------------------------------------------------------------------
class MCPortalAsyncTransport(httpx.AsyncBaseTransport):
    """sync MCPortalTransport를 워커 스레드로 위임하는 async 트랜스포트.

    쿼터가드·캐시·정규화·record/replay를 async에서 재구현하지 않고 그대로
    재사용한다. FastMCP가 ``httpx.AsyncClient`` 를 요구하기 때문에 필요한 얇은
    어댑터다. sync 경로는 ``time.sleep`` 백오프와 SQLite 원장 쓰기 같은 블로킹
    연산을 하므로 워커 스레드 위임이 오히려 정석이다.

    위임 수단인 :mod:`anyio` 는 httpx의 필수 의존성이므로 새 코어 의존성이
    아니다("런타임 코어 의존성 httpx 단일" 원칙 위반이 아니다).

    Args:
        inner: 위임할 sync 트랜스포트(보통 ``MCPortalTransport``).
    """

    def __init__(self, inner: httpx.BaseTransport) -> None:
        self._inner = inner

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        """요청 본문을 먼저 완독한 뒤 워커 스레드에서 sync 트랜스포트를 호출한다.

        async 스트림은 워커 스레드 안에서 읽을 수 없으므로 ``aread()`` 를
        스레드 **밖에서** 먼저 수행한다. 또한 httpx의 sync 트랜스포트는
        ``request.stream`` 이 ``SyncByteStream`` 이라고 단언하므로, 완독한 본문
        으로 sync 전용 요청을 새로 만들어 넘긴다.

        Args:
            request: AsyncClient가 만든 요청.

        Returns:
            바이트 본문으로 재구성한 응답. ``content=`` 로 만든 응답의 스트림은
            sync/async 양쪽 인터페이스를 모두 만족하므로 AsyncClient의 스트림
            타입 단언을 통과한다.
        """
        body = await request.aread()
        sync_request = httpx.Request(
            request.method,
            request.url,
            headers=request.headers,
            content=body,
            extensions=request.extensions,
        )
        status_code, headers, content, extensions = await anyio.to_thread.run_sync(
            self._send_sync, sync_request
        )
        return httpx.Response(
            status_code=status_code,
            headers=headers,
            content=content,
            request=request,
            extensions=extensions,
        )

    async def aclose(self) -> None:
        """inner.close()를 워커 스레드에서 호출한다(record 모드면 카세트 저장이 연쇄된다)."""
        await anyio.to_thread.run_sync(self._inner.close)

    def _send_sync(
        self, request: httpx.Request
    ) -> tuple[int, httpx.Headers, bytes, dict[str, Any]]:
        """워커 스레드에서 실행되는 sync 구간(호출 → 본문 완독 → 정규화 → 헤더 정리).

        스레드 왕복을 1회로 묶기 위해 응답 재구성에 필요한 재료만 값으로
        돌려준다. 이미 디코딩된 바이트에 ``content-encoding`` 이 남아 있으면
        httpx가 다시 gunzip을 시도하다 죽으므로 인코딩·길이 헤더를 벗긴다.

        XML 본문은 여기서 **정규화 JSON 으로 재직렬화한다.** MCPortal 이 내세우는
        "정규화 계층"의 실체가 바로 이 지점이다. 이것이 없으면 컴파일러는 XML→dict
        변환 결과에서 추론한 스키마를 ``outputSchema`` 로 싣는데 런타임은 원본 XML
        바이트를 그대로 넘기므로, fastmcp 가 structured output 을 만들지 못해
        **XML 응답 서비스의 도구 호출이 100% 실패한다**(도구 목록에는 뜬다).
        ``_type=json`` 을 무시하고 XML 을 돌려주는 data.go.kr 게이트웨이 오류도
        같은 경로로 흡수된다.
        """
        response = self._inner.handle_request(request)
        try:
            content = response.read()
        finally:
            response.close()
        headers = response.headers.copy()
        content, converted = _normalize_xml_payload(
            content, headers.get("content-type")
        )
        if converted:
            headers["content-type"] = _JSON_CONTENT_TYPE
        for stale in _STALE_RESPONSE_HEADERS:
            if stale in headers:
                del headers[stale]
        return response.status_code, headers, content, dict(response.extensions)


# ---------------------------------------------------------------------------
# 클라이언트 팩토리
# ---------------------------------------------------------------------------
def build_async_client(
    service_key: str | None = None,
    *,
    base_url: str = "",
    budget: int | None = None,
    profile: ProviderProfile = DATA_GO_KR,
    cache_ttl: float = 300.0,
    ledger_path: PathLike | None = None,
    mode: str = "live",
    cassette_path: PathLike | None = None,
    timeout: float = 30.0,
) -> httpx.AsyncClient:
    """FastMCP에 넘길 httpx.AsyncClient를 MCPortal 스택 위에 배선한다.

    내부적으로 :func:`mcportal.create_client` 와 **동일한 조립 규칙**(live/record/
    replay)을 따르되, 최종 트랜스포트를 :class:`MCPortalAsyncTransport` 로
    감싼다. 조립 규칙이 갈라지지 않도록 두 함수가 같은 내부 헬퍼를 공유한다.

    Args:
        service_key: data.go.kr 인증키(replay 모드는 None 허용).
        base_url: 요청 기준 URL. 보통 ``SourceSpec.base_url`` 또는 OpenAPI
            ``servers[0].url`` 을 넘긴다.
        budget: 일일 예산 상한(생략 시 CALL_BUDGET → 프로파일 기본 예산 순).
        profile: 프로바이더 프로파일.
        cache_ttl: live 캐시 TTL(초).
        ledger_path: 사용량 원장 경로.
        mode: "live" | "record" | "replay".
        cassette_path: record/replay 카세트 경로.
        timeout: 클라이언트 타임아웃(초).

    Returns:
        MCPortal 스택 위에 배선된 ``httpx.AsyncClient``.

    Raises:
        ValueError: 모드별 필수 인자 누락(create_client와 동일한 한국어 메시지).
    """
    inner = _build_transport(
        service_key,
        budget=budget,
        profile=profile,
        cache_ttl=cache_ttl,
        ledger_path=ledger_path,
        mode=mode,
        cassette_path=cassette_path,
    )
    return httpx.AsyncClient(
        transport=MCPortalAsyncTransport(inner),
        base_url=base_url,
        timeout=timeout,
    )


# ---------------------------------------------------------------------------
# FastMCP 연결
# ---------------------------------------------------------------------------
def server_from_spec(
    document: Mapping[str, Any],
    *,
    client: httpx.AsyncClient,
    name: str | None = None,
    tags: set[str] | None = None,
) -> Any:
    """OpenAPI 문서를 FastMCP 서버 객체로 변환한다(변환 로직 100% 위임).

    - 2.x 경로: ``FastMCP.from_openapi(openapi_spec=..., client=..., ...)``
    - 3.x 경로: ``FastMCP(name=..., providers=[OpenAPIProvider(document, client)])``

    호출 전 :func:`inspect.signature` 로 지원 파라미터를 확인하고 교집합만
    키워드로 넘기므로, 계열 간 시그니처 차이를 코드 수정 없이 흡수한다.

    Args:
        document: OpenAPI 3.1 문서(컴파일러 산출물).
        client: :func:`build_async_client` 가 만든 async 클라이언트.
        name: MCP 서버 이름(진입점이 받을 때만 전달된다).
        tags: 도구에 부여할 태그 집합(진입점이 받을 때만 전달된다).

    Returns:
        fastmcp의 서버 객체. MCPortal은 이 타입에 의존하지 않는다.

    Raises:
        ImportError: fastmcp 미설치이거나, 설치된 fastmcp에서 OpenAPI 진입점을
            찾을 수 없을 때(한국어 안내 + :data:`FASTMCP_REQUIREMENT`).
    """
    module = require_fastmcp()
    server_cls = getattr(module, "FastMCP", None)
    spec: dict[str, Any] = dict(document)

    factory = getattr(server_cls, "from_openapi", None) if server_cls is not None else None
    if factory is not None:
        accepted = _accepted_keywords(factory)
        kwargs: dict[str, Any] = {"openapi_spec": spec, "client": client}
        for key, value in (("name", name), ("tags", tags)):
            if value is None:
                continue
            if accepted is None or key in accepted:
                kwargs[key] = value
        return factory(**kwargs)

    provider_cls = _openapi_provider(module)
    if provider_cls is not None and server_cls is not None:
        provider = provider_cls(spec, client)
        accepted = _accepted_keywords(server_cls)
        kwargs = {"providers": [provider]}
        if name is not None and (accepted is None or "name" in accepted):
            kwargs["name"] = name
        # tags는 3.x FastMCP 생성자가 명시적으로 받을 때만 넘긴다(암묵 전달 금지).
        if tags is not None and accepted is not None and "tags" in accepted:
            kwargs["tags"] = tags
        return server_cls(**kwargs)

    raise ImportError(
        "설치된 fastmcp에서 OpenAPI 진입점을 찾을 수 없습니다. "
        "FastMCP.from_openapi(2.x) 또는 OpenAPIProvider(3.x) 중 하나가 필요합니다. "
        f"지원 버전 범위: {FASTMCP_REQUIREMENT}\n"
        '    pip install "mcportal[mcp]"'
    )


def build_server(
    spec_path: PathLike,
    *,
    service_key: str | None = None,
    name: str | None = None,
    mode: str = "live",
    **client_kwargs: Any,
) -> Any:
    """스펙 파일 경로 하나로 MCP 서버를 세우는 최상위 편의 함수.

    ``spec_path`` 의 JSON을 읽어 ``servers[0].url`` 을 base_url로 삼아
    :func:`build_async_client` 를 만들고 :func:`server_from_spec` 에 넘긴다.

    Args:
        spec_path: OpenAPI 3.1 문서(JSON) 경로.
        service_key: data.go.kr 인증키(replay 모드는 None 허용).
        name: MCP 서버 이름. None이면 문서의 ``info.title`` 을 쓴다.
        mode: "live" | "record" | "replay".
        **client_kwargs: :func:`build_async_client` 에 그대로 전달되는 인자들
            (budget·profile·cache_ttl·ledger_path·cassette_path·timeout).

    Returns:
        fastmcp의 서버 객체.

    Raises:
        ImportError: fastmcp 미설치 또는 OpenAPI 진입점 부재.
        ValueError: 모드별 필수 인자 누락.
    """
    document: dict[str, Any] = json.loads(
        Path(spec_path).read_text(encoding="utf-8")
    )
    base_url = ""
    servers = document.get("servers")
    if isinstance(servers, list) and servers and isinstance(servers[0], Mapping):
        base_url = str(servers[0].get("url", ""))

    server_name = name
    if server_name is None:
        info = document.get("info")
        if isinstance(info, Mapping):
            title = info.get("title")
            if isinstance(title, str) and title:
                server_name = title

    client = build_async_client(
        service_key,
        base_url=base_url,
        mode=mode,
        **client_kwargs,
    )
    return server_from_spec(document, client=client, name=server_name)
