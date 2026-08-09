# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Yong Park
"""명령행 인터페이스(:mod:`mcportal.cli`) 회귀 테스트.

검증 축은 넷이다.

1. **출력 계약** - 사람용 문면과 ``--json`` 스키마가 설계대로 나오는가.
   ``--json`` 은 stdout 에 JSON 만 단독으로 실려야 파이프에서 쓸 수 있다.
2. **종료 코드 계약** - 0(정상·빈 상태) · 1(실패) · 2(사용법) · 3(드리프트) ·
   130(중단).
3. **안전 계약** - 인증키 원문이 출력에 0건, CLI 가 원장을 만들지 않음,
   모든 출력이 Windows 기본 콘솔 코드페이지(cp949)로 인코딩 가능.
4. **격리** - 프리셋 관련 검사는 실제 프리셋 번들이 아니라 ``tmp_path`` 에
   만든 **합성 번들**(가상 기관 · ``*.example.invalid``)만 쓴다.
   유일한 예외는 ``test_serve_real_preset_exposes_eight_tools`` 다 - "커밋된
   번들이 무키 replay 로 실제 MCP 서버가 된다"는 주장 자체가 검증 대상이라
   합성 번들로는 증명되지 않는다. 그 테스트도 네트워크·인증키는 0건이며 커밋된
   카세트만 읽는다.

실인증키·실응답데이터·네트워크는 어디에도 없다. 합성 키는 W2 이래의 고정
문자열 ``ab12+CD/34==`` (인코딩 표기 ``ab12%2BCD%2F34%3D%3D``)를 쓴다.
"""
from __future__ import annotations

import asyncio
import importlib
import json
import os
import subprocess
import sys
import tomllib
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx

from mcportal import cli
from mcportal import mcp as mcp_module
from mcportal.quota.ledger import UsageLedger, key_fp
from mcportal.replay import Cassette
from mcportal.runtime.keys import prepare_service_key

#: 합성 인증키(디코딩 표기)와 그 인코딩 표기. 실키가 아니다.
SYNTHETIC_KEY = "ab12+CD/34=="
SYNTHETIC_KEY_ENCODED = "ab12%2BCD%2F34%3D%3D"

#: 합성 원장에 기록할 고정 시각(UTC 03:00 = KST 12:00). 날짜 경계 흔들림 차단.
FIXED_UTC = datetime(2026, 8, 6, 3, 0, tzinfo=timezone.utc)
FIXED_DAY = "2026-08-06"

#: 합성 프리셋 번들 식별자(실제 포털 데이터셋 ID 와 겹치지 않는 9로 시작하는 값).
SYNTH_PRESET_ID = "99000001"

def _curation_available() -> bool:
    """큐레이션 모듈을 실제로 임포트할 수 있는지 확인한다.

    ``importlib.util.find_spec`` 은 이미 ``sys.modules`` 에 올라 있는 모듈의
    ``__spec__`` 이 None 이면 ``ValueError`` 를 던지므로 쓰지 않는다.
    """
    try:
        importlib.import_module("mcportal.compiler.curation")
    except Exception:  # noqa: BLE001 - 부재·문법오류 등 어떤 이유든 '없음'으로 본다
        return False
    return True


#: 큐레이션 모듈(B1 산출물) 없이는 프리셋 서브커맨드를 끝까지 실행할 수 없다.
CURATION_AVAILABLE = _curation_available()
requires_curation = pytest.mark.skipif(
    not CURATION_AVAILABLE,
    reason="mcportal.compiler.curation 모듈이 아직 없다(프리셋 컴파일 계약 검증 보류)",
)


# ---------------------------------------------------------------------------
# 공용 도우미
# ---------------------------------------------------------------------------
def assert_cp949_safe(text: str, *, where: str) -> None:
    """문자열이 Windows 기본 콘솔 코드페이지로 인코딩 가능한지 확인한다.

    박스드로잉·이모지·em dash(U+2014)가 섞이면 실제 콘솔에서
    ``UnicodeEncodeError`` 로 명령이 죽는다. cp949 에 em dash 는 없다(0xA1A9 는
    U+2015 horizontal bar 다) - 설계 예시 문면을 그대로 베끼면 여기서 걸린다.
    """
    try:
        text.encode("cp949")
    except UnicodeEncodeError as exc:
        bad = text[exc.start : exc.end]
        raise AssertionError(
            f"{where} 출력에 cp949 로 인코딩할 수 없는 문자가 있습니다: "
            f"{bad!r} (U+{ord(bad[0]):04X}) / 문맥: {text[max(0, exc.start - 30):exc.end + 30]!r}"
        ) from exc


def run_cli(capsys: pytest.CaptureFixture[str], *argv: str) -> tuple[int, str, str]:
    """CLI 를 한 번 실행하고 ``(종료코드, stdout, stderr)`` 를 돌려준다.

    호출할 때마다 cp949 안전성을 함께 검사하므로, 어느 테스트에서든 콘솔에서
    죽을 문자가 새어 나오면 그 자리에서 실패한다(설계 §9-4 회귀).
    """
    code = cli.main(list(argv))
    captured = capsys.readouterr()
    assert_cp949_safe(captured.out, where=f"stdout({' '.join(argv)})")
    assert_cp949_safe(captured.err, where=f"stderr({' '.join(argv)})")
    return code, captured.out, captured.err


@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """예산·원장·프리셋 루트 환경변수를 테스트마다 지운다.

    개발자 셸에 ``CALL_BUDGET`` 이 남아 있으면 예산 우선순위 검증이 조용히
    깨진다. 원장 경로도 마찬가지로 홈 디렉터리를 건드리지 않게 격리한다.
    """
    for name in ("CALL_BUDGET", "MCPORTAL_LEDGER", "MCPORTAL_PRESETS", "MCPORTAL_DEBUG"):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture()
def ledger_path(tmp_path: Path) -> Path:
    """합성 호출 3건이 기록된 원장을 만들고 커넥션을 닫아 돌려준다."""
    path = tmp_path / "ledger.db"
    ledger = UsageLedger(path)
    try:
        for index in range(3):
            ledger.record(SYNTHETIC_KEY, f"/synth/{index}", now=FIXED_UTC)
    finally:
        ledger.close()
    return path


def write_synthetic_bundle(root: Path, preset_id: str = SYNTH_PRESET_ID) -> Path:
    """합성 프리셋 번들 1개를 만든다(가상 기관 · example.invalid).

    실제 공공 스펙을 복사하지 않는다. 합성임이 자명하도록 기관명·호스트·경로를
    전부 가상값으로 둔다.
    """
    directory = root / preset_id
    directory.mkdir(parents=True, exist_ok=True)
    document: dict[str, Any] = {
        "swagger": "2.0",
        "info": {
            "title": "가상 시험기관 합성 조회 서비스",
            "description": "테스트용 합성 문서입니다. 실제 서비스가 아닙니다.",
            "version": "1.0",
        },
        "host": "apis.example.invalid",
        "basePath": "/9900000/synth",
        "schemes": ["https"],
        "produces": ["application/json"],
        "paths": {
            "/getSynthList": {
                "get": {
                    "operationId": "getSynthList",
                    "summary": "합성 목록 조회",
                    "parameters": [
                        {
                            "name": "serviceKey",
                            "in": "query",
                            "required": True,
                            "type": "string",
                            "description": "인증키(도구 인자에서 제거되어야 한다)",
                        },
                        {
                            "name": "targetYm",
                            "in": "query",
                            "required": True,
                            "type": "string",
                            "description": "조회 연월",
                            "x-example": "202601",
                        },
                    ],
                    "responses": {
                        "200": {
                            "description": "정상",
                            "schema": {
                                "type": "object",
                                "properties": {"totalCount": {"type": "integer"}},
                            },
                        }
                    },
                }
            }
        },
    }
    source = {
        "mcportal_preset_source": 1,
        "preset_id": preset_id,
        "service_id": preset_id,
        "service_name": "가상 시험기관 합성 조회",
        "source_kind": "gw_swagger",
        "key_param": "serviceKey",
        "source_url": f"https://portal.example.invalid/data/{preset_id}/openapi.do",
        "fetched_at": "2026-08-06T00:00:00+09:00",
        "license_note": "합성 픽스처(실제 이용허락범위 아님)",
        "provenance": {
            "spec_origin": "테스트가 생성한 합성 문서",
            "spec_url": f"https://portal.example.invalid/data/{preset_id}/openapi.do",
            "raw_files": [],
            "acquisition": "네트워크 호출 0회(합성)",
            "personal_data_scan": "합성 문서이므로 개인정보 0건",
            "notes": ["CLI 테스트 전용"],
        },
        "document": document,
    }
    curation = {
        "mcportal_curation": 1,
        "preset_id": preset_id,
        "service": {
            "group": "합성 시험 묶음",
            "title": "가상 시험기관 합성 조회",
            "version": "0.1.0",
            "description": "테스트 전용 합성 서비스입니다. 실제 기관이 아닙니다.",
            "license_note": "합성 픽스처(실제 이용허락범위 아님)",
            "source_url": f"https://portal.example.invalid/data/{preset_id}/openapi.do",
            "notes": ["합성 메모 첫째 줄", "합성 메모 둘째 줄"],
        },
    }
    _write_json(directory / "source.json", source)
    _write_json(directory / "curation.json", curation)
    return directory


