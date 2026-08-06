# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Yong Park
"""정규화 JSON 샘플 → JSON Schema 추론기(오프라인·결정론).

공공데이터포털 API 상당수는 응답 스키마를 공개하지 않는다. 그래서 MCPortal 은
샘플 응답 3~5건을 **관측**해 JSON Schema(2020-12 부분집합)를 만들어 낸다. 이
모듈은 그 추론기이며 다음 세 성질을 계약으로 지킨다.

1. **오프라인·순수 함수** — 네트워크·시계·난수·환경변수를 읽지 않는다.
2. **입력 순서 무관** — 샘플을 어떤 순서로 넣어도 같은 스키마가 나온다.
   값을 하나씩 누산기(:class:`_Acc`)에 관측시키고 마지막에 동결(freeze)하는
   구조를 쓰는데, 누산이 교환·결합 법칙을 만족하므로 순서 무관이 구조적으로
   보장된다. 출력에 들어가는 모든 반복은 예외 없이 ``sorted()`` 를 거친다
   (파이썬 ``str`` 해시는 실행마다 랜덤화되므로 ``set`` 순회 순서에 의존하면
   실행마다 다른 결과가 나온다 — 결정론을 깨는 가장 현실적인 경로다).
3. **샘플 값 비유출** — 출력 스키마에 ``example``·``default``·``enum``·``title``
   같은 값 유래 항목을 넣지 않는다. 샘플 값이 스키마로 새어 나가면 무키 원칙과
   스크러빙 게이트가 함께 무너진다.

입력은 ``runtime.normalize.normalize_response(...).data`` — 이미 정규화된 JSON
값이다. XML 응답은 ``runtime.normalize.xml_to_dict`` 산출물이 들어오며, 이
모듈이 XML 을 직접 파싱하는 일은 없다. 봉투(``response.header.resultCode`` 등)는
벗기지 않고 통째로 추론한다. MCP 도구가 돌려주는 것이 봉투 포함 전체 응답이기
때문이다.
"""
from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Union

__all__ = [
    "JSONValue",
    "InferenceError",
    "InferenceConfig",
    "DEFAULT_CONFIG",
    "TypeConflict",
    "InferenceReport",
    "json_type_of",
    "infer_schema",
    "infer_schema_with_report",
]

JSONValue = Union[None, bool, int, float, str, list["JSONValue"], dict[str, "JSONValue"]]

#: 루트 위치를 가리키는 JSON 포인터.
_ROOT_POINTER = "#"

#: 배열 원소 누산기의 포인터 토큰.
_ITEMS_TOKEN = "items"

#: R6 format 후보. 이름 → 정규식. ``date`` 와 ``date-time`` 은 상호 배타적이다.
_FORMAT_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("date", re.compile(r"^\d{4}-\d{2}-\d{2}$")),
    ("date-time", re.compile(r"^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}")),
    ("uri", re.compile(r"^https?://")),
)


class InferenceError(ValueError):
    """샘플이 없거나 추론이 불가능할 때 발생한다(한국어 메시지)."""


@dataclass(frozen=True)
class InferenceConfig:
    """추론기 동작 설정. 전 필드가 결정론에 영향을 주지 않는 상한·스위치다.

    Attributes:
        max_samples: 사용할 샘플 수 상한. 초과분은 정규화 직렬화 문자열
            오름차순으로 정렬한 뒤 앞에서부터 골라 쓰고 ``truncated`` 를 세운다
            (입력 순서에 결과가 의존하지 않게 하기 위해서다).
        max_depth: 관측 깊이 상한(루트가 0). 초과 깊이는 ``{}``(any)로 절단한다.
        max_properties: 한 객체의 속성 수 상한. 초과분은 알파벳 뒤쪽부터 버린다.
        detect_formats: ``date``·``date-time``·``uri`` format 판정 여부.
        xml_singleton_arrays: XML 단수화 보정(R5) 적용 여부. XML 유래 샘플에서만
            켠다. JSON 소스에서 object/array 가 섞이면 그건 진짜 스키마 충돌이다.
    """

    max_samples: int = 5
    max_depth: int = 12
    max_properties: int = 200
    detect_formats: bool = True
    xml_singleton_arrays: bool = True


DEFAULT_CONFIG: InferenceConfig = InferenceConfig()


