# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Yong Park
"""JSON 카세트(cassette)와 record/replay 트랜스포트.

카세트는 HTTP 상호작용(request/response)을 JSON 파일로 녹화해 두었다가
serviceKey 없이도 동일한 응답 흐름을 재생하게 해 주는 record/replay 골격이다.
심사 기능테스트처럼 실키가 없는 환경에서 전체 경로를 초록불로 돌리는 생명줄이며,
녹화 시점의 스크러빙(:mod:`mcportal.replay.scrub`)이 시크릿 유출 방지 게이트다.

카세트 JSON 포맷::

    {
      "version": 1,
      "recorded_at": "2026-07-13T16:30:00+09:00",
      "interactions": [
        {
          "request":  {"method": "GET", "url": "...", "params": {...}},
          "response": {"status": 200, "headers": {"content-type": "..."},
                       "body": "..."}
        }
      ]
    }

url / params / body 는 저장 시점에 이미 스크러빙된 값만 담긴다.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional, Union
from urllib.parse import parse_qsl, urlsplit

import httpx

from .scrub import scrub_params, scrub_text, scrub_url

#: 카세트가 기록하는 KST 고정 오프셋(+09:00). tzdata 의존을 피하기 위한 고정값.
_KST = timezone(timedelta(hours=9))

#: 매칭 시 무시(제거)하는 파라미터 키의 정규화 이름.
_SERVICE_KEY_NAME = "servicekey"

PathLike = Union[str, Path]
Interaction = dict[str, Any]

CASSETTE_VERSION = 1


def _now_kst_iso() -> str:
    """현재 시각을 KST(+09:00) ISO8601 문자열로 돌려준다."""
    return datetime.now(_KST).isoformat()


def _canonical_key(
    method: str, url: str, params: Optional[Mapping[str, object]]
) -> tuple[str, str, tuple[tuple[str, str], ...]]:
    """매칭용 정규 키를 만든다.

    method(대문자) + base URL(쿼리 제외) + serviceKey를 제거하고 정렬한 파라미터.
    serviceKey를 아예 빼기 때문에 **실키 요청과 무키 요청이 같은 인터랙션에 매칭**된다.
    params가 비어 있으면 URL 쿼리에서 파라미터를 파싱한다.
    """
    parts = urlsplit(url)
    base = f"{parts.scheme}://{parts.netloc}{parts.path}"
    if params:
        items = [(str(k), str(v)) for k, v in dict(params).items()]
    else:
        items = list(parse_qsl(parts.query, keep_blank_values=True))
    filtered = sorted(
        (k, v) for k, v in items if k.lower() != _SERVICE_KEY_NAME
    )
    return (method.upper(), base, tuple(filtered))


class CassetteMissError(Exception):
    """요청과 매칭되는 인터랙션이 카세트에 없을 때 발생한다."""

    def __init__(self, method: str, url: str) -> None:
        self.method = method
        # 메시지에 실키가 새지 않도록 URL도 스크러빙한다.
        self.url = scrub_url(url)
        super().__init__(
            f"{method.upper()} {self.url} — 카세트에 기록이 없습니다. "
            "RecordingTransport로 먼저 라이브 응답을 녹화하세요."
        )


class Cassette:
    """스크러빙된 HTTP 인터랙션의 JSON 카세트."""

    def __init__(
        self,
        interactions: Optional[list[Interaction]] = None,
        *,
        version: int = CASSETTE_VERSION,
        recorded_at: Optional[str] = None,
    ) -> None:
        self.version = version
        self.recorded_at = recorded_at or _now_kst_iso()
        self.interactions: list[Interaction] = interactions or []

    # -- 영속화 ---------------------------------------------------------
    @classmethod
    def load(cls, path: PathLike) -> "Cassette":
        """JSON 파일에서 카세트를 읽어들인다."""
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        return cls(
            interactions=list(data.get("interactions", [])),
            version=int(data.get("version", CASSETTE_VERSION)),
            recorded_at=data.get("recorded_at"),
        )

    def save(self, path: PathLike) -> None:
        """카세트를 JSON 파일로 저장한다."""
        data = {
            "version": self.version,
            "recorded_at": self.recorded_at or _now_kst_iso(),
            "interactions": self.interactions,
        }
        Path(path).write_text(
            json.dumps(data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    # -- 녹화/조회 ------------------------------------------------------
    def add(
        self,
        method: str,
        url: str,
        params: Mapping[str, object],
        status: int,
        content_type: str,
        body_text: str,
        secrets: Sequence[str],
    ) -> Interaction:
        """인터랙션을 스크러빙해서 추가한다.

        url/params/body 전부에 scrub_url·scrub_params·scrub_text 를 무조건 적용한다.
        """
        secret_list = list(secrets)
        scrubbed_url = scrub_text(scrub_url(url), secret_list)
        scrubbed_params = {
            k: scrub_text(v, secret_list) if isinstance(v, str) else v
            for k, v in scrub_params(params).items()
        }
        scrubbed_body = scrub_text(body_text, secret_list)
        interaction: Interaction = {
            "request": {
                "method": method.upper(),
                "url": scrubbed_url,
                "params": scrubbed_params,
            },
            "response": {
                "status": int(status),
                "headers": {"content-type": content_type},
                "body": scrubbed_body,
            },
        }
        self.interactions.append(interaction)
        return interaction

    def find(
        self,
        method: str,
        url: str,
        params: Optional[Mapping[str, object]] = None,
    ) -> Optional[Interaction]:
        """method+url+params 로 매칭되는 인터랙션을 찾는다(없으면 None).

        매칭 키는 스크러빙/정규화 후 serviceKey를 제거한 값이라, 실키 요청과
        무키 요청이 동일 인터랙션에 매칭된다.
        """
        target = _canonical_key(method, url, params)
        for interaction in self.interactions:
            request = interaction["request"]
            candidate = _canonical_key(
                request["method"], request["url"], request.get("params")
            )
            if candidate == target:
                return interaction
        return None


class ReplayTransport(httpx.BaseTransport):
    """카세트에서 응답을 재생하는 트랜스포트. 네트워크에 절대 접근하지 않는다."""

    def __init__(self, cassette: Cassette) -> None:
        self._cassette = cassette

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        method = request.method
        url = str(request.url)
        params = dict(request.url.params)
        interaction = self._cassette.find(method, url, params)
        if interaction is None:
            raise CassetteMissError(method, url)
        response = interaction["response"]
        headers = dict(response.get("headers", {}))
        body: str = response.get("body", "")
        return httpx.Response(
            status_code=int(response["status"]),
            headers=headers,
            content=body.encode("utf-8"),
            request=request,
        )


class RecordingTransport(httpx.BaseTransport):
    """실 트랜스포트로 라이브 호출하고, 응답을 스크러빙해 카세트에 녹화한다.

    응답 스트림을 소진(read)해 본문을 확보한 뒤, 소비한 콘텐츠로 새 Response 를
    구성해 그대로 돌려주므로 하류 소비자가 본문을 다시 읽을 수 있다.
    """

    def __init__(
        self,
        inner: httpx.BaseTransport,
        cassette: Cassette,
        secrets: Sequence[str],
        *,
        save_path: Optional[PathLike] = None,
    ) -> None:
        self._inner = inner
        self._cassette = cassette
        self._secrets = list(secrets)
        self._save_path = save_path

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        response = self._inner.handle_request(request)
        content = response.read()  # 스트림 소진, response._content 확정
        content_type = response.headers.get("content-type", "")
        try:
            body_text = response.text
        except Exception:  # pragma: no cover - 디코딩 실패 방어
            body_text = content.decode("utf-8", errors="replace")

        self._cassette.add(
            method=request.method,
            url=str(request.url),
            params=dict(request.url.params),
            status=response.status_code,
            content_type=content_type,
            body_text=body_text,
            secrets=self._secrets,
        )

        # 소비한 content 로 새 Response 구성(원본 스트림은 이미 소진됨).
        # content 는 이미 디코딩된 바이트다. 원본 헤더에 Content-Encoding(gzip 등)
        # 이나 Content-Length/Transfer-Encoding 이 남아 있으면, httpx 가 이 Response
        # 를 만들며 디코딩된 바이트를 다시 gunzip 하려다 Decoding(incorrect header
        # check) 로 즉시 크래시한다. 그래서 인코딩·길이 관련 헤더를 벗긴 사본으로
        # 재구성한다(httpx 가 content 로부터 Content-Length 를 다시 계산한다).
        headers = response.headers.copy()
        for stale in ("content-encoding", "content-length", "transfer-encoding"):
            if stale in headers:
                del headers[stale]
        return httpx.Response(
            status_code=response.status_code,
            headers=headers,
            content=content,
            request=request,
            extensions=dict(response.extensions),
        )

    def save(self, path: Optional[PathLike] = None) -> None:
        """카세트를 저장한다. path 미지정 시 생성 시 준 save_path 를 쓴다."""
        target = path or self._save_path
        if target is None:
            raise ValueError(
                "저장 경로가 없습니다. save(path) 인자나 save_path 를 지정하세요."
            )
        self._cassette.save(target)

    def close(self) -> None:
        """생성 시 save_path 가 있으면 저장하고 내부 트랜스포트를 닫는다."""
        if self._save_path is not None:
            self._cassette.save(self._save_path)
        self._inner.close()