def _write_json(path: Path, payload: Any) -> None:
    """결정론 규약(UTF-8 · LF · sort_keys · 끝 개행 1개)대로 JSON 을 쓴다."""
    text = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    path.write_text(text, encoding="utf-8", newline="\n")


# ---------------------------------------------------------------------------
# 1~2. 사용법과 종료 코드
# ---------------------------------------------------------------------------
def test_help_returns_ok(capsys: pytest.CaptureFixture[str]) -> None:
    """``--help`` 는 사용법을 stdout 에 찍고 0 으로 끝난다(예외 누출 없음)."""
    code, out, _ = run_cli(capsys, "--help")
    assert code == cli.EXIT_OK
    assert "usage: mcportal" in out
    for name in ("quota", "compile", "presets"):
        assert name in out


def test_quota_help_returns_ok(capsys: pytest.CaptureFixture[str]) -> None:
    """서브커맨드 ``--help`` 도 같은 규약을 따른다."""
    code, out, _ = run_cli(capsys, "quota", "--help")
    assert code == cli.EXIT_OK
    assert "status" in out


def test_version_returns_ok(capsys: pytest.CaptureFixture[str]) -> None:
    """``--version`` 은 패키지 버전을 찍고 0 으로 끝난다."""
    import mcportal

    code, out, _ = run_cli(capsys, "--version")
    assert code == cli.EXIT_OK
    assert mcportal.__version__ in out


def test_missing_subcommand_is_usage_error(capsys: pytest.CaptureFixture[str]) -> None:
    """서브커맨드가 없으면 사용법 오류(2)다."""
    code, out, err = run_cli(capsys)
    assert code == cli.EXIT_USAGE
    assert out == ""
    assert "usage: mcportal" in err


