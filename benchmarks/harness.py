# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Yong Park
"""MCPortal 무키(no-key) 벤치마크 하네스.

``benchmarks/PROTOCOL.md`` 에 **먼저 선등록된** 측정 항목 5종(B1~B5)을 실행하고
결과를 JSON 으로 남긴다. 프로토콜에 없는 항목은 재지 않으며, 측정 결과를 본 뒤
프로토콜을 고치지 않는다(선등록 고정 조항).

철칙
----
* **네트워크 호출 0건.** 상위 트랜스포트는 :class:`httpx.MockTransport` 또는
  :class:`~mcportal.replay.cassette.ReplayTransport` 뿐이며, 이 모듈은
  ``httpx.HTTPTransport`` · ``httpx.AsyncHTTPTransport`` 를 만들지 않는다.
  모든 도메인은 RFC 2606 예약 TLD ``.invalid`` 를 쓴다.
* **인증키 0건 · 실응답데이터 0건.** 입력은 100% 합성이며 합성임이 자명하다.
* **개인정보 0건.** 환경 블록에 호스트명·사용자명·홈 디렉터리·절대 경로를 싣지
  않는다(:func:`collect_environment` · :func:`_sanitize_text`).
* **저장 직전 시크릿 게이트.** 결과 전문에 인증키 대입이 남아 있으면 저장을
  중단하고 파일을 만들지 않는다(:func:`write_result`).

실행::

    python benchmarks/harness.py [--out PATH] [--label TEXT] [--only ID[,ID...]]
                                 [--repeat N] [--quick] [--presets-root PATH]
"""

from __future__ import annotations

import argparse
import asyncio
import gc
import hashlib
import importlib.util
import json
import math
import os
import platform
import re
import shutil
import sqlite3
import statistics
import sys
import tempfile
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Union

import httpx

from mcportal.compiler.openapi import CompileOptions, build_openapi, dumps
from mcportal.compiler.sources import SourceSpec, load_source
from mcportal.profiles import DATA_GO_KR
from mcportal.quota import DailyBudget, QuotaGuard, UsageLedger
from mcportal.replay.cassette import Cassette, ReplayTransport
from mcportal.replay.scrub import (
    find_key_assignments,
    scrub_params,
    scrub_text,
    scrub_url,
)
from mcportal.transport import MCPortalTransport

PathLike = Union[str, Path]

# ---------------------------------------------------------------------------
# 상수
# ---------------------------------------------------------------------------
#: 결과 파일 스키마 식별자.
SCHEMA_ID: str = "mcportal.benchmark/1"

#: 이 파일이 있는 디렉터리(= ``benchmarks/``).
BENCHMARKS_DIR: Path = Path(__file__).resolve().parent

#: 리포 루트. 결과에 절대 경로를 싣지 않기 위한 상대화 기준점이다.
REPO_ROOT: Path = BENCHMARKS_DIR.parent

#: 선등록 프로토콜 문서 경로.
PROTOCOL_PATH: Path = BENCHMARKS_DIR / "PROTOCOL.md"

#: 기본 결과 디렉터리.
RESULTS_DIR: Path = BENCHMARKS_DIR / "results"

#: 결과 파일에 싣는 원자료 배열의 상한(초과 시 균등 간격 표본추출).
MAX_STORED_SAMPLES: int = 1000

#: ``--label`` 허용 문자(파일명 안전 · 경로 탈출 차단).
LABEL_RE: re.Pattern[str] = re.compile(r"^[A-Za-z0-9_-]{1,32}$")

#: 종료 코드 규약(PROTOCOL.md §5-1).
EXIT_OK: int = 0
EXIT_ERROR: int = 1
EXIT_USAGE: int = 2

#: KST 고정 오프셋(+09:00). tzdata 의존을 피하기 위한 고정값.
_KST = timezone(timedelta(hours=9))

#: 합성 시크릿 3종. 리포 테스트 규약과 같은 형태이며 실인증키가 아니다.
SYNTHETIC_SECRETS: tuple[str, str, str] = (
    "ab12+CD/34==",
    "zz99+YX/00==",
    "qq00+MN/77==",
)

#: 합성 벤치마크가 쓰는 예약 도메인(RFC 2606 ``.invalid``). 실호출이 나갈 수 없다.
SYNTHETIC_HOST: str = "https://apis.example.invalid"

#: 측정 입력 크기 목표치(바이트).
SIZE_1KB: int = 1024
SIZE_64KB: int = 64 * 1024


class BenchmarkError(RuntimeError):
    """하네스가 결과를 만들거나 저장할 수 없을 때 발생한다(한국어 메시지)."""


class ItemSkipped(Exception):
    """항목 전체를 건너뛰어야 할 때 러너가 던진다(사유 문자열이 곧 ``reason``)."""


# ---------------------------------------------------------------------------
# 공통 유틸
# ---------------------------------------------------------------------------
def kst_date() -> str:
    """오늘 날짜(KST)를 ``YYYY-MM-DD`` 로 돌려준다.

    시·분·초는 남기지 않는다(PROTOCOL.md §3-3).
    """
    return datetime.now(_KST).date().isoformat()


def kst_filename_date() -> str:
    """기본 결과 파일명에 쓰는 날짜(KST)를 ``YYYYMMDD`` 로 돌려준다."""
    return datetime.now(_KST).strftime("%Y%m%d")


def protocol_fingerprint(path: PathLike = PROTOCOL_PATH) -> str:
    """선등록 프로토콜 문서의 지문을 ``benchmarks/PROTOCOL.md@<12hex>`` 로 만든다.

    결과가 어느 판본의 프로토콜로 측정됐는지 사후 확인할 수 있게 한다.

    Args:
        path: 프로토콜 문서 경로.

    Returns:
        지문 문자열. 문서를 읽을 수 없으면 ``@unavailable``.
    """
    target = Path(path)
    try:
        digest = hashlib.sha256(target.read_bytes()).hexdigest()[:12]
    except OSError:
        return "benchmarks/PROTOCOL.md@unavailable"
    return f"benchmarks/PROTOCOL.md@{digest}"


def _sanitize_text(text: str) -> str:
    """자유문자열에서 식별자(절대 경로·호스트명)를 자리표시자로 치환한다.

    예외 메시지·건너뜀 사유에는 파일 경로가 섞이기 쉽다. 결과 파일은 커밋 대상
    이므로 홈 디렉터리·리포 루트·호스트명이 그대로 실리면 안 된다
    (PROTOCOL.md §3-2).

    Args:
        text: 원본 문자열.

    Returns:
        치환된 문자열.
    """
    result = str(text)
    candidates: list[tuple[str, str]] = []
    for raw, placeholder in (
        (str(REPO_ROOT), "<repo>"),
        (str(Path.home()), "<home>"),
        (platform.node(), "<host>"),
    ):
        if raw and len(raw) >= 3:
            candidates.append((raw, placeholder))
            candidates.append((raw.replace("\\", "/"), placeholder))
    # 긴 후보부터 치환해야 짧은 후보가 긴 후보를 먼저 깨뜨리지 않는다.
    for raw, placeholder in sorted(candidates, key=lambda item: -len(item[0])):
        result = result.replace(raw, placeholder)
    return result


def _relative_path(path: PathLike) -> str:
    """경로를 리포 루트 기준 상대 경로 문자열(슬래시 구분)로 바꾼다.

    리포 밖 경로는 파일명만 남긴다. 결과 파일에 절대 경로를 싣지 않기 위한
    변환이다(PROTOCOL.md §3-2).
    """
    target = Path(path)
    try:
        return target.resolve().relative_to(REPO_ROOT).as_posix()
    except (ValueError, OSError):
        return target.name