@dataclass(frozen=True)
class TypeConflict:
    """한 위치에서 서로 다른 타입이 관측된 사실의 기록.

    Attributes:
        pointer: 관측 위치의 JSON 포인터(``"#"``, ``"#/response/header/resultCode"``).
        types: 그 위치에서 관측된 타입명(알파벳 오름차순).
        resolution: 해소 방식. ``"promoted:number"`` · ``"nullable"`` ·
            ``"xml_singleton_array"`` · ``"anyOf"`` 중 하나.
    """

    pointer: str
    types: tuple[str, ...]
    resolution: str


@dataclass(frozen=True)
class InferenceReport:
    """추론 1회의 관측 요약(``x-mcportal`` 메타에 실린다).

    Attributes:
        sample_count: 실제로 사용한 샘플 수(``max_samples`` 적용 후).
        property_count: 생성된 ``properties`` 총 개수(중첩 포함).
        max_depth_seen: 관측된 최대 깊이(루트가 0).
        truncated: 깊이·속성 수 상한으로 잘라낸 곳이 있으면 True.
        conflicts: 타입 충돌 기록(``pointer`` 오름차순).
    """

    sample_count: int
    property_count: int
    max_depth_seen: int
    truncated: bool
    conflicts: tuple[TypeConflict, ...]


# ---------------------------------------------------------------------------
# 타입 판정
# ---------------------------------------------------------------------------
def json_type_of(value: JSONValue) -> str:
    """JSON 타입명을 돌려준다.

    Args:
        value: 판정할 JSON 값.

    Returns:
        ``"null"`` · ``"boolean"`` · ``"integer"`` · ``"number"`` · ``"string"`` ·
        ``"array"`` · ``"object"`` 중 하나.

    Raises:
        InferenceError: JSON 값으로 다룰 수 없는 파이썬 타입일 때.

    Note:
        ``bool`` 은 ``int`` 의 서브클래스이므로 반드시 ``bool`` 을 먼저 판정한다
        (파이썬 함정). 이 순서를 뒤집으면 ``True`` 가 ``integer`` 로 새어 나간다.
    """
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, Mapping):
        return "object"
    if isinstance(value, (list, tuple)):
        return "array"
    raise InferenceError(f"JSON 값으로 다룰 수 없는 타입입니다: {type(value).__name__}")


def _format_candidates(text: str) -> set[str]:
    """문자열 하나가 만족하는 format 후보 집합을 돌려준다(R6)."""
    return {name for name, pattern in _FORMAT_PATTERNS if pattern.match(text)}


def _escape_token(token: str) -> str:
    """JSON 포인터 토큰을 이스케이프한다(``~`` → ``~0``, ``/`` → ``~1``)."""
    return token.replace("~", "~0").replace("/", "~1")


def _child_pointer(pointer: str, token: str) -> str:
    """부모 포인터에 자식 토큰을 이어 붙인다."""
    return f"{pointer}/{_escape_token(token)}"


# ---------------------------------------------------------------------------
# 내부 누산기
# ---------------------------------------------------------------------------
@dataclass
class _Acc:
    """한 위치에서 관측된 값들의 누산기(내부 전용·가변).

    공개 API 가 아니지만 결정론을 보장하는 유일한 구조이므로 설계서 §6-2 의
    필드 구성을 그대로 따른다. 카운터 누적만 하므로 관측 순서에 무관하다.
    """

    types: set[str] = field(default_factory=set)
    total: int = 0
    object_count: int = 0
    prop_seen: dict[str, int] = field(default_factory=dict)
    props: dict[str, "_Acc"] = field(default_factory=dict)
    array_count: int = 0
    item: "_Acc | None" = None
    string_count: int = 0
    formats: set[str] | None = None
    truncated: bool = False


@dataclass
class _ObserveState:
    """관측 단계에서 누적하는 전역 상태(내부 전용)."""

    config: InferenceConfig
    max_depth_seen: int = 0
    truncated: bool = False


@dataclass
class _FreezeState:
    """동결 단계에서 누적하는 전역 상태(내부 전용)."""

    config: InferenceConfig
    property_count: int = 0
    truncated: bool = False
    conflicts: dict[tuple[str, str, tuple[str, ...]], TypeConflict] = field(default_factory=dict)

    def record(self, pointer: str, types: tuple[str, ...], resolution: str) -> None:
        """타입 충돌을 중복 없이 기록한다."""
        self.conflicts.setdefault(
            (pointer, resolution, types),
            TypeConflict(pointer=pointer, types=types, resolution=resolution),
        )


