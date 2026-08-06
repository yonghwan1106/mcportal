# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Yong Park
"""MCPortal 공개 API.

data.go.kr(공공데이터포털) 오픈API를 MCP 생태계로 잇는 쿼터 가드 런타임 계층.
계층은 셋으로 갈린다.

1. **런타임(W1)** — 전송 통합 트랜스포트와 각 서브패키지
   (:mod:`~mcportal.quota` / :mod:`~mcportal.runtime` / :mod:`~mcportal.replay` /
   :mod:`~mcportal.profiles`)가 인증키 주입·쿼터 하드 가드·정규화·record/replay를
   맡는다.
2. **스펙 정규화 컴파일러(W2)** — :mod:`mcportal.compiler` 가 data.go.kr의 세 가지
   스펙 제공 방식과 목록조회 메타를 단일 중간표현으로 흡수해 OpenAPI 3.1로
   결정론 산출한다. 여기서 재수출하는 이름은 :mod:`mcportal.compiler` 의 공개
   목록과 철자·개수가 같다.
3. **MCP 변환(W2)** — :mod:`mcportal.mcp` 가 산출된 OpenAPI 문서를 fastmcp에
   위임해 MCP 서버로 만든다.

**임포트 정책**: 코어 런타임의 의존성은 httpx 단일이고 fastmcp는 선택적 추가
의존성(``[mcp]`` extra)이다. 그래서 ``import mcportal`` 은 :mod:`mcportal.mcp` 를
임포트하지 않는다. MCP 변환 심볼은 :pep:`562` 모듈 ``__getattr__`` 로 **처음
참조하는 시점에** 지연 해석되므로, ``mcportal.build_server`` 와
``from mcportal.mcp import build_server`` 가 같은 객체를 가리키면서도 fastmcp
미설치 환경에서 ``import mcportal`` 이 그대로 성공한다(fastmcp 자체는 그보다 더
늦게, 실제 변환을 호출하는 시점에만 필요하다).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from .compiler import (
    MAX_SAMPLES,
    CatalogEntry,
    CompiledSpec,
    CompileError,
    CompileOptions,
    InferenceConfig,
    InferenceError,
    InferenceReport,
    OperationSpec,
    ParamSpec,
    SampleRequest,
    SampleResult,
    SamplingError,
    SourceKind,
    SourceSpec,
    SourceSpecError,
    TypeConflict,
    build_openapi,
    build_sample_requests,
    compile_with_sampling,
    dumps,
    fingerprint_document,
    infer_response_schemas,
    infer_schema,
    infer_schema_with_report,
    load_source,
    sample_source,
    write_spec,
)
from .profiles import (
    DATA_GO_KR,
    MultiKeyUnsupportedError,
    ProviderProfile,
    validate_key_registration,
)
from .quota import (
    DailyBudget,
    QuotaExhausted,
    QuotaGuard,
    TokenBucket,
    UsageLedger,
    compute_delay,
)
from .replay import (
    Cassette,
    CassetteMissError,
    RecordingTransport,
    ReplayTransport,
)
from .runtime import (
    NormalizedResponse,
    TTLCache,
    inject_service_key,
    map_result_code,
    normalize_response,
    prepare_service_key,
)
from .transport import MCPortalTransport, create_client

if TYPE_CHECKING:  # pragma: no cover - 정적 분석 전용(런타임 임포트 아님)
    from .mcp import (
        FASTMCP_IMPORT_HINT,
        FASTMCP_REQUIREMENT,
        MCPortalAsyncTransport,
        build_async_client,
        build_server,
        fastmcp_version,
        require_fastmcp,
        server_from_spec,
    )

__version__ = "0.1.0"

#: 지연 해석 대상 — :mod:`mcportal.mcp` 의 공개 심볼. ``import mcportal`` 시점에
#: 임포트하지 않고, 처음 참조될 때 :func:`__getattr__` 가 끌어온다.
_MCP_EXPORTS: frozenset[str] = frozenset(
    {
        "FASTMCP_IMPORT_HINT",
        "FASTMCP_REQUIREMENT",
        "MCPortalAsyncTransport",
        "build_async_client",
        "build_server",
        "fastmcp_version",
        "require_fastmcp",
        "server_from_spec",
    }
)


def __getattr__(name: str) -> Any:
    """MCP 변환 심볼을 처음 참조하는 시점에 지연 임포트한다(:pep:`562`).

    :mod:`mcportal.mcp` 는 모듈 최상단에서 fastmcp를 임포트하지 않으므로 이
    해석 자체는 fastmcp 없이도 성공한다. fastmcp가 실제로 필요한 시점은 변환
    함수를 호출할 때이며, 그때 한국어 안내를 담은 ``ImportError`` 로 막힌다.

    Args:
        name: 참조된 속성 이름.

    Returns:
        해당 심볼. 한 번 해석하면 모듈 전역에 캐시된다.

    Raises:
        AttributeError: 재수출 대상이 아닌 이름일 때.
    """
    if name in _MCP_EXPORTS:
        from . import mcp as _mcp

        value = getattr(_mcp, name)
        globals()[name] = value
        return value
    raise AttributeError(f"모듈 {__name__!r} 에 {name!r} 속성이 없습니다.")


def __dir__() -> list[str]:
    """지연 해석 심볼까지 포함한 속성 목록을 돌려준다(자동완성·``dir()`` 대응)."""
    return sorted(set(globals()) | _MCP_EXPORTS)


__all__ = [
    "__version__",
    # 전송 통합 계층
    "MCPortalTransport",
    "create_client",
    # quota
    "QuotaGuard",
    "QuotaExhausted",
    "DailyBudget",
    "TokenBucket",
    "UsageLedger",
    "compute_delay",
    # runtime
    "prepare_service_key",
    "inject_service_key",
    "map_result_code",
    "normalize_response",
    "NormalizedResponse",
    "TTLCache",
    # replay
    "Cassette",
    "ReplayTransport",
    "RecordingTransport",
    "CassetteMissError",
    # profiles
    "ProviderProfile",
    "DATA_GO_KR",
    "MultiKeyUnsupportedError",
    "validate_key_registration",
    # compiler — 중간표현(sources)
    "CatalogEntry",
    "OperationSpec",
    "ParamSpec",
    "SourceKind",
    "SourceSpec",
    "SourceSpecError",
    "fingerprint_document",
    "load_source",
    # compiler — 스키마 추론(inference)
    "InferenceConfig",
    "InferenceError",
    "InferenceReport",
    "TypeConflict",
    "infer_schema",
    "infer_schema_with_report",
    # compiler — OpenAPI 산출(openapi)
    "CompileError",
    "CompileOptions",
    "CompiledSpec",
    "build_openapi",
    "dumps",
    "write_spec",
    # compiler — 라이브 샘플링(sampler)
    "MAX_SAMPLES",
    "SampleRequest",
    "SampleResult",
    "SamplingError",
    "build_sample_requests",
    "compile_with_sampling",
    "infer_response_schemas",
    "sample_source",
    # mcp — 지연 해석(fastmcp는 선택적 추가 의존성)
    "FASTMCP_IMPORT_HINT",
    "FASTMCP_REQUIREMENT",
    "MCPortalAsyncTransport",
    "build_async_client",
    "build_server",
    "fastmcp_version",
    "require_fastmcp",
    "server_from_spec",
]
