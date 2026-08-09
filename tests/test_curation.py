# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Yong Park
"""compiler.curation 테스트: 오버레이 스키마 검증 · 병합 결정론 · 프리셋 번들 로딩
· 실키 샘플링 글루(W4 §3-2).

픽스처는 100% 합성이다. 가상 기관·가상 데이터셋 ID·``.invalid`` 도메인만 쓰며
실인증키·실데이터·실네트워크는 어디에도 없다. 병합·로딩 검증은 HTTP 를 하지
않고, 샘플링 글루 검증만 respx 로 **가짜 게이트웨이**를 세워 record 모드 경로를
끝까지 돌린다(``.invalid`` 는 RFC 2606 예약 TLD 라 모킹이 빠져도 실호출이 나갈
수 없다). 실제 프리셋 번들을 쓰는 검증은 ``tests/test_presets.py`` 가 담당한다.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
from pathlib import Path
from typing import Any
from urllib.parse import quote, quote_plus

import httpx
import pytest
import respx

from mcportal.compiler.curation import (
    CURATION_SCHEMA_VERSION,
    Curation,
    CurationError,
    OperationCuration,
    ParamCuration,
    ParamRemoval,
    PRESET_SAMPLED_FILENAME,
    PRESET_SOURCE_SCHEMA_VERSION,
    PresetSampleReport,
    ResponseCuration,
    SAMPLED_SCHEMA_VERSION,
    SampledInference,
    ServiceCuration,
    apply_curation,
    apply_curation_with_report,
    apply_sampled_schemas,
    check_preset,
    compile_preset,
    default_presets_root,
    iter_presets,
    load_curation,
    load_preset,
    load_sampled_schemas,
    preset_info,
    read_curation,
    read_preset_source,
    read_sampled_schemas,
    sample_preset,
    validate_curation,
    write_preset,
    write_sampled_schemas,
)
from mcportal.compiler.openapi import X_MCPORTAL
from mcportal.compiler.sampler import SampleParamError, SampleResult
from mcportal.compiler import curation as curation_module
from mcportal.compiler.openapi import dumps
from mcportal.compiler.sources import (
    OperationSpec,
    ParamSpec,
    SourceKind,
    SourceSpec,
    unresolved_schema_operations,
)

SYNTHETIC_ID = "00000000"


# ---------------------------------------------------------------------------
# 합성 픽스처
# ---------------------------------------------------------------------------
def _document(*, description: str | None = None) -> dict[str, Any]:
    """합성 게이트웨이 Swagger 2.0 문서(가상 기관 · .invalid 도메인)."""
    info: dict[str, Any] = {"title": "가상 통계 서비스"}
    if description is not None:
        info["description"] = description
    return {
        "swagger": "2.0",
        "info": info,
        "host": "apis.example.invalid",
        "basePath": "/0000000/demoStats",
        "schemes": ["https"],
        "paths": {
            "/getDemoList": {
                "get": {
                    "operationId": "getDemoList",
                    "summary": "합성 목록 조회",
                    "description": "원 문서가 준 설명.",
                    "tags": ["원본태그"],
                    "produces": ["application/json"],
                    "parameters": [
                        {
                            "name": "serviceKey",
                            "in": "query",
                            "type": "string",
                            "required": True,
                        },
                        {
                            "name": "startYm",
                            "in": "query",
                            "type": "string",
                            "required": True,
                            "description": "원 문서 시작 표기.",
                            "example": "202601",
                        },
                        {
                            "name": "endYm",
                            "in": "query",
                            "type": "string",
                            "required": True,
                        },
                        {
                            "name": "regionCode",
                            "in": "query",
                            "type": "string",
                            "required": False,
                            "enum": ["A", "B"],
                            "default": "A",
                        },
                        {
                            "name": "unit",
                            "in": "query",
                            "type": "string",
                            "required": False,
                        },
                    ],
                    "responses": {
                        "200": {
                            "description": "정상",
                            "schema": {
                                "type": "object",
                                "properties": {"total": {"type": "integer"}},
                            },
                        }
                    },
                }
            }
        },
    }


def _wrapper(
    preset_id: str = SYNTHETIC_ID, *, description: str | None = None
) -> dict[str, Any]:
    """합성 ``source.json`` 래퍼."""
    return {
        "mcportal_preset_source": PRESET_SOURCE_SCHEMA_VERSION,
        "preset_id": preset_id,
        "service_id": preset_id,
        "service_name": "가상 통계 서비스",
        "source_kind": "gw_swagger",
        "key_param": "serviceKey",
        "source_url": "https://portal.example.invalid/data/00000000/openapi.do",
        "fetched_at": "2026-08-06T00:00:00+09:00",
        "license_note": "가상 이용허락범위(합성 픽스처)",
        "provenance": {
            "spec_origin": "합성 픽스처",
            "spec_url": "https://portal.example.invalid/data/00000000/openapi.do",
            "raw_files": [],
            "raw_sha256": "sha256:0" * 1,
            "acquisition": "합성. 네트워크 호출 0회.",
            "personal_data_scan": "합성 픽스처이므로 개인정보 0건.",
        },
        "document": _document(description=description),
    }


def _curation_document(preset_id: str = SYNTHETIC_ID, **overrides: Any) -> dict[str, Any]:
    """합성 ``curation.json`` 문서."""
    document: dict[str, Any] = {
        "mcportal_curation": CURATION_SCHEMA_VERSION,
        "preset_id": preset_id,
        "service": {
            "group": "가상 묶음",
            "title": "가상 통계(큐레이션 제목)",
            "version": "1.2.3",
            "description": "사람이 확인한 서비스 설명.",
            "license_note": "가상 이용허락범위(큐레이션)",
            "source_url": "https://portal.example.invalid/data/00000000/openapi.do",
            "notes": ["가상 메모 1", "가상 메모 2"],
        },
        "operations": {
            "getDemoList": {
                "summary": "큐레이션 요약",
                "description": "큐레이션 설명 본문.",
                "tags": ["큐레이션태그"],
                "example_prompts": ["첫 번째 예시 질문", "두 번째 예시 질문"],
                "parameters": {
                    "startYm": {
                        "description": "큐레이션 시작 표기.",
                        "example": "202512",
                    },
                    "regionCode": {
                        "description": "지역 구분.",
                        "default": "B",
                        "enum": ["B", "A", "C"],
                        "enum_note": "대표값은 A와 B다.",
                    },
                },
            }
        },
    }
    document.update(overrides)
    return document


def _bundle(
    tmp_path: Path,
    *,
    preset_id: str = SYNTHETIC_ID,
    wrapper: dict[str, Any] | None = None,
    curation: dict[str, Any] | None = None,
    directory_name: str | None = None,
) -> Path:
    """tmp_path에 합성 프리셋 번들 1개를 만든다."""
    directory = tmp_path / (directory_name or preset_id)
    directory.mkdir(parents=True, exist_ok=True)
    _write_json(directory / "source.json", wrapper or _wrapper(preset_id))
    if curation is not None:
        _write_json(directory / "curation.json", curation)
    return directory


def _write_json(path: Path, payload: Any) -> None:
    """산출 규약(UTF-8 · LF · sort_keys · 끝 개행 1개)대로 쓴다."""
    text = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)


def _source_spec(
    *, description: str | None = None, license_note: str | None = None
) -> SourceSpec:
    """합성 SourceSpec(문서를 거치지 않는 직접 조립)."""
    return SourceSpec(
        provider="portal.example.invalid",
        service_id=SYNTHETIC_ID,
        service_name="가상 통계 서비스",
        base_url="https://apis.example.invalid/0000000/demoStats",
        source_kind=SourceKind.GW_SWAGGER,
        operations=(
            OperationSpec(
                operation_id="getDemoList",
                method="GET",
                path="/getDemoList",
                summary="합성 목록 조회",
                description="원 문서가 준 설명.",
                parameters=(
                    ParamSpec(name="startYm", location="query", required=True, type="string"),
                    ParamSpec(name="regionCode", location="query", required=False, type="string"),
                ),
                response_schema={"type": "object", "properties": {"total": {"type": "integer"}}},
            ),
        ),
        key_param="serviceKey",
        source_url="https://portal.example.invalid/data/00000000/openapi.do",
        fingerprint="sha256:" + "0" * 64,
        description=description,
        license_note=license_note,
    )


def _reverse_keys(value: Any) -> Any:
    """dict 키 순서를 재귀적으로 뒤집는다(순서 비의존 검증용)."""
    if isinstance(value, dict):
        return {key: _reverse_keys(item) for key, item in reversed(list(value.items()))}
    if isinstance(value, list):
        return [_reverse_keys(item) for item in value]
    return value


def _params(operation: OperationSpec) -> dict[str, ParamSpec]:
    """파라미터를 이름으로 인덱싱한다."""
    return {param.name: param for param in operation.parameters}


# ---------------------------------------------------------------------------
# 1. 파싱
# ---------------------------------------------------------------------------
def test_minimal_curation_parses_with_defaults() -> None:
    """최소 문서도 읽히고, 생략된 필드는 규약 기본값이 된다."""
    curation = load_curation(
        {"mcportal_curation": 1, "preset_id": SYNTHETIC_ID, "service": {}}
    )
    assert isinstance(curation, Curation)
    assert curation.preset_id == SYNTHETIC_ID
    assert curation.operations == {}
    service = curation.service
    assert isinstance(service, ServiceCuration)
    assert service.version == "0.1.0"
    assert (service.group, service.title, service.description) == (None, None, None)
    assert (service.license_note, service.source_url, service.notes) == (None, None, ())


def test_full_curation_parses_every_field() -> None:
    """전 필드를 채운 문서가 자료형 전수로 옮겨진다."""
    curation = load_curation(
        _curation_document(
            operations={
                "getDemoList": {
                    "summary": "큐레이션 요약",
                    "description": "큐레이션 설명.",
                    "tags": ["가", "나", "가"],
                    "example_prompts": ["프롬프트 하나"],
                    "response": {"unresolved": True, "reason": "합성 근거."},
                    "parameters": {
                        "regionCode": {
                            "description": "설명",
                            "example": "B",
                            "default": "A",
                            "enum": ["B", "A"],
                            "enum_note": "안내 문장.",
                        }
                    },
                    "parameters_remove": [{"name": "unit", "reason": "합성 근거."}],
                }
            }
        )
    )
    service = curation.service
    assert (service.group, service.title, service.version) == (
        "가상 묶음",
        "가상 통계(큐레이션 제목)",
        "1.2.3",
    )
    assert service.notes == ("가상 메모 1", "가상 메모 2")

    operation = curation.operations["getDemoList"]
    assert isinstance(operation, OperationCuration)
    assert operation.tags == ("가", "나")  # 원 순서 보존 + 중복 제거
    assert operation.example_prompts == ("프롬프트 하나",)
    assert operation.response == ResponseCuration(unresolved=True, reason="합성 근거.")
    assert operation.parameters_remove == (
        ParamRemoval(name="unit", reason="합성 근거."),
    )
    param = operation.parameters["regionCode"]
    assert isinstance(param, ParamCuration)
    assert (param.example, param.default, param.enum, param.enum_note) == (
        "B",
        "A",
        ("B", "A"),
        "안내 문장.",
    )


# ---------------------------------------------------------------------------
# 2. V1 — 스키마 버전
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("version", [2, "1", None, 0])
def test_v1_schema_version_mismatch(version: Any) -> None:
    """지원하지 않는 버전은 받은 값과 지원 버전을 함께 알려 준다."""
    document: dict[str, Any] = {"preset_id": SYNTHETIC_ID, "service": {}}
    if version is not None:
        document["mcportal_curation"] = version
    with pytest.raises(CurationError) as excinfo:
        load_curation(document)
    message = str(excinfo.value)
    assert repr(version) in message
    assert str(CURATION_SCHEMA_VERSION) in message


# ---------------------------------------------------------------------------
# 3. V2 — 미지의 키(4계층)
# ---------------------------------------------------------------------------
def test_v2_unknown_keys_rejected_at_every_layer() -> None:
    """최상위·service·operations[*]·parameters[*] 네 계층 전부에서 오타를 잡는다."""
    cases = [
        ("curation", lambda doc: doc.update({"oprations": {}})),
        ("curation.service", lambda doc: doc["service"].update({"titel": "오타"})),
        (
            "curation.operations.getDemoList",
            lambda doc: doc["operations"]["getDemoList"].update({"sumary": "오타"}),
        ),
        (
            "curation.operations.getDemoList.parameters.startYm",
            lambda doc: doc["operations"]["getDemoList"]["parameters"]["startYm"].update(
                {"exmaple": "오타"}
            ),
        ),
    ]
    for path, mutate in cases:
        document = _curation_document()
        mutate(document)
        with pytest.raises(CurationError) as excinfo:
            load_curation(document)
        message = str(excinfo.value)
        assert path in message
        # 문제 키 이름과 그 계층의 허용 키 목록이 함께 있어야 한다.
        assert "오타" not in message  # 값이 아니라 키 이름을 보여 준다
        assert "허용되는 키" in message


def test_v2_message_lists_allowed_keys_sorted() -> None:
    """허용 키 목록은 정렬되어 나온다."""
    document = _curation_document()
    document["service"]["nots"] = []
    with pytest.raises(CurationError) as excinfo:
        load_curation(document)
    message = str(excinfo.value)
    assert "nots" in message
    listed = message.split("허용되는 키:")[1]
    names = [item.strip().rstrip(".") for item in listed.split(",")]
    assert names == sorted(names)
    assert "source_url" in names


# ---------------------------------------------------------------------------
# 4. V3 — preset_id 3자 일치
# ---------------------------------------------------------------------------
def test_v3_preset_id_must_match_directory_and_source(tmp_path: Path) -> None:
    """디렉터리명 · source.json · curation.json 세 값이 모두 같아야 한다."""
    # (a) 디렉터리명 != source.json.preset_id
    directory = _bundle(tmp_path / "a", preset_id="11111111", directory_name="22222222")
    with pytest.raises(CurationError) as excinfo:
        load_preset(directory)
    message = str(excinfo.value)
    assert "22222222" in message and "11111111" in message

    # (b) source.json.preset_id != curation.json.preset_id
    directory = _bundle(
        tmp_path / "b",
        preset_id=SYNTHETIC_ID,
        curation=_curation_document("33333333"),
    )
    with pytest.raises(CurationError) as excinfo:
        load_preset(directory)
    message = str(excinfo.value)
    assert SYNTHETIC_ID in message
    assert "33333333" in message
    assert "curation.json" in message


# ---------------------------------------------------------------------------
# 5·6·7. V4·V5·V6 — 참조 무결성
# ---------------------------------------------------------------------------
def test_v4_unknown_operation_id_lists_available_ids() -> None:
    """없는 operation_id를 참조하면 실제 id 목록을 알려 준다."""
    source = _source_spec()
    curation = load_curation(
        {
            "mcportal_curation": 1,
            "preset_id": SYNTHETIC_ID,
            "service": {},
            "operations": {"getDemoDetail": {"summary": "없는 오퍼레이션"}},
        }
    )
    with pytest.raises(CurationError) as excinfo:
        validate_curation(curation, source)
    message = str(excinfo.value)
    assert "getDemoDetail" in message
    assert "getDemoList" in message


def test_v5_unknown_parameter_lists_actual_parameters() -> None:
    """parameters·parameters_remove 양쪽 모두 실제 파라미터 목록을 알려 준다."""
    source = _source_spec()
    for block, where in (
        ({"parameters": {"nope": {"description": "x"}}}, "parameters"),
        (
            {"parameters_remove": [{"name": "nope", "reason": "근거."}]},
            "parameters_remove",
        ),
    ):
        curation = load_curation(
            {
                "mcportal_curation": 1,
                "preset_id": SYNTHETIC_ID,
                "service": {},
                "operations": {"getDemoList": block},
            }
        )
        with pytest.raises(CurationError) as excinfo:
            validate_curation(curation, source)
        message = str(excinfo.value)
        assert "nope" in message
        assert where in message
        assert "regionCode" in message and "startYm" in message


def test_v6_key_param_cannot_be_used_as_parameter_key() -> None:
    """인증키 이름을 파라미터 키로 쓰면 I3 위반을 명시하며 막는다."""
    source = _source_spec()
    curation = load_curation(
        {
            "mcportal_curation": 1,
            "preset_id": SYNTHETIC_ID,
            "service": {},
            "operations": {
                "getDemoList": {"parameters": {"SERVICEKEY": {"description": "x"}}}
            },
        }
    )
    with pytest.raises(CurationError) as excinfo:
        validate_curation(curation, source)
    message = str(excinfo.value)
    assert "I3" in message
    assert "트랜스포트" in message


# ---------------------------------------------------------------------------
# 8·9. V7·V8 — 인증키 대입
# ---------------------------------------------------------------------------
def test_v7_key_assignment_in_curation_text_is_rejected() -> None:
    """큐레이션 자유문자열의 인증키 대입은 필드 경로와 함께 차단된다."""
    document = _curation_document()
    document["service"]["description"] = (
        "호출 예시: https://apis.example.invalid/x?serviceKey=abc&startYm=202601"
    )
    with pytest.raises(CurationError) as excinfo:
        load_curation(document)
    message = str(excinfo.value)
    assert "serviceKey" in message
    assert "curation.service.description" in message


def test_v7_scans_nested_strings_including_prompts() -> None:
    """예시 프롬프트·메모 같은 중첩 문자열도 훑는다."""
    document = _curation_document()
    document["operations"]["getDemoList"]["example_prompts"] = [
        "이 주소로 불러줘 https://apis.example.invalid/x?serviceKey=zzz"
    ]
    with pytest.raises(CurationError) as excinfo:
        load_curation(document)
    assert "example_prompts[0]" in str(excinfo.value)


def test_v8_merged_description_still_carrying_key_is_blocked() -> None:
    """병합 후에도 인증키 대입이 남으면 교체할 큐레이션 필드를 알려 준다."""
    source = _source_spec(
        description="공식 안내: .../getDemoList?serviceKey=[서비스키]&startYm=202601"
    )
    curation = load_curation(
        {"mcportal_curation": 1, "preset_id": SYNTHETIC_ID, "service": {}}
    )
    with pytest.raises(CurationError) as excinfo:
        apply_curation(source, curation)
    message = str(excinfo.value)
    assert "service.description" in message
    assert "교체" in message

    # 큐레이션이 교체하면 통과한다(F-01 처방).
    fixed = load_curation(
        {
            "mcportal_curation": 1,
            "preset_id": SYNTHETIC_ID,
            "service": {"description": "사람이 확인한 요약 설명."},
        }
    )
    merged = apply_curation(source, fixed)
    assert merged.description == "사람이 확인한 요약 설명."


def test_v8_also_guards_license_note_and_source_url() -> None:
    """설명 말고 라이선스 메모·스펙 URL도 같은 게이트를 통과해야 한다."""
    source = _source_spec(license_note="근거 URL: https://x.invalid/?serviceKey=abc")
    curation = load_curation(
        {"mcportal_curation": 1, "preset_id": SYNTHETIC_ID, "service": {}}
    )
    with pytest.raises(CurationError, match="license_note"):
        apply_curation(source, curation)


# ---------------------------------------------------------------------------
# 10·11·12·13. V9·V10·V11·V12
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("layer", "field"),
    [
        ("operation", "type"),
        ("operation", "required"),
        ("operation", "response_schema"),
        ("operation", "path"),
        ("operation", "method"),
        ("operation", "operation_id"),
        ("param", "type"),
        ("param", "required"),
        ("param", "location"),
        ("param", "item_type"),
    ],
)
def test_v9_forbidden_fields_are_rejected(layer: str, field: str) -> None:
    """스펙 사실을 바꾸는 필드는 어느 계층에 있어도 막는다."""
    document = _curation_document()
    target = document["operations"]["getDemoList"]
    if layer == "param":
        target = target["parameters"]["startYm"]
    target[field] = "무엇이든"
    with pytest.raises(CurationError) as excinfo:
        load_curation(document)
    message = str(excinfo.value)
    assert field in message
    assert "큐레이션은 스펙 사실을 바꾸지 않습니다" in message


def test_v10_reason_is_required_for_both_fact_corrections() -> None:
    """응답 강등·파라미터 제거 둘 다 근거 없이는 통과하지 못한다."""
    document = _curation_document()
    document["operations"]["getDemoList"]["response"] = {"unresolved": True}
    with pytest.raises(CurationError) as excinfo:
        load_curation(document)
    assert "근거를 남기세요" in str(excinfo.value)

    document = _curation_document()
    document["operations"]["getDemoList"]["parameters_remove"] = [{"name": "unit"}]
    with pytest.raises(CurationError) as excinfo:
        load_curation(document)
    assert "근거를 남기세요" in str(excinfo.value)

    # unresolved=false 면 근거가 없어도 된다(사실을 바꾸지 않으므로).
    document = _curation_document()
    document["operations"]["getDemoList"]["response"] = {"unresolved": False}
    assert load_curation(document).operations["getDemoList"].response is not None


def test_v11_required_parameter_cannot_be_removed() -> None:
    """필수 파라미터 제거는 스펙 사실 변경이므로 막고 재취득을 안내한다."""
    source = _source_spec()
    curation = load_curation(
        {
            "mcportal_curation": 1,
            "preset_id": SYNTHETIC_ID,
            "service": {},
            "operations": {
                "getDemoList": {
                    "parameters_remove": [{"name": "startYm", "reason": "근거."}]
                }
            },
        }
    )
    with pytest.raises(CurationError) as excinfo:
        validate_curation(curation, source)
    message = str(excinfo.value)
    assert "startYm" in message
    assert "재취득" in message


@pytest.mark.parametrize(
    ("mutate", "expected"),
    [
        (lambda doc: doc["service"].update({"title": 123}), "문자열"),
        (lambda doc: doc["service"].update({"notes": "문자열이 아니라 배열"}), "배열"),
        (lambda doc: doc.update({"operations": []}), "객체"),
        (
            lambda doc: doc["operations"]["getDemoList"]["parameters"]["startYm"].update(
                {"example": 10}
            ),
            "문자열",
        ),
        (
            lambda doc: doc["operations"]["getDemoList"].update({"tags": "단일 문자열"}),
            "배열",
        ),
    ],
)
def test_v12_type_mismatch_reports_path_and_expected_type(
    mutate: Any, expected: str
) -> None:
    """타입 불일치는 필드 경로·기대 타입·받은 타입을 함께 알린다."""
    document = _curation_document()
    mutate(document)
    with pytest.raises(CurationError) as excinfo:
        load_curation(document)
    message = str(excinfo.value)
    assert "curation" in message
    assert expected in message
    assert "받은 타입" in message


def test_v12_version_format_is_enforced() -> None:
    """service.version 은 세 자리 숫자 형태여야 한다."""
    document = _curation_document()
    document["service"]["version"] = "1.2"
    with pytest.raises(CurationError, match="version"):
        load_curation(document)


# ---------------------------------------------------------------------------
# 14~18. 병합
# ---------------------------------------------------------------------------
def _merged_operation(**overrides: Any) -> tuple[SourceSpec, OperationSpec]:
    """합성 번들을 병합해 오퍼레이션 1개를 돌려준다."""
    source = _source_spec()
    document = _curation_document(**overrides)
    merged = apply_curation(source, load_curation(document))
    return merged, merged.operations[0]


def test_merge_priority_replaces_text_but_keeps_facts() -> None:
    """설명·예시·기본값·열거값은 큐레이션이 이기고, 타입·필수·위치는 소스가 이긴다."""
    source = _source_spec()
    merged = apply_curation(source, load_curation(_curation_document()))
    operation = merged.operations[0]

    assert operation.summary == "큐레이션 요약"
    assert operation.description.startswith("큐레이션 설명 본문.")
    assert operation.tags == ("큐레이션태그",)

    params = _params(operation)
    assert params["startYm"].description == "큐레이션 시작 표기."
    assert params["startYm"].example == "202512"
    assert params["regionCode"].default == "B"
    assert params["regionCode"].enum == ("B", "A", "C")  # 원 순서 보존

    # 사실은 그대로.
    assert params["startYm"].required is True
    assert params["startYm"].type == "string"
    assert params["startYm"].location == "query"
    assert params["regionCode"].required is False

    # 서비스 수준 교체.
    assert merged.description == "사람이 확인한 서비스 설명."
    assert merged.license_note == "가상 이용허락범위(큐레이션)"


def test_merge_keeps_source_values_when_curation_omits_them() -> None:
    """큐레이션이 비워 둔 자리는 소스 값이 그대로 남는다."""
    source = _source_spec(description="소스 설명.", license_note="소스 라이선스.")
    merged = apply_curation(
        source,
        load_curation({"mcportal_curation": 1, "preset_id": SYNTHETIC_ID, "service": {}}),
    )
    assert merged.description == "소스 설명."
    assert merged.license_note == "소스 라이선스."
    assert merged.operations[0].summary == "합성 목록 조회"
    assert merged.operations[0].description == "원 문서가 준 설명."


def test_e1_example_prompts_block_is_deterministic() -> None:
    """E1: 예시 프롬프트는 설명 말미에 고정 블록으로 붙는다."""
    _, operation = _merged_operation()
    assert operation.description == (
        "큐레이션 설명 본문.\n\n예시 프롬프트:\n- 첫 번째 예시 질문\n- 두 번째 예시 질문"
    )

    # 0개면 블록 자체가 없다.
    document = _curation_document()
    document["operations"]["getDemoList"]["example_prompts"] = []
    merged = apply_curation(_source_spec(), load_curation(document))
    assert merged.operations[0].description == "큐레이션 설명 본문."


def test_e3_prompt_block_alone_when_no_description() -> None:
    """E3: 소스도 큐레이션도 설명이 없으면 블록만으로 설명을 만든다."""
    source = dataclasses.replace(
        _source_spec(),
        operations=(dataclasses.replace(_source_spec().operations[0], description=None),),
    )
    document = _curation_document()
    del document["operations"]["getDemoList"]["description"]
    merged = apply_curation(source, load_curation(document))
    assert merged.operations[0].description == (
        "예시 프롬프트:\n- 첫 번째 예시 질문\n- 두 번째 예시 질문"
    )


def test_e2_enum_note_joins_description_but_not_schema() -> None:
    """E2: enum_note는 설명 말미에 붙고 열거값에는 실리지 않는다."""
    _, operation = _merged_operation()
    region = _params(operation)["regionCode"]
    assert region.description == "지역 구분. 대표값은 A와 B다."
    assert "대표값" not in " ".join(region.enum)

    # 설명이 비어 있으면 안내 문장만 남는다.
    document = _curation_document()
    document["operations"]["getDemoList"]["parameters"]["regionCode"] = {
        "enum_note": "안내만 있다."
    }
    merged = apply_curation(_source_spec(), load_curation(document))
    assert _params(merged.operations[0])["regionCode"].description == "안내만 있다."


def test_response_unresolved_demotes_schema() -> None:
    """response.unresolved=true 는 응답 스키마를 미확정으로 강등한다."""
    document = _curation_document()
    document["operations"]["getDemoList"]["response"] = {
        "unresolved": True,
        "reason": "실제 응답과 어긋난다는 근거.",
    }
    merged = apply_curation(_source_spec(), load_curation(document))
    assert merged.operations[0].response_schema is None
    assert unresolved_schema_operations(merged) == ("getDemoList",)

    # 지정하지 않으면 소스 선언 스키마가 유지된다.
    kept = apply_curation(_source_spec(), load_curation(_curation_document()))
    assert kept.operations[0].response_schema is not None
    assert unresolved_schema_operations(kept) == ()


def test_parameter_removal_keeps_i5_ordering() -> None:
    """제거 후에도 파라미터는 (필수 먼저, 이름 오름차순)으로 정렬된다."""
    source = SourceSpec(
        provider="portal.example.invalid",
        service_id=SYNTHETIC_ID,
        service_name="가상 통계 서비스",
        base_url="https://apis.example.invalid/0000000/demoStats",
        source_kind=SourceKind.GW_SWAGGER,
        operations=(
            OperationSpec(
                operation_id="getDemoList",
                method="GET",
                path="/getDemoList",
                parameters=(
                    ParamSpec(name="endYm", location="query", required=True, type="string"),
                    ParamSpec(name="startYm", location="query", required=True, type="string"),
                    ParamSpec(name="page", location="query", required=False, type="integer"),
                    ParamSpec(name="regionCode", location="query", required=False, type="string"),
                    ParamSpec(name="unit", location="query", required=False, type="string"),
                ),
            ),
        ),
    )
    curation = load_curation(
        {
            "mcportal_curation": 1,
            "preset_id": SYNTHETIC_ID,
            "service": {},
            "operations": {
                "getDemoList": {
                    "parameters_remove": [
                        {"name": "page", "reason": "본문형에 의미 없는 유령 인자."}
                    ]
                }
            },
        }
    )
    merged, report = apply_curation_with_report(source, curation)
    names = [param.name for param in merged.operations[0].parameters]
    assert names == ["endYm", "startYm", "regionCode", "unit"]
    assert report.parameters_removed == 1
    assert report.operations_curated == 1


def test_report_counts_are_accurate() -> None:
    """CurationReport 의 집계가 실제 적용 횟수와 일치한다."""
    _, report = apply_curation_with_report(
        _source_spec(), load_curation(_curation_document())
    )
    assert report.operations_curated == 1
    assert report.parameters_curated == 2
    assert report.parameters_removed == 0
    assert report.responses_unresolved == 0
    assert report.example_prompt_count == 2


# ---------------------------------------------------------------------------
# 19·20·21. 결정론과 불변성
# ---------------------------------------------------------------------------
def test_determinism_same_input_same_bytes(tmp_path: Path) -> None:
    """같은 입력을 두 번 컴파일하면 바이트가 같다."""
    directory = _bundle(tmp_path, curation=_curation_document())
    first = dumps(compile_preset(directory).document)
    second = dumps(compile_preset(directory).document)
    assert first == second


def test_determinism_key_order_does_not_matter(tmp_path: Path) -> None:
    """curation.json 의 키 순서를 뒤집어도 산출물이 같다."""
    straight = _bundle(tmp_path / "s", curation=_curation_document())
    flipped = _bundle(tmp_path / "f", curation=_reverse_keys(_curation_document()))
    assert dumps(compile_preset(straight).document) == dumps(
        compile_preset(flipped).document
    )


def test_apply_curation_does_not_mutate_source() -> None:
    """병합은 원본 SourceSpec 을 건드리지 않는다."""
    source = _source_spec(description="소스 설명.")
    snapshot = dataclasses.asdict(source)
    merged = apply_curation(source, load_curation(_curation_document()))

    assert dataclasses.asdict(source) == snapshot
    assert merged is not source
    assert source.description == "소스 설명."
    assert source.operations[0].summary == "합성 목록 조회"
    with pytest.raises(dataclasses.FrozenInstanceError):
        source.description = "다른 값"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# 22·23. 번들 탐색
# ---------------------------------------------------------------------------
def test_iter_presets_filters_and_sorts(tmp_path: Path) -> None:
    """_ 로 시작하는 디렉터리와 source.json 없는 디렉터리는 제외하고 이름순으로 준다."""
    for preset_id in ("00000002", "00000001"):
        _bundle(tmp_path, preset_id=preset_id, curation=_curation_document(preset_id))
    _bundle(tmp_path, preset_id="00000003", directory_name="_raw")
    (tmp_path / "00000009").mkdir()
    (tmp_path / "00000009" / "note.txt").write_text("소스 없음", encoding="utf-8")

    infos = iter_presets(tmp_path)
    assert [info.preset_id for info in infos] == ["00000001", "00000002"]
    assert infos[0].group == "가상 묶음"
    assert infos[0].service_name == "가상 통계(큐레이션 제목)"
    assert infos[0].operation_count == 1
    assert infos[0].unresolved_count == 0
    assert infos[0].curation_path is not None
    assert infos[0].notes == ("가상 메모 1", "가상 메모 2")


def test_iter_presets_without_curation(tmp_path: Path) -> None:
    """큐레이션이 없는 번들도 그대로 요약된다."""
    _bundle(tmp_path, preset_id="00000004")
    (info,) = iter_presets(tmp_path)
    assert info.curation_path is None
    assert info.group is None
    assert info.notes == ()
    assert info.service_name == "가상 통계 서비스"


def test_default_presets_root_prefers_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """환경변수가 가리키는 루트를 최우선으로 채택한다."""
    _bundle(tmp_path, curation=_curation_document())
    monkeypatch.setenv(curation_module.ENV_PRESETS_ROOT, str(tmp_path))
    assert default_presets_root() == tmp_path


def test_default_presets_root_skips_candidates_without_bundles(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """번들이 없는 후보는 건너뛰고, 전부 실패하면 None + 빈 튜플이다."""
    empty = tmp_path / "empty"
    empty.mkdir()
    monkeypatch.setenv(curation_module.ENV_PRESETS_ROOT, str(empty))
    assert default_presets_root() != empty

    monkeypatch.setattr(curation_module, "_has_bundle", lambda candidate: False)
    assert default_presets_root() is None
    assert iter_presets(None) == ()  # 빈 상태는 예외가 아니다


def test_iter_presets_missing_root_returns_empty(tmp_path: Path) -> None:
    """없는 경로를 받아도 예외가 아니라 빈 튜플이다."""
    assert iter_presets(tmp_path / "없는경로") == ()


# ---------------------------------------------------------------------------
# 24·25. 비교군 컴파일과 드리프트 검사
# ---------------------------------------------------------------------------
def test_compile_preset_uncurated_is_the_control_group(tmp_path: Path) -> None:
    """curated=False 는 큐레이션을 하나도 반영하지 않는다(벤치마크 비교군)."""
    directory = _bundle(tmp_path, curation=_curation_document())
    curated = compile_preset(directory).document
    plain = compile_preset(directory, curated=False).document

    assert curated["info"]["title"] == "가상 통계(큐레이션 제목)"
    assert plain["info"]["title"] == "가상 통계 서비스"
    assert curated["info"]["version"] == "1.2.3"
    assert plain["info"]["version"] == "0.1.0"

    operation = plain["paths"]["/getDemoList"]["get"]
    assert operation["summary"] == "합성 목록 조회"
    assert operation["description"] == "원 문서가 준 설명."
    assert "예시 프롬프트" not in json.dumps(plain, ensure_ascii=False)

    assert load_preset(directory, curated=False).description is None


def test_check_preset_detects_drift(tmp_path: Path) -> None:
    """커밋본과 재생성본이 1바이트라도 다르면 False, 파일이 없어도 False."""
    directory = _bundle(tmp_path, curation=_curation_document())
    assert check_preset(directory) is False  # 아직 산출물이 없다

    path = write_preset(directory)
    assert path.name == "openapi.json"
    assert check_preset(directory) is True

    original = path.read_bytes()
    path.write_bytes(original.replace(b"openapi", b"openapl", 1))
    assert check_preset(directory) is False

    path.write_bytes(original)
    assert check_preset(directory) is True


def test_read_preset_source_returns_provenance(tmp_path: Path) -> None:
    """래퍼의 provenance 는 그대로 돌려주고, 스펙 본문은 어댑터에 위임한다."""
    directory = _bundle(tmp_path)
    source, provenance = read_preset_source(directory / "source.json")
    assert source.service_id == SYNTHETIC_ID
    assert source.source_kind is SourceKind.GW_SWAGGER
    assert source.license_note == "가상 이용허락범위(합성 픽스처)"
    assert provenance["acquisition"].startswith("합성")


def test_provenance_keys_are_a_content_rule_not_a_load_gate(tmp_path: Path) -> None:
    """출처 메타 6키는 '커밋 번들의 내용 규약'이지 로더의 안전 조건이 아니다.

    임시 디렉터리에 만든 합성 번들까지 출처 메타를 갖추라고 요구하면 엔진이
    데이터 정책을 강제하는 자리가 된다. 실제 커밋 번들이 여섯 키를 모두 채웠는지는
    ``tests/test_presets.py`` 가 검사한다.
    """
    assert "raw_sha256" in curation_module.PROVENANCE_KEYS
    wrapper = _wrapper()
    wrapper["provenance"] = {"spec_origin": "합성"}
    directory = _bundle(tmp_path, wrapper=wrapper)
    source, provenance = read_preset_source(directory / "source.json")
    assert source.service_id == SYNTHETIC_ID
    assert provenance == {"spec_origin": "합성"}


def test_preset_source_wrapper_validation(tmp_path: Path) -> None:
    """래퍼 스키마 위반은 각각 다른 이유로 막힌다."""
    wrapper = _wrapper()
    wrapper["mcportal_preset_source"] = 2
    directory = _bundle(tmp_path / "v", wrapper=wrapper)
    with pytest.raises(CurationError, match="프리셋 소스 스키마 버전"):
        load_preset(directory)

    wrapper = _wrapper()
    wrapper["provenance"] = "매핑이 아님"
    directory = _bundle(tmp_path / "p", wrapper=wrapper)
    with pytest.raises(CurationError, match="source.provenance"):
        load_preset(directory)

    wrapper = _wrapper()
    del wrapper["source_url"]
    directory = _bundle(tmp_path / "s", wrapper=wrapper)
    with pytest.raises(CurationError, match="source_url"):
        load_preset(directory)

    wrapper = _wrapper()
    wrapper["source_kind"] = "존재하지_않는_종류"
    directory = _bundle(tmp_path / "k", wrapper=wrapper)
    with pytest.raises(CurationError, match="source_kind"):
        load_preset(directory)

    wrapper = _wrapper()
    wrapper["새로운키"] = 1
    directory = _bundle(tmp_path / "u", wrapper=wrapper)
    with pytest.raises(CurationError, match="새로운키"):
        load_preset(directory)


def test_bundle_without_source_json_is_reported(tmp_path: Path) -> None:
    """번들에 source.json 이 없으면 무엇이 없는지 알려 준다."""
    directory = tmp_path / SYNTHETIC_ID
    directory.mkdir()
    with pytest.raises(CurationError, match="source.json"):
        preset_info(directory)


def test_read_curation_rejects_broken_json(tmp_path: Path) -> None:
    """JSON 파손은 경로와 함께 알린다. 파일 부재는 호출자가 판단한다."""
    path = tmp_path / "curation.json"
    path.write_text("{ 이건 JSON 이 아니다", encoding="utf-8")
    with pytest.raises(CurationError, match="JSON"):
        read_curation(path)
    with pytest.raises(OSError):
        read_curation(tmp_path / "없는파일.json")


# ---------------------------------------------------------------------------
# 25-1. V15 — 열거값 멤버십 (2026-08-09 Advisor 검증 A1)
# ---------------------------------------------------------------------------
def _source_with_enum_param(enum: tuple[str, ...] = ("A", "B")) -> SourceSpec:
    """``regionCode`` 에 열거값을 선언한 합성 SourceSpec."""
    base = _source_spec()
    operation = base.operations[0]
    parameters = tuple(
        dataclasses.replace(param, enum=enum) if param.name == "regionCode" else param
        for param in operation.parameters
    )
    return dataclasses.replace(
        base, operations=(dataclasses.replace(operation, parameters=parameters),)
    )


def _param_curation(**overlay: Any) -> Curation:
    """``regionCode`` 하나만 손보는 최소 큐레이션."""
    return load_curation(
        {
            "mcportal_curation": 1,
            "preset_id": SYNTHETIC_ID,
            "service": {},
            "operations": {"getDemoList": {"parameters": {"regionCode": overlay}}},
        }
    )


@pytest.mark.parametrize("field", ["example", "default"])
def test_v15_value_outside_the_declared_enum_is_blocked(field: str) -> None:
    """타입이 맞아도 열거값 밖의 예시·기본값은 커밋 전에 접는다.

    V14 는 ``cast_scalar`` 성공만 봤다. ``type: string`` 파라미터에 ``enum:
    [A, B]`` 가 붙어 있어도 ``"Z"`` 는 문자열이라 캐스팅에 성공하므로 그대로
    통과했고, 산출물은 ``{"enum": ["A","B"], "examples": ["Z"]}`` 라는 자기모순
    스키마가 됐다. 그 예시를 따라 하는 툴콜은 100% 검증에 실패한다.
    """
    curation = _param_curation(**{field: "Z"})
    with pytest.raises(CurationError) as excinfo:
        validate_curation(curation, _source_with_enum_param())
    message = str(excinfo.value)
    assert "regionCode" in message  # 문제 파라미터
    assert field in message
    assert "'Z'" in message  # 문제 값
    assert "'A', 'B'" in message  # 허용값 목록


def test_v15_overlay_enum_becomes_the_new_baseline() -> None:
    """오버레이가 enum 을 교체하면 판정 기준도 교체된 열거값이다."""
    source = _source_with_enum_param(("A", "B"))

    # 소스 기준으로는 유효했던 "A" 가 교체 후에는 위반이 된다.
    with pytest.raises(CurationError) as excinfo:
        validate_curation(_param_curation(enum=["C", "D"], example="A"), source)
    assert "'C', 'D'" in str(excinfo.value)

    # 새 열거값의 멤버면 통과한다(과잉 차단 방지).
    validate_curation(_param_curation(enum=["C", "D"], example="C"), source)
    validate_curation(_param_curation(example="B"), source)


def test_v15_source_example_conflicting_with_replaced_enum_is_blocked() -> None:
    """오버레이가 enum 만 갈아 끼우고 소스 예시를 물려받는 조합도 같은 모순이다."""
    base = _source_with_enum_param(("A", "B"))
    operation = base.operations[0]
    parameters = tuple(
        dataclasses.replace(param, example="A") if param.name == "regionCode" else param
        for param in operation.parameters
    )
    source = dataclasses.replace(
        base, operations=(dataclasses.replace(operation, parameters=parameters),)
    )
    with pytest.raises(CurationError) as excinfo:
        validate_curation(_param_curation(enum=["C", "D"]), source)
    message = str(excinfo.value)
    assert "'A'" in message
    assert "소스 파라미터" in message


def test_v15_leaves_parameters_without_enum_alone() -> None:
    """열거값이 없는 파라미터는 아무것도 막지 않는다."""
    validate_curation(_param_curation(example="무엇이든"), _source_spec())


# ---------------------------------------------------------------------------
# 26. 도메인 로직 0줄 회귀
# ---------------------------------------------------------------------------
def test_curation_engine_has_no_domain_knowledge() -> None:
    """엔진 소스에 특정 프리셋의 ID·기관명·도메인 파라미터명이 없어야 한다.

    도메인 지식은 전부 ``presets/<id>/curation.json`` 에 있어야 한다는 설계 원칙을
    소스 문자열 스캔으로 강제한다. 이 테스트가 깨졌다면 엔진에 도메인 분기가
    새어 들어간 것이다.
    """
    text = Path(curation_module.__file__).read_text(encoding="utf-8")
    forbidden = (
        # 프리셋 ID 4종
        "15000115",
        "15081808",
        "15101612",
        "15102108",
        # 기관명 3종
        "국세청",
        "관세청",
        "법제처",
        # 도메인 파라미터명 3종
        "strtYymm",
        "b_no",
        "cntyCd",
    )
    found = [token for token in forbidden if token in text]
    assert found == [], f"엔진에 도메인 명사가 들어 있다: {found}"


# ---------------------------------------------------------------------------
# 9. 실키 샘플링 글루(W4 §3-2) - 합성 게이트웨이 record 모드 E2E
# ---------------------------------------------------------------------------
#: '+', '/', '=' 를 포함해 인코딩 변형이 뚜렷이 달라지는 **합성** 인증키(실키 아님).
SAMPLE_KEY = "ab12+CD/34=="
SAMPLE_KEY_ENCODED = "ab12%2BCD%2F34%3D%3D"

SAMPLE_PRESET_ID = "99000009"
SAMPLE_BASE = "https://apis.example.invalid/9990000/demo"


def _sampling_document() -> dict[str, Any]:
    """미확정 축 두 가지를 한 문서에 담은 합성 게이트웨이 Swagger 2.0.

    * ``getUnknownList`` - 원 문서가 응답 스키마를 **주지 않는다**(소스 미확정).
    * ``getDeclaredList`` - 원 문서는 스키마를 주지만 사람이 확인한 결과 실제
      응답과 달라 큐레이션이 ``response.unresolved`` 로 강등한다(게이트웨이 2종
      실물과 같은 형태).
    """

    def _operation(operation_id: str, *, schema: dict[str, Any] | None) -> dict[str, Any]:
        response: dict[str, Any] = {"description": "정상"}
        if schema is not None:
            response["schema"] = schema
        return {
            "get": {
                "operationId": operation_id,
                "summary": f"{operation_id} 합성 조회",
                "produces": ["application/json"],
                "parameters": [
                    {"name": "serviceKey", "in": "query", "type": "string", "required": True},
                    {"name": "pageNo", "in": "query", "type": "integer", "required": True},
                    {"name": "numOfRows", "in": "query", "type": "integer", "required": True},
                ],
                "responses": {"200": response},
            }
        }

    return {
        "swagger": "2.0",
        "info": {"title": "가상 표본 서비스", "version": "1.0"},
        "host": "apis.example.invalid",
        "basePath": "/9990000/demo",
        "schemes": ["https"],
        "paths": {
            "/getDeclaredList": _operation(
                "getDeclaredList",
                schema={"type": "object", "properties": {"total": {"type": "integer"}}},
            ),
            "/getUnknownList": _operation("getUnknownList", schema=None),
        },
    }


def _sampling_wrapper(**overrides: Any) -> dict[str, Any]:
    """샘플링 시험용 ``source.json`` 래퍼."""
    wrapper = _wrapper(SAMPLE_PRESET_ID)
    wrapper["service_name"] = "가상 표본 서비스"
    wrapper["document"] = _sampling_document()
    wrapper.update(overrides)
    return wrapper


def _sampling_curation(*, unresolved: bool = True) -> dict[str, Any]:
    """``getDeclaredList`` 의 응답 스키마를 강등하는 합성 큐레이션."""
    document: dict[str, Any] = {
        "mcportal_curation": CURATION_SCHEMA_VERSION,
        "preset_id": SAMPLE_PRESET_ID,
        "service": {"title": "가상 표본 서비스", "version": "0.2.0"},
    }
    if unresolved:
        document["operations"] = {
            "getDeclaredList": {
                "response": {
                    "unresolved": True,
                    "reason": "합성 근거: 선언 스키마가 실제 응답과 다르다.",
                }
            }
        }
    return document


def _sampling_bundle(tmp_path: Path, **curation_kwargs: Any) -> Path:
    """샘플링 시험용 번들을 만든다."""
    return _bundle(
        tmp_path,
        preset_id=SAMPLE_PRESET_ID,
        wrapper=_sampling_wrapper(),
        curation=_sampling_curation(**curation_kwargs),
    )


def _mock_gateway() -> None:
    """두 오퍼레이션을 표준형 JSON 으로 답하는 가짜 게이트웨이를 세운다.

    응답 본문이 **요청 인증키를 되비추게** 만든다(실제 게이트웨이에서 관측된
    형태). 그래야 카세트·샘플 양쪽의 값 기반 스크러빙이 실제로 일하는지 볼 수 있다.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        page = request.url.params.get("pageNo", "0")
        received = request.url.params.get("serviceKey", "")
        return httpx.Response(
            200,
            json={
                "response": {
                    "header": {"resultCode": "00", "resultMsg": "정상"},
                    "body": {
                        "pageNo": int(page),
                        "totalCount": 12,
                        "items": {"item": [{"name": "가상항목", "amount": 3}]},
                    },
                    "echoKey": received,
                }
            },
        )

    for operation_id in ("getDeclaredList", "getUnknownList"):
        respx.get(f"{SAMPLE_BASE}/{operation_id}").mock(side_effect=handler)


