# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Yong Park
"""지수 백오프 지연 계산(full jitter 방식).

rng를 주입해 테스트 결정론을 확보한다.
"""
from __future__ import annotations

import random
from typing import Callable


def compute_delay(
    attempt: int,
    base: float = 0.5,
    factor: float = 2.0,
    max_delay: float = 30.0,
    rng: Callable[[], float] = random.random,
) -> float:
    """full jitter 지수 백오프 지연(초)을 계산한다.

    지연 = rng() * min(max_delay, base * factor**attempt).

    Args:
        attempt: 재시도 회차(0부터). 음수면 ValueError.
        base: 기준 지연.
        factor: 지수 증가 배수.
        max_delay: 지연 상한(캡).
        rng: [0.0, 1.0) 난수를 반환하는 콜러블(테스트 주입용).
    """
    if attempt < 0:
        raise ValueError("attempt는 0 이상이어야 합니다")
    ceiling = min(max_delay, base * factor ** attempt)
    return rng() * ceiling