def _package_version(name: str) -> str | None:
    """설치된 패키지 버전을 돌려준다(미설치·조회 실패면 ``None``)."""
    from importlib import metadata

    try:
        return metadata.version(name)
    except Exception:  # pragma: no cover - 배포 메타데이터 부재 방어
        return None


def fastmcp_available() -> bool:
    """fastmcp 가 임포트 가능한지 확인한다(임포트하지는 않는다).

    테스트가 이 함수를 대체해 "미설치 환경"을 흉내낸다.

    Returns:
        임포트 가능하면 ``True``.
    """
    try:
        return importlib.util.find_spec("fastmcp") is not None
    except (ImportError, ValueError):  # pragma: no cover - 손상된 배포 방어
        return False


def collect_environment(label: str) -> dict[str, Any]:
    """환경 블록을 만든다(개인정보 0 - PROTOCOL.md §3).

    호스트명(``platform.node()``)·사용자명·홈 디렉터리·절대 경로·환경변수 덤프는
    **일부러 담지 않는다.**

    Args:
        label: 실행 라벨.

    Returns:
        결과 파일 ``environment`` 블록.
    """
    import mcportal

    # mcportal 은 배포 메타데이터(편집 설치 시 dev 접미가 붙는다)가 아니라 패키지가
    # 스스로 선언한 __version__ 을 싣는다 - 산출물 결정론의 기준이 그 값이다.
    packages: dict[str, str] = {"mcportal": str(getattr(mcportal, "__version__", "unknown"))}
    httpx_version = _package_version("httpx")
    if httpx_version is not None:
        packages["httpx"] = httpx_version
    if fastmcp_available():
        fastmcp_ver = _package_version("fastmcp")
        if fastmcp_ver is not None:
            packages["fastmcp"] = fastmcp_ver
    return {
        "python_version": platform.python_version(),
        "implementation": platform.python_implementation(),
        "system": platform.system(),
        "release": platform.release(),
        "machine": platform.machine(),
        "processor": platform.processor(),
        "cpu_count": os.cpu_count() or 0,
        "sqlite_version": sqlite3.sqlite_version,
        "packages": packages,
        "label": label,
    }


# ---------------------------------------------------------------------------
# 통계 (PROTOCOL.md §4)
# ---------------------------------------------------------------------------
def percentile_nearest_rank(samples: Sequence[int], ratio: float) -> int:
    """nearest-rank 백분위수를 돌려준다(보간하지 않는다).

    오름차순 정렬 후 ``ceil(ratio * n)`` 번째(1-기반) 값이다. 보간 방식 차이로
    수치가 흔들리지 않도록 계산식을 프로토콜에 고정해 두었다.

    Args:
        samples: 표본(나노초).
        ratio: 0 초과 1 이하의 비율(예: p95 는 ``0.95``).

    Returns:
        해당 순위의 표본값.

    Raises:
        ValueError: 표본이 비었을 때.
    """
    if not samples:
        raise ValueError("표본이 비어 있어 백분위수를 계산할 수 없습니다.")
    ordered = sorted(int(value) for value in samples)
    rank = max(1, min(len(ordered), math.ceil(ratio * len(ordered))))
    return ordered[rank - 1]


def median_ns(samples: Sequence[int]) -> int:
    """중앙값을 나노초 정수로 돌려준다(짝수 표본은 **반올림 half-up**).

    파이썬 내장 ``round()`` 는 은행가 반올림(half-to-even)이라 ``median([1,2,3,4])``
    가 2.5 → 2 가 됐다. PROTOCOL.md §4-4 는 "가운데 두 값의 평균을 **반올림한
    정수**"라고 선등록했으므로 문면과 구현이 어긋났다(적대 리뷰 F15). 수치 영향은
    최대 1 ns 지만 이 프로젝트가 내세우는 값이 "선등록 문면과 구현의 일치"이므로
    ``math.floor(x + 0.5)`` 로 half-up 을 명시한다.

    Raises:
        ValueError: 표본이 비었을 때.
    """
    if not samples:
        raise ValueError("표본이 비어 있어 중앙값을 계산할 수 없습니다.")
    return math.floor(statistics.median([int(value) for value in samples]) + 0.5)


def stdev_ns(samples: Sequence[int]) -> float | None:
    """표본표준편차를 돌려준다. ``n < 2`` 면 ``None``."""
    values = [int(value) for value in samples]
    if len(values) < 2:
        return None
    return float(statistics.stdev(values))


def _stored_samples(samples: Sequence[int]) -> tuple[list[int], bool]:
    """결과 파일에 실을 표본 배열을 만든다(상한 초과 시 균등 간격 추출).

    통계는 **언제나 전체 표본**으로 계산하며, 줄어드는 것은 배열뿐이다.

    Returns:
        ``(실을 배열, 잘렸는지 여부)``.
    """
    values = [int(value) for value in samples]
    if len(values) <= MAX_STORED_SAMPLES:
        return values, False
    step = len(values) / MAX_STORED_SAMPLES
    picked = [values[min(len(values) - 1, int(index * step))] for index in range(MAX_STORED_SAMPLES)]
    return picked, True


def summarize_samples(name: str, samples: Sequence[int]) -> dict[str, Any]:
    """표본을 조건(condition) 딕셔너리로 요약한다(PROTOCOL.md §4·§9).

    Args:
        name: 조건 이름.
        samples: 나노초 표본.

    Returns:
        ``status="ok"`` 인 조건 딕셔너리.

    Raises:
        ValueError: 표본이 비었을 때.
    """
    values = [int(value) for value in samples]
    if not values:
        raise ValueError(f"조건 '{name}'의 표본이 비어 있습니다.")
    stored, truncated = _stored_samples(values)
    return {
        "name": name,
        "status": "ok",
        "n": len(values),
        "min_ns": min(values),
        "median_ns": median_ns(values),
        "p95_ns": percentile_nearest_rank(values, 0.95),
        "mean_ns": float(statistics.fmean(values)),
        "stdev_ns": stdev_ns(values),
        "samples_ns": stored,
        "samples_truncated": truncated,
    }


def skipped_condition(name: str, reason: str) -> dict[str, Any]:
    """건너뛴 조건 딕셔너리를 만든다(통계 필드 없이 사유만)."""
    return {"name": name, "status": "skipped", "reason": _sanitize_text(reason)}


def determinism_check(texts: Sequence[str]) -> tuple[bool, int | None]:
    """여러 번 산출한 문자열이 전부 동일한지 판정한다.

    B3 의 결정론은 시간이 아니라 불리언이다. 하나라도 다르면 **첫 불일치 문자
    오프셋**을 함께 돌려준다.

    Args:
        texts: 같은 입력으로 반복 산출한 결과들.

    Returns:
        ``(전부 동일한가, 첫 불일치 오프셋 또는 None)``.

    Raises:
        ValueError: 비교할 결과가 없을 때.
    """
    if not texts:
        raise ValueError("비교할 산출물이 없습니다.")
    first = texts[0]
    for other in texts[1:]:
        if other == first:
            continue
        limit = min(len(first), len(other))
        for offset in range(limit):
            if first[offset] != other[offset]:
                return False, offset
        return False, limit
    return True, None


def measure(
    operation: Callable[[int], None],
    *,
    warmup: int,
    repeat: int,
) -> list[int]:
    """warmup 을 버리고 본 측정 ``repeat`` 회의 소요 시간을 재서 돌려준다.

    시계는 ``time.perf_counter_ns()`` 이며 벽시계를 쓰지 않는다. 측정 구간 안에서
    GC 를 끄지 않는다(현실 조건 유지 - PROTOCOL.md §4).

    Args:
        operation: 반복 인덱스를 받는 측정 대상 호출.
        warmup: 버릴 예열 횟수.
        repeat: 남길 본 측정 횟수.

    Returns:
        나노초 표본 리스트(길이 ``repeat``).
    """
    for index in range(max(0, warmup)):
        operation(index)
    samples: list[int] = []
    for index in range(max(1, repeat)):
        start = time.perf_counter_ns()
        operation(index)
        samples.append(time.perf_counter_ns() - start)
    return samples