def _assert_no_key(text: str, *, where: str) -> None:
    """알려진 인증키 표기 변형이 하나도 남지 않았는지 본다."""
    for variant in (
        SAMPLE_KEY,
        SAMPLE_KEY_ENCODED,
        quote(SAMPLE_KEY),
        quote(SAMPLE_KEY, safe=""),
        quote_plus(SAMPLE_KEY),
    ):
        assert variant not in text, f"{where} 에 키 변형이 남았다: {variant}"


def _synthetic_results(
    operation_id: str = "getUnknownList", *, count: int = 2
) -> dict[str, tuple[SampleResult, ...]]:
    """네트워크 없이 조립한 샘플 결과(추론·산출 단계만 볼 때 쓴다)."""
    return {
        operation_id: tuple(
            SampleResult(
                operation_id=operation_id,
                status_code=200,
                ok=True,
                result_code="00",
                source_format="json",
                payload={
                    "response": {
                        "header": {"resultCode": "00"},
                        "body": {"pageNo": index, "items": {"item": [{"name": "가상"}]}},
                    }
                },
            )
            for index in range(1, count + 1)
        )
    }


@respx.mock
def test_sample_preset_targets_curation_forced_unresolved(tmp_path: Path) -> None:
    """대상 선정은 **큐레이션 적용 소스** 기준이다(게이트웨이 강등 건 포함).

    원 소스(``curated=False``) 기준으로 고르면 ``curation.json`` 이 강등한
    ``getDeclaredList`` 가 대상에서 빠져, 사람이 "실제 응답과 다르다"고 확인한
    스키마가 영원히 채워지지 않는다.
    """
    _mock_gateway()
    directory = _sampling_bundle(tmp_path)

    # 전제 확인: 강등은 큐레이션 적용 소스에서만 보인다.
    assert unresolved_schema_operations(load_preset(directory, curated=False)) == (
        "getUnknownList",
    )

    report = sample_preset(
        directory,
        service_key=SAMPLE_KEY,
        count=3,
        budget=100,
        ledger_path=tmp_path / "ledger.db",
    )
    assert isinstance(report, PresetSampleReport)
    assert report.target_operations == ("getDeclaredList", "getUnknownList")
    assert report.resolved_operations == ("getDeclaredList", "getUnknownList")
    assert report.call_count == 6
    assert (report.ok_count, report.failed_count) == (6, 0)
    assert {summary.operation_id for summary in report.operations} == {
        "getDeclaredList",
        "getUnknownList",
    }
    for summary in report.operations:
        assert summary.schema_inferred is True
        assert summary.sample_count == 3
        assert summary.status_codes == (200,)
        assert summary.result_codes == ("00",)


