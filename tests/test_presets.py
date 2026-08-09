# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Yong Park
"""프리셋 번들 3종(4데이터셋) 검증: 결정론 · 시크릿 게이트 · 어댑터 회귀.

여기서만 **실제 커밋된 프리셋 번들**을 읽는다(엔진 자체의 단위 검증은
``tests/test_curation.py`` 가 합성 픽스처로 한다). 번들에는 공공 API 의 **스펙
메타데이터**와, 2026-08-09 실키 샘플링으로 받은 **실응답 데이터**(``samples/`` ·
``cassettes/``)가 함께 들어 있다. 실인증키는 0건이며 — 카세트의 키 자리는
``__SCRUBBED__`` 자리표시자로 치환돼 있고 그 사실을 아래 테스트가 강제한다 —
``presets/NOTICE-DATA.md`` 가 출처와 근거의 정본이다.

이 테스트는 네트워크를 쓰지 않는다. 샘플링은 이미 끝난 일이고, 검증은 디스크에
있는 파일만 읽는다.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest

from mcportal.compiler.curation import (
    PROVENANCE_KEYS,
    check_preset,
    compile_preset,
    iter_presets,
    load_preset,
    preset_info,
    read_curation,
    read_preset_source,
)
from mcportal.compiler.sources import (
    SourceKind,
    load_source,
    unresolved_schema_operations,
)
from mcportal.replay.scrub import find_key_assignments

PRESETS_ROOT = Path(__file__).resolve().parents[1] / "presets"

#: 데이터셋 ID → 기대하는 소스 종류.
EXPECTED_KINDS: dict[str, SourceKind] = {
    "15000115": SourceKind.REST_DOC_MANUAL,
    "15081808": SourceKind.ODCLOUD_SWAGGER,
    "15101612": SourceKind.GW_SWAGGER,
    "15102108": SourceKind.GW_SWAGGER,
}

PRESET_IDS = tuple(sorted(EXPECTED_KINDS))

#: 자유문자열 게이트가 검사하는 인증키 이름들(openapi.py 의 목록과 같은 축).
KEY_PARAM_NAMES: tuple[str, ...] = (
    "serviceKey",
    "apiKey",
    "api_key",
    "authKey",
    "auth_key",
    "accessKey",
    "access_key",
    "secretKey",
    "secret_key",
)

#: 정부 스펙 문서의 관용 표기로 허용되는 인증키 자리표시자.
#: 원문 보존 규약상 ``source.json`` 의 ``document`` 안에는 이 형태가 남을 수 있다.
ALLOWED_KEY_PLACEHOLDERS = frozenset({"[서비스키]", "[인증키]", "xxxxxx", "인증키"})

_KEY_ASSIGNMENT_RE = re.compile(r"(?<![A-Za-z0-9_])(serviceKey)=([^&#]*)", re.IGNORECASE)
_VALUE_DELIMITERS = " \t\r\n\"'\\,;)"

_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_PHONE_RE = re.compile(r"(?<!\d)01[016-9]-?\d{3,4}-?\d{4}(?!\d)")
_RRN_RE = re.compile(r"(?<!\d)\d{6}-\d{7}(?!\d)")
_BIZ_RE = re.compile(r"(?<!\d)\d{3}-\d{2}-\d{5}(?!\d)")

#: 하이픈 없는 사업자등록번호 형태(정확히 10자리). 프리셋 안에서는 자리표시자
#: ``0000000000`` 만 허용한다.
_BIZ_PLAIN_RE = re.compile(r"(?<!\d)\d{10}(?!\d)")

#: 하이픈 없는 주민등록번호 형태(6자리 + 성별코드 1~4 + 6자리). 0건이어야 한다.
#: 길이 10 이상 숫자열 전부를 금지하지 않는 이유: 포털 출력결과 표의 샘플 칸에는
#: 행정규칙일련번호(13자리)·문서 키의 타임스탬프(12자리) 같은 **개인정보가 아닌**
#: 식별자가 들어 있고, 그것들은 원문 보존 대상이다(``NOTICE-DATA.md`` §2 등재).
_RRN_PLAIN_RE = re.compile(r"(?<!\d)\d{6}[1-4]\d{6}(?!\d)")

pytestmark = pytest.mark.skipif(
    not PRESETS_ROOT.is_dir(),
    reason="프리셋 디렉터리가 없다(소스 배포본이 아닌 설치본에서 실행 중).",
)


def _document(preset_id: str) -> dict:
    """커밋된 openapi.json 을 읽는다."""
    return json.loads(
        (PRESETS_ROOT / preset_id / "openapi.json").read_text(encoding="utf-8")
    )


def _operations(document: dict) -> dict[str, dict]:
    """산출 문서의 오퍼레이션을 operationId 로 인덱싱한다."""
    return {
        operation["operationId"]: operation
        for item in document["paths"].values()
        for operation in item.values()
    }


def _params(operation: dict) -> dict[str, dict]:
    """오퍼레이션 파라미터를 이름으로 인덱싱한다."""
    return {param["name"]: param for param in operation.get("parameters", [])}


def _leading_token(value: str) -> str:
    """인증키 대입 값에서 구분자 앞의 선두 토큰만 잘라낸다."""
    for index, char in enumerate(value):
        if char in _VALUE_DELIMITERS:
            return value[:index]
    return value


# ---------------------------------------------------------------------------
# 1. 번들 로딩
# ---------------------------------------------------------------------------
def test_all_bundles_load_with_expected_source_kind() -> None:
    """4개 번들이 전부 읽히고 소스 종류가 기대와 같다."""
    infos = iter_presets(PRESETS_ROOT)
    assert [info.preset_id for info in infos] == list(PRESET_IDS)

    for preset_id, kind in EXPECTED_KINDS.items():
        source, provenance = read_preset_source(
            PRESETS_ROOT / preset_id / "source.json"
        )
        assert source.source_kind is kind
        assert source.service_id == preset_id
        assert source.source_url is not None
        assert source.license_note
        # 출처 메타 6키는 커밋 번들의 내용 규약이다(로더가 아니라 여기서 강제한다).
        for key in PROVENANCE_KEYS:
            assert provenance.get(key), f"{preset_id}: provenance.{key} 가 비었다"
        assert provenance["raw_sha256"].startswith("sha256:")
        assert "인증키 사용 0회" in provenance["acquisition"]


def test_preset_groups_cover_three_domains() -> None:
    """4데이터셋이 3개 묶음으로 라벨링돼 있다(기획안의 '3종(4데이터셋)')."""
    groups = {info.preset_id: info.group for info in iter_presets(PRESETS_ROOT)}
    assert all(group for group in groups.values())
    assert len(set(groups.values())) == 3
    assert groups["15101612"] == groups["15102108"]


# ---------------------------------------------------------------------------
# 2. 결정론
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("preset_id", PRESET_IDS)
def test_committed_openapi_matches_regeneration(preset_id: str) -> None:
    """커밋된 openapi.json 이 재생성 결과와 바이트 동일하다."""
    assert check_preset(PRESETS_ROOT / preset_id) is True


@pytest.mark.parametrize("preset_id", PRESET_IDS)
def test_compilation_is_repeatable(preset_id: str) -> None:
    """같은 번들을 두 번 컴파일하면 같은 문서가 나온다."""
    first = compile_preset(PRESETS_ROOT / preset_id).document
    second = compile_preset(PRESETS_ROOT / preset_id).document
    assert json.dumps(first, sort_keys=True) == json.dumps(second, sort_keys=True)


# ---------------------------------------------------------------------------
# 3. 시크릿 게이트
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("preset_id", PRESET_IDS)
def test_no_key_assignment_in_outputs(preset_id: str) -> None:
    """산출물과 큐레이션 문서에는 인증키 대입이 0건이다."""
    for name in ("openapi.json", "curation.json"):
        text = (PRESETS_ROOT / preset_id / name).read_text(encoding="utf-8")
        assert find_key_assignments(text, KEY_PARAM_NAMES) == (), f"{preset_id}/{name}"


@pytest.mark.parametrize("preset_id", PRESET_IDS)
def test_source_json_keeps_only_placeholders(preset_id: str) -> None:
    """source.json 의 원문 보존 구간에는 자리표시자만 남는다.

    설계 §4 는 ``document`` 안의 ``인증키=<자리표시자>`` 표기를 원문 보존 차원에서
    허용한다(정부 스펙 문서의 관용 표기다). 그래서 여기서는 "탐지 0건"이 아니라
    **"탐지된 값이 전부 알려진 자리표시자인가"**를 본다. 실키가 들어오면 값이
    허용목록에 없으므로 즉시 걸린다.
    """
    text = (PRESETS_ROOT / preset_id / "source.json").read_text(encoding="utf-8")
    values = [
        _leading_token(match.group(2)) for match in _KEY_ASSIGNMENT_RE.finditer(text)
    ]
    unexpected = [value for value in values if value not in ALLOWED_KEY_PLACEHOLDERS]
    assert unexpected == [], f"{preset_id}/source.json 에 미지의 인증키 값: {unexpected}"


@pytest.mark.parametrize("preset_id", PRESET_IDS)
def test_no_key_parameter_in_tool_surface(preset_id: str) -> None:
    """산출 문서 어디에도 인증키 파라미터가 없다(불변식 I3)."""
    document = _document(preset_id)
    for operation in _operations(document).values():
        names = {str(param["name"]).lower() for param in operation.get("parameters", [])}
        assert "servicekey" not in names
    assert document["info"]["x-mcportal"]["key_injection"] == "transport"
    assert "securitySchemes" not in document.get("components", {})
    assert "security" not in document


# ---------------------------------------------------------------------------
# 4. 개인정보
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("preset_id", PRESET_IDS)
@pytest.mark.parametrize("name", ["openapi.json", "curation.json", "source.json"])
def test_no_personal_data_in_bundle(preset_id: str, name: str) -> None:
    """이메일·전화·주민번호형·사업자번호 0건. 10자리 숫자열은 자리표시자만."""
    text = (PRESETS_ROOT / preset_id / name).read_text(encoding="utf-8")
    where = f"{preset_id}/{name}"
    assert _EMAIL_RE.findall(text) == [], where
    assert _PHONE_RE.findall(text) == [], where
    assert _RRN_RE.findall(text) == [], where
    assert _RRN_PLAIN_RE.findall(text) == [], where
    assert _BIZ_RE.findall(text) == [], where
    real_looking = [run for run in _BIZ_PLAIN_RE.findall(text) if set(run) != {"0"}]
    assert real_looking == [], f"{where}: 자리표시자가 아닌 10자리 숫자열 {real_looking}"


# ---------------------------------------------------------------------------
# 5·6·7·8. 어댑터 회귀(정찰 F-02 · F-04 · F-06)
# ---------------------------------------------------------------------------
def test_f02_body_operations_have_no_phantom_pagination() -> None:
    """본문형 오퍼레이션에는 페이징 유령 인자가 붙지 않고 응답 형식만 남는다."""
    document = _document("15081808")
    operations = _operations(document)
    assert set(operations) == {"status", "validate"}
    for operation in operations.values():
        params = _params(operation)
        assert "page" not in params
        assert "perPage" not in params
        assert "returnType" in params
        assert params["returnType"]["required"] is False
        assert "requestBody" in operation


def test_f04_gateway_required_params_carry_examples() -> None:
    """게이트웨이 필수 파라미터의 예시값이 비표준 메타 블록에서 채워진다."""
    for preset_id in ("15101612", "15102108"):
        operation = next(iter(_operations(_document(preset_id)).values()))
        params = _params(operation)
        for name in ("strtYymm", "endYymm"):
            assert params[name]["required"] is True
            assert params[name].get("example"), f"{preset_id}/{name}: example 없음"

    # 표준 parameters 자체에는 example 이 없다는 사실을 함께 못박는다.
    raw = json.loads(
        (PRESETS_ROOT / "15101612" / "source.json").read_text(encoding="utf-8")
    )["document"]
    declared = raw["paths"]["/getNationtradeList"]["parameters"]
    assert all("example" not in param for param in declared)


def test_f06_gateway_response_schema_is_sampled_not_declared() -> None:
    """게이트웨이 2종은 선언 스키마를 버리고 **실측 추론 스키마**를 싣는다.

    이력(정찰 F-06): 두 게이트웨이 스펙의 200 응답 선언은 실제 XML 과 어긋난다고
    보고 큐레이션이 ``response.unresolved`` 로 강등했었다. 근거는 ① 루트 래퍼가
    한 겹 얕고 ② 반복 항목의 배열성이 표기돼 있지 않다는 두 가지였다.
    **2026-08-09 실키 샘플 1건씩으로 둘 다 사실로 확인됐다** — 실제 정규화 결과는
    루트 ``response`` 아래 ``header``·``body`` 가 들어가 한 겹 깊고,
    ``body.items.item`` 은 배열이다.

    그래서 강등 지시(``unresolved``)는 **유지한다.** 소스 선언을 믿지 않겠다는
    판단이 실측으로 강화됐기 때문이다. 대신 산출물에 실리는 것은 선언 스키마가
    아니라 ``sampled_schemas.json`` 의 실측 추론 스키마이고, 그 결과 남은 미확정
    개수는 0 이 된다. 이 테스트는 그 두 가지를 함께 못박는다 — 강등 지시가 살아
    있을 것, 그리고 미확정이 추론으로 실제로 메워졌을 것.
    """
    for preset_id in ("15101612", "15102108"):
        meta = _document(preset_id)["info"]["x-mcportal"]
        # 실측으로 메워졌다: 미확정 0 · 샘플에서 생성됨.
        assert meta["schema_inference"].get("unresolved", 0) == 0
        assert meta["generation_mode"] == "sampled"
        assert meta["sample_count"] == 1

        # 강등 지시와 근거는 그대로 살아 있다(선언을 믿지 않는다는 판단).
        curation = read_curation(PRESETS_ROOT / preset_id / "curation.json")
        (operation,) = curation.operations.values()
        assert operation.response is not None
        assert operation.response.unresolved is True
        assert len(operation.response.reason) > 30

        # 큐레이션 적용 소스 기준으로는 여전히 '미확정' 이다 — 추론이 그 자리를
        # 메웠을 뿐, 선언이 신뢰를 되찾은 것이 아니다.
        source = load_preset(PRESETS_ROOT / preset_id)
        (operation_id,) = _operations(_document(preset_id))
        assert unresolved_schema_operations(source) == (operation_id,)

        # 추론 스키마가 실제로 F-06 이 예측한 모양이다.
        sampled = json.loads(
            (PRESETS_ROOT / preset_id / "sampled_schemas.json").read_text(
                encoding="utf-8"
            )
        )
        schema = sampled["operations"][operation_id]["response_schema"]
        root = schema["properties"]["response"]["properties"]
        assert set(root) == {"header", "body"}
        assert root["body"]["properties"]["items"]["properties"]["item"]["type"] == (
            "array"
        )


def test_f03_empty_shell_schema_is_demoted_to_none() -> None:
    """정보량이 0인 '껍데기' 응답 스키마는 None 으로 강등된다(정찰 F-03).

    ⚠️ 설계 §13 B1-9 는 이 회귀 입력으로 ``_raw/15081808`` 의 네임스페이스 스텁
    문서를 지목하면서 "로드하면 response_schema is None" 을 기대했다. **실측 결과
    그렇게 되지 않는다** — 그 문서의 응답 정의는 껍데기 ``data`` 하나만 있는 것이
    아니라 페이징 봉투(page·perPage·totalCount·currentCount·matchCount) 정수 5개를
    함께 선언하고 있어 §7 G2 의 정보량 판정에서 5점을 받는다. 설계의 전제(정찰
    요약의 "필드 0개")가 페이로드만 가리킨 것이었다.

    그래서 이 테스트는 두 가지를 나눠서 본다.
    ① 규칙 자체는 합성 입력으로 정확히 검증한다(껍데기 → None).
    ② 실제 스텁 문서에 대해서는 **측정된 사실**을 못박는다(봉투가 살려 두고,
       페이로드 자리는 여전히 빈 껍데기로 남는다).
    """
    envelope_only = {
        "swagger": "2.0",
        "info": {"title": "가상 껍데기 서비스"},
        "host": "apis.example.invalid",
        "basePath": "/0000000/shell",
        "schemes": ["https"],
        "paths": {
            "/getShell": {
                "get": {
                    "operationId": "getShell",
                    "responses": {
                        "200": {
                            "description": "정상",
                            "schema": {"type": "object", "properties": {}},
                        }
                    },
                }
            },
            "/getShellList": {
                "get": {
                    "operationId": "getShellList",
                    "responses": {
                        "200": {
                            "description": "정상",
                            "schema": {
                                "type": "array",
                                "items": {"type": "object", "properties": {}},
                            },
                        }
                    },
                }
            },
        },
    }
    shell = load_source(envelope_only, service_id="00000000")
    assert all(operation.response_schema is None for operation in shell.operations)
    assert unresolved_schema_operations(shell) == ("getShell", "getShellList")

    # ② 실제 스텁 문서의 측정된 사실.
    stub = json.loads(
        (
            PRESETS_ROOT / "_raw" / "15081808" / "infuser_oas_nts-businessman_v1.json"
        ).read_text(encoding="utf-8")
    )
    stub_source = load_source(stub, service_id="15081808")
    for operation in stub_source.operations:
        schema = operation.response_schema
        assert schema is not None, "봉투 정수 5개가 정보량 판정을 통과한다"
        assert schema["properties"]["data"]["items"] == {
            "type": "object",
            "properties": {},
        }
    # 그리고 이 스텁은 프리셋으로 채택되지 않았다(정본은 stages 문서다).
    assert load_preset(PRESETS_ROOT / "15081808").operations[0].method == "POST"


# ---------------------------------------------------------------------------
# 10. 큐레이션 효용
# ---------------------------------------------------------------------------
def test_rest_doc_preset_has_eight_distinct_tools() -> None:
    """8개 오퍼레이션의 설명이 서로 달라야 LLM 이 도구를 고를 수 있다."""
    source = load_preset(PRESETS_ROOT / "15000115")
    assert len(source.operations) == 8
    assert all(operation.response_schema is None for operation in source.operations)
    assert len(unresolved_schema_operations(source)) == 8

    summaries = [operation.summary for operation in source.operations]
    descriptions = [operation.description for operation in source.operations]
    assert all(summaries) and all(descriptions)
    assert len(set(summaries)) == 8
    assert len(set(descriptions)) == 8

    # 자동생성 단독(비교군)에서는 8개 설명이 거의 구분되지 않는다는 대조.
    plain = load_preset(PRESETS_ROOT / "15000115", curated=False)
    plain_first = plain.operations[0].description or ""
    assert "수동 매핑 기술서" in plain_first  # 전사 문구가 그대로 도구 설명이 된다


def test_example_prompts_round_trip_into_descriptions() -> None:
    """curation.json 의 예시 프롬프트가 산출 문서 설명에 그대로 실린다(E1 왕복)."""
    total = 0
    for preset_id in PRESET_IDS:
        curation = read_curation(PRESETS_ROOT / preset_id / "curation.json")
        operations = _operations(_document(preset_id))
        for operation_id, overlay in curation.operations.items():
            if not overlay.example_prompts:
                continue
            description = operations[operation_id]["description"]
            assert "예시 프롬프트:" in description
            for prompt in overlay.example_prompts:
                assert f"- {prompt}" in description
                total += 1
    assert total >= 20, "벤치마크 표본으로 쓰기에 프롬프트가 너무 적다"


# ---------------------------------------------------------------------------
# 11·12. 파일 규약과 요약 일치
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("preset_id", PRESET_IDS)
@pytest.mark.parametrize("name", ["openapi.json", "curation.json", "source.json"])
def test_json_file_conventions(preset_id: str, name: str) -> None:
    """UTF-8(BOM 없음) · CR 0개 · 끝 개행 1개 · sort_keys 재직렬화와 동일."""
    raw = (PRESETS_ROOT / preset_id / name).read_bytes()
    assert not raw.startswith(b"\xef\xbb\xbf"), f"{preset_id}/{name}: BOM"
    text = raw.decode("utf-8")
    assert "\r" not in text, f"{preset_id}/{name}: CR 포함"
    assert text.endswith("\n") and not text.endswith("\n\n")
    payload = json.loads(text)
    assert (
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n" == text
    )


@pytest.mark.parametrize("preset_id", PRESET_IDS)
def test_preset_info_matches_document(preset_id: str) -> None:
    """PresetInfo 의 집계가 산출 문서의 수치와 일치한다."""
    info = preset_info(PRESETS_ROOT / preset_id)
    document = _document(preset_id)
    operations = _operations(document)
    assert info.operation_count == len(operations)
    assert info.unresolved_count == document["info"]["x-mcportal"][
        "schema_inference"
    ].get("unresolved", 0)
    assert info.service_id == preset_id
    assert info.preset_id == preset_id
    assert info.openapi_path == PRESETS_ROOT / preset_id / "openapi.json"
    assert info.curation_path is not None
    assert info.license_note == document["info"]["x-mcportal"]["license_note"]
    assert info.notes, "service.notes 가 비어 있다(사람이 읽을 운영 정보가 없다)"


# ---------------------------------------------------------------------------
# 14. 문서
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("preset_id", PRESET_IDS)
def test_preset_readme_states_unresolved_count_and_sampling_outcome(
    preset_id: str,
) -> None:
    """번들 README 가 미확정 개수와 **샘플링 결과**를 숫자·날짜로 적고 있다.

    W3 까지 이 테스트는 "v0.2 에서 채워진다"는 미래형 고지를 강제했다. v0.2.0 이
    실제로 그 일을 했으므로 강제 대상을 **과거형 실측 문면**으로 바꾼다. 검사
    강도는 낮추지 않는다 — 여전히 미확정 개수를 숫자로 적을 것을 요구하고, 거기에
    더해 샘플링한 번들은 측정일을 적도록 요구한다. 측정일 없는 "확정했다"는
    출처 없는 주장이기 때문이다.

    ``15081808`` 은 개인정보 축(요청 본문의 사업자등록번호) 때문에 샘플링에서
    의도적으로 뺐다. 그래서 이 번들에는 측정일 대신 **제외 사실**을 적도록
    요구한다 — 빠진 이유를 적지 않으면 "미확정 0" 이 실측처럼 읽힌다.
    """
    text = (PRESETS_ROOT / preset_id / "README.md").read_text(encoding="utf-8")
    info = preset_info(PRESETS_ROOT / preset_id)
    assert f"**{info.unresolved_count} / {info.operation_count}**" in text
    assert "무인증" in text
    assert "mcportal compile" in text

    sampled = (PRESETS_ROOT / preset_id / "sampled_schemas.json").exists()
    if sampled:
        assert "2026-08-09" in text, "샘플링한 번들인데 측정일이 없다"
        assert "sampled_schemas.json" in text
        assert "v0.2.0" in text
    else:
        assert "샘플링" in text, "샘플링하지 않았다는 사실이 적혀 있어야 한다"
        assert "제외" in text or "뺐다" in text


def test_presets_root_documents_exist() -> None:
    """프리셋 루트 문서 2종이 있고 3종(4데이터셋) 표기를 쓴다."""
    readme = (PRESETS_ROOT / "README.md").read_text(encoding="utf-8")
    notice = (PRESETS_ROOT / "NOTICE-DATA.md").read_text(encoding="utf-8")
    assert "3종(4데이터셋)" in readme
    assert "v0.2" in readme
    for preset_id in PRESET_IDS:
        assert preset_id in readme
    assert "공공누리 제1유형" in notice
    assert "인증키 사용" in notice
    # "최초" 표현 금지 규칙 회귀.
    assert "최초" not in readme
    assert "최초" not in notice


# ---------------------------------------------------------------------------
# 15. 실키 샘플링 산출물 (2026-08-09)
# ---------------------------------------------------------------------------
#: 샘플링을 실제로 수행한 번들. ``15081808`` 은 요청 본문에 사업자등록번호가
#: 실리는 개인정보 축이라 의도적으로 제외했다.
SAMPLED_PRESET_IDS: tuple[str, ...] = ("15000115", "15101612", "15102108")

#: 카세트의 인증키 자리에 들어가야 하는 자리표시자.
SCRUB_PLACEHOLDER_TEXT = "__SCRUBBED__"


def _sampling_artifacts(preset_id: str) -> tuple[Path, ...]:
    """한 번들의 샘플링 산출물 경로 전부(추론 결과 · 샘플 · 카세트)."""
    directory = PRESETS_ROOT / preset_id
    return (
        directory / "sampled_schemas.json",
        *sorted((directory / "samples").glob("*.json")),
        *sorted((directory / "cassettes").glob("*.json")),
    )


@pytest.mark.parametrize("preset_id", SAMPLED_PRESET_IDS)
def test_sampling_artifacts_follow_file_conventions(preset_id: str) -> None:
    """샘플링 산출물도 커밋 파일 규약(UTF-8 · LF · 끝개행 1개)을 지킨다.

    실키 샘플링은 파일을 **기계가 쓴다.** 사람이 손으로 만든 파일에만 규약을
    적용하면 리포에서 가장 큰 새 파일들이 규약 밖에 놓인다 — cp949 로 열리거나
    CRLF 가 섞이면 diff 가 통째로 흔들리고, 그때 진짜 변경이 무엇이었는지 볼 수
    없게 된다.
    """
    artifacts = _sampling_artifacts(preset_id)
    assert artifacts, f"{preset_id}: 샘플링 산출물이 하나도 없다"

    for path in artifacts:
        raw = path.read_bytes()
        assert raw, f"{path.name}: 빈 파일"
        assert not raw.startswith(b"\xef\xbb\xbf"), f"{path.name}: BOM 이 붙었다"
        assert b"\r\n" not in raw, f"{path.name}: CRLF 가 섞였다"
        assert raw.endswith(b"\n"), f"{path.name}: 끝개행이 없다"
        assert not raw.endswith(b"\n\n"), f"{path.name}: 끝개행이 2개 이상이다"
        # UTF-8 로 디코드되고 JSON 으로 파싱된다.
        json.loads(raw.decode("utf-8"))


@pytest.mark.parametrize("preset_id", SAMPLED_PRESET_IDS)
def test_sampling_artifacts_carry_no_live_key(preset_id: str) -> None:
    """샘플링 산출물에 살아 있는 인증키 대입이 0건이다.

    ⚠️ 이 테스트가 **증명할 수 없는 것**을 먼저 밝힌다. 실인증키 원문은 이
    리포에 들어온 적이 없으므로 "그 값이 없다"를 값으로 대조할 방법이 없다.
    검사할 수 있는 것은 **형태**뿐이다 — 인증키 자리에 자리표시자가 아닌 것이
    들어 있는지.

    ⚠️ :func:`find_key_assignments` 를 파일 전문에 그대로 걸면 **거짓 양성**이
    난다. 그 함수의 값 패턴은 ``[^&#]*`` 라서, JSON 안에 든 URL 처럼 뒤에 ``&``
    가 없으면 문서 끝까지를 값으로 삼는다(``__SCRUBBED__",\\n  "params": …``
    전체가 값이 된다 → 자리표시자와 다르다고 판정). 그 함수는 docstring 이
    밝히듯 **사람이 쓰는 자유문자열**용이다. 그래서 여기서는 JSON 을 파싱해
    **구조로** 본다 — URL 은 질의문자열을 파싱해서, 매핑은 키 이름을 보고.

    그리고 그것만으로는 **스크러빙이 통째로 꺼져도 통과할 수 있다** — 키
    파라미터가 아예 기록되지 않았다면 위반이 0 건이다. 그래서 카세트에는
    자리표시자가 **실제로 1개 이상** 있을 것을 따로 요구한다(위생 처리가
    돌았다는 양성 증거).
    """
    lowered = {name.lower() for name in KEY_PARAM_NAMES}

    def violations(node: object, where: str) -> list[str]:
        """인증키 자리에 자리표시자가 아닌 값이 들어 있는 지점을 모은다."""
        found: list[str] = []
        if isinstance(node, dict):
            for key, value in node.items():
                if str(key).lower() in lowered:
                    if value != SCRUB_PLACEHOLDER_TEXT:
                        found.append(f"{where}.{key}")
                else:
                    found.extend(violations(value, f"{where}.{key}"))
        elif isinstance(node, list):
            for index, value in enumerate(node):
                found.extend(violations(value, f"{where}[{index}]"))
        elif isinstance(node, str) and "?" in node:
            query = urlparse(node).query
            for key, values in parse_qs(query, keep_blank_values=True).items():
                if key.lower() in lowered:
                    for value in values:
                        if value != SCRUB_PLACEHOLDER_TEXT:
                            found.append(f"{where}?{key}")
        return found

    placeholders = 0
    for path in _sampling_artifacts(preset_id):
        document = json.loads(path.read_text(encoding="utf-8"))
        assert violations(document, path.name) == [], (
            f"{preset_id}/{path.name}: 인증키 자리에 자리표시자가 아닌 값이 있다"
        )
        placeholders += path.read_text(encoding="utf-8").count(
            SCRUB_PLACEHOLDER_TEXT
        )

    assert placeholders > 0, (
        f"{preset_id}: 자리표시자가 0개다 — 스크러빙이 돌지 않았을 수 있다"
    )


@pytest.mark.parametrize("preset_id", SAMPLED_PRESET_IDS)
def test_sampled_schemas_match_the_generated_document(preset_id: str) -> None:
    """``sampled_schemas.json`` 이 산출 문서·측정일과 앞뒤가 맞는다.

    추론 결과 파일과 산출물이 서로 다른 이야기를 하면 어느 쪽이 정본인지 알 수
    없다. 개수·측정일·오퍼레이션 식별자를 맞춰 둔다.
    """
    directory = PRESETS_ROOT / preset_id
    sampled = json.loads(
        (directory / "sampled_schemas.json").read_text(encoding="utf-8")
    )
    meta = _document(preset_id)["info"]["x-mcportal"]

    assert sampled["preset_id"] == preset_id
    assert sampled["sampled_on"] == "2026-08-09"
    assert meta["generation_mode"] == "sampled"

    # 추론한 오퍼레이션은 전부 산출 문서에 있는 오퍼레이션이다.
    operation_ids = set(_operations(_document(preset_id)))
    assert set(sampled["operations"]) <= operation_ids

    # 샘플 개수는 산출물과 추론 결과와 실제 파일 개수가 모두 같은 값을 말한다.
    sample_files = sorted((directory / "samples").glob("*.json"))
    assert meta["sample_count"] == sampled["provenance"]["sample_count"]
    assert len(sample_files) == len(sampled["operations"])

    # 오퍼레이션마다 추론 근거(표본 수)가 남아 있고 충돌이 없다.
    for operation_id, entry in sampled["operations"].items():
        inference = entry["inference"]
        assert inference["sample_count"] >= 1, operation_id
        assert inference["conflicts"] == 0, operation_id
        assert entry["response_schema"], operation_id
