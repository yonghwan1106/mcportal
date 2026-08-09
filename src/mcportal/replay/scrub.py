# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Yong Park
"""시크릿 스크러빙(secret scrubbing) 유틸리티.

설계 원칙
---------
스크러빙은 **record 단계의 기본값이며 끌 수 없다.** 카세트에 기록되는 URL,
쿼리 파라미터, 응답 본문은 저장 시점에 무조건 스크러빙을 거친다. serviceKey
같은 자격증명이 카세트 파일로 새어 나가면 공개 리포·CI 로그를 통해 즉시
유출되므로, 스크러빙을 옵션으로 두지 않고 항상 강제한다. 이 모듈의 함수는
그 강제 게이트의 원자적 구성요소다.

"끌 수 없다"는 선언은 **빈 키 이름 목록을 받아도 유지된다**:
:func:`scrub_url` · :func:`scrub_params` 는 빈 시퀀스를 받으면
:data:`DEFAULT_KEY_PARAMS` 로 폴백한다(F10 매개변수화가 off-switch가 되지
않게 하는 게이트). 키 이름을 매개변수화한 목적은 *별칭을 더하는 것*이지
스크러빙을 끄는 것이 아니다.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping, Sequence
from functools import lru_cache
from urllib.parse import quote, quote_plus, unquote, unquote_plus

#: 스크러빙된 값이 치환되는 고정 플레이스홀더.
SCRUB_PLACEHOLDER = "__SCRUBBED__"

#: 매개변수를 생략했을 때 적용되는 기본 인증키 파라미터 이름들(serviceKey 계열).
#: W1 동작과 동일한 기본값이므로 기존 호출부·기존 카세트는 그대로 호환된다(F10).
DEFAULT_KEY_PARAMS: tuple[str, ...] = ("serviceKey",)

#: **자격증명으로 취급하는 파라미터 이름의 정본 집합**(별칭 포함).
#:
#: :data:`DEFAULT_KEY_PARAMS` 는 "MCPortal 트랜스포트가 주입하는 키 이름"이라는
#: 좁은 뜻이고, 이 상수는 "값이 새어 나가면 안 되는 이름"이라는 넓은 뜻이다.
#: 포털·기관마다 인증 파라미터 이름이 달라서(``authKey`` · ``apiKey`` 등)
#: ``serviceKey`` 정확 일치만 보면 다른 이름의 자격증명이 산출물로 흘러간다
#: (2026-08-06 적대 리뷰 F4). 컴파일러(자유문자열 게이트 · 메타 예시값 흡수)와
#: 저장 게이트가 **같은 목록**을 보도록 여기 한 곳에 둔다.
CREDENTIAL_PARAM_NAMES: tuple[str, ...] = (
    *DEFAULT_KEY_PARAMS,
    "apiKey",
    "api_key",
    "authKey",
    "auth_key",
    "accessKey",
    "access_key",
    "secretKey",
    "secret_key",
)

#: 퍼센트 인코딩 시퀀스(``%2B`` 등). 대소문자 양쪽 변형을 만들 때 쓴다.
_PERCENT_RE = re.compile(r"%([0-9A-Fa-f]{2})")

#: 자격증명 이름 비교용 정규화 정규식(영숫자만 남긴다).
_CREDENTIAL_NAME_NOISE_RE = re.compile(r"[^a-z0-9]")


def normalize_credential_name(name: str) -> str:
    """자격증명 이름 비교용 정규형을 만든다(소문자화 + 영숫자만).

    ``authKey`` · ``auth_key`` · ``AUTH-KEY`` 가 모두 ``authkey`` 가 된다.

    Args:
        name: 비교할 파라미터 이름.

    Returns:
        정규화된 이름(빈 문자열일 수 있다).
    """
    return _CREDENTIAL_NAME_NOISE_RE.sub("", str(name).lower())


def is_credential_param(name: str, *, extra: Iterable[str] = ()) -> bool:
    """이름이 자격증명 파라미터인지 대소문자·구분자를 무시하고 판정한다.

    Args:
        name: 판정할 파라미터 이름.
        extra: 소스별로 추가할 이름(예: 그 소스의 ``key_param``).

    Returns:
        :data:`CREDENTIAL_PARAM_NAMES` 또는 ``extra`` 와 정규형이 같으면 True.
    """
    normalized = normalize_credential_name(name)
    if not normalized:
        return False
    known = {normalize_credential_name(item) for item in CREDENTIAL_PARAM_NAMES}
    known.update(normalize_credential_name(item) for item in extra)
    known.discard("")
    return normalized in known


def effective_key_params(key_params: Sequence[str]) -> tuple[str, ...]:
    """빈 시퀀스를 기본값으로 폴백한 유효 키 이름 목록을 돌려준다.

    F10 매개변수화가 "스크러빙을 끄는 스위치"가 되지 않도록, 이름이 하나도
    남지 않으면 :data:`DEFAULT_KEY_PARAMS` 를 적용한다. 모듈 docstring·
    README 가 선언한 "끌 수 없다"를 코드로 지키는 지점이다.

    Args:
        key_params: 호출자가 준 인증키 파라미터 이름들(빈 시퀀스 허용).

    Returns:
        최소 1개 이상의 이름을 담은 튜플.
    """
    names = tuple(str(name) for name in key_params if str(name))
    return names or DEFAULT_KEY_PARAMS


@lru_cache(maxsize=32)
def _url_pattern(key_params: tuple[str, ...]) -> re.Pattern[str]:
    """키 이름 목록으로 URL 스크러빙 정규식을 만든다(이름은 정규식 이스케이프한다).

    앞에 영숫자·밑줄이 붙은 경우(``myServiceKey=`` 등)를 배제하는 경계 조건을
    넣어, 다른 파라미터의 접미사와 우연히 일치하는 오탐을 막는다. 값은 ``&``
    또는 ``#`` 직전까지로 본다.

    Args:
        key_params: 인증키로 간주할 파라미터 이름들(해시 가능한 튜플).

    Returns:
        컴파일된 정규식. group(1)이 키 이름, group(2)가 값이다.
    """
    names = "|".join(re.escape(name) for name in key_params)
    return re.compile(rf"(?<![A-Za-z0-9_])({names})=([^&#]*)", re.IGNORECASE)


def scrub_url(url: str, key_params: Sequence[str] = DEFAULT_KEY_PARAMS) -> str:
    """URL 쿼리의 인증키 값을 플레이스홀더로 치환한다.

    키 이름은 대소문자를 무시하고 매칭하되 원래 표기는 보존한다(값만 교체).
    여러 번 등장하면 모두 치환한다. ``key_params`` 를 생략하면 serviceKey 계열
    기본값(:data:`DEFAULT_KEY_PARAMS`)을 쓴다.

    Args:
        url: 스크러빙할 URL 문자열.
        key_params: 인증키로 간주할 파라미터 이름들. **비면 기본값으로
            폴백한다**(스크러빙은 끌 수 없다).

    Returns:
        인증키 값이 플레이스홀더로 바뀐 URL 문자열.
    """
    names = effective_key_params(key_params)
    return _url_pattern(names).sub(
        lambda m: f"{m.group(1)}={SCRUB_PLACEHOLDER}", url
    )


def scrub_params(
    params: Mapping[str, object],
    key_params: Sequence[str] = DEFAULT_KEY_PARAMS,
) -> dict[str, object]:
    """매핑에서 인증키 계열 키의 값을 플레이스홀더로 치환한 새 dict를 돌려준다.

    키 이름은 대소문자를 무시하고 판정하며, 원래 키 표기는 그대로 유지한다.
    인증키 이외의 항목은 값을 건드리지 않으며 원본 매핑도 변형하지 않는다.

    Args:
        params: 원본 파라미터 매핑.
        key_params: 인증키로 간주할 파라미터 이름들. **비면 기본값으로
            폴백한다**(스크러빙은 끌 수 없다).

    Returns:
        인증키 값만 치환된 새 dict.
    """
    targets = {name.lower() for name in effective_key_params(key_params)}
    scrubbed: dict[str, object] = {}
    for key, value in params.items():
        if str(key).lower() in targets:
            scrubbed[key] = SCRUB_PLACEHOLDER
        else:
            scrubbed[key] = value
    return scrubbed


def _percent_case(text: str, *, upper: bool) -> str:
    """퍼센트 인코딩 시퀀스의 hex 대소문자를 통일한 사본을 돌려준다.

    ``quote`` 계열은 대문자 hex(``%2B``)를 만들지만, 실제 게이트웨이·PHP·
    Java 구현은 소문자 hex(``%2b``)를 되비추는 경우가 흔하다. 두 표기를 모두
    치환 후보로 확보하기 위한 헬퍼다.
    """
    return _PERCENT_RE.sub(
        lambda m: "%" + (m.group(1).upper() if upper else m.group(1).lower()), text
    )


def _json_escaped(text: str) -> tuple[str, str]:
    """JSON 문자열 리터럴 안에 놓였을 때의 표기 2종을 돌려준다.

    ``json.dumps`` 는 ``/`` 를 이스케이프하지 않지만 PHP ``json_encode`` 기본
    동작은 ``\\/`` 로 이스케이프한다. 응답 본문이 JSON일 때 시크릿이 이 표기로
    되비쳐 오면 원문 비교로는 잡히지 않으므로 둘 다 후보로 만든다.

    Returns:
        ``(표준 이스케이프, '/'까지 이스케이프한 표기)`` 쌍.
    """
    escaped = json.dumps(text, ensure_ascii=False)[1:-1]
    return escaped, escaped.replace("/", "\\/")


def _secret_variants(secret: str) -> set[str]:
    """시크릿 1개에서 치환 후보 문자열 집합을 만든다.

    후보 축은 3개다.

    1. **전송 표기** — 원문, ``quote`` 기본(safe="/"), ``quote_plus``,
       ``quote(safe="")``, 그리고 폼 디코딩으로 ``+`` 가 공백이 된 표기와
       공백이 ``%20`` 이 된 표기.
    2. **퍼센트 hex 대소문자** — ``%2B`` 와 ``%2b`` 양쪽.
    3. **JSON 이스케이프** — ``json.dumps`` 표준 표기와 ``/`` → ``\\/`` 표기.

    이 3축의 곱집합을 전부 후보로 둔다. 하나라도 빠지면 "응답이 요청 키를
    되비추는" 실제 사례에서 카세트에 평문이 남는다.
    """
    bases = {
        secret,
        quote(secret),
        quote_plus(secret),
        quote(secret, safe=""),
        secret.replace("+", " "),
        secret.replace("+", "%20"),
        secret.replace(" ", "+"),
        secret.replace(" ", "%20"),
    }
    variants: set[str] = set()
    for base in bases:
        if not base:
            continue
        for cased in (base, _percent_case(base, upper=True), _percent_case(base, upper=False)):
            variants.add(cased)
            variants.update(_json_escaped(cased))
    return {variant for variant in variants if variant}


def scrub_text(text: str, secrets: Iterable[str]) -> str:
    """본문 텍스트에서 각 시크릿의 알려진 모든 표기 변형을 치환한다.

    변형 후보는 :func:`_secret_variants` 가 만든다(전송 인코딩 × 퍼센트 hex
    대소문자 × JSON 이스케이프). 빈 문자열 시크릿은 무시한다. 부분 일치로 인한
    누락을 막기 위해 긴 변형부터 먼저 치환하며, 길이가 같으면 사전순으로
    치환해 결과가 결정론적이다.

    Args:
        text: 스크러빙할 본문 텍스트.
        secrets: 지울 시크릿 원문들.

    Returns:
        모든 변형이 :data:`SCRUB_PLACEHOLDER` 로 바뀐 텍스트.
    """
    if not text:
        return text

    variants: set[str] = set()
    for secret in secrets:
        if not secret:
            continue
        variants.update(_secret_variants(str(secret)))

    result = text
    # 긴 후보부터 치환해야 짧은 후보가 긴 후보의 일부를 먼저 깨뜨리지 않는다.
    # 길이가 같은 후보 사이의 순서는 사전순으로 고정해 결정론을 지킨다.
    for variant in sorted(variants, key=lambda item: (-len(item), item)):
        if variant and variant != SCRUB_PLACEHOLDER:
            result = result.replace(variant, SCRUB_PLACEHOLDER)
    return result


def find_key_assignments(
    text: str,
    key_params: Sequence[str] = DEFAULT_KEY_PARAMS,
) -> tuple[str, ...]:
    """텍스트에서 **값이 붙어 있는** 인증키 파라미터 이름들을 찾는다.

    ``serviceKey=<무언가>`` 처럼 인증키 이름 뒤에 비어 있지 않은 값이 이어지는
    구간을 탐지한다. 값이 이미 :data:`SCRUB_PLACEHOLDER` 이거나 비어 있으면
    위생 처리가 끝난 것으로 보고 탐지 대상에서 제외한다.

    카세트가 아니라 **사람이 손으로 채우는 자유문자열**(스펙 원본 URL·라이선스
    메모·서비스 설명)을 커밋 산출물에 싣기 전에 거르는 용도다. 스펙 문서를
    실키가 붙은 URL로 받아 오는 것은 자연스러운 사용법이므로, 그 URL을 그대로
    기록하면 공개 리포로 자격증명이 나간다.

    Args:
        text: 검사할 자유문자열.
        key_params: 인증키로 간주할 파라미터 이름들(빈 시퀀스면 기본값 폴백).

    Returns:
        값이 남아 있는 인증키 이름들(원문 표기, 중복 제거·정렬).
    """
    if not text:
        return ()
    names = effective_key_params(key_params)
    found = {
        match.group(1)
        for match in _url_pattern(names).finditer(str(text))
        if match.group(2) and match.group(2) != SCRUB_PLACEHOLDER
    }
    return tuple(sorted(found))


def harvest_key_values(
    url: str,
    params: Mapping[str, object] | None = None,
    key_params: Sequence[str] = DEFAULT_KEY_PARAMS,
    *,
    headers: Mapping[str, object] | None = None,
) -> tuple[str, ...]:
    """URL 쿼리·파라미터 매핑·요청 헤더에서 **이름으로 식별한** 인증키 값을 수확한다.

    :func:`scrub_url` · :func:`scrub_params` 는 이름 기준으로 값을 지우지만,
    응답 본문은 값 기준(:func:`scrub_text`)으로만 지운다. 호출자가 직접 실은
    키나 프로파일 별칭으로 선언한 키는 클라이언트가 보관한 시크릿 목록에
    없으므로, 이름으로 식별한 값을 값 기반 스크러빙 대상에 합류시켜야 본문
    에코까지 덮인다.

    ``headers`` 축은 F-08(인증키 헤더 주입)이 열었다. 헤더로 실린 키는 URL 에도
    params 에도 없으므로 이름 기반 수확이 없으면 **값 기반 스크러빙 목록에 아예
    오르지 못한다**. 그 상태에서 응답 본문이 인증키를 되비추면(실제 사례가 있다)
    평문이 그대로 카세트·샘플 파일에 남는다. 카세트가 요청 헤더를 기록하지
    않는다는 사실은 "헤더 값이 새지 않는다"를 URL/params 축에서만 보장하므로,
    본문 에코 축은 여기서 막아야 한다.

    헤더 이름은 HTTP 규약대로 대소문자를 무시해 비교한다.

    수확 값은 URL 원문 표기와 그 디코딩 표기(``unquote`` · ``unquote_plus``)를
    모두 담는다. 어느 쪽으로 되비쳐 와도 :func:`scrub_text` 가 잡게 하기 위함이다.

    Args:
        url: 요청 URL(쿼리 포함 가능).
        params: 명시 파라미터 매핑(없어도 된다).
        key_params: 인증키로 간주할 파라미터 이름들(빈 시퀀스면 기본값 폴백).
        headers: 요청 헤더 매핑(없어도 된다). 키워드 전용 인자라 기존 위치 인자
            호출부는 전부 그대로 동작한다.

    Returns:
        중복이 제거된 시크릿 후보 값 튜플(결정론적으로 정렬된다).
    """
    names = effective_key_params(key_params)
    lowered = {name.lower() for name in names}
    collected: set[str] = set()

    for match in _url_pattern(names).finditer(url or ""):
        collected.add(match.group(2))

    for key, value in dict(params or {}).items():
        if str(key).lower() in lowered:
            collected.add(str(value))

    for key, value in dict(headers or {}).items():
        if str(key).lower() in lowered:
            collected.add(str(value))

    harvested: set[str] = set()
    for raw in collected:
        if not raw or raw == SCRUB_PLACEHOLDER:
            continue
        harvested.add(raw)
        harvested.add(unquote(raw))
        harvested.add(unquote_plus(raw))
    harvested.discard(SCRUB_PLACEHOLDER)
    return tuple(sorted(value for value in harvested if value))