@respx.mock
def test_sample_preset_fills_openapi_and_scrubs_artifacts(tmp_path: Path) -> None:
    """샘플링이 폴백 스키마를 실제 구조로 바꾸고, 산출물에 키를 남기지 않는다."""
    _mock_gateway()
    directory = _sampling_bundle(tmp_path)
    before = compile_preset(directory).document
    fallback = before["components"]["schemas"]["GetUnknownListResponse"]
    assert fallback == {"type": "object", "description": "응답 스키마 미확정(샘플링 미수행)"}

    report = sample_preset(
        directory,
        service_key=SAMPLE_KEY,
        count=3,
        budget=100,
        ledger_path=tmp_path / "ledger.db",
    )

    assert report.openapi_path == directory / "openapi.json"
    document = json.loads(report.openapi_path.read_text(encoding="utf-8"))
    meta = document["info"][X_MCPORTAL]
    assert meta["generation_mode"] == "sampled"
    assert meta["sample_count"] == 6
    assert "unresolved" not in meta["schema_inference"]
    for name in ("GetUnknownListResponse", "GetDeclaredListResponse"):
        schema = document["components"]["schemas"][name]
        assert schema != fallback
        assert "response" in schema["properties"]

    # 카세트·샘플 위치는 번들 하위의 고정 이름이다.
    assert report.cassette_path == directory / "cassettes" / f"{SAMPLE_PRESET_ID}.json"
    assert report.samples_dir == directory / "samples"
    assert len(report.sample_paths) == 6
    assert {path.name for path in report.sample_paths} == {
        f"{operation_id}_{index:02d}.json"
        for operation_id in ("getDeclaredList", "getUnknownList")
        for index in (1, 2, 3)
    }

    # 응답이 요청 키를 되비췄는데도 어느 산출물에도 평문이 남지 않는다.
    cassette_text = report.cassette_path.read_text(encoding="utf-8")
    _assert_no_key(cassette_text, where="카세트")
    assert "__SCRUBBED__" in cassette_text
    for path in report.sample_paths:
        text = path.read_text(encoding="utf-8")
        _assert_no_key(text, where=f"샘플 {path.name}")
        assert "__SCRUBBED__" in text
        assert text.endswith("\n")
    _assert_no_key(report.openapi_path.read_text(encoding="utf-8"), where="openapi.json")


