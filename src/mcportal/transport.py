# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Yong Park
"""httpx 전송 계층 통합: 쿼터가드·캐시·정규화·record/replay를 하나의 트랜스포트로 배선한다.

:class:`MCPortalTransport` 는 ``httpx.BaseTransport`` 구현으로, 하위 트랜스포트
호출 전후에 다음을 강제한다.

1. serviceKey 원문 주입(httpx가 정확히 1회 인코딩하도록 남겨 이중 인코딩(코드 30) 방지).
2. TTL 캐시 조회(GET 한정): 히트 시 ``X-MCPortal-Cache: hit`` 헤더를 달아 즉시 반환.
3. 쿼터가드 before_call: 하드 예산 초과·소진 마킹 키를 차단(QuotaExhausted).
   fallback 트랜스포트(예: replay 카세트)가 있으면 위임한다.
4. 하위 트랜스포트 호출 + 지수 백오프 재시도(TransportError·HTTP 5xx).
5. 응답 정규화로 result_code 추출 → after_call 기록.
6. result_code 22(쿼터 초과)는 재시도 없이 graceful stop(QuotaExhausted; fallback 위임).
7. 정상 응답은 캐시에 저장.

:func:`create_client` 는 live/record/replay 세 모드를 배선하는 헬퍼다.
"""
from __future__ import annotations

import base64
import json
import time
from pathlib import Path
from typing import Callable, Optional, Union

import httpx

from .profiles import DATA_GO_KR, ProviderProfile
from .quota import DailyBudget, QuotaExhausted, QuotaGuard, UsageLedger, compute_delay
from .replay import Cassette, RecordingTransport, ReplayTransport
from .runtime.cache import TTLCache, make_key
from .runtime.keys import prepare_service_key
from .runtime.normalize import normalize_response

PathLike = Union[str, Path]

#: data.go.kr가 일일 요청 제한 초과를 알리는 result_code.
_QUOTA_ERROR_CODE = "22"

#: 캐시 히트를 표시하는 응답 헤더 이름.
_CACHE_HEADER = "X-MCPortal-Cache"


