# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Yong Park
"""DailyBudget 테스트: 기본값/환경변수/명시인자 우선순위, OK/WARN/EXHAUSTED 경계."""
from __future__ import annotations

import pytest

from mcportal.quota import BudgetStatus, DailyBudget


def test_default_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CALL_BUDGET", raising=False)
    assert DailyBudget().limit == 10_000


def test_env_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CALL_BUDGET", "500")
    assert DailyBudget().limit == 500


def test_explicit_arg_overrides_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CALL_BUDGET", "500")
    assert DailyBudget(limit=42).limit == 42


def test_blank_env_falls_back_to_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CALL_BUDGET", "   ")
    assert DailyBudget().limit == 10_000


def test_invalid_env_raises_korean(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CALL_BUDGET", "not-an-int")
    with pytest.raises(ValueError) as ei:
        DailyBudget()
    assert "CALL_BUDGET" in str(ei.value)


def test_status_boundaries(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CALL_BUDGET", raising=False)
    b = DailyBudget(limit=10, soft_ratio=0.8)  # WARN 임계 = 8
    assert b.status(0) is BudgetStatus.OK
    assert b.status(7) is BudgetStatus.OK
    assert b.status(8) is BudgetStatus.WARN
    assert b.status(9) is BudgetStatus.WARN
    assert b.status(10) is BudgetStatus.EXHAUSTED
    assert b.status(11) is BudgetStatus.EXHAUSTED


def test_status_is_strenum() -> None:
    assert BudgetStatus.EXHAUSTED == "EXHAUSTED"
    assert str(BudgetStatus.OK) == "OK"