@respx.mock
def test_sample_preset_without_targets_makes_no_calls(tmp_path: Path) -> None:
    """미확정이 0건이면 네트워크에 나가지 않고 파일도 만들지 않는다.

    이미 전부 확정된 프리셋에 예산을 태우는 경로가 있으면 안 된다(설계 §3-1 이
    국세청 프리셋을 호출 대상에서 뺀 이유와 같은 규칙).
    """
    _mock_gateway()
    directory = _sampling_bundle(tmp_path, unresolved=False)
    # 선언 스키마가 살아 있으면 미확정은 getUnknownList 하나뿐이므로,
    # 그 오퍼레이션까지 없앤 번들로 0건 상태를 만든다.
    wrapper = _sampling_wrapper()
    del wrapper["document"]["paths"]["/getUnknownList"]
    _write_json(directory / "source.json", wrapper)

    report = sample_preset(
        directory,
        service_key=SAMPLE_KEY,
        budget=100,
        ledger_path=tmp_path / "ledger.db",
    )
    assert report.target_operations == ()
    assert (report.call_count, report.ok_count, report.failed_count) == (0, 0, 0)
    assert report.operations == ()
    assert report.openapi_path is None
    assert report.sample_paths == ()
    assert not (directory / "cassettes").exists()
    assert not (directory / "samples").exists()
    assert len(respx.calls) == 0


