# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Yong Park
"""쿼터가드(QuotaGuard): 호출 전/후 훅으로 일일 예산과 초당 레이트를 강제한다.

원장(UsageLedger)은 MCPortal 경유 호출만 집계하는 베스트에포트 추정이며,
data.go.kr가 잔여 쿼터 조회 API를 제공하지 않으므로 실제 잔여량과 어긋날 수 있다.
따라서 신뢰 축은 하드 예산 상한(DailyBudget)이다. 원장은 관측·경고 보조용이다.
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Callable, Optional

from .budget import BudgetStatus, DailyBudget
from .bucket import TokenBucket
from .exceptions import QuotaExhausted
from .ledger import UsageLedger, key_fp, kst_day

_logger = logging.getLogger("mcportal.quota")

# data.go.kr가 쿼터 초과를 알리는 result_code.
_QUOTA_ERROR_CODE = "22"


class QuotaGuard:
    """일일 예산·초당 레이트·쿼터오류 마킹을 통합 강제하는 가드.

    신뢰 축은 하드 예산 상한(DailyBudget)이다. 원장(UsageLedger)은 MCPortal
    경유 호출만 집계하는 베스트에포트 추정으로, 관측과 소프트 경고에만 쓴다.

    스레드 안전: 내부 마킹 상태(_warned·_exhausted_keys)와 예산 판정은 가드
    자체 Lock으로 보호한다. 다만 before_call(판정)→실제 상위 호출→after_call(기록)
    구간은 상위 호출이 가드 밖에서 일어나므로 완전 원자화되지 않는다(한 논리
    요청 내 최대 오버슈트 = 재시도 수). 하드 예산 상한은 그 한계 위에서 상한을
    보증하는 신뢰 축이다.

    Args:
        ledger: 사용량 원장.
        budget: 일일 예산 정책.
        bucket: 선택적 초당 레이트 리미터. None이면 레이트 제한 없음.
        sleep: 대기 함수(테스트 주입용). 기본 time.sleep.
    """

    def __init__(
        self,
        ledger: UsageLedger,
        budget: DailyBudget,
        bucket: TokenBucket | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._ledger = ledger
        self._budget = budget
        self._bucket = bucket
        self._sleep = sleep
        # 내부 마킹 상태(_warned·_exhausted_keys)와 예산 판정 직렬화용 락.
        self._lock = threading.Lock()
        # 같은 날 같은 키 중복 WARN 억제: {(day_kst, key_fp)}
        self._warned: set[tuple[str, str]] = set()
        # result_code 22로 소진 마킹된 키: {key_fp: day_kst}
        self._exhausted_keys: dict[str, str] = {}

    def before_call(
        self,
        key: str,
        endpoint: str,
        now: Optional[object] = None,
    ) -> None:
        """호출 직전 훅. 예산 초과면 QuotaExhausted, WARN이면 1회 경고, 필요 시 레이트 대기.

        ① 오늘 이 키가 쿼터초과(22)로 마킹됐으면 즉시 차단한다.
        ② 하드 예산 상한 도달 시 QuotaExhausted, 소프트 임계 초과 시 경고(중복 억제).
        ③ 레이트 버킷이 있으면 토큰 확보까지 대기 후 진행한다.
        """
        day = kst_day(now)  # type: ignore[arg-type]
        fp = key_fp(key)

        used = self._ledger.count_today(key, now=now)  # type: ignore[arg-type]

        should_warn = False
        with self._lock:
            # 당일이 아닌 소진 마킹을 정리(_exhausted_keys 무한 증가 방지).
            self._prune_exhausted_locked(day)

            # ① 쿼터오류(22)로 오늘 소진 마킹된 키는 즉시 차단.
            if self._exhausted_keys.get(fp) == day:
                raise QuotaExhausted(used, self._budget.limit)

            # ② 하드 예산 상한 평가.
            status = self._budget.status(used)
            if status is BudgetStatus.EXHAUSTED:
                raise QuotaExhausted(used, self._budget.limit)
            if status is BudgetStatus.WARN:
                warn_key = (day, fp)
                if warn_key not in self._warned:
                    self._warned.add(warn_key)
                    should_warn = True

        # 로깅은 락 밖에서(핸들러 지연이 락 구간을 늘리지 않게).
        if should_warn:
            _logger.warning(
                "일일 호출 예산 경고: %d/%d건 사용(소프트 임계 %.0f%% 초과). "
                "운영계정 전환을 검토하세요.",
                used,
                self._budget.limit,
                self._budget.soft_ratio * 100,
            )

        # ③ 초당 레이트 리미터: 토큰을 확보할 때까지 (대기→재시도) 반복.
        #    첫 try_acquire 후 다른 스레드가 리필 토큰을 채가면 두 번째 획득이
        #    실패할 수 있으므로, 성공할 때까지 진행하지 않는다.
        if self._bucket is not None:
            while not self._bucket.try_acquire():
                wait = self._bucket.wait_time()
                if wait > 0:
                    self._sleep(wait)

    def after_call(
        self,
        key: str,
        endpoint: str,
        result_code: Optional[str] = None,
        status: str = "ok",
        now: Optional[object] = None,
    ) -> None:
        """호출 직후 훅. 원장에 기록하고, result_code 22면 오늘 소진 상태로 마킹한다."""
        if result_code is not None and str(result_code) == _QUOTA_ERROR_CODE:
            status = "quota_error"
            with self._lock:
                self._exhausted_keys[key_fp(key)] = kst_day(now)  # type: ignore[arg-type]
        self._ledger.record(
            key,
            endpoint,
            status=status,
            result_code=result_code,
            now=now,  # type: ignore[arg-type]
        )

    def record_retry(
        self,
        key: str,
        endpoint: str,
        now: Optional[object] = None,
    ) -> None:
        """재시도로 소비된 물리 상위 호출 1건을 원장에 기록한다.

        지수 백오프 재시도는 논리 요청 1건당 여러 번의 실제 상위 호출을 만든다.
        after_call 은 최종 응답 1건만 기록하므로, 재시도로 나간 물리 호출은 이
        훅으로 별도 집계한다. 그래야 원장 기록 수가 실제 상위 호출 수와 일치하고,
        CALL_BUDGET 하드가드(count_today 기반)가 실제 소비를 하회하지 않는다.
        """
        self._ledger.record(
            key,
            endpoint,
            status="retry",
            result_code=None,
            now=now,  # type: ignore[arg-type]
        )

    def _prune_exhausted_locked(self, day: str) -> None:
        """당일(day)이 아닌 소진 마킹을 제거한다(락 보유 상태에서 호출).

        서로 다른 키·날짜의 소진 마킹이 누적돼 _exhausted_keys 가 무한정
        증가하는 것을 막는다.
        """
        stale = [fp for fp, marked in self._exhausted_keys.items() if marked != day]
        for fp in stale:
            del self._exhausted_keys[fp]