# ---------------------------------------------------------------------------
# 합성 입력
# ---------------------------------------------------------------------------
def synthetic_body(target_bytes: int, marker: str) -> str:
    """목표 크기에 맞춘 합성 JSON 응답 본문을 만든다(실응답 아님).

    본문은 표준형 봉투 모양이며 채움 문자열로 크기를 맞춘다. 값은 전부
    ``SYNTHETIC`` 계열 자리표시자다.

    Args:
        target_bytes: 목표 바이트 수(ASCII 채움이라 문자 수와 같다).
        marker: 상호작용 식별 문자열.

    Returns:
        JSON 문자열.
    """
    envelope: dict[str, Any] = {
        "response": {
            "header": {"resultCode": "00", "resultMsg": "SYNTHETIC-OK"},
            "body": {"marker": marker, "filler": ""},
        }
    }
    base = json.dumps(envelope, ensure_ascii=False)
    pad = max(target_bytes - len(base), 0)
    envelope["response"]["body"]["filler"] = "S" * pad
    return json.dumps(envelope, ensure_ascii=False)


def synthetic_url(target_bytes: int, *, key_assignments: int) -> str:
    """목표 길이에 맞춘 합성 URL 을 만든다(B2 입력).

    Args:
        target_bytes: 목표 문자 수(ASCII).
        key_assignments: 인증키 대입 개수(0 또는 3).

    Returns:
        질의문자열이 붙은 합성 URL.
    """
    parts: list[str] = [f"{SYNTHETIC_HOST}/bench/scrub/v1/list?fixed=1"]
    for index in range(key_assignments):
        parts.append(f"&{DATA_GO_KR.key_param}={SYNTHETIC_SECRETS[index % len(SYNTHETIC_SECRETS)]}")
    filler_index = 0
    while sum(len(part) for part in parts) < target_bytes:
        parts.append(f"&p{filler_index}=SYNTHETICVALUE{filler_index:04d}")
        filler_index += 1
    return "".join(parts)


def synthetic_params(target_bytes: int, *, key_assignments: int) -> dict[str, str]:
    """목표 크기에 맞춘 합성 파라미터 매핑을 만든다(B2 입력).

    인증키 대입 개수는 **대소문자 변형이 다른 키 이름**으로 표현한다
    (``scrub_params`` 는 이름을 대소문자 무시로 판정한다).

    Args:
        target_bytes: 키·값 문자 수 합계의 목표치.
        key_assignments: 인증키 항목 개수(0 또는 3).

    Returns:
        합성 파라미터 매핑.
    """
    variants = (
        DATA_GO_KR.key_param,
        DATA_GO_KR.key_param.lower(),
        DATA_GO_KR.key_param.upper(),
    )
    params: dict[str, str] = {"fixed": "1"}
    for index in range(key_assignments):
        params[variants[index % len(variants)]] = SYNTHETIC_SECRETS[index % len(SYNTHETIC_SECRETS)]
    filler_index = 0
    while sum(len(key) + len(value) for key, value in params.items()) < target_bytes:
        params[f"p{filler_index}"] = f"SYNTHETICVALUE{filler_index:04d}"
        filler_index += 1
    return params


def synthetic_text(target_bytes: int, *, echo_secrets: int) -> str:
    """목표 크기에 맞춘 합성 본문 텍스트를 만든다(B2 ``scrub_text`` 입력).

    Args:
        target_bytes: 목표 문자 수(ASCII).
        echo_secrets: 본문에 되비칠 합성 시크릿 개수.

    Returns:
        합성 텍스트.
    """
    chunks: list[str] = ["SYNTHETIC-BENCH-BODY|"]
    for index in range(echo_secrets):
        chunks.append(f"echo{index}:{SYNTHETIC_SECRETS[index % len(SYNTHETIC_SECRETS)]}|")
    filler_index = 0
    while sum(len(chunk) for chunk in chunks) < target_bytes:
        chunks.append(f"row{filler_index:05d}=SYNTHETICVALUE|")
        filler_index += 1
    return "".join(chunks)


@dataclass(frozen=True)
class SyntheticSource:
    """B3·B5 가 쓰는 합성 스펙 소스 1건."""

    name: str
    service_id: str
    service_name: str
    document: Mapping[str, Any]


def _syn_gw_document() -> dict[str, Any]:
    """게이트웨이 Swagger 2.0 형 합성 문서를 만든다(실기관·실서비스 아님)."""
    item_schema: dict[str, Any] = {
        "type": "object",
        "properties": {
            "areaCd": {"type": "string"},
            "areaNm": {"type": "string"},
            "inCnt": {"type": "integer"},
            "inAmt": {"type": "number"},
            "outCnt": {"type": "integer"},
            "outAmt": {"type": "number"},
            "ym": {"type": "string"},
        },
    }
    return {
        "swagger": "2.0",
        "info": {
            "title": "가상통계원 지역별 물동실적 서비스",
            "description": "벤치마크 전용 합성 스펙. 실제 서비스가 아니다.",
            "version": "1.0",
        },
        "host": "apis.example.invalid/1000000/benchflow",
        "basePath": "",
        "schemes": ["https"],
        "produces": ["application/xml"],
        "paths": {
            "/getBenchFlowList": {
                "get": {
                    "operationId": "getBenchFlowList",
                    "summary": "합성 지역별 물동실적 조회",
                    "description": "합성 입력이다. 월 범위와 지역코드로 물동실적을 조회한다.",
                    "parameters": [
                        {
                            "name": "serviceKey",
                            "in": "query",
                            "required": True,
                            "type": "string",
                            "description": "인증키(트랜스포트가 주입한다).",
                        },
                        {
                            "name": "fromYm",
                            "in": "query",
                            "required": True,
                            "type": "string",
                            "description": "조회 시작 연월(YYYYMM).",
                        },
                        {
                            "name": "toYm",
                            "in": "query",
                            "required": True,
                            "type": "string",
                            "description": "조회 종료 연월(YYYYMM).",
                        },
                        {
                            "name": "areaCd",
                            "in": "query",
                            "required": False,
                            "type": "string",
                            "description": "지역코드 2자리(생략 시 전체).",
                        },
                    ],
                    "responses": {
                        "200": {
                            "description": "정상",
                            "schema": {
                                "type": "object",
                                "properties": {
                                    "header": {
                                        "type": "object",
                                        "properties": {
                                            "resultCode": {"type": "string", "properties": {}},
                                            "resultMsg": {"type": "string", "properties": {}},
                                        },
                                    },
                                    "body": {
                                        "type": "object",
                                        "properties": {
                                            "items": {
                                                "type": "object",
                                                "properties": {"item": item_schema},
                                            }
                                        },
                                    },
                                },
                            },
                        }
                    },
                }
            }
        },
    }


