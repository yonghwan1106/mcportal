# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Yong Park
"""profiles 테스트: DATA_GO_KR 필드값, 멀티키 거부, 단일키 통과, 인증키 별칭(F10)."""

from __future__ import annotations

import dataclasses

import pytest

from mcportal.profiles import (
    DATA_GO_KR,
    MultiKeyUnsupportedError,
    ProviderProfile,
    validate_key_registration,
)

# profiles/__init__.py 는 통합자 소유이므로 원 모듈에서 직접 임포트한다.
from mcportal.profiles.datago import key_params_of


def test_data_go_kr_field_values() -> None:
    assert DATA_GO_KR.name == "data.go.kr"
    assert DATA_GO_KR.key_param == "serviceKey"
    assert DATA_GO_KR.host_suffixes == (
        "apis.data.go.kr",
        "api.odcloud.kr",
    )
    assert DATA_GO_KR.default_daily_budget == 10_000
    assert DATA_GO_KR.multi_key_supported is False
    # 안내문 취지 확인.
    assert "운영계정" in DATA_GO_KR.guidance_exhausted
    assert "CALL_BUDGET" in DATA_GO_KR.guidance_exhausted
    assert "운영계정" in DATA_GO_KR.refusal_multikey


def test_profile_is_frozen() -> None:
    assert dataclasses.is_dataclass(ProviderProfile)
    with pytest.raises(dataclasses.FrozenInstanceError):
        DATA_GO_KR.name = "mutated"  # type: ignore[misc]


def test_two_keys_refused_with_operational_account_guidance() -> None:
    with pytest.raises(MultiKeyUnsupportedError) as excinfo:
        validate_key_registration(DATA_GO_KR, ["KEY1", "KEY2"])
    assert "운영계정" in str(excinfo.value)


def test_single_key_passes() -> None:
    # 예외가 발생하지 않아야 한다.
    validate_key_registration(DATA_GO_KR, ["KEY1"])


def test_empty_keys_pass() -> None:
    # 0개 등록도 멀티키가 아니므로 통과.
    validate_key_registration(DATA_GO_KR, [])


def test_multikey_supported_profile_allows_many() -> None:
    permissive = ProviderProfile(
        name="permissive",
        key_param="apiKey",
        host_suffixes=("example.com",),
        default_daily_budget=100,
        multi_key_supported=True,
        guidance_exhausted="",
        refusal_multikey="",
    )
    # multi_key_supported=True 면 여러 키도 허용.
    validate_key_registration(permissive, ["A", "B", "C"])


# ---------------------------------------------------------------------------
# F10 — 인증키 파라미터 별칭
# ---------------------------------------------------------------------------
def test_key_param_aliases_default_is_empty_tuple() -> None:
    # 기본값이 있으므로 기존 생성자 호출(별칭 미지정)이 전부 호환된다.
    assert DATA_GO_KR.key_param_aliases == ()


def test_key_params_of_data_go_kr_is_single_canonical_name() -> None:
    # 현행 동작 보존: data.go.kr 는 serviceKey 하나만 본다.
    assert key_params_of(DATA_GO_KR) == ("serviceKey",)


def test_key_params_of_preserves_order_and_spelling() -> None:
    profile = ProviderProfile(
        name="가상포털",
        key_param="apiKey",
        host_suffixes=("apis.example.invalid",),
        default_daily_budget=100,
        multi_key_supported=False,
        guidance_exhausted="",
        refusal_multikey="",
        key_param_aliases=("api_key", "AuthKey"),
    )
    # (정본, *별칭) 순서와 프로파일에 적힌 표기를 그대로 보존한다.
    assert key_params_of(profile) == ("apiKey", "api_key", "AuthKey")


def test_key_params_of_dedupes_case_insensitively() -> None:
    profile = ProviderProfile(
        name="가상포털",
        key_param="apiKey",
        host_suffixes=("apis.example.invalid",),
        default_daily_budget=100,
        multi_key_supported=False,
        guidance_exhausted="",
        refusal_multikey="",
        # APIKEY 는 정본과 대소문자만 다르고, 빈 문자열은 무시된다.
        key_param_aliases=("APIKEY", "", "serviceKey", "ServiceKey"),
    )
    assert key_params_of(profile) == ("apiKey", "serviceKey")
