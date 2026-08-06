# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Yong Park
"""cassette 테스트: 녹화 스크러빙(파일 스캔) → 무키 재생 → 미스 에러 → 키 이름 전파(F10)."""

from __future__ import annotations

import json
from pathlib import Path
from urllib.parse import quote, quote_plus

import httpx
import pytest

from mcportal.replay import (
    SCRUB_PLACEHOLDER,
    Cassette,
    CassetteMissError,
    RecordingTransport,
    ReplayTransport,
)

# replay/__init__.py 는 통합자 소유이므로 원 모듈에서 직접 임포트한다.
from mcportal.replay.scrub import DEFAULT_KEY_PARAMS

# '+', '/', '=' 를 포함해 인코딩 변형이 서로 달라지는 가짜 키.
FAKE_KEY = "ab12+CD/34=="
BASE = "https://apis.data.go.kr/svc/list"
# 응답 본문이 요청 키를 되비추는(echo) 상황까지 스크러빙되는지 확인.
BODY_XML = (
    "<response><header><resultCode>00</resultCode></header>"
    f"<echoedKey>{FAKE_KEY}</echoedKey>"
    "<body><item><name>세종</name></item></body></response>"
)


def _mock_handler(request: httpx.Request) -> httpx.Response:
    # 실제 네트워크 대신 합성 응답을 돌려주는 inner 트랜스포트 핸들러.
    return httpx.Response(
        200,
        headers={"content-type": "application/xml; charset=utf-8"},
        text=BODY_XML,
    )


def test_record_scrubs_key_then_replay_without_key(tmp_path: Path) -> None:
    inner = httpx.MockTransport(_mock_handler)
    cassette = Cassette()
    recording = RecordingTransport(inner, cassette, secrets=[FAKE_KEY])

    # 1) 실키 포함 요청을 녹화.
    with httpx.Client(transport=recording) as client:
        resp = client.get(
            BASE, params={"serviceKey": FAKE_KEY, "pageNo": "1"}
        )
    assert resp.status_code == 200
    # 하류 소비자는 원본(비스크러빙) 라이브 응답을 그대로 받는다.
    assert FAKE_KEY in resp.text

    # 2) 저장 후 파일 전문에서 키 원문/인코딩 변형이 전부 사라졌는지 스캔.
    path = tmp_path / "cassette.json"
    cassette.save(path)
    raw = path.read_text(encoding="utf-8")
    for variant in (
        FAKE_KEY,
        quote(FAKE_KEY),
        quote(FAKE_KEY, safe=""),
        quote_plus(FAKE_KEY),
    ):
        assert variant not in raw, f"카세트 파일에 키 변형이 남았다: {variant}"
    assert SCRUB_PLACEHOLDER in raw

    # 3) 같은 카세트를 열어 serviceKey 없이 동일 응답 재생.
    loaded = Cassette.load(path)
    replay = ReplayTransport(loaded)
    with httpx.Client(transport=replay) as client:
        replayed = client.get(BASE, params={"pageNo": "1"})
    assert replayed.status_code == 200
    # 스크러빙된 본문(플레이스홀더 포함)이 그대로 재생된다.
    assert SCRUB_PLACEHOLDER in replayed.text
    assert "<resultCode>00</resultCode>" in replayed.text
    assert FAKE_KEY not in replayed.text


def test_replay_real_key_matches_no_key_interaction(tmp_path: Path) -> None:
    # 실키 요청과 무키 요청이 동일 인터랙션에 매칭되어야 한다.
    inner = httpx.MockTransport(_mock_handler)
    cassette = Cassette()
    recording = RecordingTransport(inner, cassette, secrets=[FAKE_KEY])
    with httpx.Client(transport=recording) as client:
        client.get(BASE, params={"serviceKey": FAKE_KEY, "pageNo": "1"})

    replay = ReplayTransport(cassette)
    # 실키를 포함한 재생 요청도 같은 인터랙션에 매칭.
    with httpx.Client(transport=replay) as client:
        with_key = client.get(
            BASE, params={"serviceKey": "OTHER_KEY", "pageNo": "1"}
        )
        without_key = client.get(BASE, params={"pageNo": "1"})
    assert with_key.status_code == 200
    assert without_key.status_code == 200
    assert with_key.text == without_key.text


def test_cassette_miss_raises(tmp_path: Path) -> None:
    inner = httpx.MockTransport(_mock_handler)
    cassette = Cassette()
    recording = RecordingTransport(inner, cassette, secrets=[FAKE_KEY])
    with httpx.Client(transport=recording) as client:
        client.get(BASE, params={"serviceKey": FAKE_KEY, "pageNo": "1"})

    replay = ReplayTransport(cassette)
    # 기록되지 않은 경로 → CassetteMissError.
    request = httpx.Request(
        "GET", "https://apis.data.go.kr/svc/other?serviceKey=" + FAKE_KEY
    )
    with pytest.raises(CassetteMissError) as excinfo:
        replay.handle_request(request)
    msg = str(excinfo.value)
    assert "카세트에 기록이 없습니다" in msg
    # 미스 에러 메시지에도 실키가 새지 않는다.
    assert FAKE_KEY not in msg
    assert SCRUB_PLACEHOLDER in msg