@respx.mock
def test_sample_preset_passes_overrides_down_to_requests(tmp_path: Path) -> None:
    """``overrides`` 가 샘플 요청 조립까지 내려간다(W4 §3-2 가 뚫은 통로).

    통로가 없으면 값을 스펙에서 정할 수 없는 필수 파라미터를 가진 오퍼레이션은
    :class:`SampleParamError` 로 막히고 호출자가 우회할 방법이 없다.
    """
    wrapper = _sampling_wrapper()
    unknown = wrapper["document"]["paths"]["/getUnknownList"]["get"]
    unknown["parameters"].append(
        {"name": "regionCode", "in": "query", "type": "string", "required": True}
    )
    directory = _bundle(
        tmp_path,
        preset_id=SAMPLE_PRESET_ID,
        wrapper=wrapper,
        curation=_sampling_curation(unresolved=False),
    )
    _mock_gateway()

    with pytest.raises(SampleParamError, match="regionCode"):
        sample_preset(
            directory,
            service_key=SAMPLE_KEY,
            budget=100,
            ledger_path=tmp_path / "ledger.db",
        )

    report = sample_preset(
        directory,
        service_key=SAMPLE_KEY,
        count=2,
        budget=100,
        ledger_path=tmp_path / "ledger2.db",
        overrides={"regionCode": "A11"},
    )
    assert report.call_count == 2
    assert report.ok_count == 2
    assert respx.calls.last.request.url.params["regionCode"] == "A11"


