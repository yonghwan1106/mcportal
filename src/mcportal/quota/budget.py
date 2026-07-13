# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Yong Park
"""일일 호출 예산(DailyBudget)과 예산 상태(BudgetStatus).

한도 해석 우선순위: 명시 인자 > 환경변수 CALL_BUDGET > 기본 10,000.
"""
from __future__ import annotations

import os
from enum import StrEnum

_DEFAULT_LIMIT = 10_000
_ENV_VAR = "CALL_BUDGET"


class BudgetStatus(StrEnum):
    """예산 사용 상태."""

    OK = "OK"
    WARN = "WARN"
    EXHAUSTED = "EXHAUSTED"


class DailyBudget:
    """일일 호출 예산 상한과 소프트 경고 임계.

    Args:
        limit: 일일 상한. None이면 환경변수 CALL_BUDGET, 없으면 기본 10,000.
        soft_ratio: 경고(WARN) 임계 비율. used >= limit*soft_ratio 이면 WARN.
    """

    def __init__(self, limit: int | None = None, soft_ratio: float = 0.8) -> None:
        self.limit: int = self._resolve_limit(limit)
        self.soft_ratio: float = soft_ratio

    @staticmethod
    def _resolve_limit(limit: int | None) -> int:
        if limit is not None:
            return int(limit)
        raw = os.environ.get(_ENV_VAR)
        if raw is not None and raw.strip() != "":
            try:
                return int(raw)
            except ValueError as exc:
                raise ValueError(
                    f"환경변수 {_ENV_VAR} 값이 올바른 정수가 아닙니다: {raw!r}"
                ) from exc
        return _DEFAULT_LIMIT

    def status(self, used: int) -> BudgetStatus:
        """사용량 used에 대한 예산 상태를 반환한다."""
        if used >= self.limit:
            return BudgetStatus.EXHAUSTED
        if used >= self.limit * self.soft_ratio:
            return BudgetStatus.WARN
        return BudgetStatus.OK
