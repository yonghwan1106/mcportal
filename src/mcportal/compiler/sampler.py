# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Yong Park
"""라이브 샘플링 오케스트레이터 — 스키마 추론용 응답 표본을 최소 비용으로 얻는다.

data.go.kr 스펙에는 응답 스키마가 없는 경우가 많아, 도구 설명을 채우려면 실제
응답 표본이 필요하다. 이 모듈은 그 표본 채취를 **쿼터 안전하게** 수행한다.

강제 규칙(Advisor 결정 F)
-------------------------
* **하드캡 5회.** :data:`MAX_SAMPLES` 를 넘기는 경로는 존재하지 않는다. 조용히
  자르지 않고 :class:`ValueError` 로 거부한다.
* **QuotaGuard 경유 강제.** 샘플러는 ``httpx.Client()`` 를 직접 만들지 않고 항상
  :func:`mcportal.create_client` 로 배선한다. 예산 하드가드·원장 집계·백오프·
  정규화가 구조적으로 끼어든다.
* **중복 요청 제거.** 정규화한 ``(method, path, params)`` 가 같은 요청은 1회만
  보낸다(페이징 파라미터가 없는 오퍼레이션에서 쿼터를 태우지 않기 위해).
* **스크러빙 강제.** record 모드는 카세트 스크러빙을 거치고, 샘플 파일 저장은
  :func:`write_samples` 가 다시 한 번 시크릿을 치환한다(응답이 요청 키를
  되비추는 사례가 실재하므로 2중 방어). 그 두 번째 층이 기본값으로 꺼지지
  않도록 ``write_samples(secrets=...)`` 는 **키워드 필수 인자**다.
* **키가 없으면 명확한 한국어 안내.** 무키 환경에서는 ``mode="replay"`` 와 기존
  카세트가 정답이며, :class:`SampleKeyMissingError` 가 그 경로를 안내한다.
* **``QuotaExhausted`` 를 삼키지 않는다.** 예산 소진은 조용히 넘길 사건이 아니라
  호출자가 안내를 받아야 하는 사건이다.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Union

import httpx

from ..profiles import DATA_GO_KR, ProviderProfile
from ..replay.scrub import scrub_text
from ..runtime.normalize import normalize_response
from ..transport import create_client
from .inference import InferenceConfig, InferenceReport, infer_schema_with_report
from .openapi import DEFAULT_OPTIONS, CompiledSpec, CompileOptions, build_openapi
from .sources import OperationSpec, SourceSpec

__all__ = [
    "MAX_SAMPLES",
    "SamplingError",
    "SampleKeyMissingError",
    "SampleParamError",
    "SampleRequest",
    "SampleResult",
    "build_sample_requests",
    "sample_operation",
    "sample_source",
    "infer_response_schemas",
    "write_samples",
    "compile_with_sampling",
]

PathLike = Union[str, Path]

#: 오퍼레이션당 라이브 샘플 호출의 하드캡(Advisor 결정 F). 어떤 경로로도 넘길 수 없다.
MAX_SAMPLES: int = 5

#: 무키 상태로 라이브 샘플링을 시도했을 때의 안내(S5).
_KEY_MISSING_MESSAGE = (
    "라이브 샘플링에는 data.go.kr 인증키가 필요합니다. "
    "무키로는 mode='replay'와 기존 카세트를 사용하세요."
)

#: 샘플 순번을 값으로 쓰는 페이징 파라미터(대소문자 무시).
_PAGE_PARAMS = frozenset({"pageno", "page", "pageindex", "pagenum"})

#: 페이지 크기 파라미터(대소문자 무시). 값은 "10".
_ROWS_PARAMS = frozenset(
    {"numofrows", "perpage", "rowsperpage", "pagesize", "display"}
)

#: 응답 형식 파라미터(대소문자 무시). 값은 "json".
_TYPE_PARAMS = frozenset({"_type", "datatype", "resulttype", "returntype", "type"})

#: 응답 포맷 파라미터(대소문자 무시). 값은 "json".
_FORMAT_PARAMS = frozenset({"format"})

#: 페이지 크기 기본 샘플값.
_ROWS_VALUE = "10"

#: 응답 형식 기본 샘플값.
_JSON_VALUE = "json"


class SamplingError(RuntimeError):
    """샘플링 실패의 기반 예외(한국어 메시지)."""


class SampleKeyMissingError(SamplingError):
    """실인증키 없이 라이브 샘플링을 시도했을 때 발생한다."""


class SampleParamError(SamplingError):
    """필수 파라미터의 샘플값을 결정할 수 없을 때 발생한다."""


@dataclass(frozen=True)
class SampleRequest:
    """샘플 호출 1건. ``serviceKey`` 는 여기에 절대 담기지 않는다.

    Attributes:
        operation_id: 대상 오퍼레이션.
        method: HTTP 메서드(대문자).
        path: ``base_url`` 이후 경로(경로 파라미터는 이미 치환된 상태).
        params: 정렬된 ``(name, value)`` 쌍. dict 가 아닌 이유는 해시 가능성과
            결정론(중복 제거 키로 쓰인다) 때문이다.
    """

    operation_id: str
    method: str
    path: str
    params: tuple[tuple[str, str], ...]


@dataclass(frozen=True)
class SampleResult:
    """샘플 호출 1건의 정규화 결과.

    Attributes:
        operation_id: 대상 오퍼레이션.
        status_code: HTTP 상태 코드.
        ok: 전송·업무 응답이 모두 정상인지 여부(추론 입력 자격).
        result_code: data.go.kr resultCode(없으면 None).
        source_format: ``"json"`` 또는 ``"xml"``.
        payload: 정규화된 응답 본문(``NormalizedResponse.data``).
    """

    operation_id: str
    status_code: int
    ok: bool
    result_code: str | None
    source_format: str
    payload: Mapping[str, Any]


def _find_operation(source: SourceSpec, operation_id: str) -> OperationSpec:
    """operation_id 로 오퍼레이션을 찾는다.

    Raises:
        ValueError: 소스에 해당 operation_id 가 없을 때.
    """
    for operation in source.operations:
        if str(operation.operation_id) == str(operation_id):
            return operation
    known = ", ".join(str(op.operation_id) for op in source.operations)
    raise ValueError(
        f"오퍼레이션을 찾을 수 없습니다: {operation_id!r} (사용 가능: {known or '없음'})"
    )


def _check_count(count: int) -> int:
    """샘플 수가 하드캡(S1) 안에 있는지 확인한다.

    Raises:
        ValueError: 1 미만이거나 :data:`MAX_SAMPLES` 초과일 때.
    """
    value = int(count)
    if value < 1:
        raise ValueError(f"샘플 수는 1 이상이어야 합니다: {count!r}")
    if value > MAX_SAMPLES:
        raise ValueError(
            f"샘플 수는 하드캡 {MAX_SAMPLES}을 넘을 수 없습니다: {count!r} "
            "(쿼터 보호를 위한 상한이며 우회 경로는 없습니다)"
        )
    return value


def _sample_value(
    param: Any,
    *,
    index: int,
    overrides: Mapping[str, str],
    operation_id: str,
) -> str:
    """필수 파라미터 1개의 샘플값을 결정론적으로 정한다(§8-2).

    우선순위: overrides → example → default → enum[0] → 관용 파라미터 표 →
    타입 기본값. 어느 규칙에도 걸리지 않으면 실패한다.

    Args:
        param: 대상 ``ParamSpec``.
        index: 샘플 순번(1-기반). 페이징 파라미터 값으로 쓰인다.
        overrides: 이름 → 값 강제 지정.
        operation_id: 오류 메시지에 넣을 오퍼레이션 이름.

    Returns:
        문자열 샘플값.

    Raises:
        SampleParamError: 값을 정할 수 없을 때(오퍼레이션명·파라미터명 포함).
    """
    name = str(param.name)
    if name in overrides:
        return str(overrides[name])
    if param.example is not None:
        return str(param.example)
    if param.default is not None:
        return str(param.default)
    if param.enum:
        return str(param.enum[0])
    lowered = name.lower()
    if lowered in _PAGE_PARAMS:
        return str(index)
    if lowered in _ROWS_PARAMS:
        return _ROWS_VALUE
    if lowered in _TYPE_PARAMS:
        return _JSON_VALUE
    if lowered in _FORMAT_PARAMS:
        return _JSON_VALUE
    param_type = str(param.type)
    if param_type in ("integer", "number"):
        return "1"
    if param_type == "boolean":
        return "true"
    raise SampleParamError(
        f"오퍼레이션 {operation_id}의 필수 파라미터 {name!r}의 샘플값을 정할 수 "
        "없습니다. overrides로 값을 지정하세요."
    )


def _dedup(requests: Sequence[SampleRequest]) -> tuple[SampleRequest, ...]:
    """정규화 키가 같은 요청을 제거한다(S3). 입력 순서를 보존한다."""
    seen: set[tuple[str, str, tuple[tuple[str, str], ...]]] = set()
    unique: list[SampleRequest] = []
    for request in requests:
        key = (request.method.upper(), request.path, request.params)
        if key in seen:
            continue
        seen.add(key)
        unique.append(request)
    return tuple(unique)


def build_sample_requests(
    source: SourceSpec,
    operation_id: str,
    *,
    count: int = 3,
    overrides: Mapping[str, str] | None = None,
) -> tuple[SampleRequest, ...]:
    """오퍼레이션 1개에 대한 샘플 요청들을 결정론적으로 만든다.

    필수 파라미터만 싣는다(선택 파라미터를 넣으면 응답 형태가 흔들려 추론이
    불안정해진다). ``location`` 이 ``"path"`` 인 파라미터는 경로에 치환하고,
    ``"header"`` 인 파라미터는 W2 샘플러가 다루지 않으므로 건너뛴다.
    같은 요청이 반복되면 중복을 제거하므로 결과 길이는 ``count`` 이하다(S3).

    Args:
        source: 정규화된 스펙 소스.
        operation_id: 대상 오퍼레이션.
        count: 만들 샘플 수(1 이상 :data:`MAX_SAMPLES` 이하).
        overrides: 파라미터 이름 → 강제 값.

    Returns:
        중복이 제거된 :class:`SampleRequest` 튜플.

    Raises:
        ValueError: ``count`` 가 범위를 벗어나거나 operation_id 가 없을 때.
        SampleParamError: 필수 파라미터의 값을 정할 수 없을 때.
    """
    total = _check_count(count)
    operation = _find_operation(source, operation_id)
    forced = dict(overrides or {})
    key_param = str(source.key_param).lower()

    requests: list[SampleRequest] = []
    for index in range(1, total + 1):
        query: list[tuple[str, str]] = []
        path_values: dict[str, str] = {}
        for param in operation.parameters:
            if not param.required:
                continue
            name = str(param.name)
            if name.lower() == key_param:
                # 인증키는 트랜스포트가 주입한다. 샘플 요청에는 담지 않는다.
                continue
            location = str(param.location)
            if location == "header":
                continue
            value = _sample_value(
                param,
                index=index,
                overrides=forced,
                operation_id=str(operation.operation_id),
            )
            if location == "path":
                path_values[name] = value
            else:
                query.append((name, value))
        path = str(operation.path)
        for name, value in path_values.items():
            path = path.replace("{" + name + "}", value)
        requests.append(
            SampleRequest(
                operation_id=str(operation.operation_id),
                method=str(operation.method).upper(),
                path=path,
                params=tuple(sorted(query)),
            )
        )
    return _dedup(requests)


def sample_operation(
    client: httpx.Client,
    requests: Sequence[SampleRequest],
    *,
    base_url: str,
) -> tuple[SampleResult, ...]:
    """배선된 클라이언트로 샘플 요청들을 순차 실행하고 정규화 결과를 돌려준다.

    ``client`` 는 반드시 :func:`mcportal.create_client` 가 만든 '가드 배선된'
    클라이언트여야 한다(S2). ``QuotaExhausted`` 는 잡지 않고 그대로 올린다(S6).

    Args:
        client: 배선된 httpx 클라이언트.
        requests: 실행할 샘플 요청들(중복은 여기서도 한 번 더 제거한다).
        base_url: 끝에 ``/`` 가 없는 서비스 기본 URL.

    Returns:
        :class:`SampleResult` 튜플.

    Raises:
        ValueError: 요청 수가 하드캡을 넘을 때(S1).
    """
    unique = _dedup(requests)
    if len(unique) > MAX_SAMPLES:
        raise ValueError(
            f"샘플 요청 수가 하드캡 {MAX_SAMPLES}을 넘습니다: {len(unique)}건"
        )
    root = str(base_url).rstrip("/")
    results: list[SampleResult] = []
    for request in unique:
        response = client.request(
            request.method,
            f"{root}{request.path}",
            params=dict(request.params),
        )
        content = response.read()
        normalized = normalize_response(content, response.headers.get("content-type"))
        transport_ok = 200 <= response.status_code < 300
        results.append(
            SampleResult(
                operation_id=request.operation_id,
                status_code=response.status_code,
                ok=bool(normalized.ok and transport_ok),
                result_code=normalized.result_code,
                source_format=normalized.source_format,
                payload=normalized.data,
            )
        )
    return tuple(results)


def sample_source(
    source: SourceSpec,
    *,
    service_key: str | None = None,
    count: int = 3,
    mode: str = "record",
    cassette_path: PathLike | None = None,
    budget: int | None = None,
    ledger_path: PathLike | None = None,
    profile: ProviderProfile = DATA_GO_KR,
    operation_ids: Sequence[str] | None = None,
    overrides: Mapping[str, str] | None = None,
) -> dict[str, tuple[SampleResult, ...]]:
    """소스의 오퍼레이션들을 샘플링한다(operation_id → 결과들).

    - ``mode="record"``(기본): 라이브 호출 + 카세트 녹화 + 스크러빙 강제. 키 필수.
    - ``mode="replay"``: 카세트 재생. 키 불요(무키 재현 경로).
    - ``mode="live"``: 카세트 없이 라이브. 픽스처가 남지 않으므로 권장하지 않는다.

    클라이언트는 항상 :func:`mcportal.create_client` 로 만든다 → QuotaGuard 경유가
    구조적으로 강제된다(S2). ``QuotaExhausted`` 는 전파된다(S6).

    Args:
        source: 정규화된 스펙 소스.
        service_key: data.go.kr 인증키(replay 모드는 None 허용).
        count: 오퍼레이션당 샘플 수(1 이상 :data:`MAX_SAMPLES` 이하).
        mode: ``"record"`` | ``"replay"`` | ``"live"``.
        cassette_path: record/replay 카세트 경로.
        budget: 일일 예산 상한.
        ledger_path: 사용량 원장 경로.
        profile: 프로바이더 프로파일.
        operation_ids: 샘플링할 오퍼레이션 부분집합(None 이면 전부).
        overrides: 파라미터 이름 → 강제 값. :func:`build_sample_requests` 로 그대로
            내려간다. 이 통로가 없어서, 필수 파라미터의 값을 스펙에서 정할 수 없는
            오퍼레이션(``example`` · ``default`` · ``enum`` 이 전부 비고 관용 이름도
            아닌 경우)은 :class:`SampleParamError` 로 막히고 **호출자가 우회할 방법이
            없었다** — 저수준 :func:`build_sample_requests` 를 직접 쓰지 않는 한
            오케스트레이터 경로에서 값을 지정할 자리가 아예 없었다. 기본값이
            ``None`` 이라 기존 호출부의 동작은 1비트도 바뀌지 않는다.

    Returns:
        operation_id → :class:`SampleResult` 튜플.

    Raises:
        SampleKeyMissingError: record/live 인데 ``service_key`` 가 없을 때.
        ValueError: ``count`` 범위 위반, 알 수 없는 mode, record/replay 인데
            ``cassette_path`` 가 없을 때, 없는 operation_id 를 지정했을 때.
    """
    total = _check_count(count)
    if mode not in ("record", "replay", "live"):
        raise ValueError(f"알 수 없는 mode입니다: {mode!r} (live/record/replay 중 하나)")
    if mode in ("record", "live") and not service_key:
        raise SampleKeyMissingError(_KEY_MISSING_MESSAGE)
    if mode in ("record", "replay") and cassette_path is None:
        raise ValueError(f"{mode} 모드에는 cassette_path가 필요합니다.")

    if operation_ids is None:
        targets = [str(op.operation_id) for op in source.operations]
    else:
        known = {str(op.operation_id) for op in source.operations}
        unknown = [str(name) for name in operation_ids if str(name) not in known]
        if unknown:
            raise ValueError(
                "소스에 없는 operation_id를 지정했습니다: " + ", ".join(unknown)
            )
        # 순서 보존 중복 제거(S1). 중복을 그대로 두면 같은 오퍼레이션마다 샘플
        # 요청이 새로 나가 실제 상위 호출이 (중복 횟수 × count)회가 되고, 결과
        # dict 에는 마지막 count 개만 남아 호출자는 초과 소비를 볼 수조차 없다.
        # 하드캡은 "오퍼레이션당 count 회"이므로 지정 목록의 중복은 무의미하다.
        targets = list(dict.fromkeys(str(name) for name in operation_ids))

    results: dict[str, tuple[SampleResult, ...]] = {}
    client = create_client(
        service_key=service_key,
        budget=budget,
        profile=profile,
        ledger_path=ledger_path,
        mode=mode,
        cassette_path=cassette_path,
    )
    with client:
        for operation_id in targets:
            requests = build_sample_requests(
                source, operation_id, count=total, overrides=overrides
            )
            results[operation_id] = sample_operation(
                client, requests, base_url=str(source.base_url)
            )
    return results


def infer_response_schemas(
    results: Mapping[str, Sequence[SampleResult]],
    *,
    config: InferenceConfig | None = None,
) -> tuple[dict[str, dict[str, Any]], dict[str, InferenceReport]]:
    """샘플 결과를 operation_id 별로 추론기에 넘겨 스키마와 리포트를 만든다.

    ``config`` 가 None 이면 해당 오퍼레이션의 샘플 중 하나라도
    ``source_format == "xml"`` 일 때 ``xml_singleton_arrays=True`` 인 설정을,
    아니면 False 인 설정을 자동 선택한다(§6-3 R5). XML 정규화는 같은 태그가 1건일
    때 dict, 2건 이상일 때 list 를 만들기 때문에 이 보정이 없으면 같은 위치가
    ``anyOf`` 로 갈린다.

    ``ok=False``(오류 응답) 결과는 추론 입력에서 제외한다. 남은 샘플이 0개면 그
    오퍼레이션은 결과 dict 에서 빠진다(= :func:`build_openapi` 의 폴백으로 넘어간다).

    Args:
        results: operation_id → 샘플 결과들.
        config: 추론 설정(None 이면 오퍼레이션별 자동 선택).

    Returns:
        ``(operation_id → 스키마, operation_id → 리포트)`` 쌍.
    """
    schemas: dict[str, dict[str, Any]] = {}
    reports: dict[str, InferenceReport] = {}
    for operation_id in sorted(results):
        samples = list(results[operation_id])
        used = [result for result in samples if result.ok]
        usable = [dict(result.payload) for result in used]
        if not usable:
            continue
        if config is None:
            # 판정 모수는 **추론에 실제로 쓰는 표본**이다. 실패 샘플까지 세면
            # data.go.kr 게이트웨이 오류 1건(JSON 을 요청해도 XML 로 온다)이
            # 순수 JSON 오퍼레이션에 R5 XML 단수화 보정을 켜 버린다. 그러면
            # "JSON 소스에서 object/array 가 섞이면 진짜 스키마 충돌이므로
            # anyOf 로 남긴다"는 R5 규칙이 무너지고, 추론기가 만든 스키마가
            # 추론에 쓴 샘플을 거부하는 상태가 된다.
            # 혼재할 때도 False 로 간다(진짜 충돌을 anyOf 로 보존).
            is_xml = all(result.source_format == "xml" for result in used)
            effective = InferenceConfig(xml_singleton_arrays=is_xml)
        else:
            effective = config
        schema, report = infer_schema_with_report(usable, config=effective)
        schemas[operation_id] = schema
        reports[operation_id] = report
    return schemas, reports


def write_samples(
    results: Mapping[str, Sequence[SampleResult]],
    directory: PathLike,
    *,
    secrets: Sequence[str],
) -> tuple[Path, ...]:
    """샘플 페이로드를 결정론 JSON 으로 저장한다(``<operation_id>_NN.json``).

    저장 직전 :func:`scrub_text` 로 시크릿 변형(원문·quote·quote_plus 등)을 전부
    치환한다. 응답 본문이 요청 키를 되비추는(echo) 실제 사례가 있으므로, 녹화
    계층과 별개로 여기서도 방어한다(S7).

    ``secrets`` 는 **키워드 필수 인자다**(기본값이 없다). 기본값 ``()`` 를 두면
    문서가 안내하는 기본 호출이 곧 무방비가 되어 "2중 방어"의 두 번째 층이
    코드가 아니라 주장으로만 남는다 — 실제로 그 상태에서 인증키 평문이 커밋
    대상 샘플 파일에 남았다. 무키 경로(합성 픽스처·replay)에서는 ``secrets=[]``
    를 **명시**해 "지울 시크릿이 없음"을 호출자가 선언하게 한다.

    파일은 UTF-8(BOM 없음) · LF · 끝 개행 1개 · ``sort_keys=True`` · 들여쓰기 2칸이다.

    Args:
        results: operation_id → 샘플 결과들.
        directory: 저장 디렉터리(자동 생성).
        secrets: 치환할 시크릿 원문들(무키 경로면 빈 시퀀스를 명시).

    Returns:
        저장된 파일 경로 튜플(operation_id·순번 오름차순).
    """
    root = Path(directory)
    root.mkdir(parents=True, exist_ok=True)
    secret_list = [str(secret) for secret in secrets if secret]
    written: list[Path] = []
    for operation_id in sorted(results):
        for index, result in enumerate(results[operation_id], start=1):
            text = json.dumps(
                dict(result.payload), ensure_ascii=False, indent=2, sort_keys=True
            )
            text = scrub_text(text, secret_list) + "\n"
            target = root / f"{operation_id}_{index:02d}.json"
            with open(target, "w", encoding="utf-8", newline="\n") as handle:
                handle.write(text)
            written.append(target)
    return tuple(written)


def compile_with_sampling(
    source: SourceSpec,
    *,
    options: CompileOptions = DEFAULT_OPTIONS,
    **sample_kwargs: Any,
) -> tuple[CompiledSpec, dict[str, tuple[SampleResult, ...]]]:
    """:func:`sample_source` → :func:`infer_response_schemas` → :func:`build_openapi`.

    ``options.generation_mode`` 는 ``"sampled"`` 로 덮어쓴다(산출물이 라이브 표본에
    근거했다는 사실을 문서에 남기기 위해).

    Args:
        source: 정규화된 스펙 소스.
        options: 컴파일 옵션(generation_mode 는 무시되고 "sampled"가 된다).
        **sample_kwargs: :func:`sample_source` 에 그대로 전달되는 인자들.

    Returns:
        ``(CompiledSpec, operation_id → 샘플 결과들)`` 쌍.
    """
    results = sample_source(source, **sample_kwargs)
    schemas, reports = infer_response_schemas(results)
    sampled_options = replace(options, generation_mode="sampled")
    compiled = build_openapi(
        source, schemas, options=sampled_options, reports=reports
    )
    return compiled, results
