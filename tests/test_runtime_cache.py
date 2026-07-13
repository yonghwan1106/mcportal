# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Yong Park
"""runtime.cache 테스트: TTL 캐시 + serviceKey 없는 캐시 키."""
from __future__ import annotations

from mcportal.runtime.cache import TTLCache, make_key


class FakeClock:
    """수동 전진 가짜 단조 시계."""

    def __init__(self, start: float = 0.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, delta: float) -> None:
        self.now += delta


# ---------------------------------------------------------------------------
# TTL 동작
# ---------------------------------------------------------------------------
def test_get_returns_stored_value() -> None:
    """저장 후 조회하면 바이트가 그대로 나온다."""
    cache = TTLCache(ttl=100.0, clock=FakeClock())
    cache.set("k", b"payload")
    entry = cache.get("k")
    assert entry is not None
    assert entry.value == b"payload"


def test_ttl_expiry_with_fake_clock() -> None:
    """TTL 경과 후에는 만료되어 None 을 반환하고 항목이 제거된다."""
    clock = FakeClock()
    cache = TTLCache(ttl=300.0, clock=clock)
    cache.set("k", b"v")
    clock.advance(299.0)
    assert cache.get("k") is not None  # 아직 유효.
    clock.advance(1.0)  # 정확히 TTL 경계(300) → 만료.
    assert cache.get("k") is None
    assert len(cache) == 0  # 만료 시 제거됨.


def test_miss_returns_none() -> None:
    """없는 키는 None."""
    cache = TTLCache(clock=FakeClock())
    assert cache.get("missing") is None


# ---------------------------------------------------------------------------
# make_key — serviceKey 제거
# ---------------------------------------------------------------------------
def test_service_key_only_difference_same_cache_key() -> None:
    """serviceKey 만 다른 두 요청은 같은 캐시 키를 갖는다."""
    key_a = make_key("GET", "https://apis.data.go.kr/svc/list", {"serviceKey": "AAA", "pageNo": "1"})
    key_b = make_key("GET", "https://apis.data.go.kr/svc/list", {"serviceKey": "BBB", "pageNo": "1"})
    assert key_a == key_b


def test_service_key_in_query_string_stripped() -> None:
    """URL 쿼리스트링의 serviceKey 도 제거되어 캐시 키에 영향 없다."""
    key_a = make_key("GET", "https://apis.data.go.kr/svc/list?serviceKey=AAA&pageNo=1", None)
    key_b = make_key("GET", "https://apis.data.go.kr/svc/list?serviceKey=ZZZ&pageNo=1", None)
    assert key_a == key_b


def test_service_key_case_insensitive_stripped() -> None:
    """serviceKey 대소문자 변형(ServiceKey/SERVICEKEY)도 제거된다."""
    key_a = make_key("GET", "https://x/y", {"ServiceKey": "AAA", "n": "1"})
    key_b = make_key("GET", "https://x/y", {"SERVICEKEY": "BBB", "n": "1"})
    key_c = make_key("GET", "https://x/y", {"n": "1"})
    assert key_a == key_b == key_c


def test_service_key_raw_not_in_make_key_output() -> None:
    """인증키 원문이 make_key 결과(해시)에 절대 스며들지 않는다."""
    secret = "SUPER-SECRET-KEY-1234567890"
    key = make_key("GET", f"https://x/y?serviceKey={secret}", {"serviceKey": secret})
    assert secret not in key
    # 해시는 16진수 64자.
    assert len(key) == 64
    assert all(c in "0123456789abcdef" for c in key)


def test_different_params_different_key() -> None:
    """serviceKey 외 파라미터가 다르면 키가 달라진다."""
    key_a = make_key("GET", "https://x/y", {"pageNo": "1"})
    key_b = make_key("GET", "https://x/y", {"pageNo": "2"})
    assert key_a != key_b


def test_param_order_does_not_matter() -> None:
    """파라미터 순서가 달라도 같은 키(정렬 정규화)."""
    key_a = make_key("GET", "https://x/y", {"a": "1", "b": "2"})
    key_b = make_key("GET", "https://x/y", {"b": "2", "a": "1"})
    assert key_a == key_b


def test_method_case_normalized() -> None:
    """HTTP 메서드 대소문자는 정규화된다."""
    assert make_key("get", "https://x/y") == make_key("GET", "https://x/y")


# ---------------------------------------------------------------------------
# max_entries 축출
# ---------------------------------------------------------------------------
def test_max_entries_eviction() -> None:
    """용량 초과 시 가장 오래된 항목부터 축출한다."""
    cache = TTLCache(ttl=1000.0, max_entries=2, clock=FakeClock())
    cache.set("a", b"1")
    cache.set("b", b"2")
    cache.set("c", b"3")  # 'a' 가 축출되어야 함.
    assert len(cache) == 2
    assert cache.get("a") is None
    assert cache.get("b") is not None
    assert cache.get("c") is not None


def test_reset_existing_key_refreshes_recency() -> None:
    """기존 키를 다시 set 하면 최신으로 갱신되어 축출 대상에서 밀려난다."""
    cache = TTLCache(ttl=1000.0, max_entries=2, clock=FakeClock())
    cache.set("a", b"1")
    cache.set("b", b"2")
    cache.set("a", b"1-new")  # 'a' 갱신 → 최신.
    cache.set("c", b"3")  # 가장 오래된 'b' 축출.
    assert cache.get("b") is None
    entry_a = cache.get("a")
    assert entry_a is not None and entry_a.value == b"1-new"
    assert cache.get("c") is not None