def _merge_acc(left: _Acc, right: _Acc) -> _Acc:
    """두 누산기를 합친 **새** 누산기를 만든다(입력은 변경하지 않는다).

    R5(XML 단수화 보정)에서 object 로 관측된 인스턴스를 배열 원소 누산기에
    합류시킬 때 쓴다. 모든 병합 연산이 합집합·덧셈·교집합이므로 교환·결합
    법칙을 만족한다.
    """
    merged = _Acc()
    merged.types = set(left.types) | set(right.types)
    merged.total = left.total + right.total
    merged.object_count = left.object_count + right.object_count

    merged.prop_seen = dict(left.prop_seen)
    for key, count in right.prop_seen.items():
        merged.prop_seen[key] = merged.prop_seen.get(key, 0) + count

    for key in sorted(set(left.props) | set(right.props)):
        left_child = left.props.get(key)
        right_child = right.props.get(key)
        if left_child is not None and right_child is not None:
            merged.props[key] = _merge_acc(left_child, right_child)
        elif left_child is not None:
            merged.props[key] = left_child
        elif right_child is not None:
            merged.props[key] = right_child

    merged.array_count = left.array_count + right.array_count
    if left.item is None:
        merged.item = right.item
    elif right.item is None:
        merged.item = left.item
    else:
        merged.item = _merge_acc(left.item, right.item)

    merged.string_count = left.string_count + right.string_count
    if left.formats is None:
        merged.formats = None if right.formats is None else set(right.formats)
    elif right.formats is None:
        merged.formats = set(left.formats)
    else:
        merged.formats = set(left.formats) & set(right.formats)

    merged.truncated = left.truncated or right.truncated
    return merged


def _object_part(acc: _Acc) -> _Acc:
    """누산기에서 'object 로 관측된 부분'만 떼어낸 누산기를 만든다(R5용)."""
    part = _Acc()
    part.types.add("object")
    part.total = acc.object_count
    part.object_count = acc.object_count
    part.prop_seen = dict(acc.prop_seen)
    part.props = dict(acc.props)
    part.truncated = acc.truncated
    return part


# ---------------------------------------------------------------------------
# 관측
# ---------------------------------------------------------------------------
def _observe(acc: _Acc, value: JSONValue, depth: int, state: _ObserveState) -> None:
    """값 하나를 누산기에 관측시킨다(R7 깊이 절단 포함)."""
    if depth > state.config.max_depth:
        acc.truncated = True
        state.truncated = True
        return
    if depth > state.max_depth_seen:
        state.max_depth_seen = depth

    kind = json_type_of(value)
    acc.types.add(kind)
    acc.total += 1

    if kind == "object":
        assert isinstance(value, Mapping)
        acc.object_count += 1
        keys = list(value.keys())
        for key in keys:
            if not isinstance(key, str):
                raise InferenceError(
                    f"객체 키는 문자열이어야 합니다: {type(key).__name__}"
                )
        for key in sorted(keys):
            acc.prop_seen[key] = acc.prop_seen.get(key, 0) + 1
            child = acc.props.get(key)
            if child is None:
                child = _Acc()
                acc.props[key] = child
            _observe(child, value[key], depth + 1, state)
    elif kind == "array":
        assert isinstance(value, (list, tuple))
        acc.array_count += 1
        for element in value:
            if acc.item is None:
                acc.item = _Acc()
            _observe(acc.item, element, depth + 1, state)
    elif kind == "string":
        assert isinstance(value, str)
        acc.string_count += 1
        candidates = _format_candidates(value)
        acc.formats = candidates if acc.formats is None else (acc.formats & candidates)


# ---------------------------------------------------------------------------
# 동결
# ---------------------------------------------------------------------------
def _object_schema(acc: _Acc, pointer: str, depth: int, state: _FreezeState) -> dict[str, Any]:
    """object 관측분을 스키마로 만든다(R3)."""
    keys = sorted(acc.prop_seen)
    if len(keys) > state.config.max_properties:
        state.truncated = True
        keys = keys[: state.config.max_properties]

    properties: dict[str, Any] = {}
    for key in keys:
        child = acc.props.get(key)
        properties[key] = _freeze(
            child if child is not None else _Acc(),
            _child_pointer(pointer, key),
            depth + 1,
            state,
        )
        state.property_count += 1

    schema: dict[str, Any] = {"type": "object"}
    if properties:
        schema["properties"] = properties
    required = [key for key in keys if acc.prop_seen[key] == acc.object_count]
    if required:
        schema["required"] = required
    return schema