def test_missing_quota_subcommand_is_usage_error(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``quota`` 만 주면 하위명령이 없어 사용법 오류(2)다."""
    code, _, err = run_cli(capsys, "quota")
    assert code == cli.EXIT_USAGE
    assert err != ""


def test_unknown_option_is_usage_error(capsys: pytest.CaptureFixture[str]) -> None:
    """알 수 없는 옵션도 argparse 표준대로 2다."""
    code, _, err = run_cli(capsys, "quota", "status", "--nope")
    assert code == cli.EXIT_USAGE
    assert "--nope" in err


def test_invalid_day_is_usage_error(capsys: pytest.CaptureFixture[str]) -> None:
    """``--day`` 형식 오류는 사용법 오류(2)이며 한국어로 안내한다."""
    code, _, err = run_cli(capsys, "quota", "status", "--day", "2026/08/06")
    assert code == cli.EXIT_USAGE
    assert "YYYY-MM-DD" in err


def test_key_env_rejects_raw_key_value(capsys: pytest.CaptureFixture[str]) -> None:
    """``--key-env`` 에 키 원문을 넘기면 사용법 오류로 막는다(오용 방지)."""
    code, out, err = run_cli(capsys, "quota", "status", "--key-env", SYNTHETIC_KEY)
    assert code == cli.EXIT_USAGE
    assert "환경변수" in err
    # 막으려던 노출을 거부 메시지가 대신 저지르면 안 된다.
    assert SYNTHETIC_KEY not in out + err


def test_key_env_rejects_url_encoded_key_value(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """URL 인코딩 표기 인증키도 환경변수 '이름'으로 통과하지 못한다.

    인코딩 표기(``ab12%2BCD%2F34%3D%3D``)에는 ``=``·공백·탭이 하나도 없어 옛 가드를
    그대로 통과했고, 그러면 부재 오류 메시지가 키 원문을 stderr 로 되울렸다
    (2026-08-09 Advisor 검증 V6). 거부 자체와 **출력 전문에 원문 부재**를 함께 본다.
    """
    code, out, err = run_cli(
        capsys, "quota", "status", "--key-env", SYNTHETIC_KEY_ENCODED
    )
    assert code == cli.EXIT_USAGE
    assert "환경변수" in err
    assert SYNTHETIC_KEY_ENCODED not in out + err
    assert SYNTHETIC_KEY not in out + err
    # 디코딩 결과의 조각도 새지 않는다(부분 에코 회귀 차단).
    assert "ab12" not in out + err


def test_key_fp_and_key_env_are_mutually_exclusive(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """지문 필터 두 경로를 동시에 주면 사용법 오류(2)다."""
    code, _, err = run_cli(
        capsys, "quota", "status", "--key-fp", "0123456789ab", "--key-env", "SOME_VAR"
    )
    assert code == cli.EXIT_USAGE
    assert err != ""


# ---------------------------------------------------------------------------
# 3~9. quota status
# ---------------------------------------------------------------------------
def test_quota_status_without_ledger_is_ok(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """원장 파일이 없어도 정상 출력 + 0 이다(빈 상태는 실패가 아니다)."""
    missing = tmp_path / "absent" / "ledger.db"
    code, out, err = run_cli(capsys, "quota", "status", "--ledger", str(missing))
    assert code == cli.EXIT_OK
    assert err == ""
    assert "호출 이력 없음" in out
    assert "(없음)" in out
    assert "10,000 회/일" in out
    assert "베스트에포트" in out


def test_quota_status_does_not_create_ledger(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """CLI 는 원장을 **만들지 않는다**(UsageLedger 를 생성하면 파일이 생긴다)."""
    parent = tmp_path / "absent"
    missing = parent / "ledger.db"
    code, _, _ = run_cli(capsys, "quota", "status", "--ledger", str(missing))
    assert code == cli.EXIT_OK
    assert not missing.exists()
    assert not parent.exists()


def test_quota_status_counts_recorded_calls(
    capsys: pytest.CaptureFixture[str], ledger_path: Path
) -> None:
    """합성 원장 3건이 사용량·잔여·상태로 표에 실린다."""
    fingerprint = key_fp(SYNTHETIC_KEY)
    code, out, err = run_cli(
        capsys, "quota", "status", "--ledger", str(ledger_path), "--day", FIXED_DAY
    )
    assert code == cli.EXIT_OK
    assert err == ""
    assert fingerprint in out
    assert "9,997" in out  # 10,000 - 3
    assert "합계" in out
    assert "OK" in out
    assert SYNTHETIC_KEY not in out


def test_quota_status_other_day_is_empty(
    capsys: pytest.CaptureFixture[str], ledger_path: Path
) -> None:
    """다른 KST 날짜를 물으면 이력이 없다(집계 축이 일 단위임을 증명)."""
    code, out, _ = run_cli(
        capsys, "quota", "status", "--ledger", str(ledger_path), "--day", "2026-08-05"
    )
    assert code == cli.EXIT_OK
    assert "호출 이력 없음" in out


def test_quota_status_json_contract(
    capsys: pytest.CaptureFixture[str], ledger_path: Path
) -> None:
    """``--json`` 은 안정 스키마를 지키고 키 원문을 담지 않는다."""
    code, out, err = run_cli(
        capsys,
        "quota",
        "status",
        "--ledger",
        str(ledger_path),
        "--day",
        FIXED_DAY,
        "--json",
    )
    assert code == cli.EXIT_OK
    assert err == ""
    payload = json.loads(out)
    assert payload["schema"] == "mcportal.quota.status/1"
    assert payload["day_kst"] == FIXED_DAY
    assert payload["ledger_path"] == str(ledger_path)
    assert payload["ledger_exists"] is True
    assert payload["budget"] == {
        "limit": 10_000,
        "source": "profile_default",
        "soft_ratio": 0.8,
    }
    assert isinstance(payload["keys"], list)
    assert payload["keys"] == [
        {
            "key_fp": key_fp(SYNTHETIC_KEY),
            "used": 3,
            "remaining": 9_997,
            "status": "OK",
        }
    ]
    assert payload["total"] == {"used": 3, "remaining": 9_997, "status": "OK"}
    assert isinstance(payload["notes"], list) and payload["notes"]
    assert SYNTHETIC_KEY not in out
    assert SYNTHETIC_KEY_ENCODED not in out


def test_quota_status_json_is_single_document(
    capsys: pytest.CaptureFixture[str], ledger_path: Path
) -> None:
    """``--json`` stdout 전체가 JSON 하나여야 파이프에 안전하다."""
    _, out, _ = run_cli(
        capsys, "quota", "status", "--ledger", str(ledger_path), "--json"
    )
    assert out.endswith("\n")
    assert out.count("\n") == len(out.rstrip("\n").splitlines())
    json.loads(out)  # 앞뒤에 사람용 문구가 붙으면 여기서 깨진다.


def test_quota_status_json_without_ledger(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """이력이 없어도 ``--json`` 은 같은 스키마를 유지한다(빈 배열)."""
    code, out, _ = run_cli(
        capsys, "quota", "status", "--ledger", str(tmp_path / "none.db"), "--json"
    )
    assert code == cli.EXIT_OK
    payload = json.loads(out)
    assert payload["ledger_exists"] is False
    assert payload["keys"] == []
    assert payload["total"]["used"] == 0


@pytest.mark.parametrize(
    ("argv_extra", "env_budget", "expected_limit", "expected_source"),
    [
        (["--budget", "5"], "7", 5, "argument"),
        ([], "7", 7, "env:CALL_BUDGET"),
        ([], None, 10_000, "profile_default"),
    ],
)
def test_budget_resolution_priority(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    argv_extra: list[str],
    env_budget: str | None,
    expected_limit: int,
    expected_source: str,
) -> None:
    """예산 상한은 인자 > CALL_BUDGET > 프로파일 기본값 순으로 해석된다."""
    monkeypatch.delenv("CALL_BUDGET", raising=False)
    if env_budget is not None:
        monkeypatch.setenv("CALL_BUDGET", env_budget)
    code, out, _ = run_cli(
        capsys,
        "quota",
        "status",
        "--ledger",
        str(tmp_path / "none.db"),
        "--json",
        *argv_extra,
    )
    assert code == cli.EXIT_OK
    payload = json.loads(out)
    assert payload["budget"]["limit"] == expected_limit
    assert payload["budget"]["source"] == expected_source


def test_budget_env_invalid_is_error(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """CALL_BUDGET 이 정수가 아니면 한국어로 안내하고 1로 끝난다."""
    monkeypatch.setenv("CALL_BUDGET", "열개")
    code, out, err = run_cli(
        capsys, "quota", "status", "--ledger", str(tmp_path / "none.db")
    )
    assert code == cli.EXIT_ERROR
    assert out == ""
    assert "CALL_BUDGET" in err


def test_key_env_filters_by_local_fingerprint(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch, ledger_path: Path
) -> None:
    """``--key-env`` 는 지문만 로컬 계산해 필터하고 키 원문을 출력하지 않는다."""
    monkeypatch.setenv("SYNTH_KEY_VAR", SYNTHETIC_KEY)
    code, out, err = run_cli(
        capsys,
        "quota",
        "status",
        "--ledger",
        str(ledger_path),
        "--day",
        FIXED_DAY,
        "--key-env",
        "SYNTH_KEY_VAR",
    )
    assert code == cli.EXIT_OK
    assert key_fp(SYNTHETIC_KEY) in out
    assert "9,997" in out
    assert SYNTHETIC_KEY not in out + err


def test_key_env_normalizes_encoded_key(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch, ledger_path: Path
) -> None:
    """인코딩키를 담은 환경변수도 같은 지문으로 필터된다.

    트랜스포트가 :func:`~mcportal.runtime.keys.prepare_service_key` 로 정규화한
    키를 원장에 기록하므로, CLI 가 원문 그대로 지문을 내면 같은 키인데도 0건으로
    보인다. 그 어긋남을 막는 회귀다.
    """
    monkeypatch.setenv("SYNTH_KEY_VAR", SYNTHETIC_KEY_ENCODED)
    code, out, err = run_cli(
        capsys,
        "quota",
        "status",
        "--ledger",
        str(ledger_path),
        "--day",
        FIXED_DAY,
        "--key-env",
        "SYNTH_KEY_VAR",
    )
    assert code == cli.EXIT_OK
    assert key_fp(SYNTHETIC_KEY) in out
    assert "호출 이력 없음" not in out
    assert SYNTHETIC_KEY not in out + err
    assert SYNTHETIC_KEY_ENCODED not in out + err


def test_key_env_missing_variable_is_error(
    capsys: pytest.CaptureFixture[str], ledger_path: Path
) -> None:
    """환경변수가 비어 있으면 1로 끝나고 키 원문은 어디에도 없다.

    부재 안내는 **변수 이름을 되울리지 않는다**(W4 §5). ``_env_name_arg`` 가 키
    형태를 걸러 내지만 순수 영숫자 인증키는 이름으로 통과하므로, 이름을 에코하면
    막으려던 노출을 오류 메시지가 대신 저지른다. 그래도 오류 자체는 한국어로
    무엇이 잘못됐는지 알려야 하므로 안내 문면의 존재를 함께 본다.
    """
    code, out, err = run_cli(
        capsys, "quota", "status", "--ledger", str(ledger_path), "--key-env", "NO_SUCH_VAR"
    )
    assert code == cli.EXIT_ERROR
    assert out == ""
    assert cli.KEY_ENV_MISSING in err
    assert "NO_SUCH_VAR" not in err
    assert SYNTHETIC_KEY not in err


def test_key_env_missing_message_never_echoes_an_alphanumeric_key(
    capsys: pytest.CaptureFixture[str], ledger_path: Path
) -> None:
    """순수 영숫자 인증키를 ``--key-env`` 에 넘겨도 그 값이 stderr 로 새지 않는다.

    이것이 이름 에코를 지운 이유다 - ``=``·공백·``%`` 가 하나도 없는 키는
    :func:`~mcportal.cli._env_name_arg` 를 '이름'으로 통과하고, 옛 문면은 그
    값을 그대로 부재 오류에 실었다.
    """
    # 저엔트로피 반복 패턴을 일부러 쓴다 — gitleaks 기본 룰(generic-api-key)의
    # 엔트로피 문턱에 걸리지 않는 자명한 합성값이어야 CI 시크릿 게이트가 조용하다.
    alnum_key = "aaaa1111bbbb2222cccc"
    code, out, err = run_cli(
        capsys, "quota", "status", "--ledger", str(ledger_path), "--key-env", alnum_key
    )
    assert code == cli.EXIT_ERROR
    assert alnum_key not in out + err


def test_key_fp_without_match_is_ok(
    capsys: pytest.CaptureFixture[str], ledger_path: Path
) -> None:
    """일치하는 지문이 없으면 0건으로 표시하고 0으로 끝난다."""
    code, out, _ = run_cli(
        capsys,
        "quota",
        "status",
        "--ledger",
        str(ledger_path),
        "--day",
        FIXED_DAY,
        "--key-fp",
        "0123456789ab",
    )
    assert code == cli.EXIT_OK
    assert "호출 이력 없음" in out
    assert "0123456789ab" in out


def test_key_fp_rejects_bad_format(capsys: pytest.CaptureFixture[str]) -> None:
    """지문 형식이 아니면 사용법 오류(2)다."""
    code, _, err = run_cli(capsys, "quota", "status", "--key-fp", "zzz")
    assert code == cli.EXIT_USAGE
    assert "16진수" in err


def test_corrupted_ledger_is_error(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """SQLite 가 아닌 파일을 가리키면 1 + 한국어 안내로 접는다(예외 누출 없음)."""
    broken = tmp_path / "broken.db"
    broken.write_text("이것은 데이터베이스가 아닙니다\n" * 20, encoding="utf-8")
    code, out, err = run_cli(capsys, "quota", "status", "--ledger", str(broken))
    assert code == cli.EXIT_ERROR
    assert out == ""
    assert "원장을 읽지 못했습니다" in err


def test_non_ledger_sqlite_file_is_ok_with_notice(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """calls 테이블이 없는 정상 SQLite 파일은 이력 없음(0) + 안내를 낸다."""
    import sqlite3

    other = tmp_path / "other.db"
    conn = sqlite3.connect(str(other))
    try:
        conn.execute("CREATE TABLE unrelated (x INTEGER)")
        conn.commit()
    finally:
        conn.close()
    code, out, _ = run_cli(capsys, "quota", "status", "--ledger", str(other))
    assert code == cli.EXIT_OK
    assert "호출 이력 없음" in out
    assert "calls 테이블" in out


# ---------------------------------------------------------------------------
# 10. cp949 안전(대표 케이스 명시 검증)
# ---------------------------------------------------------------------------
def test_all_outputs_are_cp949_encodable(
    capsys: pytest.CaptureFixture[str], tmp_path: Path, ledger_path: Path
) -> None:
    """대표 출력 전부가 cp949 로 인코딩된다(박스드로잉·em dash 회귀 차단).

    :func:`run_cli` 가 매 호출마다 같은 검사를 하지만, 이 테스트는 그 규칙이
    사라지지 않도록 명시적으로 한 번 더 못박는다.
    """
    invocations = [
        ["--help"],
        ["quota", "status", "--ledger", str(ledger_path), "--day", FIXED_DAY],
        ["quota", "status", "--ledger", str(tmp_path / "none.db")],
        ["quota", "status", "--ledger", str(ledger_path), "--json"],
        ["presets", "--presets-root", str(tmp_path)],
    ]
    for argv in invocations:
        cli.main(argv)
        captured = capsys.readouterr()
        assert_cp949_safe(captured.out, where=f"stdout({' '.join(argv)})")
        assert_cp949_safe(captured.err, where=f"stderr({' '.join(argv)})")


def test_cli_source_file_is_cp949_encodable() -> None:
    """``cli.py`` **소스 파일 전체**가 cp949 로 인코딩된다.

    출력 검사(:func:`run_cli`)는 실제로 실행된 경로의 문면만 본다. 그래서 독스트링
    ·주석·아직 안 밟은 분기의 메시지에 em dash(U+2014)가 숨어 있어도 초록이 난다.
    실제로 ``_select_presets`` 독스트링의 em dash 1자가 그렇게 살아남았다
    (2026-08-09 Advisor 검증 V5). 소스 전문을 통째로 검사해 그 사각지대를 닫는다.
    """
    source = Path(cli.__file__).read_text(encoding="utf-8")
    assert_cp949_safe(source, where="cli.py 소스")


def test_display_width_counts_wide_characters() -> None:
    """폭 계산 헬퍼가 한글을 2칸으로 센다(표 정렬의 전제)."""
    assert cli.display_width("abc") == 3
    assert cli.display_width("합계") == 4
    assert cli.display_width("합계ab") == 6


def test_render_table_uses_ascii_rules_only() -> None:
    """표 구분선은 ASCII 하이픈만 쓴다."""
    lines = cli.render_table(["ID", "이름"], [["1", "가"]], ["left", "left"])
    assert set(lines[1]) <= {"-", " "}
    assert_cp949_safe("\n".join(lines), where="render_table")


# ---------------------------------------------------------------------------
# 11~12. presets 루트 해석
# ---------------------------------------------------------------------------
@requires_curation
def test_presets_empty_root_is_ok(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """빈 루트는 실패가 아니다 - 안내 후 0."""
    code, out, err = run_cli(capsys, "presets", "--presets-root", str(tmp_path))
    assert code == cli.EXIT_OK
    assert err == ""
    assert "프리셋을 찾지 못했습니다" in out


def test_presets_missing_root_is_error(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """명시한 루트가 없으면 실패(1)다 - 오타를 조용히 넘기지 않는다."""
    code, out, err = run_cli(
        capsys, "presets", "--presets-root", str(tmp_path / "nowhere")
    )
    assert code == cli.EXIT_ERROR
    assert out == ""
    assert "프리셋 루트가 없습니다" in err


def test_presets_root_is_file_is_error(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """루트로 파일을 넘기면 실패(1)다."""
    target = tmp_path / "not-a-dir.txt"
    target.write_text("x", encoding="utf-8")
    code, _, err = run_cli(capsys, "presets", "--presets-root", str(target))
    assert code == cli.EXIT_ERROR
    assert "디렉터리가 아닙니다" in err


def test_compile_missing_root_is_error(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """compile 도 명시 루트 부재는 실패(1)다."""
    code, _, err = run_cli(
        capsys, "compile", "--presets-root", str(tmp_path / "nowhere")
    )
    assert code == cli.EXIT_ERROR
    assert "프리셋 루트가 없습니다" in err


@requires_curation
def test_compile_empty_root_is_ok(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """번들이 하나도 없는 루트에서 compile 은 0으로 끝난다."""
    code, out, _ = run_cli(capsys, "compile", "--presets-root", str(tmp_path))
    assert code == cli.EXIT_OK
    assert "프리셋을 찾지 못했습니다" in out


# ---------------------------------------------------------------------------
# 13~16. 합성 번들로 보는 compile / presets
# ---------------------------------------------------------------------------
@requires_curation
def test_compile_writes_then_reports_unchanged(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """첫 실행은 openapi.json 을 만들고, 두 번째 실행은 '변경 없음'이다."""
    directory = write_synthetic_bundle(tmp_path)
    target = directory / "openapi.json"

    code, out, err = run_cli(capsys, "compile", "--presets-root", str(tmp_path))
    assert code == cli.EXIT_OK
    assert err == ""
    assert target.is_file()
    assert "갱신" in out
    first = target.read_bytes()

    code, out, _ = run_cli(capsys, "compile", "--presets-root", str(tmp_path))
    assert code == cli.EXIT_OK
    assert "변경 없음" in out
    assert target.read_bytes() == first  # 재기록으로 mtime·바이트가 흔들리지 않는다


@requires_curation
def test_compile_json_contract(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """compile ``--json`` 은 배열 필드를 실제 JSON 배열로 낸다."""
    write_synthetic_bundle(tmp_path)
    code, out, _ = run_cli(capsys, "compile", "--presets-root", str(tmp_path), "--json")
    assert code == cli.EXIT_OK
    payload = json.loads(out)
    assert payload["schema"] == "mcportal.compile/1"
    assert payload["root"] == str(tmp_path)
    assert isinstance(payload["presets"], list)
    assert len(payload["presets"]) == 1
    entry = payload["presets"][0]
    assert entry["preset_id"] == SYNTH_PRESET_ID
    assert entry["status"] == "written"
    assert isinstance(entry["operation_count"], int)
    assert isinstance(entry["unresolved_count"], int)
    assert payload["written"] == 1
    assert payload["unchanged"] == 0
    assert payload["drift"] == 0


@requires_curation
def test_compile_check_detects_drift(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """``--check`` 는 일치하면 0, 1바이트만 달라도 3(드리프트)이다."""
    directory = write_synthetic_bundle(tmp_path)
    assert run_cli(capsys, "compile", "--presets-root", str(tmp_path))[0] == cli.EXIT_OK

    code, out, _ = run_cli(capsys, "compile", "--presets-root", str(tmp_path), "--check")
    assert code == cli.EXIT_OK
    assert "일치" in out

    target = directory / "openapi.json"
    raw = bytearray(target.read_bytes())
    raw[0:1] = b" "  # 첫 바이트만 바꾼다(길이 동일).
    target.write_bytes(bytes(raw))

    code, out, _ = run_cli(capsys, "compile", "--presets-root", str(tmp_path), "--check")
    assert code == cli.EXIT_DRIFT
    assert "드리프트" in out
    assert target.read_bytes() == bytes(raw)  # --check 는 파일을 쓰지 않는다


@requires_curation
def test_compile_check_json_reports_drift(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """드리프트는 ``--json`` 에서도 상태·집계로 드러난다."""
    directory = write_synthetic_bundle(tmp_path)
    run_cli(capsys, "compile", "--presets-root", str(tmp_path))
    target = directory / "openapi.json"
    target.write_bytes(b" " + target.read_bytes()[1:])

    code, out, _ = run_cli(
        capsys, "compile", "--presets-root", str(tmp_path), "--check", "--json"
    )
    assert code == cli.EXIT_DRIFT
    payload = json.loads(out)
    assert payload["drift"] == 1
    assert payload["presets"][0]["status"] == "drift"


@requires_curation
def test_compile_unknown_preset_id_is_error(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """없는 ID 를 지정하면 1 + 사용 가능한 ID 목록을 보여 준다."""
    write_synthetic_bundle(tmp_path)
    code, out, err = run_cli(
        capsys, "compile", "12340000", "--presets-root", str(tmp_path)
    )
    assert code == cli.EXIT_ERROR
    assert out == ""
    assert "12340000" in err
    assert SYNTH_PRESET_ID in err


@requires_curation
def test_compile_selects_named_preset_only(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """ID 를 지정하면 그 번들만 컴파일한다."""
    write_synthetic_bundle(tmp_path, SYNTH_PRESET_ID)
    other = write_synthetic_bundle(tmp_path, "99000002")
    code, out, _ = run_cli(
        capsys, "compile", SYNTH_PRESET_ID, "--presets-root", str(tmp_path), "--json"
    )
    assert code == cli.EXIT_OK
    payload = json.loads(out)
    assert [entry["preset_id"] for entry in payload["presets"]] == [SYNTH_PRESET_ID]
    assert not (other / "openapi.json").exists()


@requires_curation
def test_presets_lists_synthetic_bundle(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """presets 표에 ID·서비스명·종류·개수·이용허락범위가 실린다."""
    write_synthetic_bundle(tmp_path)
    code, out, err = run_cli(capsys, "presets", "--presets-root", str(tmp_path))
    assert code == cli.EXIT_OK
    assert err == ""
    assert SYNTH_PRESET_ID in out
    assert "가상 시험기관 합성 조회" in out
    assert "1종(1데이터셋)" in out
    assert "합성 메모 첫째 줄" not in out  # --verbose 없이는 메모를 찍지 않는다


@requires_curation
def test_presets_verbose_shows_curation_notes(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """``--verbose`` 는 큐레이션 메모를 프리셋 아래에 들여써 보여 준다."""
    write_synthetic_bundle(tmp_path)
    code, out, _ = run_cli(
        capsys, "presets", "--presets-root", str(tmp_path), "--verbose"
    )
    assert code == cli.EXIT_OK
    assert "합성 메모 첫째 줄" in out
    assert "합성 메모 둘째 줄" in out


@requires_curation
def test_presets_json_contract(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """presets ``--json`` 은 PresetInfo 전 필드를 JSON 안전하게 낸다."""
    write_synthetic_bundle(tmp_path)
    code, out, _ = run_cli(capsys, "presets", "--presets-root", str(tmp_path), "--json")
    assert code == cli.EXIT_OK
    payload = json.loads(out)
    assert payload["schema"] == "mcportal.presets/1"
    assert isinstance(payload["presets"], list)
    entry = payload["presets"][0]
    assert entry["preset_id"] == SYNTH_PRESET_ID
    assert entry["service_name"] == "가상 시험기관 합성 조회"
    assert isinstance(entry["notes"], list)
    for field in ("directory", "source_path", "openapi_path"):
        assert isinstance(entry[field], str)


@requires_curation
def test_presets_root_from_environment(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """``--presets-root`` 없이도 환경변수로 지정한 루트를 찾는다."""
    write_synthetic_bundle(tmp_path)
    monkeypatch.setenv("MCPORTAL_PRESETS", str(tmp_path))
    code, out, _ = run_cli(capsys, "presets")
    assert code == cli.EXIT_OK
    assert SYNTH_PRESET_ID in out


@requires_curation
def test_explicit_root_silences_env_warning(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """``--presets-root`` 를 주면 ``MCPORTAL_PRESETS`` 경고를 내지 않는다.

    명시 인자가 환경변수를 이기는 것은 설계된 우선순위다. 그런데 옛 구현은
    ``_root_source`` 가 ``argument`` 를 돌려준다는 이유로 **같은 유효 경로를 양쪽에
    준 경우에도** "번들이 없어 무시했다"는 거짓 진단을 냈다(2026-08-09 Advisor
    검증 V7). 유효 경로 · 다른 유효 경로 두 배치를 모두 본다.
    """
    write_synthetic_bundle(tmp_path)
    other = tmp_path / "other"
    write_synthetic_bundle(other, preset_id="99000002")

    for env_root in (tmp_path, other):
        monkeypatch.setenv("MCPORTAL_PRESETS", str(env_root))
        code, out, err = run_cli(capsys, "presets", "--presets-root", str(tmp_path))
        assert code == cli.EXIT_OK
        assert err == "", f"MCPORTAL_PRESETS={env_root} 에서 거짓 경고가 났다: {err!r}"
        assert SYNTH_PRESET_ID in out
        # 명시 루트가 이겼다는 사실도 함께 못박는다(경고만 지운 것이 아니다).
        assert "99000002" not in out


@requires_curation
def test_env_root_without_bundle_still_warns(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """명시 인자가 없을 때 채택되지 않은 환경변수 경고는 그대로 살아 있다.

    V7 수정이 경고를 통째로 죽이지 않았음을 확인한다. 문면은 채택 실패 **사유**를
    단정하지 않아야 한다(경로 부재·비디렉터리·번들 0건을 CLI 는 구분하지 못한다).
    """
    curation = importlib.import_module("mcportal.compiler.curation")
    bundle_root = tmp_path / "bundles"
    write_synthetic_bundle(bundle_root)
    empty = tmp_path / "empty"
    empty.mkdir()
    monkeypatch.setenv("MCPORTAL_PRESETS", str(empty))

    code, _, err = run_cli(capsys, "presets", "--presets-root", str(bundle_root))
    assert code == cli.EXIT_OK
    assert err == ""  # 명시 인자가 있으면 여전히 침묵이다

    # 명시 인자 없는 배치. 탐색이 커밋된 실제 프리셋 루트로 새지 않도록 대체한다
    # (이 파일의 격리 규약: 프리셋 검사는 합성 번들만 만진다).
    monkeypatch.setattr(curation, "default_presets_root", lambda: bundle_root)
    code, out, err = run_cli(capsys, "presets")
    assert code == cli.EXIT_OK
    assert SYNTH_PRESET_ID in out
    assert "MCPORTAL_PRESETS" in err
    assert str(empty) in err
    assert "무시" not in err  # 사유 단정 문면으로 되돌아가지 않았는가


@requires_curation
def test_broken_source_json_is_error(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """번들의 JSON 이 깨졌으면 스택트레이스 대신 한국어 안내 + 1이다."""
    directory = write_synthetic_bundle(tmp_path)
    (directory / "source.json").write_text("{ 이건 JSON 이 아니다", encoding="utf-8")
    code, out, err = run_cli(capsys, "compile", "--presets-root", str(tmp_path))
    assert code == cli.EXIT_ERROR
    assert out == ""
    assert err != ""


# ---------------------------------------------------------------------------
# 17. 중단
# ---------------------------------------------------------------------------
def test_keyboard_interrupt_returns_130(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Ctrl+C 는 130 으로 접히고 예외가 밖으로 새지 않는다."""

    def _boom(_args: Any) -> int:
        raise KeyboardInterrupt

    monkeypatch.setattr(cli, "_cmd_presets", _boom)
    code, out, err = run_cli(capsys, "presets", "--presets-root", str(tmp_path))
    assert code == cli.EXIT_INTERRUPTED
    assert out == ""
    assert "사용자 중단" in err


def test_unexpected_exception_is_folded(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """예상 밖 예외도 밖으로 던지지 않고 1 + 타입·메시지로 알린다."""

    def _boom(_args: Any) -> int:
        raise RuntimeError("합성 실패")

    monkeypatch.setattr(cli, "_cmd_presets", _boom)
    code, out, err = run_cli(capsys, "presets", "--presets-root", str(tmp_path))
    assert code == cli.EXIT_ERROR
    assert out == ""
    assert "RuntimeError" in err
    assert "합성 실패" in err
    assert "Traceback" not in err  # MCPORTAL_DEBUG 없이 스택트레이스를 흘리지 않는다


# ---------------------------------------------------------------------------
# 서브프로세스: 실제 콘솔 인코딩 경로
# ---------------------------------------------------------------------------
def _subprocess_env() -> dict[str, str]:
    """cp949 콘솔을 흉내 낸 서브프로세스 환경을 만든다."""
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "cp949"
    for name in ("CALL_BUDGET", "MCPORTAL_LEDGER", "MCPORTAL_PRESETS"):
        env.pop(name, None)
    return env


def test_subprocess_quota_status_survives_cp949_console(tmp_path: Path) -> None:
    """cp949 stdout 을 강제한 실제 프로세스에서도 죽지 않고 0으로 끝난다."""
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "mcportal.cli",
            "quota",
            "status",
            "--ledger",
            str(tmp_path / "none.db"),
        ],
        capture_output=True,
        env=_subprocess_env(),
    )
    assert proc.returncode == cli.EXIT_OK, proc.stderr.decode("cp949", "replace")
    text = proc.stdout.decode("cp949")
    assert "호출 이력 없음" in text


