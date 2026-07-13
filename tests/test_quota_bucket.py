# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Yong Park
"""TokenBucket 테스트: 가짜 클록으로 소진→리필→try_acquire/wait_time 검증."""
from __future__ import annotations

import pytest

from mcportal.quota import TokenBucket


class FakeClock:
    """수동으로 전진시키는 단조 클록."""

    def __init__(self, t: float = 0.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t

    def advance(self, dt: float) -> None:
        self.t += dt


def test_drain_then_refill() -> None:
    clk = FakeClock()
    bucket = TokenBucket(capacity=2, refill_rate=1.0, clock=clk)

    assert bucket.try_acquire() is True
    assert bucket.try_acquire() is True
    assert bucket.try_acquire() is False  # 소진
    assert bucket.wait_time() == pytest.approx(1.0)  # 1토큰/1초 = 1.0s 대기

    clk.advance(1.0)  # 1토큰 리필
    assert bucket.wait_time() == 0.0
    assert bucket.try_acquire() is True


def test_partial_refill_wait_time() -> None:
    clk = FakeClock()
    bucket = TokenBucket(capacity=5, refill_rate=2.0, clock=clk)  # 2토큰/초
    for _ in range(5):
        assert bucket.try_acquire() is True
    assert bucket.try_acquire() is False
    assert bucket.wait_time() == pytest.approx(0.5)  # 1토큰 / 2초당토큰 = 0.5s

    clk.advance(0.25)  # +0.5 토큰
    assert bucket.try_acquire() is False
    assert bucket.wait_time() == pytest.approx(0.25)
    clk.advance(0.25)  # 이제 1토큰
    assert bucket.try_acquire() is True


def test_capacity_is_capped() -> None:
    clk = FakeClock()
    bucket = TokenBucket(capacity=3, refill_rate=1.0, clock=clk)
    clk.advance(1000.0)  # 대량 경과해도 capacity=3 상한
    assert bucket.try_acquire(3) is True
    assert bucket.try_acquire() is False


def test_multi_token_acquire() -> None:
    clk = FakeClock()
    bucket = TokenBucket(capacity=10, refill_rate=5.0, clock=clk)
    assert bucket.try_acquire(4) is True
    assert bucket.try_acquire(6) is True
    assert bucket.try_acquire(1) is False
    assert bucket.wait_time(1) == pytest.approx(0.2)  # 1/5


def test_invalid_construction() -> None:
    with pytest.raises(ValueError):
        TokenBucket(capacity=0, refill_rate=1.0)
    with pytest.raises(ValueError):
        TokenBucket(capacity=1.0, refill_rate=0)


def test_default_clock_starts_full() -> None:
    # 실제 time.monotonic 사용, 즉시 소비 가능(대기 불필요)
    bucket = TokenBucket(capacity=1, refill_rate=1.0)
    assert bucket.wait_time() == 0.0
    assert bucket.try_acquire() is True
