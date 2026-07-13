# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Yong Park
"""시크릿 스크러빙(secret scrubbing) 유틸리티.

설계 원칙
---------
스크러빙은 **record 단계의 기본값이며 끌 수 없다.** 카세트에 기록되는 URL,
쿼리 파라미터, 응답 본문은 저장 시점에 무조건 스크러빙을 거친다. serviceKey
같은 자격증명이 카세트 파일로 새어 나가면 공개 리포·CI 로그를 통해 즉시
유출되므로, 스크러빙을 옵션으로 두지 않고 항상 강제한다. 이 모듈의 함수는
그 강제 게이트의 원자적 구성요소다.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from urllib.parse import quote, quote_plus

#: 스크러빙된 값이 치환되는 고정 플레이스홀더.
SCRUB_PLACEHOLDER = "__SCRUBBED__"

# 쿼리 문자열의 serviceKey 값(대소문자 무시)을 잡는다. 값은 & 또는 # 직전까지.
_SERVICE_KEY_URL_RE = re.compile(r"(serviceKey=)([^&#]*)", re.IGNORECASE)

# 파라미터 키가 serviceKey 계열인지 판정하는 정규화 이름.
_SERVICE_KEY_NAME = "servicekey"


def scrub_url(url: str) -> str:
    """URL 쿼리의 serviceKey 값을 플레이스홀더로 치환한다.

    ``serviceKey`` 키 이름은 대소문자를 무시하고 매칭하되, 원래 표기는
    보존한다(값 부분만 교체). 여러 번 등장하면 모두 치환한다.
    """
    return _SERVICE_KEY_URL_RE.sub(
        lambda m: f"{m.group(1)}{SCRUB_PLACEHOLDER}", url
    )


def scrub_params(params: Mapping[str, object]) -> dict[str, object]:
    """매핑에서 serviceKey 계열 키의 값을 플레이스홀더로 치환한 새 dict를 돌려준다.

    키 이름은 대소문자를 무시하고 판정하며, 원래 키 표기는 그대로 유지한다.
    serviceKey 이외의 항목은 값을 건드리지 않는다.
    """
    scrubbed: dict[str, object] = {}
    for key, value in params.items():
        if str(key).lower() == _SERVICE_KEY_NAME:
            scrubbed[key] = SCRUB_PLACEHOLDER
        else:
            scrubbed[key] = value
    return scrubbed


def scrub_text(text: str, secrets: Iterable[str]) -> str:
    """본문 텍스트에서 각 시크릿의 원문 및 URL 인코딩 변형을 모두 치환한다.

    각 시크릿에 대해 원문 그대로, ``quote`` 기본(safe="/"), ``quote_plus``,
    ``quote(safe="")`` 변형을 전부 후보로 만들어 치환한다. 빈 문자열 시크릿은
    무시한다. 부분 일치로 인한 누락을 막기 위해 긴 변형부터 먼저 치환한다.
    """
    if not text:
        return text

    variants: set[str] = set()
    for secret in secrets:
        if not secret:
            continue
        variants.add(secret)
        variants.add(quote(secret))
        variants.add(quote_plus(secret))
        variants.add(quote(secret, safe=""))

    result = text
    # 긴 후보부터 치환해야 짧은 후보가 긴 후보의 일부를 먼저 깨뜨리지 않는다.
    for variant in sorted(variants, key=len, reverse=True):
        if variant:
            result = result.replace(variant, SCRUB_PLACEHOLDER)
    return result
