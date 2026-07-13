# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Yong Park
"""쿼터가드 예외와 한국어 안내문 상수.

data.go.kr는 잔여 쿼터를 조회하는 API를 제공하지 않으므로, 소진 안내는
MCPortal 경유 호출 집계(베스트에포트)에 기반한다. 신뢰 축은 하드 예산 상한이다.
"""
from __future__ import annotations

# 안내 문구는 프로바이더 프로파일(profiles.datago)에 단일 출처로 두고 재수출한다.
# 예외 메시지와 프로파일 안내가 서로 어긋나(drift) 사용자에게 다른 안내가 나가는
# 것을 막기 위함이다. QuotaExhausted 기본 guidance 로 EXHAUSTED_GUIDANCE 를 쓴다.
from ..profiles.datago import EXHAUSTED_GUIDANCE, MULTIKEY_REFUSAL

__all__ = ["EXHAUSTED_GUIDANCE", "MULTIKEY_REFUSAL", "QuotaExhausted"]


class QuotaExhausted(Exception):
    """일일 호출 예산이 소진되어 라이브 호출을 중단할 때 발생하는 예외.

    Attributes:
        used: 오늘(KST) 집계된 사용량.
        limit: 적용 중인 일일 예산 상한.
        guidance: 사용자에게 노출할 한국어 운영계정 전환 안내.
    """

    def __init__(
        self,
        used: int,
        limit: int,
        guidance: str = EXHAUSTED_GUIDANCE,
    ) -> None:
        self.used = used
        self.limit = limit
        self.guidance = guidance
        message = f"일일 호출 예산 소진: {used}/{limit}건. {guidance}"
        super().__init__(message)