class MCPortalTransport(httpx.BaseTransport):
    """쿼터가드·캐시·정규화·재시도를 통합한 httpx 트랜스포트.

    Args:
        service_key: data.go.kr 인증키. 생성 시 :func:`prepare_service_key` 로
            1회 정규화해 보관한다(디코딩키 원문). None이면 주입을 건너뛴다.
        inner: 실제 전송을 담당하는 하위 트랜스포트. 기본 httpx.HTTPTransport.
        guard: 쿼터가드. None이면 예산/레이트 강제를 건너뛴다.
        cache: TTL 캐시. None이면 캐시를 쓰지 않는다.
        profile: 프로바이더 프로파일(키 파라미터 이름 등). 기본 DATA_GO_KR.
        max_retries: TransportError·5xx에 대한 최대 재시도 횟수.
        sleep: 백오프 대기 함수(테스트 주입용). 기본 time.sleep.
        fallback: 쿼터 소진 시 위임할 트랜스포트(예: ReplayTransport). None이면 재발생.
    """

    def __init__(
        self,
        service_key: str | None,
        inner: httpx.BaseTransport | None = None,
        guard: QuotaGuard | None = None,
        cache: TTLCache | None = None,
        profile: ProviderProfile = DATA_GO_KR,
        max_retries: int = 3,
        sleep: Callable[[float], None] = time.sleep,
        fallback: httpx.BaseTransport | None = None,
    ) -> None:
        self._service_key: str | None = (
            prepare_service_key(service_key) if service_key is not None else None
        )
        self._inner: httpx.BaseTransport = inner if inner is not None else httpx.HTTPTransport()
        self._guard = guard
        self._cache = cache
        self._profile = profile
        self._max_retries = int(max_retries)
        self._sleep = sleep
        self._fallback = fallback

    # -- httpx 인터페이스 -------------------------------------------------
    def handle_request(self, request: httpx.Request) -> httpx.Response:
        """요청 1건을 처리한다(주입→캐시→가드→호출→정규화→기록→캐시저장)."""
        self._inject_key(request)
        endpoint = f"{request.url.host}{request.url.path}"
        is_get = request.method.upper() == "GET"

        # ② 캐시 조회(GET 한정). 히트면 쿼터를 소모하지 않고 즉시 반환.
        cache_key: str | None = None
        if self._cache is not None and is_get:
            cache_key = make_key(request.method, str(request.url))
            entry = self._cache.get(cache_key)
            if entry is not None:
                return self._response_from_cache(entry.value, request)

        # ③ 쿼터가드 before_call.
        if self._guard is not None and self._service_key is not None:
            try:
                self._guard.before_call(self._service_key, endpoint)
            except QuotaExhausted:
                if self._fallback is not None:
                    return self._fallback.handle_request(request)
                raise

        # ④ 하위 트랜스포트 호출(백오프 재시도). 각 물리 상위 호출(재시도 포함)이
        #    원장에 집계되도록 endpoint 를 넘긴다.
        response = self._call_with_retry(request, endpoint)

        # ⑤ 응답 정규화로 result_code 추출(파싱 실패는 None으로 무시).
        content = response.read()
        content_type = response.headers.get("content-type")
        result_code: str | None = None
        ok: bool
        try:
            normalized = normalize_response(content, content_type)
            result_code = normalized.result_code
            ok = normalized.ok
        except Exception:  # pragma: no cover - 정규화 방어(비정형 본문)
            ok = 200 <= response.status_code < 300

        # after_call 기록.
        if self._guard is not None and self._service_key is not None:
            self._guard.after_call(
                self._service_key,
                endpoint,
                result_code=result_code,
                status="ok" if ok else "error",
            )

        # ⑥ result_code 22 → graceful stop(재시도 금지).
        if result_code == _QUOTA_ERROR_CODE:
            if self._fallback is not None:
                return self._fallback.handle_request(request)
            if self._guard is not None and self._service_key is not None:
                # 방금 after_call이 키를 소진 마킹했으므로 before_call이 일관된
                # QuotaExhausted를 던진다.
                self._guard.before_call(self._service_key, endpoint)
            raise QuotaExhausted(0, 0)

        # ⑦ 정상 응답 캐시 저장(GET 한정).
        if (
            self._cache is not None
            and is_get
            and ok
            and 200 <= response.status_code < 300
        ):
            if cache_key is None:
                cache_key = make_key(request.method, str(request.url))
            self._cache.set(cache_key, self._serialize_response(response, content))

        return response

    def close(self) -> None:
        """하위·fallback 트랜스포트를 닫는다(record 모드면 카세트 저장이 연쇄된다)."""
        try:
            self._inner.close()
        finally:
            if self._fallback is not None:
                self._fallback.close()

    # -- 내부 헬퍼 --------------------------------------------------------
    def _inject_key(self, request: httpx.Request) -> None:
        """요청 쿼리에 serviceKey 원문을 주입한다(이미 있으면 중복 주입하지 않음)."""
        if self._service_key is None:
            return
        key_param = self._profile.key_param
        if key_param in request.url.params:
            return
        request.url = request.url.copy_merge_params({key_param: self._service_key})

    def _call_with_retry(
        self, request: httpx.Request, endpoint: str
    ) -> httpx.Response:
        """TransportError·HTTP 5xx를 지수 백오프로 max_retries까지 재시도한다.

        재시도로 소비되는 각 물리 상위 호출을 원장에 기록해, 지속적 5xx/전송오류
        하에서도 CALL_BUDGET 하드가드(count_today 기반)가 실제 상위 요청량을
        하회하지 않게 한다. 최종적으로 반환되는 응답 1건은 호출부의 after_call 이
        기록하므로 여기서는 중복 기록하지 않는다.
        """
        attempt = 0
        while True:
            try:
                response = self._inner.handle_request(request)
            except httpx.TransportError:
                # 이 물리 호출은 실제로 상위에 나갔다 → 예산 소비로 집계.
                self._record_physical_call(endpoint)
                if attempt >= self._max_retries:
                    raise
                self._sleep(compute_delay(attempt))
                attempt += 1
                continue
            if response.status_code >= 500 and attempt < self._max_retries:
                # 재시도되는 5xx 물리 호출을 예산 소비로 집계(최종 응답은 after_call).
                self._record_physical_call(endpoint)
                response.close()
                self._sleep(compute_delay(attempt))
                attempt += 1
                continue
            return response

    def _record_physical_call(self, endpoint: str) -> None:
        """재시도로 소비된 물리 상위 호출 1건을 원장에 기록한다(가드가 있을 때만)."""
        if self._guard is not None and self._service_key is not None:
            self._guard.record_retry(self._service_key, endpoint)

    @staticmethod
    def _serialize_response(response: httpx.Response, content: bytes) -> bytes:
        """응답을 캐시 저장용 바이트(JSON 프레이밍)로 직렬화한다."""
        payload = {
            "status": response.status_code,
            "content_type": response.headers.get("content-type", ""),
            "body_b64": base64.b64encode(content).decode("ascii"),
        }
        return json.dumps(payload).encode("utf-8")

    @staticmethod
    def _response_from_cache(data: bytes, request: httpx.Request) -> httpx.Response:
        """캐시 바이트에서 응답을 복원하고 X-MCPortal-Cache: hit 헤더를 단다."""
        payload = json.loads(data.decode("utf-8"))
        headers: dict[str, str] = {_CACHE_HEADER: "hit"}
        content_type = payload.get("content_type")
        if content_type:
            headers["content-type"] = content_type
        body = base64.b64decode(payload["body_b64"])
        return httpx.Response(
            status_code=int(payload["status"]),
            headers=headers,
            content=body,
            request=request,
        )