@respx.mock
def test_sample_preset_can_skip_openapi_update(tmp_path: Path) -> None:
    """``apply_schemas=False`` 면 측정만 하고 산출물은 건드리지 않는다."""
    _mock_gateway()
    directory = _sampling_bundle(tmp_path)
    write_preset(directory)
    before = (directory / "openapi.json").read_bytes()

    report = sample_preset(
        directory,
        service_key=SAMPLE_KEY,
        count=2,
        budget=100,
        ledger_path=tmp_path / "ledger.db",
        apply_schemas=False,
    )
    assert report.openapi_path is None
    assert report.call_count == 4
    assert (directory / "openapi.json").read_bytes() == before


@respx.mock
def test_sample_preset_honours_custom_artifact_directories(tmp_path: Path) -> None:
    """카세트·샘플 디렉터리를 번들 밖으로 돌릴 수 있다(리포 오염 없이 시험 실행)."""
    _mock_gateway()
    directory = _sampling_bundle(tmp_path)
    cassette_dir = tmp_path / "outside" / "cassettes"
    samples_dir = tmp_path / "outside" / "samples"

    report = sample_preset(
        directory,
        service_key=SAMPLE_KEY,
        count=2,
        budget=100,
        ledger_path=tmp_path / "ledger.db",
        cassette_dir=cassette_dir,
        samples_dir=samples_dir,
    )
    assert report.cassette_path.parent == cassette_dir
    assert report.samples_dir == samples_dir
    assert report.cassette_path.is_file()
    assert not (directory / "cassettes").exists()
    assert not (directory / "samples").exists()


def test_apply_sampled_schemas_is_deterministic(tmp_path: Path) -> None:
    """같은 샘플 결과는 바이트 동일한 ``openapi.json`` 을 만든다(네트워크 없음)."""
    directory = _sampling_bundle(tmp_path)
    results = _synthetic_results()

    first = apply_sampled_schemas(directory, results)
    payload = first.read_bytes()
    second = apply_sampled_schemas(directory, dict(results))
    assert second.read_bytes() == payload

    document = json.loads(payload.decode("utf-8"))
    assert document["info"][X_MCPORTAL]["generation_mode"] == "sampled"
    # 큐레이션 제목·버전은 오프라인 산출과 같은 규칙으로 결정된다.
    assert document["info"]["title"] == "가상 표본 서비스"
    assert document["info"]["version"] == "0.2.0"


def test_apply_sampled_schemas_leaves_untouched_operations_on_fallback(
    tmp_path: Path,
) -> None:
    """샘플이 없는 오퍼레이션은 폴백 스키마 그대로 남는다(허위 확정 금지)."""
    directory = _sampling_bundle(tmp_path)
    document = json.loads(
        apply_sampled_schemas(directory, _synthetic_results()).read_text(encoding="utf-8")
    )
    assert document["components"]["schemas"]["GetDeclaredListResponse"] == {
        "type": "object",
        "description": "응답 스키마 미확정(샘플링 미수행)",
    }
    assert document["info"][X_MCPORTAL]["schema_inference"]["unresolved"] == 1


