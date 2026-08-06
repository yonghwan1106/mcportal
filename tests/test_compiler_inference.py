# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Yong Park
"""compiler.inference 테스트: 오프라인·결정론 JSON Schema 추론기.

설계서 §6(추론 규칙표 R1~R8)·§6-4(결정론 보증 요건)와 §14 B2 케이스표를 그대로
검증한다. 네트워크·실인증키·실데이터를 일절 쓰지 않으며 샘플은 100% 합성이다
(가상 기관·가상 필드값).
"""
from __future__ import annotations

import itertools
import json
import os
import subprocess
import sys
from typing import Any

import pytest

from mcportal.compiler.inference import (
    DEFAULT_CONFIG,
    InferenceConfig,
    InferenceError,
    InferenceReport,
    TypeConflict,
    infer_schema,
    infer_schema_with_report,
    json_type_of,
)

# 스키마 출력에 등장이 허용된 키(R8 화이트리스트).
_ALLOWED_SCHEMA_KEYS = frozenset({"type", "properties", "required", "items", "anyOf", "format"})

# R8 금지 키.
_FORBIDDEN_SCHEMA_KEYS = frozenset({"example", "examples", "default", "enum", "$id", "title"})


def _canonical(schema: dict[str, Any]) -> str:
    """스키마를 결정론 문자열로 직렬화한다(비교용)."""
    return json.dumps(schema, sort_keys=True, ensure_ascii=False)


def _walk_keys(node: Any) -> list[str]:
    """스키마 트리에 등장하는 모든 '스키마 키워드'를 모은다.

    ``properties`` 아래의 필드명은 사용자 데이터의 키이므로 세지 않는다.
    """
    keys: list[str] = []
    if isinstance(node, dict):
        for key, value in node.items():
            keys.append(key)
            if key == "properties" and isinstance(value, dict):
                for child in value.values():
                    keys.extend(_walk_keys(child))
            else:
                keys.extend(_walk_keys(value))
    elif isinstance(node, list):
        for element in node:
            keys.extend(_walk_keys(element))
    return keys


def _walk_strings(node: Any) -> list[str]:
    """스키마 트리의 모든 문자열 값(키 제외)을 모은다."""
    values: list[str] = []
    if isinstance(node, dict):
        for value in node.values():
            values.extend(_walk_strings(value))
    elif isinstance(node, list):
        for element in node:
            values.extend(_walk_strings(element))
    elif isinstance(node, str):
        values.append(node)
    return values


# ---------------------------------------------------------------------------
# 1. 단일 샘플
# ---------------------------------------------------------------------------
def test_single_sample_all_keys_required() -> None:
    """단일 샘플이면 기본 object 스키마가 나오고 전 키가 required 다."""
    schema = infer_schema([{"totalCount": 3, "pageNo": 1, "resultMsg": "정상"}])
    assert schema == {
        "type": "object",
        "properties": {
            "pageNo": {"type": "integer"},
            "resultMsg": {"type": "string"},
            "totalCount": {"type": "integer"},
        },
        "required": ["pageNo", "resultMsg", "totalCount"],
    }


# ---------------------------------------------------------------------------
# 2. optional 판정
# ---------------------------------------------------------------------------
def test_optional_property_drops_from_required_but_stays_in_properties() -> None:
    """3샘플 중 2개에만 있는 키는 required 에서 빠지고 properties 에는 남는다."""
    samples = [
        {"기관명": "가상연구원", "비고": "표본"},
        {"기관명": "가상관측소", "비고": "표본"},
        {"기관명": "가상센터"},
    ]
    schema, report = infer_schema_with_report(samples)
    assert set(schema["properties"]) == {"기관명", "비고"}
    assert schema["required"] == ["기관명"]
    assert report.sample_count == 3
    assert report.property_count == 2


def test_required_denominator_is_object_instance_count_not_sample_count() -> None:
    """required 모수는 '샘플 수'가 아니라 '그 위치의 object 인스턴스 수'다(R3)."""
    samples = [
        {"item": [{"a": "1"}, {"a": "2"}, {"a": "3", "b": "9"}]},
        {"item": [{"a": "4"}]},
    ]
    schema = infer_schema(samples, config=InferenceConfig(xml_singleton_arrays=False))
    items = schema["properties"]["item"]["items"]
    # object 인스턴스 4개 중 a 는 4회, b 는 1회.
    assert items["required"] == ["a"]
    assert set(items["properties"]) == {"a", "b"}