def _build_guard(
    budget: int | None,
    ledger_path: PathLike | None,
) -> QuotaGuard | None:
    """budget/ledger_path로 QuotaGuard를 조립한다.

    budget·ledger_path 가 모두 None이면 가드를 배선하지 않는다. budget 이
    명시되면 ledger_path 가 없어도 기본 원장 경로(UsageLedger 기본값,
    ~/.mcportal/ledger.db)로 가드를 배선한다 — CALL_BUDGET 하드캡이 기본 사용
    경로에서 조용히 무력화되는 것을 막기 위함이다.
    """
    if budget is None and ledger_path is None:
        return None
    ledger = UsageLedger(ledger_path)
    daily = DailyBudget(budget)
    return QuotaGuard(ledger, daily)


def create_client(
    service_key: str | None = None,
    budget: int | None = None,
    profile: ProviderProfile = DATA_GO_KR,
    cache_ttl: float = 300.0,
    ledger_path: PathLike | None = None,
    mode: str = "live",
    cassette_path: PathLike | None = None,
) -> httpx.Client:
    """모드별 httpx.Client 배선 헬퍼.

    - ``"live"``: MCPortalTransport(가드+캐시) + httpx.HTTPTransport. service_key 필수.
    - ``"record"``: RecordingTransport를 inner로 배선(secrets=[service_key]).
      캐시는 끄고(모든 호출을 카세트에 남기기 위해) 클라이언트 close 시 카세트 저장.
    - ``"replay"``: ReplayTransport 직결(가드·캐시 불요). service_key=None 허용.

    Args:
        service_key: data.go.kr 인증키(replay 모드는 None 허용).
        budget: 일일 예산 상한(None이면 CALL_BUDGET/기본 10,000). budget 을 명시하면
            ledger_path 가 없어도 기본 원장 경로로 하드가드가 배선된다.
        profile: 프로바이더 프로파일.
        cache_ttl: live 캐시 TTL(초).
        ledger_path: 사용량 원장 경로(budget·ledger_path 가 모두 None일 때만 가드 미배선).
        mode: "live" | "record" | "replay".
        cassette_path: record/replay 카세트 경로.
    """
    if mode == "replay":
        if cassette_path is None:
            raise ValueError("replay 모드에는 cassette_path가 필요합니다.")
        transport: httpx.BaseTransport = ReplayTransport(Cassette.load(cassette_path))
        return httpx.Client(transport=transport)

    if mode == "record":
        if service_key is None:
            raise ValueError("record 모드에는 service_key가 필요합니다.")
        if cassette_path is None:
            raise ValueError("record 모드에는 cassette_path가 필요합니다.")
        # 정본 시크릿은 실제 전송에 쓰이는 '준비된(디코딩)' 키다. 이 형태의
        # 스크러빙 변형(quote/quote_plus)이 인코딩키 형태까지 함께 덮으므로,
        # 원문 입력과 준비된 키를 모두 시크릿으로 넘겨 누출을 이중 차단한다.
        prepared = prepare_service_key(service_key)
        secrets = [s for s in dict.fromkeys([service_key, prepared]) if s]
        recording = RecordingTransport(
            httpx.HTTPTransport(),
            Cassette(),
            secrets=secrets,
            save_path=cassette_path,
        )
        guard = _build_guard(budget, ledger_path)
        mc = MCPortalTransport(
            service_key,
            inner=recording,
            guard=guard,
            cache=None,
            profile=profile,
        )
        return httpx.Client(transport=mc)

    if mode == "live":
        if service_key is None:
            raise ValueError("live 모드에는 service_key가 필요합니다.")
        guard = _build_guard(budget, ledger_path)
        mc = MCPortalTransport(
            service_key,
            inner=httpx.HTTPTransport(),
            guard=guard,
            cache=TTLCache(ttl=cache_ttl),
            profile=profile,
        )
        return httpx.Client(transport=mc)

    raise ValueError(f"알 수 없는 mode입니다: {mode!r} (live/record/replay 중 하나)")
