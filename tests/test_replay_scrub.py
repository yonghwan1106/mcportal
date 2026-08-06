# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Yong Park
"""scrub 모듈 테스트: url/params/text 치환, 인코딩 변형 제거, 키 이름 매개변수화(F10)."""

from __future__ import annotations

from urllib.parse import quote, quote_plus

from mcportal.replay import (
    SCRUB_PLACEHOLDER,
    scrub_params,
    scrub_text,
    scrub_url,
)

# replay/__init__.py 는 통합자 소유이므로 원 모듈에서 직접 임포트한다.
from mcportal.replay.scrub import DEFAULT_KEY_PARAMS

# '+', '/', '=' 를 포함해 인코딩 변형이 서로 달라지는 가짜 키.
FAKE_KEY = "ab12+CD/34=="


def test_scrub_url_replaces_service_key_value() -> None:
    url = "https://apis.data.go.kr/svc/list?serviceKey=SECRET123&pageNo=1"
    out = scrub_url(url)
    assert "SECRET123" not in out
    assert f"serviceKey={SCRUB_PLACEHOLDER}" in out
    # 다른 파라미터는 보존.
    assert "pageNo=1" in out


def test_scrub_url_case_insensitive_key_name() -> None:
    url = "https://apis.data.go.kr/svc?SERVICEKEY=SECRET&x=1"
    out = scrub_url(url)
    assert "SECRET" not in out
    assert SCRUB_PLACEHOLDER in out
    # 원래 키 표기(SERVICEKEY)는 보존한다.
    assert "SERVICEKEY=" in out


def test_scrub_url_multiple_occurrences() -> None:
    url = "https://h/a?serviceKey=K1&b=2&serviceKey=K2"
    out = scrub_url(url)
    assert "K1" not in out
    assert "K2" not in out
    assert out.count(SCRUB_PLACEHOLDER) == 2


def test_scrub_params_replaces_service_key_family() -> None:
    params = {"serviceKey": "SECRET", "pageNo": "1", "numOfRows": "10"}
    out = scrub_params(params)
    assert out["serviceKey"] == SCRUB_PLACEHOLDER
    assert out["pageNo"] == "1"
    assert out["numOfRows"] == "10"
    # 원본 매핑은 변형하지 않는다.
    assert params["serviceKey"] == "SECRET"


def test_scrub_params_case_insensitive() -> None:
    out = scrub_params({"ServiceKey": "SECRET", "keep": "v"})
    assert out["ServiceKey"] == SCRUB_PLACEHOLDER
    assert out["keep"] == "v"


def test_scrub_text_removes_raw_and_all_encoding_variants() -> None:
    variants = {
        "raw": FAKE_KEY,
        "quote": quote(FAKE_KEY),
        "quote_safe": quote(FAKE_KEY, safe=""),
        "quote_plus": quote_plus(FAKE_KEY),
    }
    # %2B(=+)를 포함한 quote/quote_plus 변형이 실제로 다른지 사전 확인.
    assert "%2B" in variants["quote"]
    assert variants["quote"] != variants["quote_plus"]

    text = (
        "prefix "
        + variants["raw"]
        + " mid "
        + variants["quote"]
        + " mid2 "
        + variants["quote_safe"]
        + " mid3 "
        + variants["quote_plus"]
        + " suffix"
    )
    out = scrub_text(text, [FAKE_KEY])
    for label, v in variants.items():
        assert v not in out, f"{label} 변형이 남았다: {v}"
    assert "prefix" in out and "suffix" in out
    assert SCRUB_PLACEHOLDER in out


def test_scrub_text_ignores_empty_secret() -> None:
    text = "hello world"
    # 빈 문자열 시크릿은 무시되어 텍스트가 그대로 유지되어야 한다.
    assert scrub_text(text, ["", None or ""]) == text
    assert SCRUB_PLACEHOLDER not in scrub_text(text, [""])


def test_scrub_text_multiple_secrets() -> None:
    text = "key1=AAA key2=BBB"
    out = scrub_text(text, ["AAA", "BBB"])
    assert "AAA" not in out
    assert "BBB" not in out
    assert out.count(SCRUB_PLACEHOLDER) == 2


def test_scrub_text_empty_text_returns_empty() -> None:
    assert scrub_text("", [FAKE_KEY]) == ""


