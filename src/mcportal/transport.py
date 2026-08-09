# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Yong Park
"""httpx 전송 계층 통합: 쿼터가드·캐시·정규화·record/replay를 하나의 트랜스포트로 배선한다.

:class:`MCPortalTransport` 는 ``httpx.BaseTransport`` 구현으로, 하위 트랜스포트
호출 전후에 다음을 강제한다.

1. 인증키 원문 주입. 위치는 ``profile.key_location`` 이 정한다(F-08) — ``"query"``
   (기본)면 질의문자열에 원문을 남겨 httpx가 정확히 1회 인코딩하게 하고(이중
   인코딩(코드 30) 방지), ``"header"`` 면 ``profile.key_param`` 이름의 요청 헤더에
   싣는다. 카세트는 요청 **헤더를 기록하지 않으므로** 헤더 경로의 키는 녹화 파일로
   새지 않는다(:mod:`mcportal.replay.cassette`).
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
import os
import time
from pathlib import Path
from typing import Callable, Optional, Union

import httpx

from .profiles import DATA_GO_KR, ProviderProfile
from .profiles.datago import key_params_of
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

#: CALL_BUDGET 환경변수 이름(quota.budget 의 해석 규칙과 동일 값. 폴백 판정용).
_ENV_CALL_BUDGET = "CALL_BUDGET"


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
        owns_guard: True 면 :meth:`close` 가 가드의 원장 커넥션까지 닫는다.
            :func:`_build_transport` 가 직접 조립한 가드에만 True 를 준다(호출자가
            넘겨 준 가드의 수명은 호출자 것이다).
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
        owns_guard: bool = False,
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
        self._owns_guard = bool(owns_guard)

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

        # ③ 쿼터가드 before_call. 통과하면 in-flight 예약 1건이 선점되므로,
        #    아래 구간은 반드시 after_call(기록+반납) 또는 release(반납)로 끝나야
        #    한다. 예약이 반납되지 않으면 그 자리가 영구히 소비된 것으로 남는다.
        guarded = self._guard is not None and self._service_key is not None
        reserved = False
        if guarded:
            try:
                self._guard.before_call(self._service_key, endpoint)  # type: ignore[union-attr]
            except QuotaExhausted:
                if self._fallback is not None:
                    return self._fallback.handle_request(request)
                raise
            reserved = True

        try:
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

            # after_call 기록(+ 예약 반납). after_call 은 자체 finally 로 반납을
            # 보장하므로, 이중 반납을 막기 위해 호출 **전에** 소유권을 넘긴다.
            if guarded:
                reserved = False
                self._guard.after_call(  # type: ignore[union-attr]
                    self._service_key,
                    endpoint,
                    result_code=result_code,
                    status="ok" if ok else "error",
                )
        finally:
            if reserved:
                self._guard.release()  # type: ignore[union-attr]

        # ⑥ result_code 22 → graceful stop(재시도 금지).
        if result_code == _QUOTA_ERROR_CODE:
            if self._fallback is not None:
                return self._fallback.handle_request(request)
            if guarded:
                # 방금 after_call이 키를 소진 마킹했으므로 일관된 QuotaExhausted를
                # 만든다. before_call 이 아니라 raise_if_exhausted 를 쓰는 이유:
                # 여기서 예약을 새로 선점하면 반납할 곳이 없다.
                self._guard.raise_if_exhausted(self._service_key, endpoint)  # type: ignore[union-attr]
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
        """하위·fallback 트랜스포트를 닫고, 소유한 가드의 원장 커넥션도 닫는다.

        ``owns_guard=True`` 로 만들어진 가드(= :func:`_build_transport` 가 직접
        조립한 것)만 닫는다. 호출자가 넘겨 준 가드는 그 소유가 아니므로 건드리지
        않는다. 이 연쇄가 없으면 ``client.close()`` 만으로는 SQLite 원장 커넥션이
        회수되지 않아 Windows 에서 임시 디렉터리 정리가 실패한다.
        """
        try:
            self._inner.close()
        finally:
            try:
                if self._fallback is not None:
                    self._fallback.close()
            finally:
                if self._owns_guard and self._guard is not None:
                    self._guard.close()

    # -- 내부 헬퍼 --------------------------------------------------------
    def _inject_key(self, request: httpx.Request) -> None:
        """인증키 원문을 프로파일이 지정한 위치에 주입한다(F-08).

        ``profile.key_location`` 이 ``"query"``(기본)면 질의문자열에,
        ``"header"`` 면 ``profile.key_param`` 이름의 **요청 헤더**에 싣는다. 어느
        쪽이든 이미 값이 있으면 중복 주입하지 않는다 — 호출자가 명시적으로 실은
        값을 덮어쓰면 그쪽 의도가 조용히 사라진다.

        질의문자열 경로가 원문(디코딩키)을 넣는 이유는 httpx 가 정확히 1회
        인코딩하게 남기기 위해서다(코드 30 이중 인코딩 방지). **헤더 경로는
        httpx 가 인코딩하지 않으므로 원문이 그대로 나간다** — 이것이 헤더 인증의
        정상 동작이다.

        헤더 값의 접두 형식(``Bearer <키>`` · ``Infuser <키>`` 등)은 다루지 않는다.
        필요해지면 프로파일에 ``key_header_format`` 을 더해 그 자리에서 포맷하는
        것이 확장 지점이며, 지금은 **원문(raw)** 만 싣는다. 실제 제공자를 확보하기
        전에 포맷 축을 열면 검증할 수 없는 분기가 하나 늘어날 뿐이다.
        """
        if self._service_key is None:
            return
        key_param = self._profile.key_param
        if str(getattr(self._profile, "key_location", "query")) == "header":
            # httpx.Headers 의 포함 검사는 대소문자를 무시한다(HTTP 규약).
            if key_param in request.headers:
                return
            request.headers[key_param] = self._service_key
            return
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


def _resolve_budget(budget: int | None, profile: ProviderProfile) -> int | None:
    """일일 예산 한도를 해석한다(F9).

    우선순위: 명시 인자 > 환경변수 ``CALL_BUDGET`` > ``profile.default_daily_budget``.
    환경변수가 설정돼 있으면 None을 돌려주어 :class:`DailyBudget` 이 직접
    해석하게 둔다(환경변수 파싱·오류 메시지를 한곳에 유지하기 위함).

    Args:
        budget: 호출자가 명시한 일일 상한(없으면 None).
        profile: 폴백 기본 예산을 제공하는 프로바이더 프로파일.

    Returns:
        DailyBudget 에 넘길 한도. None이면 DailyBudget 이 환경변수를 해석한다.
    """
    if budget is not None:
        return int(budget)
    raw = os.environ.get(_ENV_CALL_BUDGET)
    if raw is not None and raw.strip() != "":
        return None
    return profile.default_daily_budget


def _build_guard(
    budget: int | None,
    ledger_path: PathLike | None,
    profile: ProviderProfile = DATA_GO_KR,
) -> QuotaGuard:
    """budget/ledger_path/profile로 QuotaGuard를 조립한다(항상 배선된다).

    W1까지는 budget·ledger_path가 모두 None이면 가드를 배선하지 않았다. 그러면
    인자를 생략한 기본 경로에서 하드 예산 상한이 조용히 사라지는데, 이는 "신뢰의
    축은 CALL_BUDGET 하드 가드"라는 README 선언과 어긋난다. F9로 프로파일 기본
    예산(``profile.default_daily_budget``)을 폴백에 배선하면서 무가드 조합을
    없앤다. 가드 없는 트랜스포트가 필요하면 ``MCPortalTransport(guard=None)`` 을
    직접 만든다.

    Args:
        budget: 명시 일일 상한(None이면 환경변수→프로파일 순으로 폴백).
        ledger_path: 사용량 원장 경로(None이면 UsageLedger 기본 경로).
        profile: 폴백 기본 예산을 제공하는 프로바이더 프로파일.

    Returns:
        조립된 :class:`QuotaGuard`.
    """
    ledger = UsageLedger(ledger_path)
    daily = DailyBudget(_resolve_budget(budget, profile))
    return QuotaGuard(ledger, daily)


def _build_transport(
    service_key: str | None = None,
    *,
    budget: int | None = None,
    profile: ProviderProfile = DATA_GO_KR,
    cache_ttl: float = 300.0,
    ledger_path: PathLike | None = None,
    mode: str = "live",
    cassette_path: PathLike | None = None,
) -> httpx.BaseTransport:
    """모드별 sync 트랜스포트를 조립한다(create_client·mcp 브리지 공용 내부 헬퍼).

    :func:`create_client` 와 :func:`mcportal.mcp.build_async_client` 가 같은
    조립 규칙을 쓰도록 한곳에 모아 둔 내부 함수다. 공개 API가 아니다.

    Args:
        service_key: data.go.kr 인증키(replay 모드는 None 허용).
        budget: 일일 예산 상한(None이면 F9 폴백 규칙).
        profile: 프로바이더 프로파일.
        cache_ttl: live 캐시 TTL(초).
        ledger_path: 사용량 원장 경로.
        mode: "live" | "record" | "replay".
        cassette_path: record/replay 카세트 경로.

    Returns:
        배선된 sync 트랜스포트.

    Raises:
        ValueError: 모드별 필수 인자가 없거나 알 수 없는 mode일 때.
    """
    if mode == "replay":
        if cassette_path is None:
            raise ValueError("replay 모드에는 cassette_path가 필요합니다.")
        return ReplayTransport(Cassette.load(cassette_path))

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
        # F10: 인증키 파라미터 이름을 프로파일에서 유도해 녹화 계층에 전파한다.
        key_params = key_params_of(profile)
        recording = RecordingTransport(
            httpx.HTTPTransport(),
            Cassette(key_params=key_params),
            secrets=secrets,
            save_path=cassette_path,
            key_params=key_params,
        )
        return MCPortalTransport(
            service_key,
            inner=recording,
            guard=_build_guard(budget, ledger_path, profile),
            cache=None,
            profile=profile,
            owns_guard=True,
        )

    if mode == "live":
        if service_key is None:
            raise ValueError("live 모드에는 service_key가 필요합니다.")
        return MCPortalTransport(
            service_key,
            inner=httpx.HTTPTransport(),
            guard=_build_guard(budget, ledger_path, profile),
            cache=TTLCache(ttl=cache_ttl),
            profile=profile,
            owns_guard=True,
        )

    raise ValueError(f"알 수 없는 mode입니다: {mode!r} (live/record/replay 중 하나)")


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

    쿼터가드는 **항상 배선된다**(F9). budget 을 생략해도 CALL_BUDGET 환경변수,
    그것도 없으면 프로파일 기본 예산이 하드 상한이 된다. 가드 없는 트랜스포트가
    필요하면 :class:`MCPortalTransport` 를 ``guard=None`` 으로 직접 만든다.

    Args:
        service_key: data.go.kr 인증키(replay 모드는 None 허용).
        budget: 일일 예산 상한. 해석 우선순위는 **명시 인자 > 환경변수
            CALL_BUDGET > profile.default_daily_budget** 이다. ledger_path 가
            없어도 기본 원장 경로(~/.mcportal/ledger.db)로 하드가드가 배선된다.
        profile: 프로바이더 프로파일(키 파라미터 이름·기본 예산 제공).
        cache_ttl: live 캐시 TTL(초).
        ledger_path: 사용량 원장 경로(None이면 UsageLedger 기본 경로).
        mode: "live" | "record" | "replay".
        cassette_path: record/replay 카세트 경로.

    Raises:
        ValueError: 모드별 필수 인자가 없거나 알 수 없는 mode일 때.
    """
    transport = _build_transport(
        service_key,
        budget=budget,
        profile=profile,
        cache_ttl=cache_ttl,
        ledger_path=ledger_path,
        mode=mode,
        cassette_path=cassette_path,
    )
    return httpx.Client(transport=transport)
