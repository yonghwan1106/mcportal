# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Yong Park
"""벤치마크 하네스 회귀 테스트.

검증 축은 다섯이다.

1. **무키·무네트워크 완주** — 하네스가 인증키 없이, 실 HTTP 트랜스포트를 하나도
   만들지 않고 끝까지 돈다.
2. **결과 스키마 유효성** — 결과 파일이 ``benchmarks/PROTOCOL.md`` §9 스키마와
   저장 규약(UTF-8 · LF · 끝 개행 1개 · ``sort_keys``)을 지킨다.
3. **반복 수 준수** — ``--quick`` · ``--repeat`` 가 프로토콜 §4-1 규칙대로 반영된다.
4. **개인정보·시크릿 0** — 결과 전문에 호스트명·사용자명·홈 디렉터리가 없고,
   인증키 대입이 섞이면 저장 자체가 차단된다.
5. **선등록 물증 사슬** — 리포에 커밋된 결과 파일이 **현행** 프로토콜 판본의
   지문을 달고 있고 ``mode`` 가 ``"full"`` 이다(§9 하단 절).

하네스는 패키지가 아니라 ``benchmarks/harness.py`` 스크립트이므로 파일 경로로
임포트한다. 테스트 입력은 전부 합성이며 실 프리셋에 의존하지 않는다.
"""

from __future__ import annotations

import getpass
import hashlib
import importlib.util
import json
import platform
import shutil
import statistics
import sys
from pathlib import Path
from typing import Any

import httpx
import pytest

_HARNESS_PATH = Path(__file__).resolve().parents[1] / "benchmarks" / "harness.py"
_SPEC = importlib.util.spec_from_file_location("mcportal_bench_harness", _HARNESS_PATH)
assert _SPEC is not None and _SPEC.loader is not None
harness = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = harness
_SPEC.loader.exec_module(harness)


# ---------------------------------------------------------------------------
# 공용 헬퍼
# ---------------------------------------------------------------------------
_REQUIRED_TOP_KEYS = frozenset(
    {
        "schema",
        "protocol",
        "mode",
        "label",
        "measured_on",
        "overrides",
        "environment",
        "items",
    }
)

_REQUIRED_STAT_KEYS = frozenset(
    {
        "name",
        "status",
        "n",
        "min_ns",
        "median_ns",
        "p95_ns",
        "mean_ns",
        "stdev_ns",
        "samples_ns",
        "samples_truncated",
    }
)


def assert_result_schema(result: dict[str, Any]) -> None:
    """결과 문서가 PROTOCOL.md §9 스키마를 지키는지 전수 검사한다."""
    assert _REQUIRED_TOP_KEYS <= set(result), sorted(_REQUIRED_TOP_KEYS - set(result))
    assert result["schema"] == harness.SCHEMA_ID
    assert result["protocol"].startswith("benchmarks/PROTOCOL.md@")
    assert result["mode"] in {"full", "quick"}
    assert len(result["measured_on"]) == 10 and result["measured_on"].count("-") == 2
    assert isinstance(result["overrides"], dict)

    environment = result["environment"]
    for key in (
        "python_version",
        "implementation",
        "system",
        "release",
        "machine",
        "processor",
        "cpu_count",
        "sqlite_version",
        "packages",
    ):
        assert key in environment, key
    assert "mcportal" in environment["packages"]

    assert result["items"], "항목이 하나도 없습니다."
    for item in result["items"]:
        assert item["id"] in harness.ITEM_IDS
        assert item["status"] in {"ok", "skipped", "failed"}
        assert item["title"]
        if item["status"] != "ok":
            assert item.get("reason"), item
            continue
        assert item["conditions"], item["id"]
        for condition in item["conditions"]:
            assert condition["status"] in {"ok", "skipped"}
            if condition["status"] == "skipped":
                assert condition.get("reason")
                assert set(condition) == {"name", "status", "reason"}
                continue
            assert set(condition) == _REQUIRED_STAT_KEYS, sorted(
                set(condition) ^ _REQUIRED_STAT_KEYS
            )
            assert condition["n"] >= 1
            assert condition["min_ns"] <= condition["median_ns"] <= condition["p95_ns"]
            assert isinstance(condition["mean_ns"], float)
            assert condition["stdev_ns"] is None or isinstance(condition["stdev_ns"], float)
            assert isinstance(condition["samples_ns"], list)
            if not condition["samples_truncated"]:
                assert len(condition["samples_ns"]) == condition["n"]