def _syn_odcloud_document() -> dict[str, Any]:
    """odcloud OAS 3.x 형 합성 문서를 만든다(POST 본문형 2오퍼레이션)."""
    return {
        "openapi": "3.0.1",
        "info": {
            "title": "가상등록원 등록상태 확인 서비스",
            "description": "벤치마크 전용 합성 스펙. 실제 서비스가 아니다.",
            "version": "1.0",
        },
        "servers": [{"url": "https://api.odcloud.invalid/api/bench-registry/v1"}],
        "paths": {
            "/status": {
                "post": {
                    "operationId": "benchStatus",
                    "summary": "합성 등록상태 조회",
                    "requestBody": {
                        "content": {
                            "application/json": {
                                "schema": {
                                    "type": "object",
                                    "properties": {
                                        "codes": {"type": "array", "items": {"type": "string"}}
                                    },
                                    "required": ["codes"],
                                }
                            }
                        }
                    },
                    "responses": {
                        "200": {
                            "description": "정상",
                            "content": {
                                "application/json": {
                                    "schema": {
                                        "type": "object",
                                        "properties": {
                                            "match_cnt": {"type": "integer"},
                                            "request_cnt": {"type": "integer"},
                                            "data": {
                                                "type": "array",
                                                "items": {
                                                    "type": "object",
                                                    "properties": {
                                                        "code": {"type": "string"},
                                                        "state": {"type": "string"},
                                                        "state_cd": {"type": "string"},
                                                    },
                                                },
                                            },
                                        },
                                    }
                                }
                            },
                        }
                    },
                }
            },
            "/verify": {
                "post": {
                    "operationId": "benchVerify",
                    "summary": "합성 등록정보 대조",
                    "requestBody": {
                        "content": {
                            "application/json": {
                                "schema": {
                                    "type": "object",
                                    "properties": {
                                        "entries": {
                                            "type": "array",
                                            "items": {
                                                "type": "object",
                                                "properties": {
                                                    "code": {"type": "string"},
                                                    "opened_on": {"type": "string"},
                                                },
                                            },
                                        }
                                    },
                                }
                            }
                        }
                    },
                    "responses": {
                        "200": {
                            "description": "정상",
                            "content": {
                                "application/json": {
                                    "schema": {
                                        "type": "object",
                                        "properties": {
                                            "data": {
                                                "type": "array",
                                                "items": {
                                                    "type": "object",
                                                    "properties": {
                                                        "code": {"type": "string"},
                                                        "valid": {"type": "string"},
                                                    },
                                                },
                                            }
                                        },
                                    }
                                }
                            },
                        }
                    },
                }
            },
        },
    }


def _syn_restdoc_document() -> dict[str, Any]:
    """수동 매핑 기술서형 합성 문서를 만든다(8 오퍼레이션).

    도메인이 갈리는 다(多)오퍼레이션 소스의 컴파일 비용을 재기 위한 입력이다.
    """
    scopes = (
        ("alpha", "합성 알파 목록 조회"),
        ("bravo", "합성 브라보 목록 조회"),
        ("charlie", "합성 찰리 목록 조회"),
        ("delta", "합성 델타 목록 조회"),
        ("echo", "합성 에코 목록 조회"),
        ("foxtrot", "합성 폭스트롯 목록 조회"),
        ("golf", "합성 골프 목록 조회"),
        ("hotel", "합성 호텔 목록 조회"),
    )
    operations: list[dict[str, Any]] = []
    for scope, summary in scopes:
        operations.append(
            {
                "operation_id": f"{scope}SearchList",
                "method": "GET",
                "path": f"/{scope}SearchList.do",
                "summary": summary,
                "description": "벤치마크 전용 합성 오퍼레이션이다.",
                "response_media_type": "application/xml",
                "parameters": [
                    {
                        "name": "serviceKey",
                        "location": "query",
                        "required": True,
                        "type": "string",
                        "description": "인증키(트랜스포트가 주입한다).",
                    },
                    {
                        "name": "scope",
                        "location": "query",
                        "required": True,
                        "type": "string",
                        "description": "대상 구분(고정값).",
                        "enum": [scope],
                        "default": scope,
                    },
                    {
                        "name": "keyword",
                        "location": "query",
                        "required": True,
                        "type": "string",
                        "description": "검색어. 전체 목록은 별표를 쓴다.",
                        "example": "*",
                        "default": "*",
                    },
                    {
                        "name": "numOfRows",
                        "location": "query",
                        "required": True,
                        "type": "integer",
                        "description": "한 페이지 결과 수.",
                        "example": "10",
                    },
                    {
                        "name": "pageNo",
                        "location": "query",
                        "required": True,
                        "type": "integer",
                        "description": "페이지 번호(1부터).",
                        "example": "1",
                    },
                ],
            }
        )
    return {
        "mcportal_rest_doc": 1,
        "service_id": "99900002",
        "service_name": "가상자료원 통합검색 공유서비스",
        "description": "벤치마크 전용 합성 기술서. 실제 서비스가 아니다.",
        "base_url": f"{SYNTHETIC_HOST}/benchsearch",
        "license_note": "합성 픽스처(라이선스 표기 없음)",
        "operations": operations,
    }


#: B3·B5 가 항상 쓰는 합성 소스 3종(프리셋 유무와 무관하게 측정된다).
SYNTHETIC_SOURCES: tuple[SyntheticSource, ...] = (
    SyntheticSource(
        name="syn_gw",
        service_id="99900003",
        service_name="가상통계원 지역별 물동실적 서비스",
        document=_syn_gw_document(),
    ),
    SyntheticSource(
        name="syn_odcloud",
        service_id="99900004",
        service_name="가상등록원 등록상태 확인 서비스",
        document=_syn_odcloud_document(),
    ),
    SyntheticSource(
        name="syn_restdoc",
        service_id="99900002",
        service_name="가상자료원 통합검색 공유서비스",
        document=_syn_restdoc_document(),
    ),
)


def _bench_response_handler(request: httpx.Request) -> httpx.Response:
    """합성 200 응답을 돌려주는 MockTransport 핸들러(네트워크 없음)."""
    return httpx.Response(
        200,
        headers={"content-type": "application/json; charset=utf-8"},
        content=synthetic_body(512, "mock").encode("utf-8"),
        request=request,
    )


# ---------------------------------------------------------------------------
# 실행 계획
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class ItemPlan:
    """항목 1개의 반복 계획(PROTOCOL.md §4-1)."""

    item_id: str
    title: str
    warmup: int
    repeat: int


#: 프로토콜 정본 반복 수. ``--repeat`` · ``--quick`` 이 이 값을 덮어쓴다.
ITEM_PLANS: tuple[ItemPlan, ...] = (
    ItemPlan("B1", "replay 왕복 지연", warmup=20, repeat=200),
    ItemPlan("B2", "스크러빙 오버헤드", warmup=50, repeat=500),
    ItemPlan("B3", "컴파일 시간 + 결정론", warmup=3, repeat=30),
    ItemPlan("B4", "쿼터가드 오버헤드", warmup=20, repeat=200),
    ItemPlan("B5", "FastMCP 도구 빌드 시간", warmup=2, repeat=10),
)

#: 항목 ID 목록(고정 실행 순서).
ITEM_IDS: tuple[str, ...] = tuple(plan.item_id for plan in ITEM_PLANS)

#: ``--quick`` 축소 실행 값(프로토콜 준수 실행이 아니다).
QUICK_WARMUP: int = 1
QUICK_REPEAT: int = 5


@dataclass(frozen=True)
class RunConfig:
    """1회 실행의 설정."""

    mode: str = "full"
    label: str = "dev"
    only: tuple[str, ...] = ITEM_IDS
    repeat_override: int | None = None
    presets_root: Path | None = None
    work_dir: Path | None = None


def plan_for(item_id: str, config: RunConfig) -> ItemPlan:
    """항목 계획에 ``--quick`` · ``--repeat`` 덮어쓰기를 적용한다.

    ``--repeat`` 는 **N 만** 덮어쓰며 warmup 은 그대로 둔다(PROTOCOL.md §4-1).

    Args:
        item_id: 항목 ID.
        config: 실행 설정.

    Returns:
        적용된 :class:`ItemPlan`.

    Raises:
        KeyError: 알 수 없는 항목 ID.
    """
    base = next((plan for plan in ITEM_PLANS if plan.item_id == item_id), None)
    if base is None:
        raise KeyError(item_id)
    warmup = base.warmup
    repeat = base.repeat
    if config.mode == "quick":
        warmup, repeat = QUICK_WARMUP, QUICK_REPEAT
    if config.repeat_override is not None:
        repeat = int(config.repeat_override)
    return ItemPlan(base.item_id, base.title, warmup=warmup, repeat=repeat)


