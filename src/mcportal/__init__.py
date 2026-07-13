# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Yong Park
"""MCPortal 공개 API.

data.go.kr(공공데이터포털) 오픈API를 MCP 생태계로 잇는 쿼터 가드 런타임 계층.
전송 통합 트랜스포트와 각 서브패키지(quota/runtime/replay/profiles)의 공개 심볼을
한곳에서 재수출한다.
"""
from __future__ import annotations

from .profiles import (
    DATA_GO_KR,
    MultiKeyUnsupportedError,
    ProviderProfile,
    validate_key_registration,
)
from .quota import (
    DailyBudget,
    QuotaExhausted,
    QuotaGuard,
    TokenBucket,
    UsageLedger,
    compute_delay,
)
from .replay import (
    Cassette,
    CassetteMissError,
    RecordingTransport,
    ReplayTransport,
)
from .runtime import (
    NormalizedResponse,
    TTLCache,
    inject_service_key,
    map_result_code,
    normalize_response,
    prepare_service_key,
)
from .transport import MCPortalTransport, create_client

__version__ = "0.1.0.dev0"

__all__ = [
    "__version__",
    # 전송 통합 계층
    "MCPortalTransport",
    "create_client",
    # quota
    "QuotaGuard",
    "QuotaExhausted",
    "DailyBudget",
    "TokenBucket",
    "UsageLedger",
    "compute_delay",
    # runtime
    "prepare_service_key",
    "inject_service_key",
    "map_result_code",
    "normalize_response",
    "NormalizedResponse",
    "TTLCache",
    # replay
    "Cassette",
    "ReplayTransport",
    "RecordingTransport",
    "CassetteMissError",
    # profiles
    "ProviderProfile",
    "DATA_GO_KR",
    "MultiKeyUnsupportedError",
    "validate_key_registration",
]
