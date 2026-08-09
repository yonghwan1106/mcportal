# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Yong Park
"""스펙 정규화 컴파일러 공개 API.

파이프라인은 다섯 단계로 갈린다.

1. :mod:`~mcportal.compiler.sources` — data.go.kr 의 세 가지 스펙 제공 방식
   (odcloud OAS · 게이트웨이 Swagger · 활용가이드 수동 기술서)과 목록조회 메타를
   단일 중간표현 ``SourceSpec`` 으로 흡수한다.
2. :mod:`~mcportal.compiler.curation` — 자동생성 ``SourceSpec`` 위에 사람이 확인한
   설명·예시·힌트를 얹는다(2층 구조의 위층). 도메인 지식은 전부
   ``presets/<id>/curation.json`` 데이터에 있고 이 모듈은 그것을 읽고 검증하고
   병합하는 일반 엔진이다.
3. :mod:`~mcportal.compiler.inference` — 정규화 응답 샘플 3~5개에서 JSON Schema 를
   추론한다(오프라인·결정론·입력 순서 무관).
4. :mod:`~mcportal.compiler.openapi` — ``SourceSpec``(+추론 스키마)을 OpenAPI 3.1
   문서로 컴파일하고 결정론 직렬화한다.
5. :mod:`~mcportal.compiler.sampler` — 라이브 샘플링을 쿼터가드 경유로 오케스트레이션
   한다(하드캡 5회·스크러빙 강제).

여기서 재수출하는 이름들은 :mod:`mcportal` 최상위가 그대로 다시 올린다. 이름을
바꾸면 최상위 공개 API 가 함께 흔들리므로 철자를 고정한다. 재수출 목록은 "사용자가
실제로 조립하는 것"으로 한정한다 — 진단용 헬퍼·타입 별칭·내부 기본값은 원 모듈에서
직접 임포트한다(예: ``from mcportal.compiler.curation import check_preset``).
"""

from __future__ import annotations

from .curation import (
    CURATION_SCHEMA_VERSION,
    Curation,
    CurationError,
    CurationReport,
    OperationCuration,
    ParamCuration,
    PresetInfo,
    ServiceCuration,
    apply_curation,
    compile_preset,
    default_presets_root,
    iter_presets,
    load_curation,
    load_preset,
    read_curation,
    write_preset,
)
from .inference import (
    InferenceConfig,
    InferenceError,
    InferenceReport,
    TypeConflict,
    infer_schema,
    infer_schema_with_report,
)
from .openapi import (
    CompiledSpec,
    CompileError,
    CompileOptions,
    build_openapi,
    dumps,
    write_spec,
)
from .sampler import (
    MAX_SAMPLES,
    SampleRequest,
    SampleResult,
    SamplingError,
    build_sample_requests,
    compile_with_sampling,
    infer_response_schemas,
    sample_source,
)
from .sources import (
    CatalogEntry,
    OperationSpec,
    ParamSpec,
    SourceKind,
    SourceSpec,
    SourceSpecError,
    fingerprint_document,
    load_source,
)

__all__ = [
    # 중간표현(sources)
    "CatalogEntry",
    "OperationSpec",
    "ParamSpec",
    "SourceKind",
    "SourceSpec",
    "SourceSpecError",
    "fingerprint_document",
    "load_source",
    # 큐레이션 오버레이(curation)
    "CURATION_SCHEMA_VERSION",
    "Curation",
    "CurationError",
    "CurationReport",
    "OperationCuration",
    "ParamCuration",
    "PresetInfo",
    "ServiceCuration",
    "apply_curation",
    "compile_preset",
    "default_presets_root",
    "iter_presets",
    "load_curation",
    "load_preset",
    "read_curation",
    "write_preset",
    # 스키마 추론(inference)
    "InferenceConfig",
    "InferenceError",
    "InferenceReport",
    "TypeConflict",
    "infer_schema",
    "infer_schema_with_report",
    # OpenAPI 산출(openapi)
    "CompileError",
    "CompileOptions",
    "CompiledSpec",
    "build_openapi",
    "dumps",
    "write_spec",
    # 라이브 샘플링(sampler)
    "MAX_SAMPLES",
    "SampleRequest",
    "SampleResult",
    "SamplingError",
    "build_sample_requests",
    "compile_with_sampling",
    "infer_response_schemas",
    "sample_source",
]