# ---------------------------------------------------------------------------
# 프리셋 접근(있으면 추가 조건, 없으면 건너뜀)
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class PresetTarget:
    """벤치마크가 쓰는 프리셋 번들 1건."""

    preset_id: str
    directory: Path


def discover_presets(root: Path | None) -> tuple[PresetTarget, ...]:
    """프리셋 번들을 찾는다(없으면 빈 튜플 - 예외를 던지지 않는다).

    하네스는 프리셋에 의존하지 않는다. 번들이 있으면 실스펙 조건을 추가로 재고,
    없으면 합성 조건만 잰다(PROTOCOL.md §2 B3·B5). 이 관용은 **경로를 지정하지
    않은** 실행에만 해당한다. ``--presets-root`` 로 명시한 경로가 없을 때는
    :func:`main` 이 사용법 오류(2)로 막는다.

    Args:
        root: 프리셋 루트. ``None`` 이면 리포 ``presets/`` 를 본다.

    Returns:
        디렉터리명 오름차순의 프리셋 목록.
    """
    base = Path(root) if root is not None else (REPO_ROOT / "presets")
    if not base.is_dir():
        return ()
    found: list[PresetTarget] = []
    for child in sorted(base.iterdir(), key=lambda path: path.name):
        if not child.is_dir() or child.name.startswith("_"):
            continue
        if (child / "source.json").is_file():
            found.append(PresetTarget(child.name, child))
    return tuple(found)


def _curation_module() -> Any | None:
    """``mcportal.compiler.curation`` 을 임포트해 돌려준다(없으면 ``None``).

    큐레이션 모듈은 하네스의 필수 의존이 아니다. 없으면 프리셋 조건만 건너뛴다.
    """
    try:
        from mcportal.compiler import curation as module
    except Exception:  # pragma: no cover - 모듈 부재·부분 구현 방어
        return None
    return module


# ---------------------------------------------------------------------------
# B1 - replay 왕복 지연
# ---------------------------------------------------------------------------
_B1_URL: str = f"{SYNTHETIC_HOST}/bench/replay/v1/items"
_B1_INTERACTIONS: int = 10


def _build_bench_cassette(payload_bytes: int) -> Cassette:
    """합성 상호작용 10건짜리 카세트를 만든다(녹화가 아니라 리터럴 합성)."""
    cassette = Cassette(recorded_at="2026-01-01T00:00:00+09:00")
    for index in range(_B1_INTERACTIONS):
        cassette.add(
            method="GET",
            url=_B1_URL,
            params={"n": str(index)},
            status=200,
            content_type="application/json; charset=utf-8",
            body_text=synthetic_body(payload_bytes, f"n{index}"),
            secrets=(),
        )
    return cassette


def run_b1(config: RunConfig, plan: ItemPlan) -> tuple[list[dict[str, Any]], dict[str, Any], list[str]]:
    """B1 - 합성 카세트 재생 1왕복 시간을 잰다."""
    conditions: list[dict[str, Any]] = []
    for name, payload_bytes in (("payload_1kb", SIZE_1KB), ("payload_64kb", SIZE_64KB)):
        cassette = _build_bench_cassette(payload_bytes)
        client = httpx.Client(transport=ReplayTransport(cassette))
        try:
            def roundtrip(index: int, _client: httpx.Client = client) -> None:
                _client.get(_B1_URL, params={"n": str(index % _B1_INTERACTIONS)})

            samples = measure(roundtrip, warmup=plan.warmup, repeat=plan.repeat)
        finally:
            client.close()
        conditions.append(summarize_samples(name, samples))
    notes = [
        f"합성 카세트 상호작용 {_B1_INTERACTIONS}건. 카세트 매칭은 선형 탐색이므로 "
        "상호작용 수가 늘면 값이 늘어난다(PROTOCOL.md L1).",
        "네트워크 호출 0건 - ReplayTransport 단독 경로다.",
    ]
    return conditions, {"interactions": _B1_INTERACTIONS}, notes


# ---------------------------------------------------------------------------
# B2 - 스크러빙 오버헤드
# ---------------------------------------------------------------------------
def run_b2(config: RunConfig, plan: ItemPlan) -> tuple[list[dict[str, Any]], dict[str, Any], list[str]]:
    """B2 - ``scrub_url`` · ``scrub_params`` · ``scrub_text`` 각 1회 호출 비용."""
    conditions: list[dict[str, Any]] = []
    sizes = (("1kb", SIZE_1KB), ("64kb", SIZE_64KB))
    secret_counts = (("s0", 0), ("s3", 3))

    for size_label, size in sizes:
        for secret_label, count in secret_counts:
            url = synthetic_url(size, key_assignments=count)

            def call_url(_index: int, _url: str = url) -> None:
                scrub_url(_url)

            conditions.append(
                summarize_samples(
                    f"url_{size_label}_{secret_label}",
                    measure(call_url, warmup=plan.warmup, repeat=plan.repeat),
                )
            )

    for size_label, size in sizes:
        for secret_label, count in secret_counts:
            params = synthetic_params(size, key_assignments=count)

            def call_params(_index: int, _params: dict[str, str] = params) -> None:
                scrub_params(_params)

            conditions.append(
                summarize_samples(
                    f"params_{size_label}_{secret_label}",
                    measure(call_params, warmup=plan.warmup, repeat=plan.repeat),
                )
            )

    for size_label, size in sizes:
        for secret_label, count in secret_counts:
            text = synthetic_text(size, echo_secrets=count)
            secrets = SYNTHETIC_SECRETS[:count]

            def call_text(
                _index: int,
                _text: str = text,
                _secrets: tuple[str, ...] = secrets,
            ) -> None:
                scrub_text(_text, _secrets)

            conditions.append(
                summarize_samples(
                    f"text_{size_label}_{secret_label}",
                    measure(call_text, warmup=plan.warmup, repeat=plan.repeat),
                )
            )

    notes = [
        "시크릿은 합성 문자열 3종이며 실인증키가 아니다.",
        "url·params 조건의 s3 은 입력에 든 인증키 대입 개수, text 조건의 s3 은 "
        "secrets 인자 길이를 뜻한다.",
        "스크러빙은 끌 수 없는 게이트이므로 이 값은 record 경로의 고정 비용이다.",
    ]
    return conditions, {}, notes


# ---------------------------------------------------------------------------
# B3 - 컴파일 시간 + 결정론
# ---------------------------------------------------------------------------
_B3_OPTIONS: CompileOptions = CompileOptions(generation_mode="offline")


def _compile_stages(loader: Callable[[], SourceSpec]) -> tuple[dict[str, int], str]:
    """``load → build → dumps`` 각 단계 소요 시간과 산출 문자열을 함께 얻는다."""
    start = time.perf_counter_ns()
    source = loader()
    loaded = time.perf_counter_ns()
    compiled = build_openapi(source, options=_B3_OPTIONS)
    built = time.perf_counter_ns()
    text = dumps(compiled.document)
    finished = time.perf_counter_ns()
    stages = {
        "load": loaded - start,
        "build": built - loaded,
        "dumps": finished - built,
        "total": finished - start,
    }
    return stages, text


def _synthetic_loader(source: SyntheticSource) -> Callable[[], SourceSpec]:
    """합성 소스를 :class:`SourceSpec` 으로 만드는 호출을 돌려준다."""

    def loader() -> SourceSpec:
        return load_source(
            source.document,
            service_id=source.service_id,
            service_name=source.service_name,
            source_url=f"{SYNTHETIC_HOST}/spec/{source.name}.json",
        )

    return loader


