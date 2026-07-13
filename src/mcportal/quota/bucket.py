# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Yong Park
"""토큰 버킷 기반 초당 호출률 제한기.

클록을 주입해 테스트 결정론을 확보하고, threading.Lock으로 스레드 안전을 보장한다.
"""
from __future__ import annotations

import threading
import time
from typing import Callable


class TokenBucket:
    """초당 refill_rate 토큰으로 리필되는 용량 capacity의 토큰 버킷.

    Args:
        capacity: 최대 토큰 수(버스트 허용량). 0보다 커야 한다.
        refill_rate: 초당 리필되는 토큰 수. 0보다 커야 한다.
        clock: 단조 시각을 반환하는 콜러블. 기본값 time.monotonic.
               테스트에서 가짜 클록을 주입해 시간 흐름을 통제한다.
    """

    def __init__(
        self,
        capacity: float,
        refill_rate: float,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if capacity <= 0:
            raise ValueError("capacity는 0보다 커야 합니다")
        if refill_rate <= 0:
            raise ValueError("refill_rate는 0보다 커야 합니다")
        self.capacity: float = float(capacity)
        self.refill_rate: float = float(refill_rate)
        self._clock = clock
        self._tokens: float = float(capacity)
        self._last: float = clock()
        self._lock = threading.Lock()

    def _refill_locked(self) -> None:
        """경과 시간만큼 토큰을 리필한다(락 보유 상태에서 호출)."""
        now = self._clock()
        elapsed = now - self._last
        if elapsed > 0:
            self._tokens = min(
                self.capacity, self._tokens + elapsed * self.refill_rate
            )
            self._last = now

    def try_acquire(self, n: float = 1) -> bool:
        """토큰 n개를 즉시 소비 시도한다. 성공하면 True, 부족하면 False."""
        with self._lock:
            self._refill_locked()
            if self._tokens >= n:
                self._tokens -= n
                return True
            return False

    def wait_time(self, n: float = 1) -> float:
        """토큰 n개가 확보될 때까지 필요한 대기 시간(초). 즉시 가용이면 0.0."""
        with self._lock:
            self._refill_locked()
            if self._tokens >= n:
                return 0.0
            deficit = n - self._tokens
            return deficit / self.refill_rate