def test_subprocess_usage_error_returns_two(tmp_path: Path) -> None:
    """서브커맨드 없이 부르면 실제 프로세스도 2로 끝난다."""
    proc = subprocess.run(
        [sys.executable, "-m", "mcportal.cli"],
        capture_output=True,
        env=_subprocess_env(),
    )
    assert proc.returncode == cli.EXIT_USAGE


# ---------------------------------------------------------------------------
# 패키징: [project.scripts] 만 추가했는가
# ---------------------------------------------------------------------------
def _pyproject() -> dict[str, Any]:
    """리포 루트의 ``pyproject.toml`` 을 읽는다."""
    path = Path(__file__).resolve().parents[1] / "pyproject.toml"
    return tomllib.loads(path.read_text(encoding="utf-8"))


def test_console_script_is_declared() -> None:
    """콘솔 스크립트가 CLI 진입점을 정확히 가리킨다."""
    assert _pyproject()["project"]["scripts"] == {"mcportal": "mcportal.cli:main"}


def test_runtime_dependencies_are_unchanged() -> None:
    """CLI 추가가 런타임 의존성을 늘리지 않았다(코어는 httpx 단일)."""
    project = _pyproject()["project"]
    assert project["dependencies"] == ["httpx>=0.27"]
    assert set(project["optional-dependencies"]) == {"mcp", "dev"}


