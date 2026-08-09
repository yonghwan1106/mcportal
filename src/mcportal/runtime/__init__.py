# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Yong Park
"""MCPortal 런타임 공통 레이어.

data.go.kr 응답의 XML/EUC-KR/오류코드 비일관성을 흡수하는 계층이다. 인증키
이중 인코딩 방지, 공통 오류코드 매핑, 응답 정규화(3계열), TTL 캐시를 제공한다.
"""
from __future__ import annotations

from .cache import TTLCache
from .errors import (
    ErrorInfo,
    is_key_problem,
    is_quota_exceeded,
    map_result_code,
)
from .keys import (
    DEFAULT_KEY_PARAM,
    inject_service_key,
    inject_service_key_header,
    prepare_service_key,
)
from .normalize import NormalizedResponse, decode_body, normalize_response

__all__ = [
    "DEFAULT_KEY_PARAM",
    "prepare_service_key",
    "inject_service_key",
    "inject_service_key_header",
    "ErrorInfo",
    "map_result_code",
    "is_quota_exceeded",
    "is_key_problem",
    "NormalizedResponse",
    "normalize_response",
    "decode_body",
    "TTLCache",
]
