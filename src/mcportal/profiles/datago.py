# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Yong Park
"""프로바이더 프로파일과 data.go.kr 정본 프로파일.

프로파일은 특정 공공데이터 제공자의 정책을 코드로 못박은 값 객체다. data.go.kr
프로파일은 (1) 일일 호출 예산 소진 시의 graceful stop 안내, (2) 멀티키 로테이션
거부를 기본 동작으로 강제한다. 멀티키 거부는 정책·약관 위반 소지와 계정 제재
위험을 코드 레벨에서 차단하는 안전 게이트다.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

#: 일일 호출 예산(CALL_BUDGET) 소진 시 라이브 호출을 멈추고 안내하는 문구.
EXHAUSTED_GUIDANCE = (
    "일일 호출 예산(CALL_BUDGET)에 도달하여 라이브 호출을 중단합니다. "
    "data.go.kr는 잔여 쿼터 조회 API를 제공하지 않으므로 이 집계는 MCPortal "
    "경유 호출 기준 베스트에포트 추정입니다. 한도 상향이 필요하면 운영계정 "
    "전환(공식 경로, 하루 최대 100,000건)을 이용하세요: data.go.kr 마이페이지 "
    "→ 활용신청 목록 → 운영계정 신청"
)

#: 동일 API에 대한 복수 키 로테이션 요청을 거부할 때의 안내 문구.
MULTIKEY_REFUSAL = (
    "data.go.kr는 1인 1키 구조이므로 동일 API에 대한 복수 키 로테이션은 타인 "
    "자격증명 풀링으로만 성립하며, 이용약관 제12조 제4항(1호·9호)·제14조 "
    "제5항에 저촉될 해석 여지가 있습니다. MCPortal은 data.go.kr 프로파일에서 "
    "멀티키를 지원하지 않습니다. 한도 상향은 운영계정 전환(공식 경로)을 "
    "이용하세요."
)


@dataclass(frozen=True)
class ProviderProfile:
    """공공데이터 제공자별 정책을 담은 불변 값 객체.

    Attributes:
        key_param: 인증키 파라미터의 정본 이름(요청 주입에 쓰인다).
        key_param_aliases: 같은 제공자가 병용하는 인증키 파라미터의 별칭들.
            스크러빙·카세트 매칭에서 정본과 동등하게 취급된다. 기본값이 빈
            튜플이므로 기존 생성자 호출은 전부 그대로 호환된다(F10).
    """

    name: str
    key_param: str
    host_suffixes: tuple[str, ...]
    default_daily_budget: int
    multi_key_supported: bool
    guidance_exhausted: str
    refusal_multikey: str
    key_param_aliases: tuple[str, ...] = ()


#: data.go.kr 정본 프로파일.
DATA_GO_KR = ProviderProfile(
    name="data.go.kr",
    key_param="serviceKey",
    host_suffixes=("apis.data.go.kr", "api.odcloud.kr"),
    default_daily_budget=10_000,
    multi_key_supported=False,
    guidance_exhausted=EXHAUSTED_GUIDANCE,
    refusal_multikey=MULTIKEY_REFUSAL,
)


def key_params_of(profile: ProviderProfile) -> tuple[str, ...]:
    """프로파일의 인증키 파라미터 이름들을 ``(정본, *별칭)`` 순서로 돌려준다.

    대소문자를 무시해 중복을 제거하되, 표기는 프로파일에 적힌 것을 보존한다.
    스크러빙(:mod:`mcportal.replay.scrub`)과 카세트 매칭이 "이 제공자에서
    인증키로 간주할 파라미터 이름"을 한곳에서 얻기 위한 진입점이다.

    Args:
        profile: 대상 프로바이더 프로파일.

    Returns:
        중복이 제거된 인증키 파라미터 이름 튜플. 빈 이름은 제외한다.
    """
    seen: set[str] = set()
    names: list[str] = []
    for raw in (profile.key_param, *profile.key_param_aliases):
        if not raw:
            continue
        lowered = raw.lower()
        if lowered in seen:
            continue
        seen.add(lowered)
        names.append(raw)
    return tuple(names)


class MultiKeyUnsupportedError(Exception):
    """멀티키 미지원 프로파일에 복수 키를 등록하려 할 때 발생한다."""


def validate_key_registration(
    profile: ProviderProfile, keys: Sequence[str]
) -> None:
    """프로파일 정책에 따라 등록 키 개수를 검증한다.

    멀티키를 지원하지 않는 프로파일에 2개 이상의 키를 등록하려 하면
    :class:`MultiKeyUnsupportedError` 를 던진다(운영계정 전환 안내 포함). 이것이
    graceful stop + 공식 경로 안내를 기본 동작으로 못박는 설계다.
    """
    if not profile.multi_key_supported and len(keys) > 1:
        raise MultiKeyUnsupportedError(profile.refusal_multikey)