def _preset_loader(module: Any, directory: Path) -> Callable[[], SourceSpec]:
    """프리셋 번들을 :class:`SourceSpec` 으로 만드는 호출을 돌려준다."""

    def loader() -> SourceSpec:
        return module.load_preset(directory)

    return loader


def _measure_compile(
    label: str,
    loader: Callable[[], SourceSpec],
    plan: ItemPlan,
) -> tuple[list[dict[str, Any]], bool, int | None]:
    """소스 1건의 단계별 조건 4개와 결정론 판정을 만든다."""
    collected: dict[str, list[int]] = {"load": [], "build": [], "dumps": [], "total": []}

    def compile_once(_index: int) -> None:
        stages, _text = _compile_stages(loader)
        for stage, value in stages.items():
            collected[stage].append(value)

    for _ in range(max(0, plan.warmup)):
        _compile_stages(loader)
    for index in range(max(1, plan.repeat)):
        compile_once(index)

    conditions = [
        summarize_samples(f"{label}:{stage}", collected[stage])
        for stage in ("load", "build", "dumps", "total")
    ]
    texts = [_compile_stages(loader)[1] for _ in range(5)]
    deterministic, offset = determinism_check(texts)
    return conditions, deterministic, offset


def run_b3(config: RunConfig, plan: ItemPlan) -> tuple[list[dict[str, Any]], dict[str, Any], list[str]]:
    """B3 - 컴파일 전 구간·단계별 시간과 결정론(불리언)."""
    conditions: list[dict[str, Any]] = []
    determinism: dict[str, Any] = {}
    notes: list[str] = [
        "결정론은 시간이 아니라 불리언이다. 같은 입력으로 5회 컴파일해 dumps 결과 "
        "바이트가 전부 같아야 true 다.",
    ]

    for source in SYNTHETIC_SOURCES:
        item_conditions, ok, offset = _measure_compile(
            source.name, _synthetic_loader(source), plan
        )
        conditions.extend(item_conditions)
        determinism[source.name] = {"deterministic": ok, "first_mismatch_offset": offset}

    presets = discover_presets(config.presets_root)
    module = _curation_module()
    if not presets:
        conditions.append(
            skipped_condition("presets", "프리셋 번들을 찾지 못했습니다(합성 소스만 측정).")
        )
    elif module is None:
        conditions.append(
            skipped_condition(
                "presets",
                "mcportal.compiler.curation 모듈이 없어 프리셋을 로드할 수 없습니다.",
            )
        )
    else:
        for preset in presets:
            label = f"preset_{preset.preset_id}"
            try:
                item_conditions, ok, offset = _measure_compile(
                    label, _preset_loader(module, preset.directory), plan
                )
            except Exception as exc:  # 프리셋 1건 실패가 항목 전체를 죽이지 않는다.
                conditions.append(
                    skipped_condition(label, f"{type(exc).__name__}: {exc}")
                )
                continue
            conditions.extend(item_conditions)
            determinism[label] = {"deterministic": ok, "first_mismatch_offset": offset}

    all_deterministic = all(entry["deterministic"] for entry in determinism.values())
    first_offsets = [
        entry["first_mismatch_offset"]
        for entry in determinism.values()
        if entry["first_mismatch_offset"] is not None
    ]
    derived: dict[str, Any] = {
        "deterministic": all_deterministic,
        "first_mismatch_offset": min(first_offsets) if first_offsets else None,
        "determinism_runs": 5,
        "per_source": determinism,
    }
    if presets:
        notes.append(f"프리셋 번들 {len(presets)}건을 실스펙 조건으로 함께 측정했다.")
    else:
        notes.append("프리셋 번들이 없어 합성 조건만 측정했다(결과는 하한값으로 읽는다).")
    return conditions, derived, notes


# ---------------------------------------------------------------------------
# B4 - 쿼터가드 오버헤드
# ---------------------------------------------------------------------------
_B4_URL: str = f"{SYNTHETIC_HOST}/bench/guard/v1/items"


def run_b4(config: RunConfig, plan: ItemPlan) -> tuple[list[dict[str, Any]], dict[str, Any], list[str]]:
    """B4 - 가드 배선 트랜스포트 vs bare MockTransport 왕복 비교.

    원장 SQLite 쓰기를 **포함**한 수치다(그것이 실제 비용이다). 원장은 임시
    디렉터리에 만들고 종료 시 반드시 닫는다(Windows 파일 잠금).
    """
    owns_temp = config.work_dir is None
    base_dir = Path(tempfile.mkdtemp(prefix="mcportal-bench-")) if owns_temp else Path(config.work_dir)
    ledger_dir = base_dir / "b4"
    ledger_dir.mkdir(parents=True, exist_ok=True)

    conditions: list[dict[str, Any]] = []
    ledger: UsageLedger | None = None
    try:
        # bare - 대조군.
        bare_client = httpx.Client(transport=httpx.MockTransport(_bench_response_handler))
        try:

            def bare_roundtrip(_index: int, _client: httpx.Client = bare_client) -> None:
                _client.get(_B4_URL)

            bare_samples = measure(bare_roundtrip, warmup=plan.warmup, repeat=plan.repeat)
        finally:
            bare_client.close()

        # guarded - 가드 배선(원장 쓰기 포함, 캐시는 끈다).
        ledger = UsageLedger(ledger_dir / "ledger.db")
        budget_limit = (plan.warmup + plan.repeat) * 10
        guard = QuotaGuard(ledger, DailyBudget(budget_limit))
        transport = MCPortalTransport(
            SYNTHETIC_SECRETS[0],
            inner=httpx.MockTransport(_bench_response_handler),
            guard=guard,
            cache=None,
            profile=DATA_GO_KR,
            owns_guard=False,
        )
        guarded_client = httpx.Client(transport=transport)
        try:

            def guarded_roundtrip(_index: int, _client: httpx.Client = guarded_client) -> None:
                _client.get(_B4_URL)

            guarded_samples = measure(
                guarded_roundtrip, warmup=plan.warmup, repeat=plan.repeat
            )
        finally:
            guarded_client.close()
    finally:
        if ledger is not None:
            ledger.close()
        if owns_temp:
            shutil.rmtree(base_dir, ignore_errors=True)

    conditions.append(summarize_samples("guarded", guarded_samples))
    conditions.append(summarize_samples("bare", bare_samples))

    guarded_median = median_ns(guarded_samples)
    bare_median = median_ns(bare_samples)
    guarded_mean = float(statistics.fmean(guarded_samples))
    bare_mean = float(statistics.fmean(bare_samples))
    derived = {
        "overhead_median_ns": guarded_median - bare_median,
        "overhead_median_pct": (
            (guarded_median / bare_median - 1.0) * 100.0 if bare_median else None
        ),
        "overhead_mean_ns": guarded_mean - bare_mean,
        "overhead_mean_pct": (
            (guarded_mean / bare_mean - 1.0) * 100.0 if bare_mean else None
        ),
        "budget_limit": budget_limit,
    }
    notes = [
        "원장 SQLite 쓰기를 포함한 수치다(저널 모드 WAL).",
        "TTL 캐시는 끈 상태(cache=None)다 - 캐시 히트가 왕복을 건너뛰면 가드 비용이 "
        "아니라 캐시 효과를 재게 된다.",
        f"하드 예산 상한 {budget_limit}회 - (warmup + N) x 10 으로 잡아 측정 도중 "
        "소진되지 않게 했다.",
        "인증키는 합성 문자열이며 원장에는 지문만 저장된다.",
    ]
    return conditions, derived, notes