def ok_conditions(item: dict[str, Any]) -> list[dict[str, Any]]:
    """항목에서 실제로 측정된(건너뛰지 않은) 조건만 골라 돌려준다."""
    return [c for c in item.get("conditions", []) if c.get("status") == "ok"]


@pytest.fixture(scope="module")
def quick_run(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, dict[str, Any]]:
    """``--quick`` 전 항목 실행 1회를 모듈 전체가 공유한다(실행 비용 절약)."""
    out = tmp_path_factory.mktemp("bench") / "quick.json"
    code = harness.main(["--quick", "--label", "pytest", "--out", str(out)])
    assert code == harness.EXIT_OK, "무키·무네트워크 완주에 실패했습니다."
    result = json.loads(out.read_text(encoding="utf-8"))
    return out, result


# ---------------------------------------------------------------------------
# 1. 무키·무네트워크 완주
# ---------------------------------------------------------------------------
def test_quick_run_completes_without_keys(quick_run: tuple[Path, dict[str, Any]]) -> None:
    """전 항목 축소 실행이 종료 코드 0으로 끝나고 결과 파일이 생긴다."""
    out, result = quick_run
    assert out.is_file()
    assert result["schema"] == "mcportal.benchmark/1"
    assert result["mode"] == "quick"
    assert result["label"] == "pytest"
    assert [item["id"] for item in result["items"]] == list(harness.ITEM_IDS)
    assert not [item for item in result["items"] if item["status"] == "failed"]


def test_result_schema_is_valid(quick_run: tuple[Path, dict[str, Any]]) -> None:
    """결과 문서가 프로토콜 §9 스키마를 전수 만족한다."""
    _out, result = quick_run
    assert_result_schema(result)


