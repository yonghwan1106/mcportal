# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Yong Park
"""TTL 기반 응답 캐시.

data.go.kr 개발계정은 일일 호출 한도(기본 10,000회)가 있어, 같은 요청을 반복
호출하면 쿼터가 빠르게 소진된다. 이 캐시는 (method, URL, params) 조합으로 응답
바이트를 짧은 TTL 동안 재사용해 라이브 쿼터를 보호한다.

핵심 안전장치: 캐시 키에는 ``serviceKey`` 가 **절대** 스며들지 않는다. 인증키만
다르고 나머지가 같은 두 요청은 같은 키로 매핑되며, 인증키 원문이 sha256 입력에
포함되지 않으므로 키가 로그·메모리 덤프에 노출되지 않는다.
"""
from __future__ import annotations

import hashlib
import threading
import time
from collections import OrderedDict
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

_SERVICE_KEY_PARAM = "servicekey"  # 소문자 비교용.


def _stringify(value: Any) -> str:
    """파라미터 값을 결정론적 문자열로 변환한다(None → 빈 문자열)."""
    if value is None:
        return ""
    return str(value)


def _strip_service_key_pairs(pairs: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """(key, value) 목록에서 serviceKey(대소문자 무시)를 제거한다."""
    return [(k, v) for k, v in pairs if k.lower() != _SERVICE_KEY_PARAM]


def make_key(
    method: str,
    url: str,
    params: Mapping[str, Any] | None = None,
) -> str:
    """캐시 키를 만든다.

    URL 쿼리스트링과 ``params`` 양쪽에서 ``serviceKey`` (대소문자 무시)를 제거한 뒤,
    ``method`` + 정규화된 URL(경로 + serviceKey 뺀 정렬 쿼리) + 정렬된 params 를
    합쳐 sha256 해시한다. 인증키 원문은 해시 입력에 포함되지 않는다.
    """
    split = urlsplit(url)
    query_pairs = _strip_service_key_pairs(parse_qsl(split.query, keep_blank_values=True))
    normalized_query = urlencode(sorted((k, _stringify(v)) for k, v in query_pairs))
    normalized_url = urlunsplit((split.scheme, split.netloc, split.path, normalized_query, ""))

    param_pairs = sorted(
        (str(k), _stringify(v))
        for k, v in (params.items() if params else [])
        if str(k).lower() != _SERVICE_KEY_PARAM
    )
    canonical = "\x00".join([method.upper(), normalized_url, urlencode(param_pairs)])
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class CacheEntry:
    """캐시 항목: 저장된 바이트와 시각 메타데이터."""

    value: bytes
    stored_at: float
    expires_at: float


class TTLCache:
    """스레드 안전한 TTL + 용량 제한 캐시.

    ``clock`` 은 단조 시계(기본 :func:`time.monotonic`)이며 테스트에서 가짜
    클록을 주입할 수 있다. 만료된 항목은 조회 시 제거되고, 용량 초과 시 가장
    오래 전에 저장된 항목부터 축출한다.
    """

    def __init__(
        self,
        ttl: float = 300.0,
        max_entries: int = 1024,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._ttl = float(ttl)
        self._max_entries = int(max_entries)
        self._clock = clock
        self._store: "OrderedDict[str, CacheEntry]" = OrderedDict()
        self._lock = threading.Lock()

    def get(self, key: str) -> CacheEntry | None:
        """유효한 항목이면 :class:`CacheEntry`, 없거나 만료면 None(만료 시 제거)."""
        with self._lock:
            entry = self._store.get(key)
            if entry is None:
                return None
            if self._clock() >= entry.expires_at:
                del self._store[key]
                return None
            return entry

    def set(self, key: str, value: bytes) -> None:
        """값을 저장한다. 기존 키는 최신으로 갱신되고, 용량 초과 시 최고참을 축출."""
        now = self._clock()
        entry = CacheEntry(value=value, stored_at=now, expires_at=now + self._ttl)
        with self._lock:
            if key in self._store:
                del self._store[key]
            self._store[key] = entry
            while len(self._store) > self._max_entries:
                self._store.popitem(last=False)  # 가장 오래된(먼저 넣은) 항목 제거.

    def clear(self) -> None:
        """전체 비우기."""
        with self._lock:
            self._store.clear()

    def __len__(self) -> int:
        with self._lock:
            return len(self._store)

    def __contains__(self, key: str) -> bool:
        return self.get(key) is not None
