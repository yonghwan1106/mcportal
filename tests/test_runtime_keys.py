# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Yong Park
"""runtime.keys 테스트: 인증키 이중 인코딩 방지."""
from __future__ import annotations

import pytest

from mcportal.runtime.keys import inject_service_key, prepare_service_key


def test_encoded_key_decoded_once() -> None:
    """퍼센트 인코딩된 인증키는 딱 한 번 디코딩되어 원문으로 돌아온다."""
    # "abcAB+/=" 를 인코딩한 형태(합성). %2B=+, %2F=/, %3D==
    encoded = "abcAB%2B%2F%3Dxyz"
    assert prepare_service_key(encoded) == "abcAB+/=xyz"


def test_decoded_key_unchanged() -> None:
    """이미 디코딩된(특수문자 원문) 인증키는 변경되지 않는다."""
    decoded = "abcAB+/=xyz"
    assert prepare_service_key(decoded) == decoded


def test_prepare_is_idempotent() -> None:
    """재적용해도 결과가 불변(멱등)이다."""
    encoded = "key%2Bwith%3Dsymbols"
    once = prepare_service_key(encoded)
    twice = prepare_service_key(once)
    assert once == "key+with=symbols"
    assert once == twice


def test_no_double_decode() -> None:
    """이중 인코딩된 값이라도 한 겹만 벗긴다(2회 디코딩 금지)."""
    # %252B 는 "%2B" 를 다시 인코딩한 것. 한 번만 디코딩하면 "%2B" 가 남아야 한다.
    assert prepare_service_key("a%252Bb") == "a%2Bb"


def test_whitespace_is_stripped() -> None:
    """앞뒤 공백은 제거된다."""
    assert prepare_service_key("  plainkey  ") == "plainkey"


@pytest.mark.parametrize("bad", ["", "   ", "\n\t"])
def test_empty_key_raises_valueerror(bad: str) -> None:
    """빈(또는 공백뿐인) 키는 한국어 ValueError 를 던진다."""
    with pytest.raises(ValueError) as excinfo:
        prepare_service_key(bad)
    assert "서비스키" in str(excinfo.value)


def test_inject_adds_prepared_key_without_mutating_source() -> None:
    """inject_service_key 는 새 dict 를 만들고 원본을 건드리지 않는다."""
    original = {"pageNo": "1", "numOfRows": "10"}
    result = inject_service_key(original, "abc%2B123")
    assert result["serviceKey"] == "abc+123"
    assert result["pageNo"] == "1"
    assert result["numOfRows"] == "10"
    # 원본 불변 + serviceKey 미주입.
    assert "serviceKey" not in original


def test_inject_with_none_params() -> None:
    """params 가 None 이어도 serviceKey 만 담은 dict 를 반환한다."""
    result = inject_service_key(None, "plainkey")
    assert result == {"serviceKey": "plainkey"}