# ---------------------------------------------------------------------------
# 9-2. 측정층 영속화 - sampled_schemas.json
# ---------------------------------------------------------------------------
def _sampled_payload(**overrides: Any) -> dict[str, Any]:
    """규약을 만족하는 최소 실측 스키마 문서(위반 표본의 기준선)."""
    document: dict[str, Any] = {
        "mcportal_sampled": SAMPLED_SCHEMA_VERSION,
        "preset_id": SAMPLE_PRESET_ID,
        "sampled_on": "2026-08-09",
        "provenance": {
            "cassette": "cassettes/99000009.json",
            "cassette_sha256": "sha256:" + "0" * 64,
            "sample_count": 2,
            "call_count": 2,
        },
        "operations": {
            "getUnknownList": {
                "response_schema": {
                    "type": "object",
                    "properties": {"total": {"type": "integer"}},
                },
                "inference": {"sample_count": 2, "conflicts": 0, "truncated": False},
            }
        },
    }
    document.update(overrides)
    return document


@respx.mock
def test_sampling_persists_schemas_so_offline_compile_reproduces_bytes(
    tmp_path: Path,
) -> None:
    """실키 샘플링 산출물을 **키 없는 재컴파일**이 바이트 그대로 재현한다.

    이것이 영속화의 존재 이유다. 추론 스키마가 산출물에만 있으면 그 문서는 라이브
    표본을 가진 사람만 다시 만들 수 있고, 오프라인 ``compile --check`` 는 커밋된
    파일을 드리프트로 보고한다 - 즉 실측을 반영한 프리셋은 CI 게이트를 영구히
    빨갛게 만든다. 측정 결과를 데이터로 남기면 그 갈림 자체가 사라진다.
    """
    _mock_gateway()
    directory = _sampling_bundle(tmp_path)

    report = sample_preset(
        directory,
        service_key=SAMPLE_KEY,
        count=3,
        budget=100,
        ledger_path=tmp_path / "ledger.db",
    )
    assert report.sampled_path == directory / PRESET_SAMPLED_FILENAME
    assert report.sampled_path.is_file()
    produced = (directory / "openapi.json").read_bytes()
    calls_during_sampling = len(respx.calls)

    # 네트워크·인증키 없이 같은 문서가 나온다(재생성·바이트 비교 두 경로 모두).
    assert check_preset(directory) is True
    assert dumps(compile_preset(directory).document).encode("utf-8") == produced
    assert write_preset(directory).read_bytes() == produced
    assert len(respx.calls) == calls_during_sampling

    # 영속화 파일에도 인증키 변형이 남지 않는다.
    _assert_no_key(
        report.sampled_path.read_text(encoding="utf-8"), where=PRESET_SAMPLED_FILENAME
    )


@respx.mock
def test_sampled_file_records_the_cassette_it_can_be_replayed_from(
    tmp_path: Path,
) -> None:
    """측정 출처는 **상대 경로**와 지문으로 남는다(절대 경로 금지)."""
    _mock_gateway()
    directory = _sampling_bundle(tmp_path)
    report = sample_preset(
        directory,
        service_key=SAMPLE_KEY,
        count=2,
        budget=100,
        ledger_path=tmp_path / "ledger.db",
    )

    document = json.loads(
        (directory / PRESET_SAMPLED_FILENAME).read_text(encoding="utf-8")
    )
    provenance = document["provenance"]
    assert provenance["cassette"] == f"cassettes/{SAMPLE_PRESET_ID}.json"
    assert provenance["cassette_sha256"] == "sha256:" + hashlib.sha256(
        report.cassette_path.read_bytes()
    ).hexdigest()
    assert (provenance["call_count"], provenance["sample_count"]) == (4, 4)
    # 측정자의 홈 디렉터리 이름이 커밋 대상 파일에 실리지 않는다.
    assert str(tmp_path) not in json.dumps(document, ensure_ascii=False)


def test_sampled_file_follows_the_output_convention(tmp_path: Path) -> None:
    """UTF-8(BOM 없음) · LF · 끝 개행 1개 · sort_keys · 들여쓰기 2칸."""
    directory = _sampling_bundle(tmp_path)
    apply_sampled_schemas(
        directory, _synthetic_results(), sampled_on="2026-08-09"
    )

    raw = (directory / PRESET_SAMPLED_FILENAME).read_bytes()
    assert not raw.startswith(b"\xef\xbb\xbf")
    assert b"\r\n" not in raw
    text = raw.decode("utf-8")
    assert text.endswith("\n") and not text.endswith("\n\n")
    payload = json.loads(text)
    assert (
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n" == text
    )
    assert payload["mcportal_sampled"] == SAMPLED_SCHEMA_VERSION
    assert payload["preset_id"] == SAMPLE_PRESET_ID
    assert payload["sampled_on"] == "2026-08-09"
    assert payload["operations"]["getUnknownList"]["inference"] == {
        "sample_count": 2,
        "conflicts": 0,
        "truncated": False,
    }


def test_sampled_layer_wins_over_source_and_curation(tmp_path: Path) -> None:
    """주입 순서는 소스 → 큐레이션(강등) → 실측이고, 실측이 마지막에 이긴다.

    ``getDeclaredList`` 는 원 문서가 스키마를 주지만 큐레이션이 "실제 응답과
    다르다"며 강등한 자리다. 실측이 그 자리를 채운 뒤 소스 선언이 되살아나면
    사람이 틀렸다고 확인한 스키마가 산출물로 돌아온다.
    """
    directory = _sampling_bundle(tmp_path)
    before = compile_preset(directory).document
    assert before["components"]["schemas"]["GetDeclaredListResponse"] == {
        "type": "object",
        "description": "응답 스키마 미확정(샘플링 미수행)",
    }

    apply_sampled_schemas(
        directory,
        _synthetic_results("getDeclaredList", count=2),
        sampled_on="2026-08-09",
    )
    after = compile_preset(directory).document
    schema = after["components"]["schemas"]["GetDeclaredListResponse"]
    assert "response" in schema["properties"]
    assert "total" not in schema["properties"], "강등된 소스 선언이 되살아났다"

    meta = after["info"][X_MCPORTAL]
    assert meta["generation_mode"] == "sampled"
    assert meta["sample_count"] == 2
    # 채우지 못한 자리는 여전히 미확정이다(허위 확정 금지).
    assert meta["schema_inference"]["unresolved"] == 1

    info = preset_info(directory)
    assert (info.unresolved_count, info.resolved_by_sampling) == (1, 1)
    assert info.sampled_path == directory / PRESET_SAMPLED_FILENAME


def test_sampled_layer_is_absent_from_the_uncurated_comparison_group(
    tmp_path: Path,
) -> None:
    """``curated=False`` 비교군에는 측정층을 얹지 않는다(자동생성 단독의 정의).

    실측 스키마는 자동생성 산물이 아니므로, 이것이 비교군에 섞이면 벤치마크의
    A/B 가 "큐레이션 유무" 하나만 다른 대조가 아니게 된다.
    """
    directory = _sampling_bundle(tmp_path)
    baseline = dumps(compile_preset(directory, curated=False).document)

    apply_sampled_schemas(
        directory, _synthetic_results(), sampled_on="2026-08-09"
    )
    document = compile_preset(directory, curated=False).document
    assert dumps(document) == baseline
    assert document["info"][X_MCPORTAL]["generation_mode"] == "offline"


def test_bundle_without_sampled_file_keeps_offline_behaviour(tmp_path: Path) -> None:
    """측정층이 없는 번들은 1바이트도 달라지지 않는다(기존 4번들의 조건)."""
    directory = _bundle(tmp_path, curation=_curation_document())
    document = compile_preset(directory).document
    meta = document["info"][X_MCPORTAL]
    assert meta["generation_mode"] == "offline"
    assert meta["sample_count"] == 0

    info = preset_info(directory)
    assert info.sampled_path is None
    assert info.resolved_by_sampling == 0

    write_preset(directory)
    assert check_preset(directory) is True


def test_applying_samples_twice_is_byte_stable(tmp_path: Path) -> None:
    """같은 표본을 다시 적용해도 두 파일 모두 바이트가 그대로다."""
    directory = _sampling_bundle(tmp_path)
    results = _synthetic_results()
    apply_sampled_schemas(directory, results, sampled_on="2026-08-09")
    openapi_bytes = (directory / "openapi.json").read_bytes()
    sampled_bytes = (directory / PRESET_SAMPLED_FILENAME).read_bytes()

    apply_sampled_schemas(directory, dict(results), sampled_on="2026-08-09")
    assert (directory / "openapi.json").read_bytes() == openapi_bytes
    assert (directory / PRESET_SAMPLED_FILENAME).read_bytes() == sampled_bytes


def test_applying_samples_without_usable_responses_is_refused(tmp_path: Path) -> None:
    """정상 응답이 0건이면 산출물을 'sampled' 로 라벨링하지 않는다."""
    directory = _sampling_bundle(tmp_path)
    failed = {
        "getUnknownList": (
            SampleResult(
                operation_id="getUnknownList",
                status_code=500,
                ok=False,
                result_code="99",
                source_format="json",
                payload={"response": {"header": {"resultCode": "99"}}},
            ),
        )
    }
    with pytest.raises(CurationError, match="정상 응답 0건"):
        apply_sampled_schemas(directory, failed)
    assert not (directory / PRESET_SAMPLED_FILENAME).exists()


def test_partial_rerun_cannot_silently_erase_earlier_measurements(
    tmp_path: Path,
) -> None:
    """일부만 성공한 재측정이 지난 실측을 덮어쓰지 못한다.

    측정 파일은 측정 1회를 통째로 서술하므로 새 측정이 이전 파일을 대체한다.
    재실행에서 일부 오퍼레이션이 전량 실패하면 그 자리는 폴백으로 되돌아가는데,
    보고서에는 "확정 N건"만 남아 성공처럼 보인다 - 산출물이 조용히 나빠지는
    경로라 접는다.
    """
    directory = _sampling_bundle(tmp_path)
    apply_sampled_schemas(
        directory,
        {
            **_synthetic_results("getUnknownList", count=2),
            **_synthetic_results("getDeclaredList", count=2),
        },
        sampled_on="2026-08-09",
    )
    intact = (directory / PRESET_SAMPLED_FILENAME).read_bytes()
    compiled = (directory / "openapi.json").read_bytes()

    with pytest.raises(CurationError, match="getDeclaredList"):
        apply_sampled_schemas(
            directory,
            _synthetic_results("getUnknownList", count=2),
            sampled_on="2026-08-10",
        )
    assert (directory / PRESET_SAMPLED_FILENAME).read_bytes() == intact
    assert (directory / "openapi.json").read_bytes() == compiled


