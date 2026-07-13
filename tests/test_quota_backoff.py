# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Yong Park
"""compute_delay 테스트: rng 주입 결정론, max_delay 캡, factor 증가, 음수 검증."""
from __future__ import annotations

import pytest

from mcportal.quota import compute_delay


def test_deterministic_full_ceiling() -> None:
    # rng()=1.0 이면 상한(ceiling)을 그대로 반환.
    assert compute_delay(0, base=0.5, factor=2.0, rng=lambda: 1.0) == pytest.approx(0.5)
    assert compute_delay(1, base=0.5, factor=2.0, rng=lambda: 1.0) == pytest.approx(1.0)
    assert compute_delay(2, base=0.5, factor=2.0, rng=lambda: 1.0) == pytest.approx(2.0)
    assert compute_delay(3, base=0.5, factor=2.0, rng=lambda: 1.0) == pytest.approx(4.0)


def test_full_jitter_scales_with_rng() -> None:
    # rng()=0.5 이면 상한의 절반.
    assert compute_delay(3, base=0.5, factor=2.0, rng=lambda: 0.5) == pytest.approx(2.0)
    assert compute_delay(0, base=1.0, factor=2.0, rng=lambda: 0.0) == 0.0


def test_max_delay_cap() -> None:
    # 큰 attempt에서 base*factor**attempt 이 폭발해도 max_delay로 캡.
    assert (
        compute_delay(20, base=0.5, factor=2.0, max_delay=30.0, rng=lambda: 1.0)
        == 30.0
    )


def test_factor_monotonic_increase() -> None:
    prev = -1.0
    for a in range(0, 6):
        d = compute_delay(a, base=1.0, factor=2.0, max_delay=1e12, rng=lambda: 1.0)
        assert d > prev
        prev = d


def test_negative_attempt_raises() -> None:
    with pytest.raises(ValueError):
        compute_delay(-1)
