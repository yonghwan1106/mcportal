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
      "key_params": ["serviceKey"],
      "interactions": [
        {
          "request":  {"method": "GET", "url": "...", "params": {...},
                       "body": "..."},
          "response": {"status": 200, "headers": {"content-type": "..."},
                       "body": "..."}
        }
      ]
    }

url / params / request.body / response.body 는 저장 시점에 이미 스크러빙된
값만 담긴다. 이름으로 식별한 인증키 값(호출자가 직접 실었거나 프로파일 별칭
으로 선언한 키)은 :func:`~mcportal.replay.scrub.harvest_key_values` 로 수확해
**값 기반 스크러빙 대상에 합류**시키므로, 응답이 요청 키를 되비추어도 본문에
평문이 남지 않는다.

``request.body`` 는 선택 필드다. 값이 있을 때만 기록되며 매칭 키에 포함된다.
없는 필드는 빈 본문으로 간주하므로 GET 전용이던 W1 카세트가 그대로 읽히고,
같은 URL에 서로 다른 본문을 보내는 POST 재생이 조용히 뒤섞이지 않는다.

``key_params`` 도 선택 필드다(F10). 기본값(:data:`~mcportal.replay.scrub.
DEFAULT_KEY_PARAMS`)과 다를 때만 기록되며, 없는 파일은 기본값으로 간주하므로
W1 형식 카세트가 그대로 읽힌다. 포맷 승격 없이 순수 추가이므로
``CASSETTE_VERSION`` 은 1을 유지한다.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional, Union
from urllib.parse import parse_qsl, urlsplit

import httpx

from .scrub import (
    DEFAULT_KEY_PARAMS,
    effective_key_params,
    harvest_key_values,
    scrub_params,
    scrub_text,
    scrub_url,
)

#: 카세트가 기록하는 KST 고정 오프셋(+09:00). tzdata 의존을 피하기 위한 고정값.
_KST = timezone(timedelta(hours=9))

PathLike = Union[str, Path]
Interaction = dict[str, Any]

CASSETTE_VERSION = 1


def _now_kst_iso() -> str:
    """현재 시각을 KST(+09:00) ISO8601 문자열로 돌려준다."""
    return datetime.now(_KST).isoformat()


def _canonical_key(
    method: str,
    url: str,
    params: Optional[Mapping[str, object]],
    key_params: Sequence[str] = DEFAULT_KEY_PARAMS,
    body: str = "",
) -> tuple[str, str, tuple[tuple[str, str], ...], str]:
    """매칭용 정규 키를 만든다.

    method(대문자) + base URL(쿼리 제외) + 인증키 파라미터를 제거하고 정렬한
    파라미터 + **요청 본문**. 인증키를 아예 빼기 때문에 **실키 요청과 무키
    요청이 같은 인터랙션에 매칭**된다. params가 비어 있으면 URL 쿼리에서
    파라미터를 파싱한다.

    요청 본문이 키에 포함되는 이유: W2가 POST 경로를 열면서 같은 URL·같은
    쿼리로 서로 다른 본문을 보내는 호출이 가능해졌다. 본문이 키에 없으면
    재생이 **오류 없이 다른 본문의 응답**을 돌려준다. 본문이 없는 GET은 빈
    문자열이므로 W1 카세트와 매칭 결과가 동일하다.

    Args:
        method: HTTP 메서드.
        url: 요청 URL(쿼리 포함 가능).
        params: 명시 파라미터 매핑(없으면 URL 쿼리를 파싱).
        key_params: 매칭에서 제외할 인증키 파라미터 이름들(대소문자 무시).
        body: 스크러빙된 요청 본문 텍스트(없으면 빈 문자열).
    """
    parts = urlsplit(url)
    base = f"{parts.scheme}://{parts.netloc}{parts.path}"
    if params:
        items = [(str(k), str(v)) for k, v in dict(params).items()]
    else:
        items = list(parse_qsl(parts.query, keep_blank_values=True))
    ignored = {str(name).lower() for name in key_params if name}
    filtered = sorted((k, v) for k, v in items if k.lower() not in ignored)
    return (method.upper(), base, tuple(filtered), body or "")


def _request_body_text(request: httpx.Request) -> str:
    """요청 본문을 텍스트로 읽는다(읽을 수 없으면 빈 문자열).

    ``httpx.Request.read()`` 는 본문을 ``_content`` 로 확정하고 스트림을
    ``ByteStream`` 으로 교체하므로 하류에서 다시 읽을 수 있다. 따라서 하위
    트랜스포트 호출 **전에** 불러도 안전하다.
    """
    try:
        content = request.read()
    except Exception:  # pragma: no cover - 스트리밍 업로드 등 방어
        return ""
    if not content:
        return ""
    return content.decode("utf-8", errors="replace")


