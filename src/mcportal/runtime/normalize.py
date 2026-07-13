# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Yong Park
"""data.go.kr 응답 정규화.

공공데이터포털 응답은 세 계열로 갈린다.

1. **표준형** — ``response.header.resultCode`` / ``resultMsg`` (JSON·XML 공통)
2. **게이트웨이 오류형** — ``OpenAPI_ServiceResponse.cmmMsgHeader.returnReasonCode``.
   주의: JSON(``_type=json``)을 요청해도 게이트웨이 단에서 나는 오류는 XML 로 온다.
   따라서 Content-Type 을 믿지 말고 본문 실제 형태로 파싱해야 한다.
3. **odcloud 형** — ``resultCode`` 가 아예 없다. ``data`` / ``currentCount`` 가 있으면
   성공, ``code`` + ``msg`` 만 있으면 오류.

또한 인코딩이 UTF-8 / CP949(EUC-KR) 로 뒤섞여 오므로 본문 디코딩도 여기서 흡수한다.
"""
from __future__ import annotations

import json
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from typing import Any

from .errors import ErrorInfo, map_result_code

# 성공으로 간주하는 resultCode 값(None 포함은 호출부에서 별도 처리).
_OK_CODES = frozenset({"00", "0"})


# ---------------------------------------------------------------------------
# 본문 디코딩
# ---------------------------------------------------------------------------
def _charset_from_content_type(content_type: str | None) -> str | None:
    """Content-Type 헤더에서 ``charset`` 파라미터를 추출한다(없으면 None)."""
    if not content_type:
        return None
    for part in content_type.split(";"):
        token = part.strip()
        if token.lower().startswith("charset="):
            value = token[len("charset="):].strip().strip('"').strip("'")
            return value or None
    return None


def decode_body(content: bytes, content_type: str | None) -> str:
    """응답 바이트를 텍스트로 디코딩한다.

    Content-Type 의 ``charset`` 파라미터가 있으면 우선 사용하고, 실패하거나
    없으면 ``utf-8`` (strict) → ``cp949`` → ``euc-kr`` 순으로 시도한 뒤,
    끝내 실패하면 ``utf-8`` 을 ``errors="replace"`` 로 강제 디코딩한다.
    """
    charset = _charset_from_content_type(content_type)
    if charset:
        try:
            return content.decode(charset)
        except (LookupError, UnicodeDecodeError):
            pass  # 잘못된/미지의 charset → 폴백 체인으로.
    for encoding in ("utf-8", "cp949", "euc-kr"):
        try:
            return content.decode(encoding)
        except (LookupError, UnicodeDecodeError):
            continue
    return content.decode("utf-8", errors="replace")


# ---------------------------------------------------------------------------
# XML → dict
# ---------------------------------------------------------------------------
def _strip_namespace(tag: str) -> str:
    """``{ns}tag`` 형태의 네임스페이스 접두를 제거한다."""
    return tag.split("}", 1)[1] if "}" in tag else tag


def _element_to_obj(element: ET.Element) -> Any:
    """엘리먼트를 재귀적으로 dict/str 로 변환한다.

    - 자식이 없는 엘리먼트 → 텍스트(strip). 텍스트가 없으면 빈 문자열.
    - 같은 태그가 반복되면 list 로 모은다.
    - 속성(attribute)은 무시한다.
    """
    children = list(element)
    if not children:
        text = element.text
        return text.strip() if text is not None else ""
    result: dict[str, Any] = {}
    for child in children:
        tag = _strip_namespace(child.tag)
        value = _element_to_obj(child)
        if tag in result:
            existing = result[tag]
            if isinstance(existing, list):
                existing.append(value)
            else:
                result[tag] = [existing, value]
        else:
            result[tag] = value
    return result


def xml_to_dict(text: str) -> dict[str, Any]:
    """XML 문자열을 dict 로 변환한다(루트 태그를 최상위 키로 감싼다).

    속성은 무시하며, 반복 태그는 list 로, 리프 엘리먼트는 텍스트로 접는다.
    """
    root = ET.fromstring(text)
    return {_strip_namespace(root.tag): _element_to_obj(root)}