def test_cli_imports_standard_library_only() -> None:
    """cli.py 가 서드파티 모듈을 임포트하지 않는다(argparse 전용 규칙 회귀)."""
    source = Path(cli.__file__).read_text(encoding="utf-8")
    for banned in ("import rich", "import click", "import typer", "import httpx"):
        assert banned not in source, f"CLI 에 금지된 의존성이 들어왔습니다: {banned}"


# ---------------------------------------------------------------------------
# 5. sample - 유일한 실호출 서브커맨드(W4 §3-2)
# ---------------------------------------------------------------------------
#: 샘플링 시험용 합성 번들 식별자와 기본 URL.
SAMPLE_PRESET_ID = "99000009"
SAMPLE_BASE = "https://apis.example.invalid/9990000/demo"


def write_sampling_bundle(root: Path, preset_id: str = SAMPLE_PRESET_ID) -> Path:
    """응답 스키마가 **미확정인** 오퍼레이션 1개짜리 합성 번들을 만든다.

    :func:`write_synthetic_bundle` 의 문서는 응답 스키마를 선언하므로 샘플링
    대상이 0건이다. 실호출 경로를 끝까지 도는 검증에는 미확정 자리가 필요하다.
    """
    directory = root / preset_id
    directory.mkdir(parents=True, exist_ok=True)
    document: dict[str, Any] = {
        "swagger": "2.0",
        "info": {"title": "가상 표본기관 미확정 조회", "version": "1.0"},
        "host": "apis.example.invalid",
        "basePath": "/9990000/demo",
        "schemes": ["https"],
        "produces": ["application/json"],
        "paths": {
            "/getUnknownList": {
                "get": {
                    "operationId": "getUnknownList",
                    "summary": "합성 미확정 조회",
                    "parameters": [
                        {
                            "name": "serviceKey",
                            "in": "query",
                            "required": True,
                            "type": "string",
                        },
                        {
                            "name": "pageNo",
                            "in": "query",
                            "required": True,
                            "type": "integer",
                        },
                    ],
                    # 응답 스키마를 주지 않는다 = 샘플링이 채울 자리.
                    "responses": {"200": {"description": "정상"}},
                }
            }
        },
    }
    source = {
        "mcportal_preset_source": 1,
        "preset_id": preset_id,
        "service_id": preset_id,
        "service_name": "가상 표본기관 미확정 조회",
        "source_kind": "gw_swagger",
        "key_param": "serviceKey",
        "source_url": f"https://portal.example.invalid/data/{preset_id}/openapi.do",
        "fetched_at": "2026-08-09T00:00:00+09:00",
        "license_note": "합성 픽스처(실제 이용허락범위 아님)",
        "provenance": {
            "spec_origin": "테스트가 생성한 합성 문서",
            "spec_url": f"https://portal.example.invalid/data/{preset_id}/openapi.do",
            "raw_files": [],
            "acquisition": "네트워크 호출 0회(합성)",
            "personal_data_scan": "합성 문서이므로 개인정보 0건",
        },
        "document": document,
    }
    curation = {
        "mcportal_curation": 1,
        "preset_id": preset_id,
        "service": {
            "group": "합성 시험 묶음",
            "title": "가상 표본기관 미확정 조회",
            "version": "0.2.0",
            "license_note": "합성 픽스처(실제 이용허락범위 아님)",
        },
    }
    _write_json(directory / "source.json", source)
    _write_json(directory / "curation.json", curation)
    return directory


