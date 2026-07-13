# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Yong Park
"""공공데이터포털 OpenAPI 공통 오류코드 매핑.

data.go.kr 계열 오픈API 는 HTTP 200 을 주면서 본문의 ``resultCode`` /
``returnReasonCode`` 로 실제 상태를 알린다. 이 모듈은 공통 오류코드 17종을
영문 상수명·한국어 메시지·대응 힌트로 매핑한다.
"""
from __future__ import annotations

from dataclasses import dataclass

# resultCode 문자열 → (영문 상수명, 한국어 메시지, 한국어 대응 힌트)
RESULT_CODES: dict[str, tuple[str, str, str]] = {
    "00": (
        "NORMAL_SERVICE",
        "정상",
        "정상 서비스입니다.",
    ),
    "01": (
        "APPLICATION_ERROR",
        "어플리케이션 에러",
        "제공기관 서비스 오류입니다. 잠시 후 재시도하세요.",
    ),
    "02": (
        "DB_ERROR",
        "데이터베이스 에러",
        "제공기관 DB 오류입니다. 잠시 후 재시도하세요.",
    ),
    "03": (
        "NODATA_ERROR",
        "데이터없음 에러",
        "조건에 맞는 데이터가 없습니다. 요청 파라미터를 확인하세요.",
    ),
    "04": (
        "HTTP_ERROR",
        "HTTP 에러",
        "전송 오류입니다. 재시도하세요.",
    ),
    "05": (
        "SERVICETIME_OUT",
        "서비스 연결실패 에러",
        "제공기관 응답이 지연됩니다. 백오프 후 재시도하세요.",
    ),
    "10": (
        "INVALID_REQUEST_PARAMETER_ERROR",
        "잘못된 요청 파라미터 에러",
        "파라미터 형식을 확인하세요.",
    ),
    "11": (
        "NO_MANDATORY_REQUEST_PARAMETERS_ERROR",
        "필수 요청 파라미터 누락",
        "필수 파라미터를 확인하세요.",
    ),
    "12": (
        "NO_OPENAPI_SERVICE_ERROR",
        "해당 오픈API 서비스가 없거나 폐기됨",
        "서비스 URL을 확인하세요.",
    ),
    "20": (
        "SERVICE_ACCESS_DENIED_ERROR",
        "서비스 접근 거부",
        "활용신청 승인 여부를 확인하세요.",
    ),
    "21": (
        "TEMPORARILY_DISABLE_THE_SERVICEKEY_ERROR",
        "일시적으로 사용할 수 없는 서비스키",
        "잠시 후 재시도하세요.",
    ),
    "22": (
        "LIMITED_NUMBER_OF_SERVICE_REQUESTS_EXCEEDS_ERROR",
        "서비스 요청 제한횟수 초과",
        "일일 쿼터가 소진되었습니다. 운영계정 전환을 안내하세요.",
    ),
    "30": (
        "SERVICE_KEY_IS_NOT_REGISTERED_ERROR",
        "등록되지 않은 서비스키",
        "디코딩키 사용 여부와 이중 인코딩 여부를 확인하세요.",
    ),
    "31": (
        "DEADLINE_HAS_EXPIRED_ERROR",
        "기한 만료된 서비스키",
        "활용기간 연장을 신청하세요.",
    ),
    "32": (
        "UNREGISTERED_IP_ERROR",
        "등록되지 않은 IP",
        "IP 등록 정보를 확인하세요.",
    ),
    "33": (
        "UNSIGNED_CALL_ERROR",
        "서명되지 않은 호출",
        "인증 방식을 확인하세요.",
    ),
    "99": (
        "UNKNOWN_ERROR",
        "기타 에러",
        "원인 불명입니다. 제공기관에 문의하세요.",
    ),
}

# 인증키 자체 문제로 분류되는 코드 집합(재시도해도 소용없고 발급/설정 확인 필요).
_KEY_PROBLEM_CODES = frozenset({"20", "21", "30", "31", "32", "33"})

# 일일 쿼터 소진 코드.
_QUOTA_CODE = "22"


@dataclass(frozen=True)
class ErrorInfo:
    """단일 오류코드에 대한 구조화 정보."""

    code: str
    name: str
    message_ko: str
    hint_ko: str


def _normalize_code(code: str | int | None) -> str | None:
    """코드 값을 비교 가능한 문자열로 정규화한다(None 은 그대로 None)."""
    if code is None:
        return None
    return str(code).strip()


def map_result_code(code: str | int | None) -> ErrorInfo | None:
    """오류코드를 :class:`ErrorInfo` 로 매핑한다.

    - ``None`` → ``None``
    - 알려진 코드 → 해당 :class:`ErrorInfo`
    - 미지의 코드 → ``UNKNOWN_ERROR`` 변형(메시지·힌트는 99번 것을 쓰되
      ``code`` 필드에는 원래 넘어온 코드를 보존한다)
    """
    normalized = _normalize_code(code)
    if normalized is None:
        return None
    entry = RESULT_CODES.get(normalized)
    if entry is not None:
        name, message_ko, hint_ko = entry
        return ErrorInfo(code=normalized, name=name, message_ko=message_ko, hint_ko=hint_ko)
    # 미지 코드: UNKNOWN_ERROR 변형에 원 코드 보존.
    _, unknown_msg, unknown_hint = RESULT_CODES["99"]
    return ErrorInfo(
        code=normalized,
        name="UNKNOWN_ERROR",
        message_ko=unknown_msg,
        hint_ko=unknown_hint,
    )


def is_quota_exceeded(code: str | int | None) -> bool:
    """코드가 일일 요청 제한 초과(22)인지 여부."""
    return _normalize_code(code) == _QUOTA_CODE


def is_key_problem(code: str | int | None) -> bool:
    """코드가 인증키 자체 문제(20/21/30/31/32/33)인지 여부."""
    return _normalize_code(code) in _KEY_PROBLEM_CODES