def test_null_valued_key_still_counts_as_present() -> None:
    """값이 null 이어도 키가 있으면 등장으로 세어 required + nullable 이 된다(R3)."""
    schema = infer_schema([{"code": "00"}, {"code": None}])
    assert schema["required"] == ["code"]
    assert schema["properties"]["code"] == {"type": ["string", "null"]}


# ---------------------------------------------------------------------------
# 3. 타입 승격
# ---------------------------------------------------------------------------
def test_integer_and_number_promote_to_number() -> None:
    """integer + number → number 승격 + conflict promoted:number(R1)."""
    schema, report = infer_schema_with_report([{"amount": 1}, {"amount": 2.5}])
    assert schema["properties"]["amount"] == {"type": "number"}
    assert report.conflicts == (
        TypeConflict(pointer="#/amount", types=("integer", "number"), resolution="promoted:number"),
    )


def test_promotion_applies_before_anyof() -> None:
    """{integer, number, string} → {number, string} → anyOf(R1 주석)."""
    schema, report = infer_schema_with_report(
        [{"v": 1}, {"v": 2.5}, {"v": "미상"}]
    )
    assert schema["properties"]["v"] == {"anyOf": [{"type": "number"}, {"type": "string"}]}
    resolutions = [conflict.resolution for conflict in report.conflicts]
    assert resolutions == ["anyOf", "promoted:number"]
    assert all(conflict.pointer == "#/v" for conflict in report.conflicts)
    assert report.conflicts[0].types == ("integer", "number", "string")


# ---------------------------------------------------------------------------
# 4. nullable
# ---------------------------------------------------------------------------
def test_nullable_single_type_uses_type_array() -> None:
    """string + null → {"type": ["string", "null"]} + conflict nullable(R2)."""
    schema, report = infer_schema_with_report([{"msg": "정상 처리"}, {"msg": None}])
    assert schema["properties"]["msg"] == {"type": ["string", "null"]}
    assert report.conflicts == (
        TypeConflict(pointer="#/msg", types=("null", "string"), resolution="nullable"),
    )


def test_null_only_observation_has_no_conflict() -> None:
    """관측 타입이 null 하나뿐이면 conflict 를 기록하지 않는다(R2 단서)."""
    schema, report = infer_schema_with_report([{"x": None}, {"x": None}])
    assert schema["properties"]["x"] == {"type": "null"}
    assert report.conflicts == ()


def test_nullable_inside_anyof_sorts_null_before_number() -> None:
    """anyOf 이면 {"type": "null"} 을 넣고 타입명 알파벳순으로 정렬한다(R2)."""
    schema, report = infer_schema_with_report(
        [{"v": 1.5}, {"v": "미상"}, {"v": None}]
    )
    assert schema["properties"]["v"] == {
        "anyOf": [{"type": "null"}, {"type": "number"}, {"type": "string"}]
    }
    assert {conflict.resolution for conflict in report.conflicts} == {"anyOf", "nullable"}


# ---------------------------------------------------------------------------
# 5. anyOf
# ---------------------------------------------------------------------------
def test_anyof_members_sorted_by_type_name() -> None:
    """string + object → anyOf 2개, 타입명 알파벳 정렬(R1)."""
    schema, report = infer_schema_with_report(
        [{"detail": "요약 없음"}, {"detail": {"code": "A1"}}]
    )
    assert schema["properties"]["detail"] == {
        "anyOf": [
            {"type": "object", "properties": {"code": {"type": "string"}}, "required": ["code"]},
            {"type": "string"},
        ]
    }
    assert report.conflicts == (
        TypeConflict(pointer="#/detail", types=("object", "string"), resolution="anyOf"),
    )


