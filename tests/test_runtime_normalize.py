# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Yong Park
"""runtime.normalize 테스트: 응답 디코딩·파싱·3계열 정규화."""
from __future__ import annotations

import json

from mcportal.runtime.normalize import (
    decode_body,
    normalize_response,
    parse_payload,
    xml_to_dict,
)

# ---------------------------------------------------------------------------
# 합성 픽스처
# ---------------------------------------------------------------------------
# 표준형 XML(EUC-KR/CP949 인코딩 대상). 한국어 값 포함.
_STANDARD_XML = (
    "<response>"
    "<header><resultCode>00</resultCode><resultMsg>정상 처리되었습니다</resultMsg></header>"
    "<body><items>"
    "<item><stnNm>세종특별자치시</stnNm><ta>25.3</ta></item>"
    "<item><stnNm>서울특별시</stnNm><ta>27.1</ta></item>"
    "</items></body>"
    "</response>"
)

# 게이트웨이 오류 XML(쿼터 초과 22). JSON 을 요청해도 이런 XML 로 온다.
_GATEWAY_XML = (
    "<OpenAPI_ServiceResponse>"
    "<cmmMsgHeader>"
    "<errMsg>SERVICE ERROR</errMsg>"
    "<returnAuthMsg>LIMITED_NUMBER_OF_SERVICE_REQUESTS_EXCEEDS_ERROR</returnAuthMsg>"
    "<returnReasonCode>22</returnReasonCode>"
    "</cmmMsgHeader>"
    "</OpenAPI_ServiceResponse>"
)


# ---------------------------------------------------------------------------
# decode_body
# ---------------------------------------------------------------------------
def test_decode_body_euckr_roundtrip_no_charset() -> None:
    """CP949 로 인코딩한 한국어 XML 을 charset 없이도 왕복 복원한다."""
    raw = _STANDARD_XML.encode("cp949")
    text = decode_body(raw, content_type="application/xml")
    assert text == _STANDARD_XML
    assert "세종특별자치시" in text


def test_decode_body_explicit_euckr_charset() -> None:
    """Content-Type 의 charset=EUC-KR 을 우선 사용한다."""
    raw = "한국어값".encode("euc-kr")
    text = decode_body(raw, content_type="text/xml; charset=EUC-KR")
    assert text == "한국어값"


def test_decode_body_utf8_default() -> None:
    """charset 이 없고 UTF-8 이면 UTF-8 로 디코딩한다."""
    raw = "정상 UTF-8".encode("utf-8")
    assert decode_body(raw, content_type=None) == "정상 UTF-8"


def test_decode_body_bad_charset_falls_back() -> None:
    """미지의 charset 이면 폴백 체인으로 넘어간다."""
    raw = "폴백".encode("utf-8")
    assert decode_body(raw, content_type="text/xml; charset=nonsense-enc") == "폴백"


# ---------------------------------------------------------------------------
# xml_to_dict / parse_payload
# ---------------------------------------------------------------------------
def test_xml_to_dict_repeated_tags_become_list() -> None:
    """반복 태그는 list 로 접힌다."""
    parsed = xml_to_dict(_STANDARD_XML)
    items = parsed["response"]["body"]["items"]["item"]
    assert isinstance(items, list)
    assert len(items) == 2
    assert items[0]["stnNm"] == "세종특별자치시"
    assert items[1]["ta"] == "27.1"


def test_xml_to_dict_single_child_is_not_list() -> None:
    """자식이 하나면 list 가 아니라 값 그대로다."""
    parsed = xml_to_dict("<root><only>x</only></root>")
    assert parsed["root"]["only"] == "x"


def test_xml_attributes_ignored() -> None:
    """속성은 무시된다."""
    parsed = xml_to_dict('<root><a attr="ignored">v</a></root>')
    assert parsed["root"]["a"] == "v"


def test_parse_payload_json_and_xml() -> None:
    """parse_payload 는 JSON/XML 형식을 올바르게 태깅한다."""
    data_json, fmt_json = parse_payload('{"a": 1}')
    assert fmt_json == "json"
    assert data_json == {"a": 1}
    data_xml, fmt_xml = parse_payload("<root><a>1</a></root>")
    assert fmt_xml == "xml"
    assert data_xml == {"root": {"a": "1"}}


