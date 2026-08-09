# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Yong Park
"""compiler.curation 테스트: 오버레이 스키마 검증 · 병합 결정론 · 프리셋 번들 로딩.

픽스처는 100% 합성이다. 가상 기관·가상 데이터셋 ID·``.invalid`` 도메인만 쓰며
실인증키·실데이터·실네트워크는 어디에도 없다(이 모듈은 HTTP를 하지 않으므로
respx 모킹도 필요하지 않다). 실제 프리셋 번들을 쓰는 검증은
``tests/test_presets.py`` 가 담당한다.
"""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path
from typing import Any

import pytest

from mcportal.compiler.curation import (
    CURATION_SCHEMA_VERSION,
    Curation,
    CurationError,
    OperationCuration,
    ParamCuration,
    ParamRemoval,
    PRESET_SOURCE_SCHEMA_VERSION,
    ResponseCuration,
    ServiceCuration,
    apply_curation,
    apply_curation_with_report,
    check_preset,
    compile_preset,
    default_presets_root,
    iter_presets,
    load_curation,
    load_preset,
    preset_info,
    read_curation,
    read_preset_source,
    validate_curation,
    write_preset,
)
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