# ---------------------------------------------------------------------------
# 6. 배열 병합
# ---------------------------------------------------------------------------
def test_array_elements_merge_into_single_items() -> None:
    """서로 다른 키를 가진 원소들이 단일 items 로 병합된다(R4)."""
    samples = [
        {"rows": [{"a": "1", "b": "2"}, {"a": "3"}]},
        {"rows": [{"a": "5", "c": "6"}]},
    ]
    schema = infer_schema(samples)
    rows = schema["properties"]["rows"]
    assert rows["type"] == "array"
    assert set(rows["items"]["properties"]) == {"a", "b", "c"}
    assert rows["items"]["required"] == ["a"]  # a 만 3/3.


def test_array_index_is_ignored_no_prefix_items() -> None:
    """prefixItems·minItems·maxItems 는 쓰지 않는다(R4)."""
    schema = infer_schema([{"rows": ["가", "나", "다"]}])
    assert schema["properties"]["rows"] == {"type": "array", "items": {"type": "string"}}


# ---------------------------------------------------------------------------
# 7. 빈 배열
# ---------------------------------------------------------------------------
def test_empty_array_only_has_no_items_key() -> None:
    """[] 만 관측되면 items 키를 넣지 않는다(R4)."""
    schema = infer_schema([{"rows": []}, {"rows": []}])
    assert schema["properties"]["rows"] == {"type": "array"}


def test_empty_and_nonempty_array_mix_creates_items() -> None:
    """[] 와 [{...}] 이 섞이면 items 가 생긴다(R4)."""
    schema = infer_schema([{"rows": []}, {"rows": [{"a": "1"}]}])
    assert schema["properties"]["rows"] == {
        "type": "array",
        "items": {"type": "object", "properties": {"a": {"type": "string"}}, "required": ["a"]},
    }


# ---------------------------------------------------------------------------
# 8. R5 XML 단수화 보정
# ---------------------------------------------------------------------------
_XML_SINGLETON_SAMPLES: list[Any] = [
    # item 이 1건인 페이지 → normalize 가 dict 로 접는다.
    {"response": {"body": {"items": {"item": {"관측소": "가상1", "값": "10"}}}}},
    # item 이 2건인 페이지 → list.
    {
        "response": {
            "body": {
                "items": {
                    "item": [
                        {"관측소": "가상2", "값": "11", "비고": "점검"},
                        {"관측소": "가상3", "값": "12"},
                    ]
                }
            }
        }
    },
]


def test_xml_singleton_arrays_unifies_object_and_array() -> None:
    """dict/list 로 갈린 같은 위치가 array 로 통일되고 required 모수가 합산된다(R5)."""
    schema, report = infer_schema_with_report(
        _XML_SINGLETON_SAMPLES, config=InferenceConfig(xml_singleton_arrays=True)
    )
    item = schema["properties"]["response"]["properties"]["body"]["properties"]["items"][
        "properties"
    ]["item"]
    assert item["type"] == "array"
    # object 인스턴스 3개(단수 1 + 배열 2) 기준으로 required 를 판정한다.
    assert item["items"]["required"] == ["값", "관측소"]
    assert set(item["items"]["properties"]) == {"값", "관측소", "비고"}
    assert any(
        conflict.resolution == "xml_singleton_array"
        and conflict.pointer == "#/response/body/items/item"
        for conflict in report.conflicts
    )


def test_xml_singleton_arrays_disabled_falls_back_to_anyof() -> None:
    """xml_singleton_arrays=False 면 진짜 스키마 충돌로 보고 anyOf 로 남긴다(R5)."""
    schema, report = infer_schema_with_report(
        _XML_SINGLETON_SAMPLES, config=InferenceConfig(xml_singleton_arrays=False)
    )
    item = schema["properties"]["response"]["properties"]["body"]["properties"]["items"][
        "properties"
    ]["item"]
    assert "anyOf" in item
    assert [member["type"] for member in item["anyOf"]] == ["array", "object"]
    assert any(conflict.resolution == "anyOf" for conflict in report.conflicts)
    assert all(conflict.resolution != "xml_singleton_array" for conflict in report.conflicts)