def parse_payload(text: str) -> tuple[dict[str, Any], str]:
    """본문 문자열을 dict 로 파싱하고 형식 태그를 함께 돌려준다.

    JSON 파싱을 먼저 시도하고, 실패하면 XML 로 파싱한다. 게이트웨이 오류는
    JSON 을 요청해도 ``<`` 로 시작하는 XML 로 오므로, JSON 우선 시도가 자연히
    실패하고 XML 경로로 흡수된다. 반환 형식 문자열은 ``"json"`` 또는 ``"xml"``.
    """
    try:
        obj = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        obj = None
    else:
        if isinstance(obj, dict):
            return obj, "json"
        # dict 가 아닌 JSON(배열/스칼라)은 균일성을 위해 감싼다.
        return {"data": obj}, "json"
    return xml_to_dict(text), "xml"


# ---------------------------------------------------------------------------
# 정규화 결과
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class NormalizedResponse:
    """세 계열 응답을 하나로 정규화한 결과."""

    ok: bool
    result_code: str | None
    result_msg: str | None
    error: ErrorInfo | None
    data: dict[str, Any] = field(default_factory=dict)
    source_format: str = "json"


def _as_str(value: Any) -> str | None:
    """스칼라 값을 문자열로(또는 None) 변환한다."""
    if value is None:
        return None
    if isinstance(value, str):
        return value
    return str(value)


def _is_ok_code(code: str | None) -> bool:
    """resultCode 가 성공(None/00/0)인지 여부."""
    return code is None or code in _OK_CODES


def _build_normalized(data: dict[str, Any], source_format: str) -> NormalizedResponse:
    """파싱된 dict 를 계열별로 판별해 :class:`NormalizedResponse` 로 만든다."""
    # (2) 게이트웨이 오류형 — 키 존재로 판별.
    gateway = data.get("OpenAPI_ServiceResponse")
    if isinstance(gateway, dict):
        header = gateway.get("cmmMsgHeader")
        header = header if isinstance(header, dict) else {}
        code = _as_str(header.get("returnReasonCode"))
        msg = _as_str(header.get("returnAuthMsg")) or _as_str(header.get("errMsg"))
        ok = _is_ok_code(code)
        return NormalizedResponse(
            ok=ok,
            result_code=code,
            result_msg=msg,
            error=None if ok else map_result_code(code),
            data=data,
            source_format=source_format,
        )

    # (1) 표준형 — response.header.resultCode.
    response = data.get("response")
    if isinstance(response, dict) and isinstance(response.get("header"), dict):
        rheader = response["header"]
        code = _as_str(rheader.get("resultCode"))
        msg = _as_str(rheader.get("resultMsg"))
        ok = _is_ok_code(code)
        return NormalizedResponse(
            ok=ok,
            result_code=code,
            result_msg=msg,
            error=None if ok else map_result_code(code),
            data=data,
            source_format=source_format,
        )

    # (3) odcloud 형 — resultCode 없음.
    if "data" in data or "currentCount" in data:
        # 성공: result_code 없음.
        return NormalizedResponse(
            ok=True,
            result_code=None,
            result_msg=None,
            error=None,
            data=data,
            source_format=source_format,
        )
    if "code" in data and "msg" in data:
        code = _as_str(data.get("code"))
        msg = _as_str(data.get("msg"))
        ok = _is_ok_code(code)
        return NormalizedResponse(
            ok=ok,
            result_code=code,
            result_msg=msg,
            error=None if ok else map_result_code(code),
            data=data,
            source_format=source_format,
        )

    # 알 수 없는 구조 — result_code 없이 성공으로 취급하고 원본을 보존한다.
    return NormalizedResponse(
        ok=True,
        result_code=None,
        result_msg=None,
        error=None,
        data=data,
        source_format=source_format,
    )


def normalize_response(content: bytes, content_type: str | None = None) -> NormalizedResponse:
    """원본 응답 바이트를 정규화한다.

    본문을 디코딩한 뒤 JSON→XML 순으로 파싱하고, 표준형·게이트웨이 오류형·
    odcloud 형 세 계열을 통합 판별한다. Content-Type 은 charset 힌트로만 쓰고,
    구조 판별은 실제 본문 내용으로 한다(게이트웨이 오류가 JSON 요청에도 XML 로
    오는 문제를 흡수).
    """
    text = decode_body(content, content_type)
    data, source_format = parse_payload(text)
    return _build_normalized(data, source_format)
