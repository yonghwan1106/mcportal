# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Yong Park
"""transport 통합 테스트: 이중 인코딩 방지·예산 하드가드·EUC-KR·쿼터22·record/replay·캐시·백오프.

F9(가드 상시 배선·예산 해석 우선순위)와 F10(프로파일 인증키 이름 전파)의 회귀
케이스도 함께 담는다.

respx로 하위 HTTPTransport 호출만 가로채며, 실제 네트워크 호출은 없다. 픽스처는
전부 합성 데이터다.
"""
from __future__ import annotations

import gzip
import json
from pathlib import Path
from urllib.parse import quote, quote_plus

import httpx
import pytest
import respx

from mcportal import (
    Cassette,
    MCPortalTransport,
    ProviderProfile,
    QuotaExhausted,
    TTLCache,
    create_client,
    normalize_response,
)
from mcportal.quota import DailyBudget, QuotaGuard, UsageLedger

BASE = "https://apis.data.go.kr/svc/list"

# '+', '/', '=' 를 포함해 인코딩 변형이 뚜렷이 달라지는 합성 키.
DECODED_KEY = "ab12+CD/34=="
# 위 키의 인코딩키 형태(발급 시 주어지는 %XX 형태).
ENCODED_KEY = "ab12%2BCD%2F34%3D%3D"


def _guard(tmp_path: Path, budget: int, *, name: str = "ledger.db") -> QuotaGuard:
    ledger = UsageLedger(tmp_path / name)
    return QuotaGuard(ledger, DailyBudget(budget))


def _client(transport: httpx.BaseTransport) -> httpx.Client:
    return httpx.Client(transport=transport)


# ---------------------------------------------------------------------------
# ① 이중 인코딩 방지
# ---------------------------------------------------------------------------
@respx.mock
def test_service_key_encoded_exactly_once() -> None:
    route = respx.get(BASE).mock(return_value=httpx.Response(200, json={"ok": True}))
    transport = MCPortalTransport(ENCODED_KEY, inner=httpx.HTTPTransport())
    with _client(transport) as client:
        resp = client.get(BASE, params={"pageNo": "1"})
    assert resp.status_code == 200

    sent_url = str(route.calls.last.request.url)
    # 원문 '+' 는 %2B 로 정확히 1회 인코딩되고, 이중 인코딩(%252B)은 없어야 한다.
    assert "%2B" in sent_url
    assert "%252B" not in sent_url
    assert "%2F" in sent_url
    assert "%252F" not in sent_url
    # httpx가 디코딩키 원문을 정확히 1회 인코딩했으므로 값을 되돌리면 원문과 같다.
    assert route.calls.last.request.url.params["serviceKey"] == DECODED_KEY


# ---------------------------------------------------------------------------
# ② 예산 하드가드
# ---------------------------------------------------------------------------
@respx.mock
def test_budget_hard_guard_blocks_after_limit(tmp_path: Path) -> None:
    respx.get(BASE).mock(return_value=httpx.Response(200, json={"ok": True}))
    guard = _guard(tmp_path, budget=3)
    transport = MCPortalTransport(
        DECODED_KEY, inner=httpx.HTTPTransport(), guard=guard, cache=None
    )
    with _client(transport) as client:
        for _ in range(3):
            assert client.get(BASE, params={"pageNo": "1"}).status_code == 200
        with pytest.raises(QuotaExhausted) as excinfo:
            client.get(BASE, params={"pageNo": "1"})
    assert "운영계정" in str(excinfo.value)


# ---------------------------------------------------------------------------
# ③ EUC-KR(CP949) XML 본문 복원
# ---------------------------------------------------------------------------
@respx.mock
def test_euckr_xml_body_is_recovered() -> None:
    xml = (
        "<response><header><resultCode>00</resultCode>"
        "<resultMsg>정상</resultMsg></header>"
        "<body><item><sido>세종특별자치시</sido></item></body></response>"
    )
    body = xml.encode("cp949")
    respx.get(BASE).mock(
        return_value=httpx.Response(
            200,
            headers={"content-type": "application/xml; charset=euc-kr"},
            content=body,
        )
    )
    transport = MCPortalTransport(DECODED_KEY, inner=httpx.HTTPTransport())
    with _client(transport) as client:
        resp = client.get(BASE, params={"pageNo": "1"})

    # 트랜스포트는 원본 바이트를 그대로 전달한다(인코딩 손상 없음).
    assert resp.content == body
    normalized = normalize_response(resp.content, resp.headers.get("content-type"))
    assert normalized.result_code == "00"
    assert "세종특별자치시" in str(normalized.data)


