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

#: 인증키 파라미터·헤더의 기본 이름(data.go.kr 규약).
#: 이름을 매개변수화하면서도 기존 호출부가 그대로 동작하도록 기본값을 상수로 둔다.
DEFAULT_KEY_PARAM = "serviceKey"


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


def inject_service_key(
    params: Mapping[str, Any] | None,
    key: str,
    *,
    param_name: str = DEFAULT_KEY_PARAM,
) -> dict[str, Any]:
    """``params`` 를 복사한 새 dict 에 인증키를 주입해 반환한다.

    원본 매핑은 변경하지 않는다. 값은 :func:`prepare_service_key` 로 정규화한
    *원문*을 넣으며, 사전 인코딩하지 않는다(HTTP 클라이언트가 정확히 한 번
    인코딩하도록 남겨 둔다).

    Args:
        params: 원본 질의 파라미터 매핑(``None`` 가능).
        key: data.go.kr 인증키(인코딩키·디코딩키 어느 쪽이든 된다).
        param_name: 인증키 파라미터 이름. 기본값은 :data:`DEFAULT_KEY_PARAM` 이라
            기존 호출부는 그대로 동작한다. 제공자마다 이름이 다르므로(``authKey``
            등) 하드코딩 대신 프로파일의 ``key_param`` 을 넘길 수 있게 열어 둔다.

    Returns:
        인증키가 주입된 새 dict.
    """
    result: dict[str, Any] = dict(params) if params else {}
    result[param_name] = prepare_service_key(key)
    return result


def inject_service_key_header(
    headers: Mapping[str, Any] | None,
    key: str,
    *,
    header_name: str = DEFAULT_KEY_PARAM,
) -> dict[str, Any]:
    """``headers`` 를 복사한 새 dict 에 인증키를 **헤더로** 주입해 반환한다(F-08).

    질의문자열 버전(:func:`inject_service_key`)과 값 규칙이 같다 —
    :func:`prepare_service_key` 로 정규화한 원문을 넣는다. 다만 이유는 반대다.
    질의문자열은 클라이언트가 뒤에서 한 번 인코딩하므로 원문을 남겨야 하고,
    헤더는 **아무도 인코딩하지 않으므로** 원문이 그대로 나가는 것이 정답이다.
    인코딩키를 헤더에 그대로 실으면 서버가 ``%2B`` 를 값의 일부로 읽는다.

    원본 매핑은 변경하지 않는다. 이미 같은 이름의 헤더가 있으면(대소문자 무시)
    **덮어쓰지 않는다** — 호출자가 명시적으로 실은 값이 우선이다.

    Args:
        headers: 원본 헤더 매핑(``None`` 가능).
        key: data.go.kr 인증키.
        header_name: 인증키 헤더 이름(기본 :data:`DEFAULT_KEY_PARAM`).

    Returns:
        인증키가 주입된 새 dict(원본 헤더 표기는 보존된다).
    """
    result: dict[str, Any] = dict(headers) if headers else {}
    lowered = header_name.lower()
    if any(str(name).lower() == lowered for name in result):
        return result
    result[header_name] = prepare_service_key(key)
    return result