def _mock_sample_gateway(result_code: str = "00") -> None:
    """합성 게이트웨이를 세운다(응답이 요청 인증키를 되비추게 만든다)."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "response": {
                    "header": {"resultCode": result_code, "resultMsg": "합성 응답"},
                    "body": {
                        "pageNo": int(request.url.params.get("pageNo", "0")),
                        "items": {"item": [{"name": "가상항목"}]},
                    },
                    "echoKey": request.url.params.get("serviceKey", ""),
                }
            },
        )

    respx.get(f"{SAMPLE_BASE}/getUnknownList").mock(side_effect=handler)


def test_sample_appears_in_help(capsys: pytest.CaptureFixture[str]) -> None:
    """최상위 사용법에 네 번째 서브커맨드가 실린다."""
    code, out, _ = run_cli(capsys, "--help")
    assert code == cli.EXIT_OK
    assert "sample" in out


def test_sample_requires_key_env(capsys: pytest.CaptureFixture[str]) -> None:
    """인증키 없이는 시작할 수 없다(사용법 오류 2)."""
    code, out, err = run_cli(capsys, "sample", SAMPLE_PRESET_ID)
    assert code == cli.EXIT_USAGE
    assert out == ""
    assert "--key-env" in err


def test_sample_requires_at_least_one_preset(capsys: pytest.CaptureFixture[str]) -> None:
    """실호출 명령이므로 대상 프리셋을 생략할 수 없다."""
    code, _, err = run_cli(capsys, "sample", "--key-env", "SOME_VAR")
    assert code == cli.EXIT_USAGE
    assert err != ""


def test_sample_rejects_raw_key_in_key_env(capsys: pytest.CaptureFixture[str]) -> None:
    """``--key-env`` 에 키 원문을 넘기면 막고, 그 값을 되울리지 않는다."""
    code, out, err = run_cli(
        capsys, "sample", SAMPLE_PRESET_ID, "--key-env", SYNTHETIC_KEY_ENCODED
    )
    assert code == cli.EXIT_USAGE
    assert SYNTHETIC_KEY_ENCODED not in out + err
    assert SYNTHETIC_KEY not in out + err


def test_sample_missing_env_value_is_error(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """환경변수가 비어 있으면 1로 끝나고 변수 이름도 되울리지 않는다."""
    write_sampling_bundle(tmp_path)
    code, out, err = run_cli(
        capsys,
        "sample",
        SAMPLE_PRESET_ID,
        "--presets-root",
        str(tmp_path),
        "--key-env",
        "NO_SUCH_SAMPLE_VAR",
    )
    assert code == cli.EXIT_ERROR
    assert out == ""
    assert cli.KEY_ENV_MISSING in err
    assert "NO_SUCH_SAMPLE_VAR" not in err


def test_sample_count_over_hard_cap_is_usage_error(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``--count`` 는 샘플러 하드캡을 넘길 수 없다(쿼터 보호)."""
    code, _, err = run_cli(
        capsys, "sample", SAMPLE_PRESET_ID, "--key-env", "SOME_VAR", "--count", "9"
    )
    assert code == cli.EXIT_USAGE
    assert "하드캡" in err


