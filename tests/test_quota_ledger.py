# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Yong Park
"""UsageLedger 테스트: 기록/집계, endpoint 필터, KST 날짜 경계, 원본 키 미저장."""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from mcportal.quota import UsageLedger
from mcportal.quota.ledger import key_fp

RAW_KEY = "RAWSECRET-datagokr-key-abcdef1234567890QWERTY"


def test_record_and_count(tmp_path: Path) -> None:
    with UsageLedger(tmp_path / "ledger.db") as led:
        led.record(RAW_KEY, "/api/a")
        led.record(RAW_KEY, "/api/a")
        led.record(RAW_KEY, "/api/b")
        assert led.count_today(RAW_KEY) == 3
        assert led.count_today(RAW_KEY, endpoint="/api/a") == 2
        assert led.count_today(RAW_KEY, endpoint="/api/b") == 1
        assert led.count_today(RAW_KEY, endpoint="/api/missing") == 0
        assert led.count_today("DIFFERENT-KEY") == 0


def test_auto_creates_dir_and_schema(tmp_path: Path) -> None:
    nested = tmp_path / "deep" / "nested" / "ledger.db"
    led = UsageLedger(nested)
    assert nested.parent.is_dir()
    led.record(RAW_KEY, "/x")
    assert led.count_today(RAW_KEY) == 1
    led.close()


def test_kst_day_boundary(tmp_path: Path) -> None:
    led = UsageLedger(tmp_path / "l.db")
    # UTC 2026-07-13T14:59Z == KST 2026-07-13 23:59 (같은 날)
    before = datetime(2026, 7, 13, 14, 59, tzinfo=timezone.utc)
    # UTC 2026-07-13T15:00Z == KST 2026-07-14 00:00 (다음 날)
    after = datetime(2026, 7, 13, 15, 0, tzinfo=timezone.utc)

    led.record(RAW_KEY, "/x", now=before)
    led.record(RAW_KEY, "/x", now=after)

    # 서로 다른 day_kst 로 분리되어야 한다.
    assert led.count_today(RAW_KEY, now=before) == 1
    assert led.count_today(RAW_KEY, now=after) == 1
    led.close()


def test_raw_key_never_persisted(tmp_path: Path) -> None:
    led = UsageLedger(tmp_path / "secret.db")
    for _ in range(5):
        led.record(RAW_KEY, "/api/x")
    led.close()  # WAL 체크포인트 및 커넥션 종료

    raw = RAW_KEY.encode("utf-8")
    # db, db-wal, db-shm 등 남은 모든 파일 검사
    checked = 0
    for p in tmp_path.iterdir():
        if p.is_file():
            checked += 1
            assert raw not in p.read_bytes(), f"원본 키가 {p.name}에 노출됨"
    assert checked >= 1
    # 지문은 저장되지만 원본 문자열과 다르다.
    assert key_fp(RAW_KEY) not in RAW_KEY


def test_key_fp_is_stable_and_short() -> None:
    fp = key_fp(RAW_KEY)
    assert len(fp) == 12
    assert all(c in "0123456789abcdef" for c in fp)
    assert fp == key_fp(RAW_KEY)
    assert fp != key_fp("another-key")
