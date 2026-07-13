# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Yong Park
"""profiles 테스트: DATA_GO_KR 필드값, 멀티키 거부, 단일키 통과."""

from __future__ import annotations

import dataclasses

import pytest

from mcportal.profiles import (
    DATA_GO_KR,
    MultiKeyUnsupportedError,
    ProviderProfile,
    validate_key_registration,
)


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
