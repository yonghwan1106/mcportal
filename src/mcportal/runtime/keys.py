# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Yong Park
"""data.go.kr 인증키(serviceKey) 처리.

공공데이터포털은 발급 시 *인코딩키*와 *디코딩키* 두 가지를 준다.
인코딩키는 ``+`` ``/`` ``=`` 같은 특수문자가 이미 퍼센트 인코딩된 형태이고,
디코딩키는 원문 그대로다. httpx 같은 HTTP 클라이언트는 쿼리스트링을 스스로
한 번 인코딩하므로, 인코딩키를 그대로 넘기면 ``%2B`` → ``%252B`` 처럼
*이중 인코딩*되어 ``SERVICE_KEY_IS_NOT_REGISTERED_ERROR``(30)가 난다.

따라서 이 모듈은 넘어온 키가 이미 인코딩된 형태이면 딱 한 번만 디코딩하여
원문(디코딩키)으로 되돌린 뒤, HTTP 클라이언트가 정확히 한 번 인코딩하도록
원문 상태로 주입한다.
"""
from __future__ import annotations

import re
import urllib.parse
from collections.abc import Mapping
from typing import Any

# ``%`` 뒤에 16진수 두 자리가 오는, 최소 한 개의 퍼센트 인코딩 시퀀스.
_PERCENT_ENCODED = re.compile(r"%[0-9A-Fa-f]{2}")


def prepare_service_key(key: str) -> str:
    """인증키를 HTTP 클라이언트에 넘기기 좋은 원문(디코딩키) 형태로 정규화한다.

    - 앞뒤 공백을 제거한다.
    - 공백 제거 후 빈 문자열이면 :class:`ValueError` 를 던진다.
    - 퍼센트 인코딩 시퀀스(``%XX``)가 하나라도 있으면 *딱 한 번* 언쿼트한다.
      (인코딩키 → 디코딩키 변환. 이중 디코딩은 절대 하지 않는다.)
    - 그 외에는 그대로 반환한다.

    이미 디코딩된 키를 다시 넣어도 결과가 바뀌지 않으므로 멱등이다.
    """
    stripped = key.strip()
    if not stripped:
        raise ValueError("서비스키가 비어 있습니다. data.go.kr 발급 인증키를 확인하세요.")
    if _PERCENT_ENCODED.search(stripped):
        # unquote 는 한 겹만 벗긴다. 스펙상 두 번 디코딩하지 않는다.
        return urllib.parse.unquote(stripped)
    return stripped


def inject_service_key(params: Mapping[str, Any] | None, key: str) -> dict[str, Any]:
    """``params`` 를 복사한 새 dict 에 ``serviceKey`` 를 주입해 반환한다.

    원본 매핑은 변경하지 않는다. 값은 :func:`prepare_service_key` 로 정규화한
    *원문*을 넣으며, 사전 인코딩하지 않는다(HTTP 클라이언트가 정확히 한 번
    인코딩하도록 남겨 둔다).
    """
    result: dict[str, Any] = dict(params) if params else {}
    result["serviceKey"] = prepare_service_key(key)
    return result