# ---------------------------------------------------------------------------
# F10 — 키 이름 매개변수화
# ---------------------------------------------------------------------------
def test_default_key_params_is_service_key_family() -> None:
    # 기본값이 W1 동작과 같아야 기존 호출부·기존 카세트가 그대로 호환된다.
    assert DEFAULT_KEY_PARAMS == ("serviceKey",)


def test_scrub_url_with_custom_key_param() -> None:
    url = "https://apis.example.invalid/svc?apiKey=SECRET&pageNo=1"
    out = scrub_url(url, ("apiKey",))
    assert "SECRET" not in out
    assert f"apiKey={SCRUB_PLACEHOLDER}" in out
    assert "pageNo=1" in out
    # 기본 키 이름으로는 apiKey 가 잡히지 않는다(매개변수화가 실제로 작동한다).
    assert "SECRET" in scrub_url(url)


def test_scrub_url_with_multiple_custom_key_params() -> None:
    url = "https://apis.example.invalid/svc?apiKey=K1&authKey=K2&pageNo=1"
    out = scrub_url(url, ("apiKey", "authKey"))
    assert "K1" not in out
    assert "K2" not in out
    assert out.count(SCRUB_PLACEHOLDER) == 2


def test_scrub_url_does_not_match_key_name_suffix() -> None:
    # myServiceKey 는 serviceKey 의 접미사 일치일 뿐이므로 오탐되면 안 된다.
    url = "https://apis.example.invalid/svc?myServiceKey=KEEPME&pageNo=1"
    out = scrub_url(url)
    assert "myServiceKey=KEEPME" in out
    assert SCRUB_PLACEHOLDER not in out


def test_scrub_url_escapes_regex_metacharacters_in_key_name() -> None:
    # 키 이름에 정규식 특수문자가 있어도 리터럴로 취급된다.
    matched = scrub_url("https://h/x?a.b=SECRET&c=1", ("a.b",))
    assert "SECRET" not in matched
    assert f"a.b={SCRUB_PLACEHOLDER}" in matched
    # '.' 가 임의 문자로 해석되면 axb 도 잡히므로, 이스케이프 확인의 반대 방향 표본.
    untouched = scrub_url("https://h/x?axb=KEEPME&c=1", ("a.b",))
    assert "axb=KEEPME" in untouched
    assert SCRUB_PLACEHOLDER not in untouched


def test_empty_key_params_falls_back_to_default_instead_of_disabling() -> None:
    """빈 키 이름 목록은 스크러빙 off-switch가 아니라 기본값 폴백이다.

    F10 매개변수화의 목적은 *별칭을 더하는 것*이지 스크러빙을 끄는 것이 아니다.
    빈 목록을 무동작으로 두면 모듈 docstring·README 가 선언한 "끌 수 없다"가
    거짓이 되고, 공개 API(``Cassette``·``RecordingTransport``)를 직접 쓰는
    경로에서 실키가 카세트 파일에 평문으로 남는다.
    """
    url = "https://apis.example.invalid/svc?serviceKey=SECRET&pageNo=1"
    assert scrub_url(url, ()) != url
    assert "SECRET" not in scrub_url(url, ())
    assert f"serviceKey={SCRUB_PLACEHOLDER}" in scrub_url(url, ())
    assert scrub_url(url, ()) == scrub_url(url)  # 기본값 호출과 동일.
    # 매핑 쪽도 같은 폴백을 따른다.
    assert scrub_params({"serviceKey": "SECRET"}, ())["serviceKey"] == SCRUB_PLACEHOLDER
    # 이름만 담긴 빈 문자열도 "이름 없음"으로 보고 폴백한다.
    assert "SECRET" not in scrub_url(url, ("", ""))


def test_scrub_params_with_custom_key_params() -> None:
    params = {"apiKey": "SECRET", "AUTH_KEY": "SECRET2", "pageNo": "1"}
    out = scrub_params(params, ("apiKey", "auth_key"))
    assert out["apiKey"] == SCRUB_PLACEHOLDER
    # 대소문자 무시 판정이고, 원래 키 표기는 유지된다.
    assert out["AUTH_KEY"] == SCRUB_PLACEHOLDER
    assert out["pageNo"] == "1"
    # 원본 매핑은 변형하지 않는다.
    assert params["apiKey"] == "SECRET"