def test_xml_singleton_merge_is_order_independent() -> None:
    """R5 합류도 카운터 덧셈이므로 샘플 순서에 무관하다."""
    forward = _canonical(infer_schema(_XML_SINGLETON_SAMPLES))
    backward = _canonical(infer_schema(list(reversed(_XML_SINGLETON_SAMPLES))))
    assert forward == backward


# ---------------------------------------------------------------------------
# 9·10·11. 결정론
# ---------------------------------------------------------------------------
_PERMUTATION_SAMPLES: list[Any] = [
    {"header": {"resultCode": "00", "resultMsg": "정상"}, "count": 1},
    {"header": {"resultCode": "00"}, "count": 2.5, "rows": [{"a": "1"}]},
    {"header": {"resultCode": None, "resultMsg": "없음"}, "rows": []},
    {"header": {"resultCode": "00", "resultMsg": "정상"}, "count": 3, "rows": [{"a": "2", "b": "3"}]},
]


def test_deterministic_across_all_permutations() -> None:
    """모든 입력 순열에서 직렬화 결과가 동일하다(§6-4 ①)."""
    baseline = _canonical(infer_schema(_PERMUTATION_SAMPLES))
    for permutation in itertools.permutations(_PERMUTATION_SAMPLES):
        assert _canonical(infer_schema(list(permutation))) == baseline


def test_report_is_deterministic_across_permutations() -> None:
    """리포트(충돌 목록 포함)도 순열에 무관하다."""
    _, baseline = infer_schema_with_report(_PERMUTATION_SAMPLES)
    for permutation in itertools.permutations(_PERMUTATION_SAMPLES):
        _, report = infer_schema_with_report(list(permutation))
        assert report == baseline


def test_repeated_calls_produce_identical_output() -> None:
    """같은 입력을 반복 호출해도 같은 결과다 — 캐시·전역 상태 오염 없음(§6-4 ②)."""
    first = _canonical(infer_schema(_PERMUTATION_SAMPLES))
    for _ in range(5):
        assert _canonical(infer_schema(_PERMUTATION_SAMPLES)) == first


def test_duplicated_samples_do_not_change_type_structure() -> None:
    """같은 샘플을 중복해 넣어도 타입 구조는 변하지 않는다(§6-4 ③ 멱등)."""
    one = infer_schema([{"a": "1", "b": 2}])
    two = infer_schema([{"a": "1", "b": 2}, {"a": "1", "b": 2}])
    assert one == two


def test_properties_and_required_are_alphabetically_sorted() -> None:
    """키가 많은 객체에서도 properties·required 가 알파벳 오름차순이다(§6-4)."""
    keys = [f"field{index:02d}" for index in range(24)]
    sample = {key: "값" for key in keys}
    optional = {key: "값" for key in keys if key != "field07"}
    schema = infer_schema([sample, optional])
    assert list(schema["properties"]) == sorted(keys)
    assert schema["required"] == sorted(key for key in keys if key != "field07")


def test_deterministic_under_different_hash_seeds() -> None:
    """PYTHONHASHSEED 가 달라도 결과가 같다 — set 순회 의존 방지(§6-4 ④).

    파이썬 ``str`` 해시는 실행마다 랜덤화되므로, 같은 프로세스 안에서만 검사하면
    이 결정론 파괴 경로를 못 잡는다. 별도 인터프리터 2개를 서로 다른 시드로
    띄워 바이트 동일성을 확인한다.
    """
    script = (
        "import json\n"
        "from mcportal.compiler.inference import infer_schema_with_report\n"
        "samples = [\n"
        "    {'h': {'c': '00', 'm': 'ok'}, 'n': 1, 'r': [{'a': '1'}]},\n"
        "    {'h': {'c': None}, 'n': 2.5, 'r': []},\n"
        "    {'h': {'c': '00', 'm': 'ok'}, 'x': {'y': 'z'}, 'r': [{'a': '2', 'b': '3'}]},\n"
        "    {'h': {'c': '00'}, 'n': 'na', 'r': [{'a': '4'}]},\n"
        "]\n"
        "schema, report = infer_schema_with_report(samples)\n"
        "payload = {'schema': schema, 'conflicts': "
        "[[c.pointer, list(c.types), c.resolution] for c in report.conflicts]}\n"
        "print(json.dumps(payload, sort_keys=True, ensure_ascii=True))\n"
    )
    outputs: list[str] = []
    for seed in ("0", "12345"):
        env = dict(os.environ)
        env["PYTHONHASHSEED"] = seed
        completed = subprocess.run(
            [sys.executable, "-c", script],
            capture_output=True,
            text=True,
            env=env,
            check=False,
        )
        assert completed.returncode == 0, completed.stderr
        outputs.append(completed.stdout.strip())
    assert outputs[0] == outputs[1]


