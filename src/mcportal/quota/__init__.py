# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Yong Park
"""쿼터가드 코어 서브패키지.

data.go.kr 1인 1키 구조 하에서 일일 호출 예산을 하드 상한으로 강제하고,
초당 레이트 제한·지수 백오프·사용량 원장을 제공한다. 원장 집계는 베스트에포트
추정이며 신뢰 축은 하드 예산 상한이다.
"""
from __future__ import annotations

from .backoff import compute_delay
from .bucket import TokenBucket
from .budget import BudgetStatus, DailyBudget
from .exceptions import EXHAUSTED_GUIDANCE, MULTIKEY_REFUSAL, QuotaExhausted
from .guard import QuotaGuard
from .ledger import UsageLedger

__all__ = [
    "TokenBucket",
    "UsageLedger",
    "DailyBudget",
    "BudgetStatus",
    "QuotaExhausted",
    "QuotaGuard",
    "compute_delay",
    "EXHAUSTED_GUIDANCE",
    "MULTIKEY_REFUSAL",
]