def test_replay_transport_never_touches_network() -> None:
    # inner 트랜스포트 없이 재생만으로 동작(네트워크 접근 없음)함을 보인다.
    cassette = Cassette()
    cassette.add(
        method="GET",
        url=BASE + "?serviceKey=" + FAKE_KEY + "&pageNo=1",
        params={"serviceKey": FAKE_KEY, "pageNo": "1"},
        status=200,
        content_type="application/json",
        body_text='{"ok": true}',
        secrets=[FAKE_KEY],
    )
    replay = ReplayTransport(cassette)
    request = httpx.Request("GET", BASE, params={"pageNo": "1"})
    resp = replay.handle_request(request)
    assert resp.status_code == 200
    assert resp.json() == {"ok": True}


# ---------------------------------------------------------------------------
# F10 — 인증키 파라미터 이름 전파
# ---------------------------------------------------------------------------
CUSTOM_KEY_PARAMS = ("apiKey", "api_key")


def test_default_key_params_field_is_not_written(tmp_path: Path) -> None:
    # 기본값이면 저장 JSON에 key_params 키가 없다(W1 파일과 바이트 동일성 유지).
    cassette = Cassette()
    assert cassette.key_params == DEFAULT_KEY_PARAMS
    path = tmp_path / "default.json"
    cassette.save(path)
    data = json.loads(path.read_text(encoding="utf-8"))
    assert "key_params" not in data
    assert data["version"] == 1


def test_custom_key_params_record_save_load_replay_roundtrip(tmp_path: Path) -> None:
    inner = httpx.MockTransport(_mock_handler)
    cassette = Cassette(key_params=CUSTOM_KEY_PARAMS)
    # secrets 를 비워 두어 값 기반 스크러빙이 아니라 '키 이름' 기반 스크러빙만 검증한다.
    recording = RecordingTransport(
        inner, cassette, secrets=[], key_params=CUSTOM_KEY_PARAMS
    )
    with httpx.Client(transport=recording) as client:
        client.get(BASE, params={"apiKey": "CUSTOM_SECRET", "pageNo": "1"})

    path = tmp_path / "custom.json"
    cassette.save(path)
    raw = path.read_text(encoding="utf-8")
    # 커스텀 키 이름이 파일에 기록되고, 그 값은 스크러빙됐다.
    assert '"key_params"' in raw
    assert "CUSTOM_SECRET" not in raw
    assert SCRUB_PLACEHOLDER in raw

    loaded = Cassette.load(path)
    assert loaded.key_params == CUSTOM_KEY_PARAMS
    # 무키 재생: 매칭 키에서 apiKey 가 제거되므로 키 없는 요청도 매칭된다.
    replay = ReplayTransport(loaded)
    with httpx.Client(transport=replay) as client:
        replayed = client.get(BASE, params={"pageNo": "1"})
    assert replayed.status_code == 200
    assert "<resultCode>00</resultCode>" in replayed.text


def test_w1_cassette_without_key_params_loads_with_default(tmp_path: Path) -> None:
    # key_params 필드가 없는 W1 형식 카세트도 그대로 읽힌다(하위호환).
    legacy = {
        "version": 1,
        "recorded_at": "2026-07-13T16:30:00+09:00",
        "interactions": [
            {
                "request": {
                    "method": "GET",
                    "url": f"{BASE}?serviceKey={SCRUB_PLACEHOLDER}&pageNo=1",
                    "params": {"serviceKey": SCRUB_PLACEHOLDER, "pageNo": "1"},
                },
                "response": {
                    "status": 200,
                    "headers": {"content-type": "application/json"},
                    "body": '{"ok": true}',
                },
            }
        ],
    }
    path = tmp_path / "legacy.json"
    path.write_text(json.dumps(legacy, ensure_ascii=False), encoding="utf-8")

    loaded = Cassette.load(path)
    assert loaded.key_params == DEFAULT_KEY_PARAMS
    replay = ReplayTransport(loaded)
    with httpx.Client(transport=replay) as client:
        resp = client.get(BASE, params={"pageNo": "1"})
    assert resp.json() == {"ok": True}


def test_miss_error_scrubs_custom_key_name(tmp_path: Path) -> None:
    # 커스텀 키 이름을 쓰는 카세트에서도 미스 에러 메시지에 값이 새지 않는다.
    cassette = Cassette(key_params=CUSTOM_KEY_PARAMS)
    replay = ReplayTransport(cassette)
    request = httpx.Request("GET", f"{BASE}/other?apiKey=CUSTOM_SECRET&pageNo=1")
    with pytest.raises(CassetteMissError) as excinfo:
        replay.handle_request(request)
    message = str(excinfo.value)
    assert "CUSTOM_SECRET" not in message
    assert SCRUB_PLACEHOLDER in message