# ---------------------------------------------------------------------------
# B5 - FastMCP 도구 빌드 시간
# ---------------------------------------------------------------------------
def _mock_async_client(base_url: str) -> httpx.AsyncClient:
    """MockTransport 로만 배선한 async 클라이언트를 만든다(네트워크 없음)."""
    return httpx.AsyncClient(
        transport=httpx.MockTransport(_bench_response_handler), base_url=base_url
    )


def _server_base_url(document: Mapping[str, Any]) -> str:
    """OpenAPI 문서의 ``servers[0].url`` 을 읽는다(없으면 합성 도메인)."""
    servers = document.get("servers")
    if isinstance(servers, Sequence) and not isinstance(servers, (str, bytes)) and servers:
        first = servers[0]
        if isinstance(first, Mapping):
            url = first.get("url")
            if isinstance(url, str) and url:
                return url
    return SYNTHETIC_HOST


def _measure_server_build(
    label: str,
    document: Mapping[str, Any],
    plan: ItemPlan,
) -> dict[str, Any]:
    """OpenAPI 문서 1건에 대한 FastMCP 서버 생성 시간을 잰다."""
    from mcportal.mcp import server_from_spec

    client = _mock_async_client(_server_base_url(document))
    try:

        def build(_index: int) -> None:
            server_from_spec(document, client=client, name=f"bench-{label}")

        samples = measure(build, warmup=plan.warmup, repeat=plan.repeat)
    finally:
        asyncio.run(client.aclose())
    return summarize_samples(label, samples)


def run_b5(config: RunConfig, plan: ItemPlan) -> tuple[list[dict[str, Any]], dict[str, Any], list[str]]:
    """B5 - OpenAPI 문서에서 FastMCP 서버 객체를 만드는 시간.

    Raises:
        ItemSkipped: fastmcp 가 설치돼 있지 않을 때(하네스는 설치하지 않는다).
    """
    if not fastmcp_available():
        raise ItemSkipped("fastmcp 미설치")

    conditions: list[dict[str, Any]] = []
    synthetic = SYNTHETIC_SOURCES[0]
    source = _synthetic_loader(synthetic)()
    document = build_openapi(source, options=_B3_OPTIONS).document
    conditions.append(_measure_server_build("synthetic", document, plan))

    presets = discover_presets(config.presets_root)
    if not presets:
        conditions.append(
            skipped_condition("presets", "프리셋 번들을 찾지 못했습니다(합성 조건만 측정).")
        )
    else:
        for preset in presets:
            label = f"preset_{preset.preset_id}"
            spec_path = preset.directory / "openapi.json"
            if not spec_path.is_file():
                conditions.append(
                    skipped_condition(
                        label, "번들에 openapi.json 이 없습니다(mcportal compile 필요)."
                    )
                )
                continue
            try:
                preset_document = json.loads(spec_path.read_text(encoding="utf-8"))
                conditions.append(_measure_server_build(label, preset_document, plan))
            except Exception as exc:  # 프리셋 1건 실패가 항목 전체를 죽이지 않는다.
                conditions.append(skipped_condition(label, f"{type(exc).__name__}: {exc}"))

    notes = [
        "async 클라이언트는 MockTransport 로만 배선했다(네트워크 호출 0건).",
        "이 값은 설치된 fastmcp 버전에 강하게 의존한다 - environment.packages 와 "
        "함께 읽는다.",
    ]
    return conditions, {}, notes


# ---------------------------------------------------------------------------
# 항목 디스패치
# ---------------------------------------------------------------------------
ItemRunner = Callable[
    [RunConfig, ItemPlan], "tuple[list[dict[str, Any]], dict[str, Any], list[str]]"
]

#: 항목 ID → 러너. 테스트가 개별 항목을 대체해 실패 격리를 검증한다.
ITEM_RUNNERS: dict[str, ItemRunner] = {
    "B1": run_b1,
    "B2": run_b2,
    "B3": run_b3,
    "B4": run_b4,
    "B5": run_b5,
}


def run_item(item_id: str, config: RunConfig) -> dict[str, Any]:
    """항목 1개를 실행해 결과 딕셔너리를 만든다.

    한 항목이 실패해도 예외를 밖으로 던지지 않는다. 실패는 그 항목의
    ``status: "failed"`` 로 기록되고 나머지 항목은 계속 측정된다.

    Args:
        item_id: 항목 ID(``B1``~``B5``).
        config: 실행 설정.

    Returns:
        결과 파일 ``items[]`` 원소.
    """
    plan = plan_for(item_id, config)
    gc.collect()
    try:
        conditions, derived, notes = ITEM_RUNNERS[item_id](config, plan)
    except ItemSkipped as exc:
        return {
            "id": plan.item_id,
            "title": plan.title,
            "status": "skipped",
            "reason": _sanitize_text(str(exc)),
            "conditions": [],
            "derived": {},
            "notes": [],
        }
    except Exception as exc:
        return {
            "id": plan.item_id,
            "title": plan.title,
            "status": "failed",
            "reason": _sanitize_text(f"{type(exc).__name__}: {exc}"),
            "conditions": [],
            "derived": {},
            "notes": [],
        }
    return {
        "id": plan.item_id,
        "title": plan.title,
        "status": "ok",
        "conditions": conditions,
        "derived": derived,
        "notes": [_sanitize_text(note) for note in notes],
        "plan": {"warmup": plan.warmup, "repeat": plan.repeat},
    }


def run_benchmarks(config: RunConfig) -> dict[str, Any]:
    """설정에 따라 항목들을 실행하고 결과 문서를 만든다.

    Args:
        config: 실행 설정.

    Returns:
        결과 파일 스키마(PROTOCOL.md §9)를 따르는 딕셔너리.
    """
    overrides: dict[str, Any] = {}
    if config.repeat_override is not None:
        overrides["repeat"] = int(config.repeat_override)
    if tuple(config.only) != ITEM_IDS:
        overrides["only"] = list(config.only)

    items = [run_item(item_id, config) for item_id in config.only]
    return {
        "schema": SCHEMA_ID,
        "protocol": protocol_fingerprint(),
        "mode": config.mode,
        "label": config.label,
        "measured_on": kst_date(),
        "overrides": overrides,
        "environment": collect_environment(config.label),
        "items": items,
    }


# ---------------------------------------------------------------------------
# 저장
# ---------------------------------------------------------------------------
def serialize_result(result: Mapping[str, Any]) -> str:
    """결과를 결정론 JSON 문자열로 직렬화한다(끝 개행 1개 포함)."""
    return json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def write_result(result: Mapping[str, Any], path: PathLike) -> Path:
    """결과를 UTF-8 · LF 로 저장한다. 저장 직전 시크릿 게이트를 통과해야 한다.

    Args:
        result: 결과 문서.
        path: 저장 경로.

    Returns:
        저장된 경로.

    Raises:
        BenchmarkError: 결과 전문에 인증키 대입이 남아 있을 때(**파일을 만들지 않는다**).
    """
    text = serialize_result(result)
    found = find_key_assignments(text)
    if found:
        raise BenchmarkError(
            "벤치마크 결과에 인증키 대입이 남아 있어 저장을 중단했습니다"
            f"(탐지된 파라미터: {', '.join(found)}). 결과 파일은 커밋 대상이므로 "
            "시크릿이 섞이면 공개 저장소로 나갑니다."
        )
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8", newline="\n")
    return target


def default_out_path(label: str) -> Path:
    """기본 결과 파일 경로 ``benchmarks/results/bench_<YYYYMMDD>_<label>.json``."""
    return RESULTS_DIR / f"bench_{kst_filename_date()}_{label}.json"