def _array_schema(acc: _Acc, pointer: str, depth: int, state: _FreezeState) -> dict[str, Any]:
    """array 관측분을 스키마로 만든다(R4)."""
    schema: dict[str, Any] = {"type": "array"}
    if acc.item is not None:
        schema["items"] = _freeze(
            acc.item, _child_pointer(pointer, _ITEMS_TOKEN), depth + 1, state
        )
    return schema


def _string_schema(acc: _Acc, state: _FreezeState) -> dict[str, Any]:
    """string 관측분을 스키마로 만든다(R6)."""
    schema: dict[str, Any] = {"type": "string"}
    if state.config.detect_formats and acc.formats:
        names = sorted(acc.formats)
        if len(names) == 1:
            schema["format"] = names[0]
    return schema


def _schema_for_type(
    type_name: str, acc: _Acc, pointer: str, depth: int, state: _FreezeState
) -> dict[str, Any]:
    """단일 타입에 대한 스키마 조각을 만든다."""
    if type_name == "object":
        return _object_schema(acc, pointer, depth, state)
    if type_name == "array":
        return _array_schema(acc, pointer, depth, state)
    if type_name == "string":
        return _string_schema(acc, state)
    return {"type": type_name}


def _freeze(acc: _Acc, pointer: str, depth: int, state: _FreezeState) -> dict[str, Any]:
    """누산기를 JSON Schema 조각으로 동결한다(R1·R2·R5·R7)."""
    if acc.total == 0:
        # 관측이 하나도 없다 = 깊이 절단(R7)으로 잘린 위치. any 로 남긴다.
        if acc.truncated:
            state.truncated = True
        return {}

    observed = tuple(sorted(acc.types))
    has_null = "null" in acc.types
    effective = set(acc.types) - {"null"}

    # R1 — integer/number 승격은 다른 타입이 섞여 있어도 먼저 적용한다.
    if {"integer", "number"} <= effective:
        effective.discard("integer")
        state.record(pointer, observed, "promoted:number")

    if not effective:
        # null 만 관측 — R2 예외로 conflict 를 기록하지 않는다.
        return {"type": "null"}

    xml_singleton = state.config.xml_singleton_arrays and effective == {"object", "array"}

    if xml_singleton:
        state.record(pointer, observed, "xml_singleton_array")
        merged = _object_part(acc)
        if acc.item is not None:
            merged = _merge_acc(merged, acc.item)
        schema: dict[str, Any] = {
            "type": "array",
            "items": _freeze(
                merged, _child_pointer(pointer, _ITEMS_TOKEN), depth + 1, state
            ),
        }
    elif len(effective) == 1:
        schema = _schema_for_type(next(iter(effective)), acc, pointer, depth, state)
    else:
        state.record(pointer, observed, "anyOf")
        members = [
            _schema_for_type(type_name, acc, pointer, depth, state)
            for type_name in sorted(effective)
        ]
        if has_null:
            state.record(pointer, observed, "nullable")
            members.append({"type": "null"})
            members.sort(key=lambda member: str(member["type"]))
        return {"anyOf": members}

    if has_null:
        state.record(pointer, observed, "nullable")
        schema["type"] = [schema["type"], "null"]
    return schema


# ---------------------------------------------------------------------------
# 공개 API
# ---------------------------------------------------------------------------
def _validate_config(config: InferenceConfig) -> None:
    """설정 값의 범위를 검사한다."""
    if config.max_samples < 1:
        raise InferenceError("InferenceConfig.max_samples 는 1 이상이어야 합니다.")
    if config.max_depth < 0:
        raise InferenceError("InferenceConfig.max_depth 는 0 이상이어야 합니다.")
    if config.max_properties < 0:
        raise InferenceError("InferenceConfig.max_properties 는 0 이상이어야 합니다.")


def _sample_sort_key(sample: JSONValue) -> str:
    """샘플 1개의 정규화 직렬화 문자열(정렬용 키)을 만든다.

    ``sort_keys=True`` 이므로 키 순서가 달라도 같은 문자열이 나온다. JSON 으로
    직렬화할 수 없는 값이 섞이면 ``repr`` 로 폴백한다(정렬만을 위한 키이며
    출력 스키마에는 영향이 없다 — 실제 값 검증은 ``_observe`` 가 한다).
    """
    try:
        return json.dumps(sample, sort_keys=True, ensure_ascii=False, default=repr)
    except (TypeError, ValueError):  # pragma: no cover - 방어
        return repr(sample)


