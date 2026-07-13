# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Yong Park
"""SQLite 기반 호출 사용량 원장(UsageLedger).

원본 인증키는 절대 저장·로깅하지 않는다. 저장 단위는 sha256 지문(앞 12자)이다.
KST 날짜는 tzdata 의존 없이 UTC+9 고정 오프셋으로 산출한다(Windows zoneinfo 부재 대응).
"""
from __future__ import annotations

import hashlib
import sqlite3
import threading
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

# KST를 tzdata 없이 고정 오프셋으로 구현(Windows에서 zoneinfo DB 부재 대응).
_KST = timezone(timedelta(hours=9))
_UTC = timezone.utc

_DEFAULT_PATH = Path.home() / ".mcportal" / "ledger.db"


def key_fp(key: str) -> str:
    """원본 키를 sha256 지문(hex 앞 12자)으로 축약한다. 원본은 복원 불가."""
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:12]


def _to_utc(now: datetime | None) -> datetime:
    """주입된 datetime을 UTC aware로 정규화한다. None이면 현재 UTC. naive는 UTC로 간주."""
    if now is None:
        return datetime.now(_UTC)
    if now.tzinfo is None:
        return now.replace(tzinfo=_UTC)
    return now.astimezone(_UTC)


def kst_day(now: datetime | None = None) -> str:
    """주어진 시각을 KST(UTC+9) 기준 날짜 문자열(ISO, YYYY-MM-DD)로 변환한다."""
    return _to_utc(now).astimezone(_KST).date().isoformat()


class UsageLedger:
    """호출 이벤트를 SQLite(WAL)에 append하는 사용량 원장.

    테이블 calls(ts_utc, day_kst, key_fp, endpoint, status, result_code)와
    인덱스(day_kst, key_fp)를 자동 생성한다. 단일 커넥션(check_same_thread=False)
    + Lock으로 동시성을 보호한다.
    """

    def __init__(self, path: str | Path | None = None) -> None:
        self.path: Path = Path(path) if path is not None else _DEFAULT_PATH
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS calls (
                ts_utc      TEXT NOT NULL,
                day_kst     TEXT NOT NULL,
                key_fp      TEXT NOT NULL,
                endpoint    TEXT,
                status      TEXT,
                result_code TEXT
            )
            """
        )
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_calls_day_key ON calls(day_kst, key_fp)"
        )
        self._conn.commit()

    def record(
        self,
        key: str,
        endpoint: str,
        status: str = "ok",
        result_code: Optional[str] = None,
        now: datetime | None = None,
    ) -> None:
        """호출 1건을 원장에 기록한다. 원본 key는 지문으로만 저장된다."""
        now_utc = _to_utc(now)
        ts_utc = now_utc.isoformat()
        day = now_utc.astimezone(_KST).date().isoformat()
        fp = key_fp(key)
        with self._lock:
            self._conn.execute(
                "INSERT INTO calls "
                "(ts_utc, day_kst, key_fp, endpoint, status, result_code) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (ts_utc, day, fp, endpoint, status, result_code),
            )
            self._conn.commit()

    def count_today(
        self,
        key: str,
        endpoint: Optional[str] = None,
        now: datetime | None = None,
    ) -> int:
        """오늘(KST) 해당 key의 호출 건수. endpoint 지정 시 해당 엔드포인트만 집계."""
        day = kst_day(now)
        fp = key_fp(key)
        with self._lock:
            if endpoint is None:
                cur = self._conn.execute(
                    "SELECT COUNT(*) FROM calls WHERE day_kst = ? AND key_fp = ?",
                    (day, fp),
                )
            else:
                cur = self._conn.execute(
                    "SELECT COUNT(*) FROM calls "
                    "WHERE day_kst = ? AND key_fp = ? AND endpoint = ?",
                    (day, fp, endpoint),
                )
            row = cur.fetchone()
            return int(row[0]) if row else 0

    def close(self) -> None:
        """커넥션을 닫는다."""
        with self._lock:
            self._conn.close()

    def __enter__(self) -> "UsageLedger":
        return self

    def __exit__(self, *exc: object) -> bool:
        self.close()
        return False