# ---------------------------------------------------------------------------
# 명령행
# ---------------------------------------------------------------------------
def _label_arg(value: str) -> str:
    """``--label`` 값을 검증한다(파일명 안전 · 경로 탈출 차단).

    Raises:
        argparse.ArgumentTypeError: 허용 문자를 벗어났을 때.
    """
    if not LABEL_RE.match(value):
        raise argparse.ArgumentTypeError(
            f"라벨 '{value}' 은(는) 쓸 수 없습니다. ASCII 영숫자와 '-' '_' 만, "
            "1~32자로 지정하세요(파일명 안전 · 경로 탈출 방지)."
        )
    return value


def _only_arg(value: str) -> tuple[str, ...]:
    """``--only`` 값을 항목 ID 튜플로 파싱한다(고정 실행 순서를 유지한다).

    Raises:
        argparse.ArgumentTypeError: 알 수 없는 항목 ID가 섞였을 때.
    """
    requested = [part.strip().upper() for part in value.split(",") if part.strip()]
    if not requested:
        raise argparse.ArgumentTypeError("--only 에 항목 ID를 하나 이상 지정하세요.")
    unknown = [item for item in requested if item not in ITEM_IDS]
    if unknown:
        raise argparse.ArgumentTypeError(
            f"알 수 없는 항목 ID: {', '.join(unknown)}. "
            f"사용 가능한 ID: {', '.join(ITEM_IDS)}."
        )
    return tuple(item for item in ITEM_IDS if item in set(requested))


def _repeat_arg(value: str) -> int:
    """``--repeat`` 값을 1 이상의 정수로 검증한다."""
    try:
        number = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"--repeat 은 정수여야 합니다: {value!r}") from exc
    if number < 1:
        raise argparse.ArgumentTypeError("--repeat 은 1 이상이어야 합니다.")
    return number


def build_parser() -> argparse.ArgumentParser:
    """하네스 명령행 파서를 만든다(표준 라이브러리 argparse 전용)."""
    parser = argparse.ArgumentParser(
        prog="harness.py",
        description=(
            "MCPortal 무키 벤치마크 하네스 - benchmarks/PROTOCOL.md 에 선등록된 "
            "항목만 측정한다. 네트워크 호출 0건, 인증키 0건."
        ),
    )
    parser.add_argument("--out", default=None, help="결과 파일 경로(생략 시 results/ 기본 경로)")
    parser.add_argument("--label", type=_label_arg, default="dev", help="실행 라벨(기본 dev)")
    parser.add_argument("--only", type=_only_arg, default=None, help="항목 부분 실행(예: B1,B4)")
    parser.add_argument("--repeat", type=_repeat_arg, default=None, help="기본 N 덮어쓰기")
    parser.add_argument(
        "--quick",
        action="store_true",
        help="축소 실행(N=5, warmup=1). 프로토콜 준수 실행이 아니다",
    )
    parser.add_argument("--presets-root", default=None, help="프리셋 루트 경로")
    return parser


def _print_summary(result: Mapping[str, Any], out_path: Path) -> None:
    """사람용 요약을 stdout 에 출력한다(cp949 안전 - ASCII 구분선만 쓴다)."""
    print("MCPortal 벤치마크 하네스")
    print(f"  프로토콜  : {result['protocol']}")
    print(f"  모드      : {result['mode']}  라벨: {result['label']}  측정일(KST): {result['measured_on']}")
    print("  " + "-" * 62)
    for item in result["items"]:
        status = item["status"]
        line = f"  {item['id']}  {item['title']}"
        print(f"{line:<44}{status}")
        if status != "ok":
            print(f"        사유: {item.get('reason', '')}")
            continue
        for condition in item["conditions"]:
            if condition.get("status") != "ok":
                print(f"        - {condition['name']:<28}건너뜀: {condition.get('reason', '')}")
                continue
            print(
                f"        - {condition['name']:<28}"
                f"n={condition['n']:<5}"
                f"median={condition['median_ns'] / 1000:.1f}us  "
                f"p95={condition['p95_ns'] / 1000:.1f}us"
            )
    print("  " + "-" * 62)
    print(f"  결과 파일 : {out_path}")
    print("  주의: 이 수치는 MCPortal 자체 계층 비용의 계량이며, 경쟁 라이브러리와의")
    print("        비교가 아닙니다. 한계는 PROTOCOL.md 8절을 함께 읽으세요.")


def harden_streams() -> None:
    """표준 출력·오류 스트림이 인코딩 오류로 죽지 않게 정책을 낮춘다.

    출력 문면은 cp949 안전하게 작성하지만(ASCII 구분선·박스드로잉 없음), 예외
    메시지나 프리셋 메타데이터처럼 **밖에서 들어온 문자열**이 cp949 에 없는
    문자를 담을 수 있다. 그 한 글자 때문에 ``UnicodeEncodeError`` 로 측정
    전체가 죽는 것보다 ``\\uXXXX`` 로 흘려 보내고 나머지를 보여 주는 편이 낫다.
    인코딩 자체는 바꾸지 않는다(파이프 호환). :mod:`mcportal.cli` 와 같은 규약이다.
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            if (getattr(stream, "errors", None) or "strict") == "strict":
                reconfigure(errors="backslashreplace")
        except (AttributeError, OSError, ValueError):  # pragma: no cover - 방어
            continue


def main(argv: Sequence[str] | None = None) -> int:
    """하네스 진입점.

    Args:
        argv: 명령행 인자(``None`` 이면 ``sys.argv[1:]``).

    Returns:
        종료 코드(0 정상 / 1 실패 / 2 사용법 오류).
    """
    harden_streams()
    parser = build_parser()
    try:
        args = parser.parse_args(list(argv) if argv is not None else None)
    except SystemExit as exc:  # argparse 는 사용법 오류에 SystemExit 을 던진다.
        code = exc.code
        return int(code) if isinstance(code, int) else EXIT_USAGE

    presets_root = Path(args.presets_root) if args.presets_root else None
    if presets_root is not None and not presets_root.is_dir():
        # 왜: discover_presets 는 없는 경로를 빈 튜플로 흡수한다(프리셋 없는 환경을
        # 정상으로 보기 위한 설계). 그 관용이 --presets-root 오타까지 삼키면 합성
        # 조건만 잰 실행이 종료 코드 0 으로 남아 프리셋 조건을 잰 실행과 구분되지
        # 않는다. 명시된 경로는 명시된 대로 있어야 한다(2026-08-09 Advisor 검증).
        print(
            f"오류: --presets-root 경로가 디렉터리가 아닙니다: {presets_root}",
            file=sys.stderr,
        )
        return EXIT_USAGE

    config = RunConfig(
        mode="quick" if args.quick else "full",
        label=args.label,
        only=args.only if args.only is not None else ITEM_IDS,
        repeat_override=args.repeat,
        presets_root=presets_root,
    )

    try:
        result = run_benchmarks(config)
    except KeyboardInterrupt:  # pragma: no cover - 대화식 중단
        print("중단되었습니다(결과를 저장하지 않았습니다).", file=sys.stderr)
        return EXIT_ERROR

    out_path = Path(args.out) if args.out else default_out_path(config.label)
    try:
        saved = write_result(result, out_path)
    except BenchmarkError as exc:
        print(f"오류: {exc}", file=sys.stderr)
        return EXIT_ERROR
    except OSError as exc:
        print(f"오류: 결과 파일을 저장하지 못했습니다 - {exc}", file=sys.stderr)
        return EXIT_ERROR

    _print_summary(result, saved)
    failed = [item["id"] for item in result["items"] if item["status"] == "failed"]
    if failed:
        print(f"실패한 항목: {', '.join(failed)}", file=sys.stderr)
        return EXIT_ERROR
    return EXIT_OK


if __name__ == "__main__":  # pragma: no cover - 스크립트 진입점
    raise SystemExit(main())
