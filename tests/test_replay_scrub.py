# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Yong Park
"""scrub 모듈 테스트: url/params/text 치환 및 인코딩 변형 전부 제거."""

from __future__ import annotations

from urllib.parse import quote, quote_plus

from mcportal.replay import (
    SCRUB_PLACEHOLDER,
    scrub_params,
    scrub_text,
    scrub_url,
)

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
