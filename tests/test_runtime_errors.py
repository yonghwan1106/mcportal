# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Yong Park
"""runtime.errors 테스트: 공통 오류코드 17종 매핑."""
from __future__ import annotations

import pytest

from mcportal.runtime.errors import (
    RESULT_CODES,
    ErrorInfo,
    is_key_problem,
    is_quota_exceeded,
    map_result_code,
)

# (code, expected 영문 상수명) 전수 목록.
_EXPECTED = {
    "00": "NORMAL_SERVICE",
    "01": "APPLICATION_ERROR",
    "02": "DB_ERROR",
    "03": "NODATA_ERROR",
    "04": "HTTP_ERROR",
    "05": "SERVICETIME_OUT",
    "10": "INVALID_REQUEST_PARAMETER_ERROR",
    "11": "NO_MANDATORY_REQUEST_PARAMETERS_ERROR",
    "12": "NO_OPENAPI_SERVICE_ERROR",
    "20": "SERVICE_ACCESS_DENIED_ERROR",
    "21": "TEMPORARILY_DISABLE_THE_SERVICEKEY_ERROR",
    "22": "LIMITED_NUMBER_OF_SERVICE_REQUESTS_EXCEEDS_ERROR",
    "30": "SERVICE_KEY_IS_NOT_REGISTERED_ERROR",
    "31": "DEADLINE_HAS_EXPIRED_ERROR",
    "32": "UNREGISTERED_IP_ERROR",
    "33": "UNSIGNED_CALL_ERROR",
    "99": "UNKNOWN_ERROR",
}


def test_result_codes_table_has_exactly_17() -> None:
    """오류코드 표는 정확히 17종이다."""
    assert len(RESULT_CODES) == 17
    assert set(RESULT_CODES) == set(_EXPECTED)


@pytest.mark.parametrize("code,name", list(_EXPECTED.items()))
def test_all_codes_map(code: str, name: str) -> None:
    """17종 전수: 코드가 올바른 ErrorInfo 로 매핑된다."""
    info = map_result_code(code)
    assert isinstance(info, ErrorInfo)
    assert info.code == code
    assert info.name == name
    assert info.message_ko  # 비어 있지 않음.
    assert info.hint_ko


def test_none_maps_to_none() -> None:
    """None 코드는 None 으로 매핑된다."""
    assert map_result_code(None) is None


def test_int_code_normalized() -> None:
    """정수 코드도 문자열로 정규화되어 매핑된다."""
    info = map_result_code(22)
    assert info is not None
    assert info.code == "22"
    assert info.name == "LIMITED_NUMBER_OF_SERVICE_REQUESTS_EXCEEDS_ERROR"


def test_unknown_code_preserved() -> None:
    """미지 코드는 UNKNOWN_ERROR 변형이되 원 코드를 보존한다."""
    info = map_result_code("77")
    assert info is not None
    assert info.name == "UNKNOWN_ERROR"
    assert info.code == "77"  # 원 코드 보존.
    # 메시지/힌트는 99번의 것을 물려받는다.
    assert info.message_ko == RESULT_CODES["99"][1]
    assert info.hint_ko == RESULT_CODES["99"][2]


def test_is_quota_exceeded() -> None:
    """22 만 쿼터 초과로 판정한다."""
    assert is_quota_exceeded("22") is True
    assert is_quota_exceeded(22) is True
    assert is_quota_exceeded("00") is False
    assert is_quota_exceeded("21") is False
    assert is_quota_exceeded(None) is False


@pytest.mark.parametrize("code", ["20", "21", "30", "31", "32", "33"])
def test_is_key_problem_true(code: str) -> None:
    """인증키 문제 코드 집합은 True."""
    assert is_key_problem(code) is True


@pytest.mark.parametrize("code", ["00", "22", "03", "99", None])
def test_is_key_problem_false(code: str | None) -> None:
    """키 문제가 아닌 코드는 False."""
    assert is_key_problem(code) is False


def test_errorinfo_is_frozen() -> None:
    """ErrorInfo 는 불변(frozen)이다."""
    info = map_result_code("00")
    assert info is not None
    with pytest.raises(Exception):
        info.code = "changed"  # type: ignore[misc]