# ---------------------------------------------------------------------------
# normalize_response — 표준형
# ---------------------------------------------------------------------------
def test_normalize_standard_json_success() -> None:
    """표준형 JSON 성공(resultCode=00)."""
    body = {
        "response": {
            "header": {"resultCode": "00", "resultMsg": "NORMAL SERVICE."},
            "body": {"totalCount": 0, "items": ""},
        }
    }
    result = normalize_response(json.dumps(body).encode("utf-8"), "application/json")
    assert result.ok is True
    assert result.result_code == "00"
    assert result.result_msg == "NORMAL SERVICE."
    assert result.error is None
    assert result.source_format == "json"


def test_normalize_standard_json_nodata_error() -> None:
    """표준형 JSON 오류(resultCode=03 NODATA)."""
    body = {"response": {"header": {"resultCode": "03", "resultMsg": "NODATA_ERROR"}}}
    result = normalize_response(json.dumps(body).encode("utf-8"), "application/json")
    assert result.ok is False
    assert result.result_code == "03"
    assert result.error is not None
    assert result.error.name == "NODATA_ERROR"


def test_normalize_standard_xml_success_euckr() -> None:
    """표준형 XML(CP949 인코딩) 에서 resultCode 를 추출한다."""
    result = normalize_response(_STANDARD_XML.encode("cp949"), "application/xml")
    assert result.ok is True
    assert result.result_code == "00"
    assert result.result_msg == "정상 처리되었습니다"
    assert result.source_format == "xml"
    # 반복 태그가 list 로 보존됨.
    items = result.data["response"]["body"]["items"]["item"]
    assert isinstance(items, list) and len(items) == 2


# ---------------------------------------------------------------------------
# normalize_response — 게이트웨이 오류형
# ---------------------------------------------------------------------------
def test_normalize_gateway_error_xml_despite_json_content_type() -> None:
    """게이트웨이 오류 XML 을 content_type=application/json 으로 줘도 22 를 추출한다."""
    result = normalize_response(_GATEWAY_XML.encode("utf-8"), content_type="application/json")
    assert result.ok is False
    assert result.result_code == "22"
    assert result.source_format == "xml"  # Content-Type 과 무관하게 실제 XML.
    assert result.error is not None
    assert result.error.name == "LIMITED_NUMBER_OF_SERVICE_REQUESTS_EXCEEDS_ERROR"
    assert result.result_msg == "LIMITED_NUMBER_OF_SERVICE_REQUESTS_EXCEEDS_ERROR"


# ---------------------------------------------------------------------------
# normalize_response — odcloud 형
# ---------------------------------------------------------------------------
def test_normalize_odcloud_success() -> None:
    """odcloud 성공: resultCode 없음, data/currentCount 존재 → ok."""
    body = {
        "page": 1,
        "perPage": 10,
        "totalCount": 2,
        "currentCount": 2,
        "matchCount": 2,
        "data": [{"지역": "세종"}, {"지역": "서울"}],
    }
    result = normalize_response(json.dumps(body).encode("utf-8"), "application/json")
    assert result.ok is True
    assert result.result_code is None
    assert result.error is None
    assert result.data["currentCount"] == 2


def test_normalize_odcloud_error() -> None:
    """odcloud 오류: code+msg 만 존재 → ok=False, 코드 보존."""
    body = {"code": "400", "msg": "해당 서비스를 찾을 수 없습니다"}
    result = normalize_response(json.dumps(body).encode("utf-8"), "application/json")
    assert result.ok is False
    assert result.result_code == "400"
    assert result.error is not None
    assert result.error.name == "UNKNOWN_ERROR"  # 표에 없는 코드 → UNKNOWN 변형.
    assert result.error.code == "400"


def test_normalize_result_code_zero_is_ok() -> None:
    """resultCode 가 '0' 이어도 성공으로 본다."""
    body = {"response": {"header": {"resultCode": "0", "resultMsg": "OK"}}}
    result = normalize_response(json.dumps(body).encode("utf-8"), "application/json")
    assert result.ok is True
    assert result.error is None