def test_write_sampled_schemas_blocks_credential_assignment(tmp_path: Path) -> None:
    """기록 직전 게이트가 인증키 대입을 막고 파일을 남기지 않는다.

    카세트·샘플의 스크러빙과 별개의 층이다. 추론 스키마의 ``description`` ·
    ``examples`` 같은 자리로 값이 새어 들어올 수 있고, 이 파일은 커밋 대상이다.
    """
    directory = _sampling_bundle(tmp_path)
    schemas = {
        "getUnknownList": {
            "type": "object",
            "description": f"예시 호출: https://apis.example.invalid/x?serviceKey={SAMPLE_KEY}",
        }
    }
    reports = {
        "getUnknownList": SampledInference(
            sample_count=2, conflict_count=0, truncated=False
        )
    }
    with pytest.raises(CurationError, match="인증키"):
        write_sampled_schemas(
            directory, schemas, reports, preset_id=SAMPLE_PRESET_ID
        )
    assert not (directory / PRESET_SAMPLED_FILENAME).exists()


def test_write_sampled_schemas_requires_a_report_for_every_schema(
    tmp_path: Path,
) -> None:
    """스키마와 리포트는 같은 추론 1회의 두 산출물이므로 짝이 맞아야 한다."""
    directory = _sampling_bundle(tmp_path)
    with pytest.raises(CurationError, match="추론 리포트가 없는"):
        write_sampled_schemas(
            directory,
            {"getUnknownList": {"type": "object"}},
            {},
            preset_id=SAMPLE_PRESET_ID,
        )


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        pytest.param({"mcportal_sampled": "1"}, "지원하지 않는", id="version-text"),
        pytest.param({"mcportal_sampled": True}, "지원하지 않는", id="version-bool"),
        pytest.param({"mcportal_sampled": 2}, "지원하지 않는", id="version-future"),
        pytest.param({"generated_by": "손"}, "알 수 없는 키", id="unknown-top-key"),
        pytest.param({"sampled_on": "2026-08-09T12:00:00+09:00"}, "표기가 올바르지", id="sampled-on-has-time"),
        pytest.param({"sampled_on": "2026-02-30"}, "실재하는 날짜", id="sampled-on-not-a-date"),
        pytest.param({"operations": {}}, "비어 있습니다", id="no-operations"),
    ],
)
def test_sampled_schema_violations_are_refused(
    overrides: dict[str, Any], message: str
) -> None:
    """문서 단독으로 볼 수 있는 규약 위반은 읽는 즉시 막는다."""
    with pytest.raises(CurationError, match=message):
        load_sampled_schemas(_sampled_payload(**overrides))


@pytest.mark.parametrize(
    ("provenance", "message"),
    [
        pytest.param({"sample_count": 2, "call_count": 2}, "cassette 필드가 없습니다", id="missing-cassette"),
        pytest.param(
            {
                "cassette": None,
                "cassette_sha256": None,
                "sample_count": 2,
                "call_count": 2,
                "operator": "누군가",
            },
            "알 수 없는 키",
            id="unknown-key",
        ),
        pytest.param(
            {"cassette": None, "cassette_sha256": None, "sample_count": "2", "call_count": 2},
            "기대: 정수",
            id="count-as-text",
        ),
        pytest.param(
            {"cassette": None, "cassette_sha256": None, "sample_count": 2, "call_count": -1},
            "범위를 벗어났습니다",
            id="negative-call-count",
        ),
    ],
)
def test_sampled_provenance_violations_are_refused(
    provenance: dict[str, Any], message: str
) -> None:
    """측정 출처가 없거나 형태가 어긋나면 막는다(재생 근거 없는 실측 금지)."""
    with pytest.raises(CurationError, match=message):
        load_sampled_schemas(_sampled_payload(provenance=provenance))


@pytest.mark.parametrize(
    ("operations", "message"),
    [
        pytest.param(
            {"getUnknownList": {"response_schema": {}, "inference": {"sample_count": 2, "conflicts": 0, "truncated": False}}},
            "비어 있습니다",
            id="empty-response-schema",
        ),
        pytest.param(
            {"getUnknownList": {"response_schema": {"type": "object"}}},
            "기대: 객체",
            id="missing-inference",
        ),
        pytest.param(
            {
                "getUnknownList": {
                    "response_schema": {"type": "object"},
                    "inference": {"sample_count": 0, "conflicts": 0, "truncated": False},
                }
            },
            "범위를 벗어났습니다",
            id="zero-samples",
        ),
        pytest.param(
            {
                "getUnknownList": {
                    "response_schema": {"type": "object"},
                    "inference": {"sample_count": 2, "conflicts": 0, "truncated": 1},
                }
            },
            "기대: 불리언",
            id="truncated-as-int",
        ),
        pytest.param(
            {
                "getUnknownList": {
                    "response_schema": {"type": "object"},
                    "inference": {"sample_count": 2, "conflicts": 0, "truncated": False},
                    "note": "손으로 덧붙인 메모",
                }
            },
            "알 수 없는 키",
            id="unknown-operation-key",
        ),
        pytest.param(
            {
                "9getUnknownList": {
                    "response_schema": {"type": "object"},
                    "inference": {"sample_count": 2, "conflicts": 0, "truncated": False},
                }
            },
            "ASCII 식별자",
            id="bad-operation-id",
        ),
    ],
)
def test_sampled_operation_violations_are_refused(
    operations: dict[str, Any], message: str
) -> None:
    """오퍼레이션 블록의 규약 위반도 읽는 즉시 막는다."""
    with pytest.raises(CurationError, match=message):
        load_sampled_schemas(_sampled_payload(operations=operations))


def test_sampled_preset_id_must_match_the_bundle(tmp_path: Path) -> None:
    """디렉터리명 · source.json · sampled_schemas.json 세 값이 모두 같아야 한다."""
    directory = _sampling_bundle(tmp_path)
    _write_json(
        directory / PRESET_SAMPLED_FILENAME, _sampled_payload(preset_id="99999999")
    )
    with pytest.raises(CurationError, match="프리셋 식별자가 어긋납니다"):
        compile_preset(directory)


def test_sampled_file_cannot_fill_an_operation_the_source_lacks(
    tmp_path: Path,
) -> None:
    """소스에 없는 오퍼레이션을 채우면 그 항목은 조용히 버려지므로 막는다."""
    directory = _sampling_bundle(tmp_path)
    payload = _sampled_payload()
    payload["operations"]["getGhostList"] = payload["operations"]["getUnknownList"]
    _write_json(directory / PRESET_SAMPLED_FILENAME, payload)
    with pytest.raises(CurationError, match="getGhostList"):
        compile_preset(directory)


def test_sampled_file_rejects_duplicate_keys(tmp_path: Path) -> None:
    """같은 계층의 중복 키는 앞 블록을 조용히 지우므로 파일 단계에서 막는다."""
    directory = _sampling_bundle(tmp_path)
    target = directory / PRESET_SAMPLED_FILENAME
    text = json.dumps(_sampled_payload(), ensure_ascii=False, indent=2, sort_keys=True)
    text = text.replace(
        '"preset_id": "99000009",', '"preset_id": "99000009",\n  "preset_id": "88",', 1
    )
    with open(target, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(text + "\n")
    with pytest.raises(CurationError, match="중복된 키"):
        read_sampled_schemas(target)


# ---------------------------------------------------------------------------
# 10. F-08 - source.json 의 선택 필드 key_location
# ---------------------------------------------------------------------------
def test_preset_source_defaults_to_query_key_location(tmp_path: Path) -> None:
    """``key_location`` 을 적지 않은 기존 번들은 질의문자열 그대로다."""
    directory = _bundle(tmp_path)
    source, _provenance = read_preset_source(directory / "source.json")
    assert source.key_location == "query"


def test_preset_source_reads_header_key_location(tmp_path: Path) -> None:
    """래퍼가 명시한 ``header`` 는 IR 로 그대로 전달된다."""
    wrapper = _wrapper()
    wrapper["key_location"] = "header"
    directory = _bundle(tmp_path, wrapper=wrapper)
    source, _provenance = read_preset_source(directory / "source.json")
    assert source.key_location == "header"
    assert load_preset(directory).key_location == "header"


def test_preset_source_rejects_unknown_key_location(tmp_path: Path) -> None:
    """허용값 밖의 위치는 번들을 읽는 시점에 막힌다."""
    from mcportal.compiler.sources import SourceSpecError

    wrapper = _wrapper()
    wrapper["key_location"] = "cookie"
    directory = _bundle(tmp_path, wrapper=wrapper)
    with pytest.raises(SourceSpecError, match="key_location"):
        read_preset_source(directory / "source.json")


def test_key_location_does_not_change_the_compiled_document(tmp_path: Path) -> None:
    """주입 위치는 **런타임 정책**이라 산출 OpenAPI 문서를 바꾸지 않는다.

    커밋된 프리셋 4종이 전부 ``query`` 라 산출물이 불변이어야 한다는 W4 §4 의
    요구를, 위치를 실제로 바꿔 놓고도 문서가 같음을 보여 못 박는다.
    """
    query_dir = _bundle(tmp_path / "q")
    header_wrapper = _wrapper()
    header_wrapper["key_location"] = "header"
    header_dir = _bundle(tmp_path / "h", wrapper=header_wrapper)
    assert dumps(compile_preset(query_dir).document) == dumps(
        compile_preset(header_dir).document
    )
