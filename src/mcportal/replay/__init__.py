# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Yong Park
"""record/replay 서브패키지 공개 API.

카세트 녹화·재생 골격과 시크릿 스크러빙을 재수출한다. 스크러빙은 record 단계의
기본값이며 끌 수 없다(설계상 강제 게이트).
"""

from __future__ import annotations

from .cassette import (
    Cassette,
    CassetteMissError,
    RecordingTransport,
    ReplayTransport,
)
from .scrub import (
    SCRUB_PLACEHOLDER,
    scrub_params,
    scrub_text,
    scrub_url,
)

__all__ = [
    "SCRUB_PLACEHOLDER",
    "scrub_url",
    "scrub_params",
    "scrub_text",
    "Cassette",
    "CassetteMissError",
    "ReplayTransport",
    "RecordingTransport",
]