# ---------------------------------------------------------------------------
# 12. max_depth
# ---------------------------------------------------------------------------
def test_max_depth_truncates_to_empty_schema() -> None:
    """깊이 상한을 넘는 위치는 {}(any)로 절단되고 truncated=True 다(R7)."""
    schema, report = infer_schema_with_report(
        [{"a": {"b": {"c": 1}}}], config=InferenceConfig(max_depth=1)
    )
    assert schema["properties"]["a"]["properties"]["b"] == {}
    assert report.truncated is True
    assert report.max_depth_seen == 1


def test_no_truncation_within_depth_budget() -> None:
    """상한 안이면 truncated 가 False 다."""
    _, report = infer_schema_with_report([{"a": {"b": {"c": 1}}}])
    assert report.truncated is False
    assert report.max_depth_seen == 3


# ---------------------------------------------------------------------------
# 13. max_properties
# ---------------------------------------------------------------------------
def test_max_properties_keeps_alphabetical_prefix() -> None:
    """속성 수 상한 초과 시 알파벳 앞에서부터 상한까지만 채택한다(R3)."""
    sample = {"e": 5, "d": 4, "c": 3, "b": 2, "a": 1}
    schema, report = infer_schema_with_report(
        [sample], config=InferenceConfig(max_properties=3)
    )
    assert list(schema["properties"]) == ["a", "b", "c"]
    assert schema["required"] == ["a", "b", "c"]
    assert report.truncated is True
    assert report.property_count == 3


# ---------------------------------------------------------------------------
# 14. max_samples
# ---------------------------------------------------------------------------
def test_max_samples_uses_only_leading_samples() -> None:
    """max_samples 초과 입력은 앞에서부터 그 개수만 쓴다."""
    samples: list[Any] = [{"a": index} for index in range(5)]
    samples.append({"a": 5, "여섯번째만있는키": "무시되어야함"})
    schema, report = infer_schema_with_report(samples)
    assert report.sample_count == 5
    assert list(schema["properties"]) == ["a"]
    assert DEFAULT_CONFIG.max_samples == 5


def test_max_samples_below_one_is_rejected() -> None:
    """max_samples < 1 은 설정 오류다."""
    with pytest.raises(InferenceError) as excinfo:
        infer_schema([{"a": 1}], config=InferenceConfig(max_samples=0))
    assert "max_samples" in str(excinfo.value)


# ---------------------------------------------------------------------------
# 15. format
# ---------------------------------------------------------------------------
def test_format_date_when_all_strings_match() -> None:
    """모든 문자열이 YYYY-MM-DD 면 format: date 를 넣는다(R6)."""
    schema = infer_schema([{"기준일": "2026-01-02"}, {"기준일": "2026-03-04"}])
    assert schema["properties"]["기준일"] == {"type": "string", "format": "date"}


def test_format_absent_when_any_string_deviates() -> None:
    """하나라도 후보를 벗어나면 format 을 넣지 않는다(R6 교집합)."""
    schema = infer_schema([{"기준일": "2026-01-02"}, {"기준일": "2026-1-2"}])
    assert schema["properties"]["기준일"] == {"type": "string"}


def test_empty_string_kills_format() -> None:
    """빈 문자열이 하나라도 있으면 교집합이 비어 format 이 없다(R6)."""
    schema = infer_schema([{"기준일": "2026-01-02"}, {"기준일": ""}])
    assert schema["properties"]["기준일"] == {"type": "string"}