def _select_samples(
    samples: Sequence[JSONValue], max_samples: int
) -> tuple[list[JSONValue], bool]:
    """상한을 넘는 샘플을 **결정론적으로** 골라낸다.

    "앞에서부터 N개"를 쓰면 샘플이 상한을 넘는 순간 입력 순서에 따라 결과가
    달라진다 — 공개 API docstring 과 §6-4.1 이 조건 없이 약속한 "순열 무관"이
    거기서 깨진다(6개 샘플이면 순열마다 속성 유무와 ``required`` 가 달라지는데
    ``truncated`` 도 False 라 호출자가 절단 사실조차 알 수 없었다).

    그래서 절단이 필요할 때는 정규화 직렬화 문자열 오름차순으로 정렬한 뒤 앞에서
    부터 고른다. 정렬 키가 입력 순서와 무관하므로 순열 무관이 실제로 성립하고,
    같은 입력 집합이면 같은 표본이 뽑힌다.

    Args:
        samples: 입력 샘플들.
        max_samples: 사용할 샘플 수 상한.

    Returns:
        ``(사용할 샘플들, 절단 여부)`` 쌍. 절단이 없으면 입력 순서를 그대로 둔다
        (누산기가 교환·결합 법칙을 만족하므로 결과는 순서와 무관하다).
    """
    items = list(samples)
    if len(items) <= max_samples:
        return items, False
    ordered = sorted(items, key=_sample_sort_key)
    return ordered[:max_samples], True


def infer_schema_with_report(
    samples: Sequence[JSONValue],
    *,
    config: InferenceConfig = DEFAULT_CONFIG,
) -> tuple[dict[str, Any], InferenceReport]:
    """샘플들을 병합해 JSON Schema 와 관측 요약을 함께 돌려준다.

    Args:
        samples: 정규화된 JSON 샘플들. ``config.max_samples`` 를 넘으면
            :func:`_select_samples` 규칙(정규화 직렬화 문자열 오름차순)으로
            결정론적으로 골라 쓰고, 리포트의 ``truncated`` 를 True 로 세운다.
        config: 추론 설정.

    Returns:
        ``(스키마, 리포트)`` 쌍. 스키마는 ``components.schemas`` 에 그대로 넣을 수
        있는 JSON Schema(2020-12 부분집합)다.

    Raises:
        InferenceError: 샘플이 비었거나 설정 값이 범위를 벗어났거나, JSON 으로
            다룰 수 없는 값이 섞였을 때.
    """
    _validate_config(config)
    if not list(samples):
        raise InferenceError("추론할 샘플이 없습니다. 정규화 JSON 샘플이 최소 1개 필요합니다.")
    used, sample_truncated = _select_samples(samples, config.max_samples)
    if not used:
        raise InferenceError("추론할 샘플이 없습니다. 정규화 JSON 샘플이 최소 1개 필요합니다.")

    observe_state = _ObserveState(config=config)
    root = _Acc()
    for sample in used:
        _observe(root, sample, 0, observe_state)

    freeze_state = _FreezeState(config=config)
    schema = _freeze(root, _ROOT_POINTER, 0, freeze_state)

    conflicts = tuple(
        sorted(
            freeze_state.conflicts.values(),
            key=lambda conflict: (conflict.pointer, conflict.resolution, conflict.types),
        )
    )
    report = InferenceReport(
        sample_count=len(used),
        property_count=freeze_state.property_count,
        max_depth_seen=observe_state.max_depth_seen,
        truncated=(
            sample_truncated or observe_state.truncated or freeze_state.truncated
        ),
        conflicts=conflicts,
    )
    return schema, report


def infer_schema(
    samples: Sequence[JSONValue],
    *,
    config: InferenceConfig = DEFAULT_CONFIG,
) -> dict[str, Any]:
    """정규화 JSON 샘플들을 병합해 JSON Schema(2020-12 부분집합)를 만든다.

    오프라인·순수 함수이며 샘플 입력 순서가 달라도 같은 결과를 돌려준다.

    Args:
        samples: 정규화된 JSON 샘플들.
        config: 추론 설정.

    Returns:
        JSON Schema 딕셔너리.

    Raises:
        InferenceError: 샘플이 비었거나 추론이 불가능할 때.
    """
    schema, _ = infer_schema_with_report(samples, config=config)
    return schema