class CassetteMissError(Exception):
    """요청과 매칭되는 인터랙션이 카세트에 없을 때 발생한다.

    Args:
        method: HTTP 메서드.
        url: 미스가 난 요청 URL.
        key_params: 메시지 스크러빙에 쓸 인증키 파라미터 이름들. 커스텀 키
            이름을 쓰는 카세트에서 값이 메시지로 새지 않게 하기 위한 인자다.
    """

    def __init__(
        self,
        method: str,
        url: str,
        key_params: Sequence[str] = DEFAULT_KEY_PARAMS,
    ) -> None:
        self.method = method
        # 메시지에 실키가 새지 않도록 URL도 스크러빙한다.
        self.url = scrub_url(url, key_params)
        super().__init__(
            f"{method.upper()} {self.url} — 카세트에 기록이 없습니다. "
            "RecordingTransport로 먼저 라이브 응답을 녹화하세요."
        )


class Cassette:
    """스크러빙된 HTTP 인터랙션의 JSON 카세트.

    Args:
        interactions: 초기 인터랙션 목록.
        version: 카세트 포맷 버전.
        recorded_at: 녹화 시각(ISO8601 KST). None이면 현재 시각.
        key_params: 이 카세트가 인증키로 간주하는 파라미터 이름들. 스크러빙과
            매칭 키 산출에 함께 쓰인다.

    정합성 철칙:
        record와 replay가 서로 다른 ``key_params`` 를 쓰면 매칭 키가 달라져
        카세트가 통째로 미스난다. 따라서 **카세트 파일에 저장된 값을 항상
        우선**한다(:meth:`load` 가 파일 값을 그대로 복원한다). 호출부는 재생
        시점에 키 이름을 다시 지정하지 않는 것이 원칙이다.
    """

    def __init__(
        self,
        interactions: Optional[list[Interaction]] = None,
        *,
        version: int = CASSETTE_VERSION,
        recorded_at: Optional[str] = None,
        key_params: Sequence[str] = DEFAULT_KEY_PARAMS,
    ) -> None:
        self.version = version
        self.recorded_at = recorded_at or _now_kst_iso()
        self.interactions: list[Interaction] = interactions or []
        # 빈 시퀀스는 기본값으로 폴백한다 — 스크러빙은 끌 수 없고(F10), 매칭 키
        # 에서 인증키를 빼는 규칙도 카세트마다 달라지면 안 되기 때문이다.
        self.key_params: tuple[str, ...] = effective_key_params(key_params)

    # -- 영속화 ---------------------------------------------------------
    @classmethod
    def load(cls, path: PathLike) -> "Cassette":
        """JSON 파일에서 카세트를 읽어들인다.

        ``key_params`` 필드가 **없는** W1 형식 카세트는 기본값
        (:data:`~mcportal.replay.scrub.DEFAULT_KEY_PARAMS`)으로 간주한다.
        필드가 있으면 그 값을 그대로 복원한다("저장된 값 우선" 철칙) — 빈
        리스트를 "필드 없음"과 같게 보면 라운드트립이 비대칭이 되므로 ``None``
        여부로만 판정한다. (빈 목록 자체는 :class:`Cassette` 생성자가 기본값으로
        폴백하므로 스크러빙 없는 카세트는 애초에 만들어지지 않는다.)
        """
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        stored = data.get("key_params")
        key_params = tuple(stored) if stored is not None else DEFAULT_KEY_PARAMS
        return cls(
            interactions=list(data.get("interactions", [])),
            version=int(data.get("version", CASSETTE_VERSION)),
            recorded_at=data.get("recorded_at"),
            key_params=key_params,
        )

    def save(self, path: PathLike) -> None:
        """카세트를 JSON 파일로 저장한다.

        ``key_params`` 가 기본값과 같으면 필드를 쓰지 않는다 — W1이 만든 기존
        카세트 파일과 바이트 동일성을 유지하기 위함이다.

        줄바꿈은 **플랫폼과 무관하게 LF** 로 쓰고 끝개행을 하나 붙인다. 기본
        ``write_text`` 는 Windows 에서 ``\\n`` 을 ``\\r\\n`` 으로 번역하므로, 같은
        카세트를 OS 마다 다른 바이트로 저장하게 된다. 그것이 문제인 이유는
        저장 바이트로 계산한 sha256 을 출처 기록에 싣기 때문이다 —
        ``.gitattributes`` 의 ``* text=auto eol=lf`` 가 커밋 시점에 CRLF 를 LF 로
        정규화하므로, CRLF 로 저장하면 **클론한 사람에게는 기록된 해시가 전부
        거짓**이 된다(``presets/_raw`` 를 ``-text`` 로 뺀 것과 같은 사고).
        """
        data: dict[str, Any] = {
            "version": self.version,
            "recorded_at": self.recorded_at or _now_kst_iso(),
        }
        if self.key_params != DEFAULT_KEY_PARAMS:
            data["key_params"] = list(self.key_params)
        data["interactions"] = self.interactions
        Path(path).write_text(
            json.dumps(data, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
            newline="\n",
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
        key_params: Optional[Sequence[str]] = None,
        request_body: str = "",
    ) -> Interaction:
        """인터랙션을 스크러빙해서 추가한다.

        url/params/요청본문/응답본문 전부에 ``scrub_url``·``scrub_params``·
        ``scrub_text`` 를 무조건 적용한다.

        **이름으로 식별한 키 값을 값 기반 스크러빙에 합류시킨다.** ``scrub_url``·
        ``scrub_params`` 는 *이름*으로 값을 지우지만 본문은 *값*으로만 지운다.
        호출자가 params 에 직접 실은 인증키나 프로파일 별칭(F10)으로 선언한
        키는 클라이언트가 보관한 시크릿 목록에 없으므로, 그 값을 여기서
        수확(:func:`~mcportal.replay.scrub.harvest_key_values`)해 ``secrets`` 에
        더하지 않으면 **응답 본문 echo 에 평문으로 남는다**. 카세트는 리포에
        커밋되는 파일이므로 그 결과는 공개 저장소 자격증명 노출이다.

        Args:
            method: HTTP 메서드.
            url: 요청 URL(쿼리 포함 가능).
            params: 요청 파라미터 매핑.
            status: 응답 상태 코드.
            content_type: 응답 Content-Type.
            body_text: 응답 본문 텍스트.
            secrets: 값 기준으로 지울 시크릿 목록(클라이언트 보관 키).
            key_params: 이번 녹화에 쓸 인증키 파라미터 이름들. None이면 카세트
                자신의 ``key_params`` 를 쓴다(빈 시퀀스는 기본값 폴백).
            request_body: 요청 본문 텍스트. 비어 있지 않을 때만 기록되며 매칭
                키에 포함된다(POST 재생이 뒤섞이지 않게 하는 식별자).
        """
        names = (
            self.key_params if key_params is None else effective_key_params(key_params)
        )
        harvested = harvest_key_values(url, params, names)
        secret_list = [
            secret for secret in dict.fromkeys([*secrets, *harvested]) if secret
        ]
        scrubbed_url = scrub_text(scrub_url(url, names), secret_list)
        scrubbed_params = {
            k: scrub_text(v, secret_list) if isinstance(v, str) else v
            for k, v in scrub_params(params, names).items()
        }
        scrubbed_body = scrub_text(body_text, secret_list)
        scrubbed_request_body = scrub_text(request_body or "", secret_list)
        request: dict[str, Any] = {
            "method": method.upper(),
            "url": scrubbed_url,
            "params": scrubbed_params,
        }
        if scrubbed_request_body:
            request["body"] = scrubbed_request_body
        interaction: Interaction = {
            "request": request,
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
        key_params: Optional[Sequence[str]] = None,
        body: str = "",
    ) -> Optional[Interaction]:
        """method+url+params+본문 으로 매칭되는 인터랙션을 찾는다(없으면 None).

        매칭 키는 스크러빙/정규화 후 인증키 파라미터를 제거한 값이라, 실키 요청과
        무키 요청이 동일 인터랙션에 매칭된다. 요청 본문도 키의 일부이므로 같은
        URL에 서로 다른 본문을 보내는 POST 가 서로의 응답을 받지 않는다.

        Args:
            method: HTTP 메서드.
            url: 요청 URL.
            params: 요청 파라미터(없으면 URL 쿼리를 파싱).
            key_params: 매칭에서 제외할 인증키 이름들. None이면 카세트 자신의
                값을 쓴다(정합성 철칙: 저장된 값 우선).
            body: 요청 본문 텍스트(없으면 빈 문자열).
        """
        names = (
            self.key_params if key_params is None else effective_key_params(key_params)
        )
        target = _canonical_key(method, url, params, names, body)
        for interaction in self.interactions:
            request = interaction["request"]
            candidate = _canonical_key(
                request["method"],
                request["url"],
                request.get("params"),
                names,
                str(request.get("body", "")),
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
        body = _request_body_text(request)
        interaction = self._cassette.find(method, url, params, body=body)
        if interaction is None:
            raise CassetteMissError(method, url, self._cassette.key_params)
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

    Args:
        inner: 실제 전송을 담당하는 하위 트랜스포트.
        cassette: 녹화 대상 카세트.
        secrets: 본문·URL에서 값 기준으로 지울 시크릿 목록.
        save_path: close 시 저장할 경로.
        key_params: 인증키로 간주할 파라미터 이름들. None이면 카세트 자신의
            값을 쓴다(정합성 철칙: record와 replay가 같은 이름을 봐야 한다).
    """

    def __init__(
        self,
        inner: httpx.BaseTransport,
        cassette: Cassette,
        secrets: Sequence[str],
        *,
        save_path: Optional[PathLike] = None,
        key_params: Optional[Sequence[str]] = None,
    ) -> None:
        self._inner = inner
        self._cassette = cassette
        self._secrets = list(secrets)
        self._save_path = save_path
        self._key_params: Optional[tuple[str, ...]] = (
            None if key_params is None else tuple(key_params)
        )

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        # 하위 호출 전에 요청 본문을 확정해 둔다(read()가 스트림을 ByteStream
        # 으로 교체하므로 하류가 다시 읽을 수 있다). POST 재생 식별자가 된다.
        request_body = _request_body_text(request)
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
            key_params=self._key_params,
            request_body=request_body,
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