# ---------------------------------------------------------------------------
# ④ resultCode 22(게이트웨이 오류) → QuotaExhausted + 같은 키 즉시 재차단
# ---------------------------------------------------------------------------
@respx.mock
def test_result_code_22_triggers_graceful_stop(tmp_path: Path) -> None:
    gateway_xml = (
        "<OpenAPI_ServiceResponse><cmmMsgHeader>"
        "<returnReasonCode>22</returnReasonCode>"
        "<returnAuthMsg>LIMITED_NUMBER_OF_SERVICE_REQUESTS_EXCEEDS_ERROR</returnAuthMsg>"
        "<errMsg>SERVICE ERROR</errMsg>"
        "</cmmMsgHeader></OpenAPI_ServiceResponse>"
    )
    route = respx.get(BASE).mock(
        return_value=httpx.Response(
            200, headers={"content-type": "application/xml"}, text=gateway_xml
        )
    )
    guard = _guard(tmp_path, budget=100)
    transport = MCPortalTransport(
        DECODED_KEY, inner=httpx.HTTPTransport(), guard=guard, cache=None
    )
    with _client(transport) as client:
        with pytest.raises(QuotaExhausted):
            client.get(BASE, params={"pageNo": "1"})
        # 같은 키의 즉시 재호출도 하위 호출 없이 차단된다.
        with pytest.raises(QuotaExhausted):
            client.get(BASE, params={"pageNo": "1"})
    # 하위 트랜스포트는 첫 호출에서 딱 1회만 접촉된다.
    assert route.call_count == 1


# ---------------------------------------------------------------------------
# ⑤ record → replay 왕복
# ---------------------------------------------------------------------------
@respx.mock
def test_record_then_replay_roundtrip(tmp_path: Path) -> None:
    # 응답 본문이 실키를 되비추는 상황까지 스크러빙되는지 확인.
    def _handler(request: httpx.Request) -> httpx.Response:
        page = request.url.params.get("pageNo", "?")
        xml = (
            "<response><header><resultCode>00</resultCode></header>"
            f"<echoKey>{DECODED_KEY}</echoKey>"
            f"<body><item><page>{page}</page></item></body></response>"
        )
        return httpx.Response(
            200, headers={"content-type": "application/xml; charset=utf-8"}, text=xml
        )

    respx.get(BASE).mock(side_effect=_handler)

    cassette_path = tmp_path / "cassette.json"
    # -- record: 두 개의 서로 다른 호출을 녹화(캐시 없음이라 둘 다 기록됨).
    rec_client = create_client(
        service_key=ENCODED_KEY,
        budget=100,
        ledger_path=tmp_path / "rec.db",
        mode="record",
        cassette_path=cassette_path,
    )
    with rec_client:
        r1 = rec_client.get(BASE, params={"pageNo": "1"})
        r2 = rec_client.get(BASE, params={"pageNo": "2"})
    # 하류 소비자는 비스크러빙 라이브 응답을 그대로 받는다.
    assert DECODED_KEY in r1.text and "<page>1</page>" in r1.text
    assert "<page>2</page>" in r2.text

    # -- 카세트 파일 전문에서 키 원문/인코딩 변형이 전부 사라졌는지 스캔.
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
    cassette = Cassette.load(cassette_path)
    assert len(cassette.interactions) == 2

    # -- replay: 무키로 동일 응답 재생.
    replay_client = create_client(mode="replay", cassette_path=cassette_path)
    with replay_client:
        p1 = replay_client.get(BASE, params={"pageNo": "1"})
        p2 = replay_client.get(BASE, params={"pageNo": "2"})
    assert p1.status_code == 200 and p2.status_code == 200
    assert "<page>1</page>" in p1.text
    assert "<page>2</page>" in p2.text
    # 재생 본문에는 실키가 없고 플레이스홀더로 스크러빙돼 있다.
    assert DECODED_KEY not in p1.text
    assert "__SCRUBBED__" in p1.text


# ---------------------------------------------------------------------------
# ⑥ 캐시: 동일 GET 2회 → 하위 호출 1회 + 캐시 헤더
# ---------------------------------------------------------------------------
@respx.mock
def test_cache_serves_second_get_without_inner_call() -> None:
    route = respx.get(BASE).mock(
        return_value=httpx.Response(
            200,
            headers={"content-type": "application/json"},
            json={"response": {"header": {"resultCode": "00", "resultMsg": "정상"}}},
        )
    )
    transport = MCPortalTransport(
        DECODED_KEY, inner=httpx.HTTPTransport(), cache=TTLCache(ttl=300.0)
    )
    with _client(transport) as client:
        first = client.get(BASE, params={"pageNo": "1"})
        second = client.get(BASE, params={"pageNo": "1"})

    assert route.call_count == 1  # 두 번째는 캐시에서 나온다.
    assert first.headers.get("X-MCPortal-Cache") is None
    assert second.headers.get("X-MCPortal-Cache") == "hit"
    assert first.content == second.content