def test_format_date_time_and_uri() -> None:
    """date-time·uri 후보도 판정한다(R6)."""
    schema = infer_schema(
        [
            {"등록시각": "2026-01-02T03:04:05", "출처": "https://example.invalid/a"},
            {"등록시각": "2026-05-06 07:08:09", "출처": "http://example.invalid/b"},
        ]
    )
    assert schema["properties"]["등록시각"]["format"] == "date-time"
    assert schema["properties"]["출처"]["format"] == "uri"


def test_detect_formats_false_disables_format() -> None:
    """detect_formats=False 면 언제나 format 이 없다."""
    schema = infer_schema(
        [{"기준일": "2026-01-02"}], config=InferenceConfig(detect_formats=False)
    )
    assert schema["properties"]["기준일"] == {"type": "string"}


# ---------------------------------------------------------------------------
# 16. json_type_of
# ---------------------------------------------------------------------------
def test_json_type_of_bool_is_boolean_not_integer() -> None:
    """bool 은 int 의 서브클래스지만 boolean 으로 판정된다(파이썬 함정)."""
    assert json_type_of(True) == "boolean"
    assert json_type_of(False) == "boolean"
    assert json_type_of(1) == "integer"
    assert json_type_of(1.0) == "number"
    assert json_type_of(None) == "null"
    assert json_type_of("가") == "string"
    assert json_type_of([]) == "array"
    assert json_type_of({}) == "object"


def test_boolean_does_not_leak_into_integer_schema() -> None:
    """boolean 과 integer 가 섞이면 승격이 아니라 anyOf 다."""
    schema = infer_schema([{"flag": True}, {"flag": 1}])
    assert schema["properties"]["flag"] == {"anyOf": [{"type": "boolean"}, {"type": "integer"}]}


def test_json_type_of_rejects_non_json_value() -> None:
    """JSON 으로 다룰 수 없는 값은 InferenceError 다."""
    with pytest.raises(InferenceError):
        json_type_of(object())  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# 17. 빈 샘플
# ---------------------------------------------------------------------------
def test_empty_sample_list_raises() -> None:
    """샘플이 없으면 InferenceError(한국어 메시지)."""
    with pytest.raises(InferenceError) as excinfo:
        infer_schema([])
    assert "샘플" in str(excinfo.value)


# ---------------------------------------------------------------------------
# 18. R8 금지 항목
# ---------------------------------------------------------------------------
def test_forbidden_keywords_never_appear_in_schema() -> None:
    """example·default·enum·title·$id 가 출력에 없다(R8)."""
    samples = [
        {"코드": "00", "값": 1, "목록": [{"이름": "가상항목"}], "여부": True},
        {"코드": "03", "값": 2.5, "목록": [], "여부": False, "옵션": None},
    ]
    schema = infer_schema(samples)
    keys = set(_walk_keys(schema))
    assert keys & _FORBIDDEN_SCHEMA_KEYS == set()
    assert keys <= _ALLOWED_SCHEMA_KEYS


def test_sample_values_do_not_leak_into_schema() -> None:
    """샘플의 문자열 '값'이 스키마에 등장하지 않는다(무키·위생 원칙)."""
    secretish = "가상값-ZZTOP-0001"
    schema = infer_schema([{"필드": secretish}, {"필드": "가상값-ZZTOP-0002"}])
    serialized = _canonical(schema)
    assert secretish not in serialized
    assert "ZZTOP" not in serialized
    # 값은 사라지고 타입명과 키(필드명)만 남는다.
    assert "필드" in schema["properties"]
    assert set(_walk_strings(schema)) == {"object", "string", "필드"}


# ---------------------------------------------------------------------------
# 19. 리포트
# ---------------------------------------------------------------------------
def test_conflicts_sorted_by_pointer_ascending() -> None:
    """conflicts 는 pointer 오름차순으로 정렬된다."""
    samples = [
        {"zeta": 1, "alpha": "가", "mid": {"deep": 1}},
        {"zeta": 2.5, "alpha": None, "mid": {"deep": 2.5}},
    ]
    _, report = infer_schema_with_report(samples)
    pointers = [conflict.pointer for conflict in report.conflicts]
    assert pointers == sorted(pointers)
    assert pointers == ["#/alpha", "#/mid/deep", "#/zeta"]