@requires_curation
def test_sample_unknown_preset_id_is_error(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """없는 프리셋 ID 는 실호출 전에 접는다(예산을 태우지 않는다)."""
    write_sampling_bundle(tmp_path)
    monkeypatch.setenv("SYNTH_SAMPLE_KEY", SYNTHETIC_KEY)
    code, out, err = run_cli(
        capsys,
        "sample",
        "99999999",
        "--presets-root",
        str(tmp_path),
        "--key-env",
        "SYNTH_SAMPLE_KEY",
    )
    assert code == cli.EXIT_ERROR
    assert out == ""
    assert "99999999" in err
    assert SYNTHETIC_KEY not in err


@requires_curation
@respx.mock
def test_sample_fills_schema_and_never_prints_the_key(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """정상 경로: 스키마를 확정하고, 출력에는 수치만 남는다."""
    _mock_sample_gateway()
    directory = write_sampling_bundle(tmp_path)
    monkeypatch.setenv("SYNTH_SAMPLE_KEY", SYNTHETIC_KEY)

    code, out, err = run_cli(
        capsys,
        "sample",
        SAMPLE_PRESET_ID,
        "--presets-root",
        str(tmp_path),
        "--ledger",
        str(tmp_path / "ledger.db"),
        "--budget",
        "100",
        "--count",
        "2",
        "--key-env",
        "SYNTH_SAMPLE_KEY",
    )
    assert code == cli.EXIT_OK
    assert SAMPLE_PRESET_ID in out
    assert "호출 2회" in out
    # 인증키도 응답 원문도 출력에 없다.
    for forbidden in (SYNTHETIC_KEY, SYNTHETIC_KEY_ENCODED, "echoKey", "가상항목"):
        assert forbidden not in out + err

    document = json.loads((directory / "openapi.json").read_text(encoding="utf-8"))
    assert document["info"]["x-mcportal"]["generation_mode"] == "sampled"
    schema = document["components"]["schemas"]["GetUnknownListResponse"]
    assert "response" in schema["properties"]


@requires_curation
@respx.mock
def test_sample_json_output_is_machine_readable(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """``--json`` 은 stdout 에 JSON 만 단독으로 싣고 키를 담지 않는다."""
    _mock_sample_gateway()
    write_sampling_bundle(tmp_path)
    monkeypatch.setenv("SYNTH_SAMPLE_KEY", SYNTHETIC_KEY)

    code, out, err = run_cli(
        capsys,
        "sample",
        SAMPLE_PRESET_ID,
        "--presets-root",
        str(tmp_path),
        "--ledger",
        str(tmp_path / "ledger.db"),
        "--budget",
        "100",
        "--count",
        "2",
        "--key-env",
        "SYNTH_SAMPLE_KEY",
        "--json",
    )
    assert code == cli.EXIT_OK
    payload = json.loads(out)
    assert payload["schema"] == cli.SCHEMA_SAMPLE
    assert payload["calls"] == 2
    assert payload["ok"] == 2
    assert payload["failed"] == 0
    assert payload["resolved"] == 1
    assert isinstance(payload["notes"], list)

    preset = payload["presets"][0]
    assert preset["preset_id"] == SAMPLE_PRESET_ID
    assert preset["target_operations"] == ["getUnknownList"]
    # 중첩 dataclass 요약이 객체 배열로 펴진다(값은 담기지 않는다).
    summary = preset["operations"][0]
    assert summary["operation_id"] == "getUnknownList"
    assert summary["schema_inferred"] is True
    assert summary["sample_count"] == 2
    assert SYNTHETIC_KEY not in out + err


@requires_curation
@respx.mock
def test_sample_reports_total_failure_as_error(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """호출은 나갔는데 정상 응답이 0건이면 0 으로 끝내지 않는다.

    전량 실패를 성공으로 보고하면 자동화가 "미확정을 채웠다"고 오인한다.
    """
    _mock_sample_gateway(result_code="99")
    directory = write_sampling_bundle(tmp_path)
    monkeypatch.setenv("SYNTH_SAMPLE_KEY", SYNTHETIC_KEY)

    code, out, err = run_cli(
        capsys,
        "sample",
        SAMPLE_PRESET_ID,
        "--presets-root",
        str(tmp_path),
        "--ledger",
        str(tmp_path / "ledger.db"),
        "--budget",
        "100",
        "--count",
        "2",
        "--key-env",
        "SYNTH_SAMPLE_KEY",
    )
    assert code == cli.EXIT_ERROR
    assert "스키마 미확정" in out
    assert SYNTHETIC_KEY not in out + err
    # 실패했으므로 산출물을 갱신하지 않는다.
    assert not (directory / "openapi.json").exists()


# ---------------------------------------------------------------------------
# 6. serve - stdio MCP 서버(W5 설계 §3)
# ---------------------------------------------------------------------------
#: 합성 번들의 기준 URL(카세트 인터랙션이 가리킬 주소).
SYNTH_BASE = "https://apis.example.invalid/9900000/synth"

#: 실번들 검증 1건에 쓰는 커밋된 프리셋 ID 와 그 오퍼레이션 수(= 노출 도구 수).
REAL_PRESET_ID = "15000115"
REAL_PRESET_TOOL_COUNT = 8


class _StubServer:
    """``run()`` 을 삼키는 서버 대역.

    진짜 ``server.run()`` 은 stdio 를 물고 무기한 블로킹하므로 테스트에 들일 수
    없다. 검증 가능한 지점은 "run 직전"까지이며, 이 대역이 그 경계를 만든다.
    """

    def __init__(self) -> None:
        self.run_calls: list[tuple[Any, Any]] = []

    def run(self, *args: Any, **kwargs: Any) -> None:
        self.run_calls.append((args, kwargs))


def _patch_build_server(
    monkeypatch: pytest.MonkeyPatch, *, wrap: bool = False
) -> dict[str, Any]:
    """``mcportal.mcp.build_server`` 를 가로채 인자를 기록한다.

    Args:
        monkeypatch: pytest 픽스처.
        wrap: True 면 **진짜** ``build_server`` 도 함께 호출해 만들어진 서버를
            ``box["server"]`` 에 남긴다(도구 노출 검증용). CLI 에는 어느 경우에도
            대역을 돌려주므로 stdio 가 블로킹되지 않는다.

    Returns:
        ``spec_path`` · ``kwargs`` · ``stub`` (· ``server``)를 담을 딕셔너리.
        CLI 가 ``build_server`` 에 닿기 전에 접혔다면 ``kwargs`` 키가 없다.
    """
    box: dict[str, Any] = {}
    real = mcp_module.build_server

    def fake(spec_path: Any, **kwargs: Any) -> Any:
        box["spec_path"] = spec_path
        box["kwargs"] = kwargs
        if wrap:
            box["server"] = real(spec_path, **kwargs)
        stub = _StubServer()
        box["stub"] = stub
        return stub

    monkeypatch.setattr(mcp_module, "build_server", fake)
    return box


def write_synthetic_cassette(directory: Path) -> Path:
    """합성 번들 옆에 ``cassettes/<ID>.json`` 을 만든다(시크릿 0건)."""
    path = directory / "cassettes" / f"{directory.name}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    cassette = Cassette()
    cassette.add(
        method="GET",
        url=f"{SYNTH_BASE}/getSynthList?targetYm=202601",
        params={"targetYm": "202601"},
        status=200,
        content_type="application/json",
        body_text=json.dumps({"totalCount": 1}, ensure_ascii=False),
        secrets=[],
    )
    cassette.save(path)
    return path


def prepare_serve_bundle(
    capsys: pytest.CaptureFixture[str], root: Path, *, cassette: bool = True
) -> Path:
    """서빙 가능한 합성 번들을 만든다(컴파일 산출물 + 선택적 카세트)."""
    directory = write_synthetic_bundle(root)
    code, _, _ = run_cli(capsys, "compile", "--presets-root", str(root))
    assert code == cli.EXIT_OK
    assert (directory / "openapi.json").is_file()
    if cassette:
        write_synthetic_cassette(directory)
    return directory


def test_serve_appears_in_help(capsys: pytest.CaptureFixture[str]) -> None:
    """최상위 사용법에 다섯 번째 서브커맨드가 실린다."""
    code, out, _ = run_cli(capsys, "--help")
    assert code == cli.EXIT_OK
    assert "serve" in out


def test_serve_requires_a_preset_id(capsys: pytest.CaptureFixture[str]) -> None:
    """대상 프리셋을 생략할 수 없다(사용법 오류 2)."""
    code, out, err = run_cli(capsys, "serve")
    assert code == cli.EXIT_USAGE
    assert out == ""
    assert "PRESET_ID" in err


def test_serve_replay_and_key_env_are_mutually_exclusive(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """무키 replay 와 라이브를 동시에 요구하면 사용법 오류(2)다."""
    code, _, err = run_cli(
        capsys, "serve", SYNTH_PRESET_ID, "--replay", "--key-env", "SOME_VAR"
    )
    assert code == cli.EXIT_USAGE
    assert err != ""


def test_serve_rejects_raw_key_in_key_env(capsys: pytest.CaptureFixture[str]) -> None:
    """``--key-env`` 에 키 원문을 넘기면 막고, 그 값을 되울리지 않는다."""
    code, out, err = run_cli(
        capsys, "serve", SYNTH_PRESET_ID, "--key-env", SYNTHETIC_KEY_ENCODED
    )
    assert code == cli.EXIT_USAGE
    assert SYNTHETIC_KEY_ENCODED not in out + err
    assert SYNTHETIC_KEY not in out + err


@requires_curation
def test_serve_defaults_to_replay_and_starts_the_server(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """모드를 주지 않으면 무키 replay 로 번들을 세우고 ``run()`` 까지 간다."""
    directory = prepare_serve_bundle(capsys, tmp_path)
    box = _patch_build_server(monkeypatch)

    code, out, err = run_cli(
        capsys, "serve", SYNTH_PRESET_ID, "--presets-root", str(tmp_path)
    )
    assert code == cli.EXIT_OK
    # stdio 전송이 stdout 으로 JSON-RPC 프레임을 주고받는다. 배너 한 글자라도
    # 섞이면 클라이언트 파서가 깨지므로 stdout 은 완전히 비어 있어야 한다.
    assert out == ""
    assert "모드: replay" in err

    assert box["kwargs"]["mode"] == "replay"
    assert box["kwargs"]["service_key"] is None
    assert box["kwargs"]["name"] is None
    assert Path(box["spec_path"]) == directory / "openapi.json"
    assert (
        Path(box["kwargs"]["cassette_path"])
        == directory / "cassettes" / f"{SYNTH_PRESET_ID}.json"
    )
    assert len(box["stub"].run_calls) == 1


@requires_curation
def test_serve_explicit_replay_flag_matches_the_default(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """``--replay`` 를 명시해도 기본값과 같은 배선이다."""
    prepare_serve_bundle(capsys, tmp_path)
    box = _patch_build_server(monkeypatch)

    code, out, _ = run_cli(
        capsys, "serve", SYNTH_PRESET_ID, "--presets-root", str(tmp_path), "--replay"
    )
    assert code == cli.EXIT_OK
    assert out == ""
    assert box["kwargs"]["mode"] == "replay"
    assert box["kwargs"]["service_key"] is None


@requires_curation
def test_serve_name_option_reaches_the_builder(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """``--name`` 은 서버 이름으로 그대로 전달되고 배너에도 실린다."""
    prepare_serve_bundle(capsys, tmp_path)
    box = _patch_build_server(monkeypatch)

    code, _, err = run_cli(
        capsys,
        "serve",
        SYNTH_PRESET_ID,
        "--presets-root",
        str(tmp_path),
        "--name",
        "합성 시험 서버",
    )
    assert code == cli.EXIT_OK
    assert box["kwargs"]["name"] == "합성 시험 서버"
    assert "합성 시험 서버" in err


@requires_curation
def test_serve_missing_cassette_is_error(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """카세트 없이 replay 를 요구하면 어느 파일이 없는지 알리고 1로 끝난다."""
    directory = prepare_serve_bundle(capsys, tmp_path, cassette=False)
    box = _patch_build_server(monkeypatch)

    code, out, err = run_cli(
        capsys, "serve", SYNTH_PRESET_ID, "--presets-root", str(tmp_path)
    )
    assert code == cli.EXIT_ERROR
    assert out == ""
    expected = directory / "cassettes" / f"{SYNTH_PRESET_ID}.json"
    assert str(expected) in err
    # 복구 경로 두 갈래를 모두 알린다(녹화 / 라이브 전환).
    assert f"{cli.PROGRAM} sample" in err
    assert "--key-env" in err
    # 서버를 세우기 전에 접었다(빌더에 닿지 않았다).
    assert "kwargs" not in box


@requires_curation
def test_serve_uncompiled_preset_is_error(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """``openapi.json`` 이 없으면 compile 로 안내하고 1로 끝난다."""
    directory = write_synthetic_bundle(tmp_path)
    box = _patch_build_server(monkeypatch)

    code, out, err = run_cli(
        capsys, "serve", SYNTH_PRESET_ID, "--presets-root", str(tmp_path)
    )
    assert code == cli.EXIT_ERROR
    assert out == ""
    assert str(directory / "openapi.json") in err
    assert f"{cli.PROGRAM} compile" in err
    assert "kwargs" not in box


@requires_curation
def test_serve_unknown_preset_id_is_error(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """없는 프리셋 ID 는 서버를 세우기 전에 접는다."""
    prepare_serve_bundle(capsys, tmp_path)
    box = _patch_build_server(monkeypatch)

    code, out, err = run_cli(
        capsys, "serve", "99999999", "--presets-root", str(tmp_path)
    )
    assert code == cli.EXIT_ERROR
    assert out == ""
    assert "99999999" in err
    assert "kwargs" not in box


@requires_curation
def test_serve_without_fastmcp_reuses_the_module_hint(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """fastmcp 미설치 안내는 :mod:`mcportal.mcp` 의 정본 문면을 그대로 쓴다.

    같은 사실(설치 명령·요구 버전 범위)을 CLI 가 따로 적으면 두 곳이 갈라진다.
    """
    write_synthetic_bundle(tmp_path)
    monkeypatch.setitem(sys.modules, "fastmcp", None)
    box = _patch_build_server(monkeypatch)

    code, out, err = run_cli(
        capsys, "serve", SYNTH_PRESET_ID, "--presets-root", str(tmp_path)
    )
    assert code == cli.EXIT_ERROR
    assert out == ""
    assert mcp_module.FASTMCP_IMPORT_HINT in err
    assert mcp_module.FASTMCP_REQUIREMENT in err
    assert "kwargs" not in box


@requires_curation
def test_serve_live_reads_the_key_from_the_environment_only(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """``--key-env`` 는 라이브 배선을 만들되 키도 변수 이름도 출력하지 않는다."""
    prepare_serve_bundle(capsys, tmp_path)
    monkeypatch.setenv("SYNTH_SERVE_KEY", SYNTHETIC_KEY)
    box = _patch_build_server(monkeypatch)

    code, out, err = run_cli(
        capsys,
        "serve",
        SYNTH_PRESET_ID,
        "--presets-root",
        str(tmp_path),
        "--key-env",
        "SYNTH_SERVE_KEY",
    )
    assert code == cli.EXIT_OK
    assert box["kwargs"]["mode"] == "live"
    assert box["kwargs"]["cassette_path"] is None
    # 트랜스포트에는 정규화(디코딩)된 키가 간다 - 지문·이중 인코딩 규약과 같다.
    assert box["kwargs"]["service_key"] == prepare_service_key(SYNTHETIC_KEY)
    assert out == ""
    assert "모드: live" in err
    for forbidden in (SYNTHETIC_KEY, SYNTHETIC_KEY_ENCODED, "ab12", "SYNTH_SERVE_KEY"):
        assert forbidden not in out + err


@requires_curation
def test_serve_live_missing_env_value_is_error(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """환경변수가 비어 있으면 1로 끝나고 변수 이름도 되울리지 않는다."""
    prepare_serve_bundle(capsys, tmp_path)
    box = _patch_build_server(monkeypatch)

    code, out, err = run_cli(
        capsys,
        "serve",
        SYNTH_PRESET_ID,
        "--presets-root",
        str(tmp_path),
        "--key-env",
        "NO_SUCH_SERVE_VAR",
    )
    assert code == cli.EXIT_ERROR
    assert out == ""
    assert cli.KEY_ENV_MISSING in err
    assert "NO_SUCH_SERVE_VAR" not in err
    assert "kwargs" not in box


@requires_curation
def test_serve_build_failure_never_echoes_the_key(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """하위 계층 예외에 인증키가 섞여 있어도 출력에는 남지 않는다.

    배선 실패 메시지에 요청 URL 이 들어오면 질의문자열의 ``serviceKey`` 가 그대로
    따라 나온다. 원문 표기와 퍼센트 인코딩 표기를 **둘 다** 가리는지 본다.
    """
    prepare_serve_bundle(capsys, tmp_path)
    monkeypatch.setenv("SYNTH_SERVE_KEY", SYNTHETIC_KEY)

    def _boom(spec_path: Any, **kwargs: Any) -> Any:
        raise ValueError(
            "합성 배선 실패: "
            f"{SYNTH_BASE}/getSynthList?serviceKey={SYNTHETIC_KEY_ENCODED} "
            f"(원문 {SYNTHETIC_KEY})"
        )

    monkeypatch.setattr(mcp_module, "build_server", _boom)

    code, out, err = run_cli(
        capsys,
        "serve",
        SYNTH_PRESET_ID,
        "--presets-root",
        str(tmp_path),
        "--key-env",
        "SYNTH_SERVE_KEY",
    )
    assert code == cli.EXIT_ERROR
    assert out == ""
    assert "ValueError" in err
    assert cli.REDACTED in err
    for forbidden in (SYNTHETIC_KEY, SYNTHETIC_KEY_ENCODED, "ab12"):
        assert forbidden not in out + err


@requires_curation
def test_serve_interrupt_during_run_returns_130(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Ctrl+C 는 서빙 중에도 130 으로 접힌다(stdio 서버의 정상 종료 경로).

    ``serve`` 는 사람이 Ctrl+C 로 끝내는 것이 기본 종료 방식이므로, 그 경로가
    예외 누출이나 종료 코드 1 로 새면 안 된다.
    """
    prepare_serve_bundle(capsys, tmp_path)

    class _InterruptingServer:
        def run(self, *args: Any, **kwargs: Any) -> None:
            raise KeyboardInterrupt

    monkeypatch.setattr(
        mcp_module, "build_server", lambda spec_path, **kwargs: _InterruptingServer()
    )

    code, out, err = run_cli(
        capsys, "serve", SYNTH_PRESET_ID, "--presets-root", str(tmp_path)
    )
    assert code == cli.EXIT_INTERRUPTED
    assert out == ""
    assert "사용자 중단" in err


def test_serve_notes_are_cp949_encodable() -> None:
    """serve 가 쓰는 상시 고지도 Windows 기본 콘솔에서 살아남는다."""
    for text in (cli.SERVE_STDIO_NOTE, cli.SERVE_LIVE_NOTE, cli.REDACTED):
        assert_cp949_safe(text, where="serve 고지")


@requires_curation
def test_serve_real_preset_exposes_eight_tools(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """커밋된 실번들 15000115 를 무키 replay 로 세우면 도구 8종이 노출된다.

    이 파일에서 실번들을 쓰는 유일한 테스트다(모듈 docstring §4). "무키 replay 로
    진짜 MCP 서버가 선다"는 주장은 합성 번들로 증명되지 않기 때문이다. 그래도
    네트워크·인증키는 0건이다 - 카세트는 커밋본을 읽고, 여기서는 도구 목록만
    조회한다. 도구 정의는 fastmcp 가 만든다(MCPortal 자체 코드젠 없음).
    """
    fastmcp = pytest.importorskip("fastmcp", reason="[mcp] extra 미설치")
    presets_root = Path(__file__).resolve().parents[1] / "presets"
    cassette = presets_root / REAL_PRESET_ID / "cassettes" / f"{REAL_PRESET_ID}.json"
    if not cassette.is_file():
        pytest.skip("커밋된 실번들 카세트가 없는 트리에서는 건너뛴다")

    box = _patch_build_server(monkeypatch, wrap=True)
    code, out, err = run_cli(
        capsys, "serve", REAL_PRESET_ID, "--presets-root", str(presets_root)
    )
    assert code == cli.EXIT_OK
    assert out == ""
    assert str(cassette) in err
    assert len(box["stub"].run_calls) == 1

    async def _list_tools() -> list[str]:
        async with fastmcp.Client(box["server"]) as session:
            return [tool.name for tool in await session.list_tools()]

    names = asyncio.run(_list_tools())
    assert len(names) == REAL_PRESET_TOOL_COUNT
    assert "lawSearchList" in names