# ---------------------------------------------------------------------------
# ⑦ 5xx 백오프 재시도: 500 두 번 후 200 성공
# ---------------------------------------------------------------------------
@respx.mock
def test_5xx_backoff_then_success() -> None:
    respx.get(BASE).mock(
        side_effect=[
            httpx.Response(500),
            httpx.Response(500),
            httpx.Response(200, json={"ok": True}),
        ]
    )
    slept: list[float] = []
    transport = MCPortalTransport(
        DECODED_KEY,
        inner=httpx.HTTPTransport(),
        max_retries=3,
        sleep=slept.append,  # 즉시 반환(실제 대기 없음).
    )
    with _client(transport) as client:
        resp = client.get(BASE, params={"pageNo": "1"})

    assert resp.status_code == 200
    assert resp.json() == {"ok": True}
    # 500 두 번에 대해 두 번 백오프 대기했다.
    assert len(slept) == 2


# ---------------------------------------------------------------------------
# ⑧ record/replay: Content-Encoding: gzip 응답도 재구성 크래시 없이 왕복
# ---------------------------------------------------------------------------
@respx.mock
def test_record_replay_gzip_encoded_response(tmp_path: Path) -> None:
    xml = (
        "<response><header><resultCode>00</resultCode></header>"
        "<body><item><sido>세종특별자치시</sido></item></body></response>"
    )
    gzipped = gzip.compress(xml.encode("utf-8"))
    respx.get(BASE).mock(
        return_value=httpx.Response(
            200,
            headers={
                "content-type": "application/xml; charset=utf-8",
                "content-encoding": "gzip",
            },
            content=gzipped,
        )
    )

    cassette_path = tmp_path / "gzip_cassette.json"
    rec_client = create_client(
        service_key=DECODED_KEY,
        budget=100,
        ledger_path=tmp_path / "gzip_rec.db",
        mode="record",
        cassette_path=cassette_path,
    )
    # 재구성 시 gunzip 된 바이트를 다시 gunzip 하려던 DecodingError 크래시가 없어야.
    with rec_client:
        resp = rec_client.get(BASE, params={"pageNo": "1"})
    assert resp.status_code == 200
    assert "세종특별자치시" in resp.text

    # replay 왕복도 정상(카세트에는 디코딩·스크러빙된 본문만 저장됨).
    replay_client = create_client(mode="replay", cassette_path=cassette_path)
    with replay_client:
        played = replay_client.get(BASE, params={"pageNo": "1"})
    assert played.status_code == 200
    assert "세종특별자치시" in played.text


# ---------------------------------------------------------------------------
# ⑨ 재시도 원장 집계: 물리 상위 호출 수 = 원장 행 수
# ---------------------------------------------------------------------------
@respx.mock
def test_retry_records_each_physical_call_in_ledger(tmp_path: Path) -> None:
    respx.get(BASE).mock(
        side_effect=[
            httpx.Response(500),
            httpx.Response(500),
            httpx.Response(200, json={"ok": True}),
        ]
    )
    ledger = UsageLedger(tmp_path / "retry.db")
    guard = QuotaGuard(ledger, DailyBudget(100))
    transport = MCPortalTransport(
        DECODED_KEY,
        inner=httpx.HTTPTransport(),
        guard=guard,
        cache=None,
        max_retries=3,
        sleep=lambda _s: None,  # 즉시 반환(실제 대기 없음).
    )
    with _client(transport) as client:
        resp = client.get(BASE, params={"pageNo": "1"})
    assert resp.status_code == 200
    # 500,500,200 → 물리 상위 호출 3회 → 원장에도 정확히 3행(언더카운트 없음).
    # DECODED_KEY 는 %XX 시퀀스가 없어 prepare_service_key 가 그대로 두므로 지문 일치.
    assert ledger.count_today(DECODED_KEY) == 3
    ledger.close()


# ---------------------------------------------------------------------------
# ⑩ F9: 인자를 생략해도 프로파일 기본 예산으로 하드가드가 배선된다
# ---------------------------------------------------------------------------
#: 예산 폴백을 몇 번의 호출로 관측하기 위한 소량 예산 커스텀 프로파일(합성).
SMALL_BUDGET_PROFILE = ProviderProfile(
    name="가상포털",
    key_param="serviceKey",
    host_suffixes=("apis.data.go.kr",),
    default_daily_budget=2,
    multi_key_supported=False,
    guidance_exhausted="",
    refusal_multikey="",
)


