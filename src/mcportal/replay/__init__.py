# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Yong Park
"""record/replay 서브패키지 공개 API.

카세트 녹화·재생 골격과 시크릿 스크러빙을 재수출한다. 스크러빙은 record 단계의
기본값이며 끌 수 없다(설계상 강제 게이트).

인증키 파라미터 이름은 매개변수화되어 있다. 이름을 넘기지 않으면
:data:`DEFAULT_KEY_PARAMS`(serviceKey 계열)가 적용되어 W1과 동일하게 동작한다.
**빈 목록을 넘겨도 마찬가지로 기본값이 적용된다** — 매개변수화의 목적은 별칭을
더하는 것이지 스크러빙을 끄는 것이 아니므로, 어떤 인자 조합으로도 "스크러빙 없는
카세트"를 만들 수 없다.

record와 replay가 서로 다른 이름 목록을 쓰면 매칭 키가 갈라져 카세트가 통째로
미스나므로, 카세트에 저장된 값을 우선한다.
"""

from __future__ import annotations

from .cassette import (
    Cassette,
    CassetteMissError,
    RecordingTransport,
    ReplayTransport,
)
from .scrub import (
    DEFAULT_KEY_PARAMS,
    SCRUB_PLACEHOLDER,
    scrub_params,
    scrub_text,
    scrub_url,
)

__all__ = [
    "SCRUB_PLACEHOLDER",
    "DEFAULT_KEY_PARAMS",
    "scrub_url",
    "scrub_params",
    "scrub_text",
    "Cassette",
    "CassetteMissError",
    "ReplayTransport",
    "RecordingTransport",
]