def test_no_real_http_transport_is_created(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """실행 중 실 HTTP 트랜스포트를 만들면 즉시 실패한다(네트워크 0건 회귀)."""

    class _Forbidden:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            raise AssertionError("하네스가 실 HTTP 트랜스포트를 만들었습니다.")

    monkeypatch.setattr(httpx, "HTTPTransport", _Forbidden)
    monkeypatch.setattr(httpx, "AsyncHTTPTransport", _Forbidden)

    out = tmp_path / "nonet.json"
    assert harness.main(["--quick", "--label", "nonet", "--out", str(out)]) == harness.EXIT_OK
    result = json.loads(out.read_text(encoding="utf-8"))
    assert not [item for item in result["items"] if item["status"] == "failed"]


# ---------------------------------------------------------------------------
# 2. 결과 파일 규약
# ---------------------------------------------------------------------------
def test_result_file_conventions(quick_run: tuple[Path, dict[str, Any]]) -> None:
    """UTF-8(BOM 없음) · CR 0개 · 끝 개행 1개 · sort_keys 재직렬화 동일."""
    out, result = quick_run
    raw = out.read_bytes()
    assert not raw.startswith(b"\xef\xbb\xbf")
    assert raw.count(b"\r") == 0
    text = raw.decode("utf-8")
    assert text.endswith("\n") and not text.endswith("\n\n")
    assert json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n" == text


def test_default_out_path_matches_protocol() -> None:
    """기본 결과 경로가 ``results/bench_<YYYYMMDD>_<label>.json`` 이다."""
    path = harness.default_out_path("dev")
    assert path.parent == harness.RESULTS_DIR
    assert path.name.startswith("bench_")
    assert path.name.endswith("_dev.json")
    assert len(path.name.split("_")[1]) == 8


# ---------------------------------------------------------------------------
# 3. 통계 정의
# ---------------------------------------------------------------------------
def test_statistics_definitions() -> None:
    """중앙값·p95(nearest-rank)·표본표준편차의 정의를 고정 표본으로 검증한다."""
    samples = [1, 2, 3, 4, 5]
    assert harness.median_ns(samples) == 3
    # nearest-rank: ceil(0.95 * 5) = 5 -> 5번째(1-기반) 값.
    assert harness.percentile_nearest_rank(samples, 0.95) == 5
    assert harness.stdev_ns(samples) == pytest.approx(statistics.stdev(samples))

    even = [10, 20, 30, 40]
    assert harness.median_ns(even) == 25
    # ceil(0.95 * 4) = 4 -> 마지막 값(보간하지 않는다).
    assert harness.percentile_nearest_rank(even, 0.95) == 40

    twenty = list(range(1, 21))
    # 보간식이면 19.05가 되지만 nearest-rank 는 ceil(19.0) = 19번째 = 19 이다.
    assert harness.percentile_nearest_rank(twenty, 0.95) == 19

    assert harness.stdev_ns([7]) is None
    assert harness.median_ns([7]) == 7
    with pytest.raises(ValueError):
        harness.percentile_nearest_rank([], 0.95)


def test_summarize_samples_shape() -> None:
    """조건 요약 딕셔너리가 스키마 필드를 정확히 갖춘다."""
    condition = harness.summarize_samples("demo", [5, 1, 3])
    assert set(condition) == _REQUIRED_STAT_KEYS
    assert condition["n"] == 3
    assert condition["min_ns"] == 1
    assert condition["median_ns"] == 3
    assert condition["samples_ns"] == [5, 1, 3]
    assert condition["samples_truncated"] is False


def test_stored_samples_truncation() -> None:
    """표본 상한을 넘으면 균등 간격으로 줄이고 그 사실을 밝힌다(통계는 전체 기준)."""
    values = list(range(harness.MAX_STORED_SAMPLES * 2))
    condition = harness.summarize_samples("big", values)
    assert condition["n"] == len(values)
    assert condition["samples_truncated"] is True
    assert len(condition["samples_ns"]) == harness.MAX_STORED_SAMPLES
    assert condition["median_ns"] == harness.median_ns(values)


def test_determinism_check() -> None:
    """결정론 판정: 동일 바이트면 True, 1바이트 달라지면 False + 오프셋."""
    assert harness.determinism_check(["abc", "abc", "abc"]) == (True, None)
    assert harness.determinism_check(["abc"]) == (True, None)
    ok, offset = harness.determinism_check(["abcdef", "abcXef"])
    assert ok is False and offset == 3
    ok, offset = harness.determinism_check(["abc", "abcd"])
    assert ok is False and offset == 3
    with pytest.raises(ValueError):
        harness.determinism_check([])


# ---------------------------------------------------------------------------
# 4. 반복 수 준수
# ---------------------------------------------------------------------------
def test_quick_mode_repeat_count(quick_run: tuple[Path, dict[str, Any]]) -> None:
    """``--quick`` 은 전 항목을 N=5 · warmup=1 로 축소한다."""
    _out, result = quick_run
    for item in result["items"]:
        if item["status"] != "ok":
            continue
        assert item["plan"] == {"warmup": harness.QUICK_WARMUP, "repeat": harness.QUICK_REPEAT}
        for condition in ok_conditions(item):
            assert condition["n"] == harness.QUICK_REPEAT, (item["id"], condition["name"])


def test_repeat_override_is_recorded(tmp_path: Path) -> None:
    """``--repeat`` 는 N만 덮어쓰고 warmup 은 그대로 두며, 그 사실이 기록된다."""
    out = tmp_path / "repeat.json"
    assert harness.main(["--only", "B1", "--repeat", "7", "--label", "rep", "--out", str(out)]) == 0
    result = json.loads(out.read_text(encoding="utf-8"))
    assert result["mode"] == "full"
    assert result["overrides"]["repeat"] == 7
    assert result["overrides"]["only"] == ["B1"]
    item = result["items"][0]
    base = next(plan for plan in harness.ITEM_PLANS if plan.item_id == "B1")
    assert item["plan"] == {"warmup": base.warmup, "repeat": 7}
    for condition in ok_conditions(item):
        assert condition["n"] == 7


def test_plan_for_precedence() -> None:
    """계획 해석 우선순위: 기본값 < ``--quick`` < ``--repeat``."""
    base = next(plan for plan in harness.ITEM_PLANS if plan.item_id == "B2")
    assert harness.plan_for("B2", harness.RunConfig()) == base
    quick = harness.plan_for("B2", harness.RunConfig(mode="quick"))
    assert (quick.warmup, quick.repeat) == (harness.QUICK_WARMUP, harness.QUICK_REPEAT)
    overridden = harness.plan_for("B2", harness.RunConfig(mode="quick", repeat_override=3))
    assert (overridden.warmup, overridden.repeat) == (harness.QUICK_WARMUP, 3)


def test_only_selects_single_item(tmp_path: Path) -> None:
    """``--only`` 로 지정한 항목만 측정된다."""
    out = tmp_path / "only.json"
    assert harness.main(["--only", "B2", "--quick", "--label", "only", "--out", str(out)]) == 0
    result = json.loads(out.read_text(encoding="utf-8"))
    assert len(result["items"]) == 1
    assert result["items"][0]["id"] == "B2"


def test_unknown_item_id_is_usage_error(tmp_path: Path) -> None:
    """알 수 없는 항목 ID는 사용법 오류(2)로 막힌다."""
    out = tmp_path / "never.json"
    assert harness.main(["--only", "B9", "--out", str(out)]) == harness.EXIT_USAGE
    assert not out.exists()


# ---------------------------------------------------------------------------
# 5. 개인정보·시크릿 게이트
# ---------------------------------------------------------------------------
def test_environment_has_no_identifiers(quick_run: tuple[Path, dict[str, Any]]) -> None:
    """결과 전문에 호스트명·사용자명·홈 디렉터리·리포 절대 경로가 없다."""
    _out, result = quick_run
    text = json.dumps(result, ensure_ascii=False)
    forbidden = {
        "호스트명": platform.node(),
        "사용자명": getpass.getuser(),
        "홈 디렉터리": str(Path.home()),
        "리포 루트": str(harness.REPO_ROOT),
    }
    for label, value in forbidden.items():
        if not value:
            continue
        assert value not in text, f"{label} 이(가) 결과에 실렸습니다: {value!r}"
    assert "node" not in result["environment"]
    assert "user" not in result["environment"]


def test_sanitize_text_masks_paths() -> None:
    """사유 문자열의 경로·호스트명이 자리표시자로 치환된다."""
    masked = harness._sanitize_text(f"열 수 없음: {harness.REPO_ROOT}\\presets\\x.json")
    assert str(harness.REPO_ROOT) not in masked
    assert "<repo>" in masked
    home = harness._sanitize_text(f"{Path.home()} 아래")
    assert str(Path.home()) not in home


def test_secret_gate_blocks_write(tmp_path: Path) -> None:
    """결과에 인증키 대입이 섞이면 저장이 중단되고 파일이 생기지 않는다."""
    poisoned = {
        "schema": harness.SCHEMA_ID,
        "items": [{"id": "B1", "notes": ["문서 예시: /list?serviceKey=abc&pageNo=1"]}],
    }
    out = tmp_path / "blocked.json"
    with pytest.raises(harness.BenchmarkError) as excinfo:
        harness.write_result(poisoned, out)
    assert "serviceKey" in str(excinfo.value)
    assert not out.exists()


def test_clean_result_is_written(tmp_path: Path) -> None:
    """시크릿이 없으면 그대로 저장된다(게이트가 정상 결과를 막지 않는다)."""
    clean = {"schema": harness.SCHEMA_ID, "items": []}
    out = tmp_path / "clean.json"
    assert harness.write_result(clean, out) == out
    assert json.loads(out.read_text(encoding="utf-8")) == clean


def test_invalid_label_is_rejected(tmp_path: Path) -> None:
    """경로 탈출 문자가 든 라벨은 사용법 오류(2)로 막힌다."""
    out = tmp_path / "label.json"
    assert harness.main(["--label", "../x", "--quick", "--out", str(out)]) == harness.EXIT_USAGE
    assert not out.exists()
    assert harness.main(["--label", "a b", "--quick", "--out", str(out)]) == harness.EXIT_USAGE
    assert not out.exists()


# ---------------------------------------------------------------------------
# 6. 항목별 동작
# ---------------------------------------------------------------------------
def test_b5_skipped_when_fastmcp_missing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """fastmcp 미설치를 흉내내면 B5만 건너뛰고 전체 실행은 성공한다."""
    monkeypatch.setattr(harness, "fastmcp_available", lambda: False)
    out = tmp_path / "nofastmcp.json"
    assert harness.main(["--only", "B5", "--quick", "--label", "nomcp", "--out", str(out)]) == 0
    result = json.loads(out.read_text(encoding="utf-8"))
    item = result["items"][0]
    assert item["id"] == "B5"
    assert item["status"] == "skipped"
    assert item["reason"] == "fastmcp 미설치"
    assert "fastmcp" not in result["environment"]["packages"]


def test_single_item_failure_is_isolated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """한 항목이 실패해도 나머지는 측정되고, 종료 코드만 1이 된다."""

    def boom(config: Any, plan: Any) -> Any:
        raise RuntimeError("합성 강제 실패")

    monkeypatch.setitem(harness.ITEM_RUNNERS, "B2", boom)
    out = tmp_path / "failed.json"
    code = harness.main(["--only", "B1,B2", "--quick", "--label", "fail", "--out", str(out)])
    assert code == harness.EXIT_ERROR
    result = json.loads(out.read_text(encoding="utf-8"))
    statuses = {item["id"]: item["status"] for item in result["items"]}
    assert statuses == {"B1": "ok", "B2": "failed"}
    failed = next(item for item in result["items"] if item["id"] == "B2")
    assert "합성 강제 실패" in failed["reason"]
    assert_result_schema(result)


def test_b4_closes_its_ledger(tmp_path: Path) -> None:
    """B4가 만든 원장이 닫혀 있어 디렉터리를 지울 수 있다(Windows 파일 잠금 회귀)."""
    config = harness.RunConfig(mode="quick", label="b4", only=("B4",), work_dir=tmp_path)
    plan = harness.plan_for("B4", config)
    conditions, derived, notes = harness.run_b4(config, plan)

    names = {condition["name"] for condition in conditions}
    assert names == {"guarded", "bare"}
    assert derived["budget_limit"] == (plan.warmup + plan.repeat) * 10
    assert "overhead_median_ns" in derived and "overhead_median_pct" in derived
    assert any("WAL" in note for note in notes)

    ledger_dir = tmp_path / "b4"
    assert (ledger_dir / "ledger.db").is_file()
    shutil.rmtree(ledger_dir)
    assert not ledger_dir.exists()


def test_b3_reports_determinism(quick_run: tuple[Path, dict[str, Any]]) -> None:
    """B3는 합성 소스 3종을 결정론 판정과 함께 보고한다."""
    _out, result = quick_run
    item = next(entry for entry in result["items"] if entry["id"] == "B3")
    assert item["status"] == "ok"
    derived = item["derived"]
    assert derived["deterministic"] is True
    assert derived["first_mismatch_offset"] is None
    assert derived["determinism_runs"] == 5
    assert set(derived["per_source"]) >= {"syn_gw", "syn_odcloud", "syn_restdoc"}
    for source in harness.SYNTHETIC_SOURCES:
        for stage in ("load", "build", "dumps", "total"):
            assert any(c["name"] == f"{source.name}:{stage}" for c in ok_conditions(item))


def test_preset_conditions_skip_without_bundles(quick_run: tuple[Path, dict[str, Any]]) -> None:
    """프리셋 번들이 없으면 프리셋 조건만 건너뛰고 항목 자체는 정상이다."""
    _out, result = quick_run
    if harness.discover_presets(None):
        pytest.skip("리포에 프리셋 번들이 있어 건너뜀 경로를 검증할 수 없습니다.")
    for item_id in ("B3", "B5"):
        item = next(entry for entry in result["items"] if entry["id"] == item_id)
        if item["status"] != "ok":
            continue
        skipped = [c for c in item["conditions"] if c.get("status") == "skipped"]
        assert skipped, item_id
        assert all(c.get("reason") for c in skipped)


def test_discover_presets_filters_directories(tmp_path: Path) -> None:
    """프리셋 탐색은 밑줄 디렉터리와 ``source.json`` 없는 디렉터리를 제외한다."""
    assert harness.discover_presets(tmp_path / "없는경로") == ()
    for name in ("99900011", "99900010", "_raw", "빈디렉터리"):
        (tmp_path / name).mkdir()
    for name in ("99900011", "99900010", "_raw"):
        (tmp_path / name / "source.json").write_text("{}", encoding="utf-8")
    found = harness.discover_presets(tmp_path)
    assert [target.preset_id for target in found] == ["99900010", "99900011"]


def test_missing_presets_root_is_usage_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """없는 ``--presets-root`` 는 조용히 무시되지 않고 사용법 오류(2)로 막힌다.

    :func:`harness.discover_presets` 는 없는 경로를 빈 튜플로 흡수한다(프리셋이
    없는 환경을 정상으로 보기 위한 설계). 그 관용이 경로 오타까지 삼키면 합성
    조건만 잰 실행이 종료 코드 0 으로 끝나 프리셋 조건을 잰 실행과 구분되지
    않는다. 2026-08-09 Advisor 검증에서 잡힌 회귀다.
    """
    out = tmp_path / "never.json"
    missing = tmp_path / "presets_오타"
    code = harness.main(
        ["--only", "B3", "--quick", "--label", "typo", "--presets-root", str(missing), "--out", str(out)]
    )
    assert code == harness.EXIT_USAGE
    assert not out.exists(), "측정도 저장도 일어나지 않아야 합니다."
    captured = capsys.readouterr()
    assert "--presets-root" in captured.err
    assert captured.out == "", "가드가 걸린 실행이 요약을 출력했습니다."


def test_file_as_presets_root_is_usage_error(tmp_path: Path) -> None:
    """디렉터리가 아닌 파일을 프리셋 루트로 주면 같은 사용법 오류로 막힌다."""
    bundle = tmp_path / "presets.json"
    bundle.write_text("{}", encoding="utf-8")
    out = tmp_path / "never_file.json"
    code = harness.main(
        ["--only", "B3", "--quick", "--label", "file", "--presets-root", str(bundle), "--out", str(out)]
    )
    assert code == harness.EXIT_USAGE
    assert not out.exists()


def test_existing_empty_presets_root_still_runs(tmp_path: Path) -> None:
    """가드는 실재하는 디렉터리를 막지 않는다(번들이 0건이어도 정상 실행)."""
    root = tmp_path / "presets"
    root.mkdir()
    out = tmp_path / "empty_root.json"
    code = harness.main(
        ["--only", "B3", "--quick", "--label", "emptyroot", "--presets-root", str(root), "--out", str(out)]
    )
    assert code == harness.EXIT_OK
    result = json.loads(out.read_text(encoding="utf-8"))
    item = result["items"][0]
    assert item["id"] == "B3" and item["status"] == "ok"


# ---------------------------------------------------------------------------
# 7. 합성 입력이 실제로 합성인지
# ---------------------------------------------------------------------------
def test_synthetic_inputs_use_reserved_domains() -> None:
    """합성 입력은 예약 TLD(.invalid)만 쓰고 실기관·실서비스를 흉내내지 않는다."""
    for source in harness.SYNTHETIC_SOURCES:
        blob = json.dumps(source.document, ensure_ascii=False)
        assert "data.go.kr" not in blob
        assert ".invalid" in blob
        assert source.service_id.startswith("999")
    assert harness.SYNTHETIC_HOST.endswith(".invalid")


def test_synthetic_payload_sizes() -> None:
    """합성 입력 생성기가 목표 크기 이상을 만든다."""
    assert len(harness.synthetic_body(harness.SIZE_1KB, "x")) >= harness.SIZE_1KB
    assert len(harness.synthetic_url(harness.SIZE_64KB, key_assignments=0)) >= harness.SIZE_64KB
    assert len(harness.synthetic_text(harness.SIZE_1KB, echo_secrets=3)) >= harness.SIZE_1KB
    params = harness.synthetic_params(harness.SIZE_1KB, key_assignments=3)
    assert sum(len(k) + len(v) for k, v in params.items()) >= harness.SIZE_1KB


# ---------------------------------------------------------------------------
# 8. Windows 콘솔(cp949) 안전
# ---------------------------------------------------------------------------
def _cp949_offenders(text: str) -> list[str]:
    """cp949 로 인코딩할 수 없는 문자를 ``U+XXXX`` 표기로 모은다."""
    return sorted({f"U+{ord(ch):04X}" for ch in text if not _encodable(ch)})


def _encodable(ch: str) -> bool:
    """한 글자가 cp949 로 인코딩되는지 판정한다."""
    try:
        ch.encode("cp949")
    except UnicodeEncodeError:
        return False
    return True


def test_harness_source_has_no_cp949_hostile_characters() -> None:
    """하네스 소스 전체에 cp949 로 못 쓰는 문자가 없다.

    Windows 기본 콘솔 코드페이지는 cp949 이고, 그 인코더에는 em dash(U+2014)와
    en dash(U+2013)가 **없다**(0xA1A9 는 U+2015 HORIZONTAL BAR 로 다른 글자다).
    argparse 의 ``description`` 처럼 소스에 적힌 문면이 그대로 콘솔로 나가는
    자리가 있어 ``--help`` 한 번에 ``UnicodeEncodeError`` 로 죽는다. 실제로
    2026-08-06 통합 시점에 이 회귀가 잡혔다. 구분선·설명 모두 ASCII 로 쓴다.
    """
    offenders = _cp949_offenders(_HARNESS_PATH.read_text(encoding="utf-8"))
    assert offenders == [], f"harness.py 에 cp949 불가 문자: {offenders}"


def test_harness_help_survives_cp949_console() -> None:
    """``--help`` 를 cp949 stdout 로 강제한 실제 프로세스에서 0으로 끝난다."""
    import os
    import subprocess

    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "cp949"
    proc = subprocess.run(
        [sys.executable, str(_HARNESS_PATH), "--help"],
        capture_output=True,
        env=env,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr.decode("cp949", "replace")
    assert b"UnicodeEncodeError" not in proc.stderr
    assert "usage:" in proc.stdout.decode("cp949")


def test_harness_summary_output_is_cp949_safe(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """요약 출력(항목명·조건명·주의 문구)이 전부 cp949 로 나간다."""
    config = harness.RunConfig(mode="quick", label="cp949", only=("B2",))
    result = harness.run_benchmarks(config)
    harness._print_summary(result, tmp_path / "out.json")
    captured = capsys.readouterr()
    assert _cp949_offenders(captured.out) == []
    assert _cp949_offenders(captured.err) == []


def test_harness_hardens_streams_against_foreign_characters() -> None:
    """``harden_streams`` 가 strict 스트림의 정책만 낮춘다(인코딩은 그대로)."""

    class _Stream:
        def __init__(self, errors: str) -> None:
            self.errors = errors
            self.encoding = "cp949"

        def reconfigure(self, *, errors: str) -> None:
            self.errors = errors

    strict, lenient = _Stream("strict"), _Stream("replace")
    original = (sys.stdout, sys.stderr)
    sys.stdout, sys.stderr = strict, lenient  # type: ignore[assignment]
    try:
        harness.harden_streams()
    finally:
        sys.stdout, sys.stderr = original
    assert strict.errors == "backslashreplace"
    assert lenient.errors == "replace"  # 이미 관대하면 건드리지 않는다


# ---------------------------------------------------------------------------
# 9. 선등록 물증 사슬 - 커밋된 결과 파일 대조
# ---------------------------------------------------------------------------
def current_protocol_fingerprint() -> str:
    """현행 ``PROTOCOL.md`` 를 그 자리에서 다시 해싱해 기대 지문을 만든다.

    지문을 상수로 박아 두면 문서를 고칠 때 테스트도 함께 고치게 되고, 그 순간
    "결과 파일이 어느 판본으로 쟀는지"를 검증하던 장치가 사라진다. 그래서 항상
    실행 시점의 파일 바이트에서 계산한다.
    """
    digest = hashlib.sha256(harness.PROTOCOL_PATH.read_bytes()).hexdigest()[:12]
    return f"benchmarks/PROTOCOL.md@{digest}"


def test_protocol_fingerprint_tracks_document_bytes(tmp_path: Path) -> None:
    """지문은 문서 바이트에서 매번 계산된다(1바이트만 달라도 지문이 달라진다)."""
    assert harness.protocol_fingerprint() == current_protocol_fingerprint()
    edited = tmp_path / "PROTOCOL.md"
    edited.write_bytes(harness.PROTOCOL_PATH.read_bytes() + "\n<!-- 개정 -->\n".encode("utf-8"))
    assert harness.protocol_fingerprint(edited) != harness.protocol_fingerprint()


def test_committed_results_match_current_protocol() -> None:
    """리포에 남은 결과 파일은 현행 프로토콜 판본으로 잰 ``full`` 실행이어야 한다.

    측정을 끝낸 뒤 프로토콜을 개정하면 결과 파일의 ``protocol`` 지문이 문서와
    어긋나 "어느 판본으로 쟀는지"의 물증 사슬이 끊긴다. 2026-08-09 Advisor
    검증에서 실제로 끊긴 파일 1건(판본 2·3 개정 이전 측정)이 발견돼 리포 밖
    아카이브로 내보냈다. ``--quick`` 결과는 인용 금지이므로(PROTOCOL.md §7-1
    제5조) 애초에 커밋 대상이 아니다.

    결과 파일이 0건이면 통과한다 - 재측정 대기 상태가 곧 정상이다.
    """
    assert harness.RESULTS_DIR.is_dir(), "결과 디렉터리가 사라졌습니다."
    assert (harness.RESULTS_DIR / ".gitkeep").is_file(), "results/.gitkeep 이 사라졌습니다."
    expected = current_protocol_fingerprint()
    for path in sorted(harness.RESULTS_DIR.glob("*.json")):
        raw = path.read_bytes()
        assert not raw.startswith(b"\xef\xbb\xbf"), path.name
        assert raw.count(b"\r") == 0, path.name
        text = raw.decode("utf-8")
        assert text.endswith("\n") and not text.endswith("\n\n"), path.name
        result = json.loads(text)
        assert json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n" == text, path.name
        assert result["protocol"] == expected, (
            f"{path.name}: 프로토콜 지문 불일치 - 파일 {result['protocol']!r} vs 현행 {expected!r}"
        )
        assert result["mode"] == "full", f"{path.name}: quick 결과는 커밋하지 않는다."
        assert_result_schema(result)