@respx.mock
def test_f9_profile_default_budget_is_wired_without_arguments(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    respx.get(BASE).mock(return_value=httpx.Response(200, json={"ok": True}))
    # 환경변수가 설정된 셸에서 돌면 환경변수가 이기므로 반드시 제거한다.
    monkeypatch.delenv("CALL_BUDGET", raising=False)
    # ledger_path 를 생략해도 배선되는지 보되, 홈 디렉터리를 건드리지 않도록
    # UsageLedger 기본 경로만 임시 경로로 바꿔 둔다.
    monkeypatch.setattr(
        "mcportal.quota.ledger._DEFAULT_PATH", tmp_path / "home_ledger.db"
    )

    # budget=None, ledger_path=None — W1에서는 이 조합이 '무가드'였다.
    client = create_client(
        service_key=DECODED_KEY, mode="live", profile=SMALL_BUDGET_PROFILE
    )
    with client:
        for page in ("1", "2"):
            assert client.get(BASE, params={"pageNo": page}).status_code == 200
        with pytest.raises(QuotaExhausted) as excinfo:
            client.get(BASE, params={"pageNo": "3"})
    assert "운영계정" in str(excinfo.value)


@respx.mock
def test_f9_env_call_budget_wins_over_profile_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    respx.get(BASE).mock(return_value=httpx.Response(200, json={"ok": True}))
    # 우선순위: 명시 인자 > CALL_BUDGET > profile.default_daily_budget(=2).
    monkeypatch.setenv("CALL_BUDGET", "1")

    client = create_client(
        service_key=DECODED_KEY,
        mode="live",
        ledger_path=tmp_path / "env.db",
        profile=SMALL_BUDGET_PROFILE,
    )
    with client:
        assert client.get(BASE, params={"pageNo": "1"}).status_code == 200
        # 프로파일 기본값 2가 아니라 환경변수 1이 상한이므로 2번째에서 막힌다.
        with pytest.raises(QuotaExhausted):
            client.get(BASE, params={"pageNo": "2"})


@respx.mock
def test_f9_explicit_budget_wins_over_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    respx.get(BASE).mock(return_value=httpx.Response(200, json={"ok": True}))
    monkeypatch.setenv("CALL_BUDGET", "1")

    client = create_client(
        service_key=DECODED_KEY,
        budget=3,
        mode="live",
        ledger_path=tmp_path / "explicit.db",
        profile=SMALL_BUDGET_PROFILE,
    )
    with client:
        # 명시 인자 3이 환경변수 1을 이긴다.
        for page in ("1", "2", "3"):
            assert client.get(BASE, params={"pageNo": page}).status_code == 200
        with pytest.raises(QuotaExhausted):
            client.get(BASE, params={"pageNo": "4"})


# ---------------------------------------------------------------------------
# ⑪ F10: record 시 프로파일의 인증키 파라미터 이름이 카세트에 전파된다
# ---------------------------------------------------------------------------
CUSTOM_KEY_PROFILE = ProviderProfile(
    name="가상포털",
    key_param="apiKey",
    host_suffixes=("apis.data.go.kr",),
    default_daily_budget=100,
    multi_key_supported=False,
    guidance_exhausted="",
    refusal_multikey="",
    key_param_aliases=("api_key",),
)


@respx.mock
def test_f10_profile_key_param_propagates_to_cassette(tmp_path: Path) -> None:
    def _handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "application/json"},
            json={"response": {"header": {"resultCode": "00"}}},
        )

    respx.get(BASE).mock(side_effect=_handler)

    cassette_path = tmp_path / "custom_key.json"
    rec_client = create_client(
        service_key=DECODED_KEY,
        budget=100,
        ledger_path=tmp_path / "custom_key.db",
        mode="record",
        cassette_path=cassette_path,
        profile=CUSTOM_KEY_PROFILE,
    )
    with rec_client:
        assert rec_client.get(BASE, params={"pageNo": "1"}).status_code == 200

    # 주입은 프로파일의 정본 키 이름으로 나간다.
    sent = respx.calls.last.request
    assert sent.url.params["apiKey"] == DECODED_KEY

    data = json.loads(cassette_path.read_text(encoding="utf-8"))
    # 카세트에 프로파일 유래 키 이름들이 기록된다(정본 + 별칭).
    assert data["key_params"] == ["apiKey", "api_key"]
    raw = cassette_path.read_text(encoding="utf-8")
    for variant in (
        DECODED_KEY,
        quote(DECODED_KEY),
        quote(DECODED_KEY, safe=""),
        quote_plus(DECODED_KEY),
    ):
        assert variant not in raw, f"카세트에 키 변형이 남았다: {variant}"
    assert "__SCRUBBED__" in raw

    # replay 는 파일에 적힌 key_params 를 그대로 쓰므로 무키 재생이 매칭된다.
    replay_client = create_client(mode="replay", cassette_path=cassette_path)
    with replay_client:
        played = replay_client.get(BASE, params={"pageNo": "1"})
    assert played.status_code == 200
    assert played.json() == {"response": {"header": {"resultCode": "00"}}}