def test_report_fields_and_immutability() -> None:
    """리포트·충돌 dataclass 는 frozen 이고 conflicts 는 tuple 이다."""
    _, report = infer_schema_with_report([{"a": 1}, {"a": 2.5}])
    assert isinstance(report, InferenceReport)
    assert isinstance(report.conflicts, tuple)
    assert isinstance(report.conflicts[0].types, tuple)
    with pytest.raises(Exception):
        report.sample_count = 9  # type: ignore[misc]
    with pytest.raises(Exception):
        report.conflicts[0].pointer = "#"  # type: ignore[misc]
    assert isinstance(DEFAULT_CONFIG, InferenceConfig)
    with pytest.raises(Exception):
        DEFAULT_CONFIG.max_samples = 9  # type: ignore[misc]


# ---------------------------------------------------------------------------
# 20. 최상위가 배열·스칼라
# ---------------------------------------------------------------------------
def test_top_level_array_samples() -> None:
    """최상위가 배열인 샘플도 처리한다."""
    schema = infer_schema([[1, 2], [3]])
    assert schema == {"type": "array", "items": {"type": "integer"}}


def test_top_level_scalar_samples() -> None:
    """최상위가 스칼라인 샘플도 처리한다."""
    assert infer_schema(["가", "나"]) == {"type": "string"}
    assert infer_schema([None]) == {"type": "null"}


def test_normalized_data_wrapper_shape() -> None:
    """normalize 가 배열 응답을 {"data": [...]} 로 감싼 형태도 처리한다."""
    samples = [
        {"data": [{"이름": "가상1"}], "currentCount": 1},
        {"data": [{"이름": "가상2", "구분": "표본"}], "currentCount": 1},
    ]
    schema = infer_schema(samples)
    assert schema["properties"]["data"]["type"] == "array"
    assert set(schema["properties"]["data"]["items"]["properties"]) == {"이름", "구분"}
    assert schema["properties"]["data"]["items"]["required"] == ["이름"]


# ---------------------------------------------------------------------------
# 부가: 한국어 보존 · 봉투 통째 추론 · 순수성
# ---------------------------------------------------------------------------
def test_korean_field_names_survive_roundtrip() -> None:
    """한국어 필드명이 손상 없이 보존된다(인코딩 회귀 방지)."""
    sample = {"응답": {"머리말": {"결과코드": "00", "결과메시지": "정상 처리되었습니다."}}}
    schema = infer_schema([sample])
    header = schema["properties"]["응답"]["properties"]["머리말"]
    assert list(header["properties"]) == ["결과메시지", "결과코드"]
    assert header["required"] == ["결과메시지", "결과코드"]
    # 직렬화 왕복에서도 글자가 깨지지 않는다.
    assert json.loads(json.dumps(schema, ensure_ascii=False)) == schema
    assert "결과코드" in json.dumps(schema, ensure_ascii=False)


def test_envelope_is_not_stripped() -> None:
    """봉투(response.header.resultCode)를 벗기지 않고 통째로 추론한다(§6-1)."""
    samples = [
        {"response": {"header": {"resultCode": "00", "resultMsg": "정상"}, "body": {"totalCount": 1}}},
        {"response": {"header": {"resultCode": "00", "resultMsg": "정상"}, "body": {"totalCount": 2}}},
    ]
    schema = infer_schema(samples)
    assert set(schema["properties"]) == {"response"}
    assert set(schema["properties"]["response"]["properties"]) == {"body", "header"}


def test_inputs_are_not_mutated() -> None:
    """추론은 순수 함수다 — 입력 샘플을 건드리지 않는다."""
    samples: list[Any] = [{"a": {"b": [1, 2]}}, {"a": {"b": []}}]
    snapshot = json.dumps(samples, sort_keys=True)
    infer_schema(samples)
    assert json.dumps(samples, sort_keys=True) == snapshot


def test_default_config_values_match_contract() -> None:
    """DEFAULT_CONFIG 기본값이 설계 계약(§6-1)과 일치한다."""
    assert DEFAULT_CONFIG == InferenceConfig(
        max_samples=5,
        max_depth=12,
        max_properties=200,
        detect_formats=True,
        xml_singleton_arrays=True,
    )
