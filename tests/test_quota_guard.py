# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Yong Park
"""QuotaGuard 테스트: 예산 소진 차단, result_code 22 마킹·다음날 재개, WARN 로깅, 레이트."""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

import pytest

from mcportal.quota import (
    DailyBudget,
    QuotaExhausted,
    QuotaGuard,
    TokenBucket,
    UsageLedger,
)

KEY = "GUARDKEY-datagokr-abcdef123456"


def _guard(
    tmp_path: Path,
    budget: DailyBudget,
    bucket: TokenBucket | None = None,
    sleep: Callable[[float], None] | None = None,
) -> tuple[QuotaGuard, UsageLedger]:
    led = UsageLedger(tmp_path / "guard.db")
    guard = QuotaGuard(
        led, budget, bucket=bucket, sleep=sleep or (lambda _s: None)
    )
    return guard, led


def test_budget_exhaustion_after_limit(tmp_path: Path) -> None:
    guard, led = _guard(tmp_path, DailyBudget(limit=3))
    # 3회 통과
    for _ in range(3):
        guard.before_call(KEY, "/x")
        guard.after_call(KEY, "/x")
    # 4회째 차단
    with pytest.raises(QuotaExhausted) as ei:
        guard.before_call(KEY, "/x")
    message = str(ei.value)
    assert "운영계정" in message  # 안내에 운영계정 전환 경로 포함
    assert ei.value.used == 3
    assert ei.value.limit == 3
    led.close()


def test_quota_error_blocks_same_day_then_resets_next_day(tmp_path: Path) -> None:
    guard, led = _guard(tmp_path, DailyBudget(limit=1000))
    d1 = datetime(2026, 7, 13, 3, 0, tzinfo=timezone.utc)  # KST 12:00 13일
    d2 = datetime(2026, 7, 14, 3, 0, tzinfo=timezone.utc)  # KST 12:00 14일

    guard.before_call(KEY, "/x", now=d1)
    guard.after_call(KEY, "/x", result_code="22", now=d1)  # 소진 마킹

    # 같은 날 즉시 차단(예산 여유와 무관)
    with pytest.raises(QuotaExhausted):
        guard.before_call(KEY, "/x", now=d1)

    # 다음 날은 재개
    guard.before_call(KEY, "/x", now=d2)  # 예외 없음
    guard.after_call(KEY, "/x", now=d2)
    led.close()


def test_warn_logged_once_per_key_per_day(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    # limit=5, soft_ratio=0.6 -> WARN 임계 = 3
    guard, led = _guard(tmp_path, DailyBudget(limit=5, soft_ratio=0.6))
    for _ in range(3):  # used 0,1,2 => OK
        guard.before_call(KEY, "/x")
        guard.after_call(KEY, "/x")

    with caplog.at_level(logging.WARNING, logger="mcportal.quota"):
        guard.before_call(KEY, "/x")  # used=3 => WARN (1회 기록)
        guard.after_call(KEY, "/x")
        guard.before_call(KEY, "/x")  # used=4 => WARN 이지만 억제

    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert "운영계정" in warnings[0].getMessage()
    led.close()


def test_bucket_throttles_via_injected_sleep(tmp_path: Path) -> None:
    class Clock:
        def __init__(self) -> None:
            self.t = 0.0

        def __call__(self) -> float:
            return self.t

    clk = Clock()
    slept: list[float] = []

    def fake_sleep(s: float) -> None:
        slept.append(s)
        clk.t += s  # 대기하면 시간이 흐르고 버킷이 리필됨

    bucket = TokenBucket(capacity=1, refill_rate=1.0, clock=clk)
    guard, led = _guard(
        tmp_path, DailyBudget(limit=1000), bucket=bucket, sleep=fake_sleep
    )

    guard.before_call(KEY, "/x")  # 토큰 1개 소비, 대기 없음
    guard.after_call(KEY, "/x")
    assert slept == []

    guard.before_call(KEY, "/x")  # 토큰 0 -> 1.0초 대기 후 진행
    guard.after_call(KEY, "/x")
    assert slept and slept[0] == pytest.approx(1.0)
    led.close()


def test_no_warn_when_ok(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    guard, led = _guard(tmp_path, DailyBudget(limit=1000))
    with caplog.at_level(logging.WARNING, logger="mcportal.quota"):
        for _ in range(5):
            guard.before_call(KEY, "/x")
            guard.after_call(KEY, "/x")
    assert [r for r in caplog.records if r.levelno == logging.WARNING] == []
    led.close()
