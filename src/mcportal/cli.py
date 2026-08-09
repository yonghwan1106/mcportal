# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Yong Park
"""MCPortal 명령행 인터페이스(표준 라이브러리 argparse 전용).

세 개의 서브커맨드를 제공한다.

* ``mcportal quota status`` - 오늘(KST) 사용량·예산·잔여를 표로 보여 준다.
  원장은 **읽기 전용**으로만 연다. CLI 는 원장을 만들지도 쓰지도 않는다.
* ``mcportal compile`` - 프리셋 번들을 재컴파일해 ``openapi.json`` 을 갱신한다.
  ``--check`` 는 파일을 쓰지 않고 커밋본과 바이트 비교만 하며, 드리프트가
  있으면 종료 코드 3 으로 CI 를 세운다.
* ``mcportal presets`` - 설치된 프리셋 번들 목록을 표로 보여 준다.

**의존성 정책**: 표준 라이브러리만 쓴다. 기획안이 적었던 rich 터미널 테이블은
「신규 런타임 의존성 0」 규칙을 지키기 위해 표준 라이브러리 텍스트 테이블로
대체했다(W3 설계 §1-1). 코어 의존성 httpx 단일 선언이 SBOM·비교표의 근거이므로
시각화 편의 하나로 그 선언을 깨지 않는다.

**Windows 콘솔 안전**: 박스드로잉·이모지·전각 기호를 출력하지 않는다. Windows
기본 콘솔 코드페이지(cp949)에서 인코딩이 불가능해 그대로 죽기 때문이다. 특히
em dash(U+2014)는 cp949 에 **없다**(cp949 의 0xA1A9 는 U+2015 horizontal bar 다).
구분선과 문장 부호는 ASCII ``-`` 만 쓰고, 마지막 안전판으로 :func:`harden_streams`
가 표준 스트림의 인코딩 오류 정책을 ``backslashreplace`` 로 낮춘다.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
import unicodedata
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import fields as dataclass_fields
from dataclasses import is_dataclass
from datetime import date
from pathlib import Path
from types import ModuleType
from typing import TYPE_CHECKING, Any, Union

if TYPE_CHECKING:  # pragma: no cover - 정적 분석 전용(런타임 임포트 아님)
    from mcportal.quota.budget import DailyBudget

#: 경로 인자 표기(W2 §13-6 규칙 계승 - 각 모듈이 자체 선언한다).
PathLike = Union[str, Path]

#: 프로그램 이름(`[project.scripts]` 의 콘솔 스크립트 이름과 같다).
PROGRAM: str = "mcportal"

#: exit code 규약(W3 설계 §9-5).
EXIT_OK: int = 0
EXIT_ERROR: int = 1
EXIT_USAGE: int = 2
EXIT_DRIFT: int = 3
EXIT_INTERRUPTED: int = 130

#: 스택트레이스 노출을 켜는 환경변수(설정돼 있을 때만 트레이스를 찍는다).
ENV_DEBUG: str = "MCPORTAL_DEBUG"

#: 일일 예산 상한을 주는 환경변수(런타임 :class:`~mcportal.quota.DailyBudget` 와 공유).
ENV_BUDGET: str = "CALL_BUDGET"

#: 프리셋 루트를 지정하는 환경변수(안내 문구 전용. 실제 탐색은 큐레이션 모듈이 한다).
ENV_PRESETS_ROOT: str = "MCPORTAL_PRESETS"

#: `--json` 출력의 스키마 식별자. 기계가독 계약이므로 함부로 바꾸지 않는다.
SCHEMA_QUOTA_STATUS: str = "mcportal.quota.status/1"
SCHEMA_COMPILE: str = "mcportal.compile/1"
SCHEMA_PRESETS: str = "mcportal.presets/1"

#: 예산 상한의 출처 표기(기계가독 값 -> 사람용 라벨).
BUDGET_SOURCE_ARGUMENT: str = "argument"
BUDGET_SOURCE_ENV: str = f"env:{ENV_BUDGET}"
BUDGET_SOURCE_PROFILE: str = "profile_default"

_BUDGET_SOURCE_LABELS: dict[str, str] = {
    BUDGET_SOURCE_ARGUMENT: "명령행 인자 --budget",
    BUDGET_SOURCE_ENV: f"환경변수 {ENV_BUDGET}",
    BUDGET_SOURCE_PROFILE: "프로파일 기본값",
}

#: 쿼터 집계의 성격을 밝히는 상시 고지(사람용 말미 / `--json` 의 notes 배열).
BEST_EFFORT_NOTE: str = (
    "이 집계는 MCPortal 경유 호출만 셉니다. data.go.kr는 잔여 쿼터 조회 API를 "
    "제공하지 않으므로 베스트에포트 추정이며, 신뢰의 축은 하드 예산 상한입니다."
)

#: 읽기 전용 열기에 실패해 immutable 로 재시도했을 때 덧붙이는 경고.
IMMUTABLE_NOTE: str = (
    "원장을 immutable 모드로 열었습니다. 아직 체크포인트되지 않은 WAL 잔여 "
    "트랜잭션은 이 집계에 보이지 않을 수 있습니다."
)

#: 원장 파일은 있는데 calls 테이블이 없을 때 덧붙이는 안내.
NO_TABLE_NOTE: str = (
    "원장 파일에 calls 테이블이 없습니다. MCPortal 원장이 아닌 다른 SQLite "
    "파일을 가리켰을 수 있으니 --ledger 경로를 확인하세요."
)

#: 응답 스키마 미확정 프리셋이 있을 때 표 아래에 붙이는 안내.
UNRESOLVED_NOTE: str = "응답 미확정 스키마는 v0.2 실키 샘플링에서 채워집니다."

#: 키 지문 인자의 형식(sha256 hex 앞 12자).
_KEY_FP_RE = re.compile(r"^[0-9a-fA-F]{12}$")

#: 컴파일 상태(기계가독 값 -> 사람용 라벨).
STATUS_WRITTEN: str = "written"
STATUS_UNCHANGED: str = "unchanged"
STATUS_DRIFT: str = "drift"

_STATUS_LABELS: dict[str, str] = {
    STATUS_WRITTEN: "갱신",
    STATUS_UNCHANGED: "변경 없음",
    STATUS_DRIFT: "드리프트",
}

#: 서브커맨드 식별자 -> 핸들러 함수 이름. 이름으로 늦게 조회하므로 테스트가
#: 모듈 속성을 monkeypatch 하면 그대로 반영된다(파서에 함수 객체를 박아 두면
#: 교체가 먹지 않는다).
HANDLERS: dict[str, str] = {
    "quota_status": "_cmd_quota_status",
    "compile": "_cmd_compile",
    "presets": "_cmd_presets",
}


class CliError(Exception):
    """CLI 가 사용자에게 한국어로 알리고 종료 코드로 되돌릴 오류.

    Attributes:
        code: 종료 코드(기본 :data:`EXIT_ERROR`).
        hints: 본문 아래에 한 줄씩 덧붙일 보조 안내.
    """

    def __init__(
        self,
        message: str,
        *,
        code: int = EXIT_ERROR,
        hints: Sequence[str] = (),
    ) -> None:
        super().__init__(message)
        self.code = int(code)
        self.hints: tuple[str, ...] = tuple(hints)


# ---------------------------------------------------------------------------
# 출력 폭 계산과 표 렌더링(표준 라이브러리만)
# ---------------------------------------------------------------------------
def display_width(text: str) -> int:
    """터미널에서 차지하는 칸 수를 센다(한글·한자 등 전각은 2칸).

    ``str.ljust`` 는 코드포인트 개수로 채우므로 한글이 섞인 열이 어긋난다.
    :func:`unicodedata.east_asian_width` 가 ``W``(Wide) · ``F``(Fullwidth) ·
    ``A``(Ambiguous)로 분류한 문자를 2칸으로 세어 그 어긋남을 없앤다.
    결합 문자(``Mn``)는 0칸이다.

    ``A``(Ambiguous)를 2칸으로 세는 이유: 이 CLI 의 표적 콘솔은 한국어 로캘의
    Windows cp949 이고, 거기서는 ``A`` 가 2셀로 그려진다. 실제로 프리셋 서비스명에
    U+00B7 MIDDLE DOT(``·``)이 들어 있어서 ``W``/``F`` 만 세면 그 행만 1칸
    밀렸다(적대 리뷰 F28. 설계 §9-4 의 폭 규칙을 이에 맞춰 개정한다).

    Args:
        text: 폭을 잴 문자열.

    Returns:
        표시 폭(칸 수).
    """
    width = 0
    for char in text:
        if unicodedata.combining(char):
            continue
        width += 2 if unicodedata.east_asian_width(char) in ("W", "F", "A") else 1
    return width


def _pad(text: str, width: int, *, align: str = "left") -> str:
    """``text`` 를 표시 폭 기준으로 ``width`` 칸에 맞춰 공백을 채운다."""
    filler = " " * max(width - display_width(text), 0)
    return filler + text if align == "right" else text + filler


def render_table(
    headers: Sequence[str],
    rows: Sequence[Sequence[str]],
    aligns: Sequence[str] | None = None,
    *,
    gap: str = "  ",
) -> list[str]:
    """헤더·구분선·본문으로 이루어진 ASCII 표를 만든다.

    구분선은 ASCII ``-`` 만 쓴다(박스드로잉 문자는 cp949 콘솔에서 죽는다).

    Args:
        headers: 열 제목.
        rows: 행별 셀 문자열. 열 개수는 ``headers`` 와 같아야 한다.
        aligns: 열별 정렬(``"left"`` 또는 ``"right"``). 생략하면 전부 왼쪽.
        gap: 열 사이 간격 문자열.

    Returns:
        줄 단위 문자열 리스트(헤더, 구분선, 본문 순).
    """
    columns = len(headers)
    alignments = list(aligns) if aligns is not None else ["left"] * columns
    widths = [display_width(header) for header in headers]
    for row in rows:
        for index, cell in enumerate(row):
            widths[index] = max(widths[index], display_width(cell))
    lines = [gap.join(_pad(h, widths[i]) for i, h in enumerate(headers)).rstrip()]
    lines.append(gap.join("-" * width for width in widths))
    for row in rows:
        lines.append(
            gap.join(
                _pad(cell, widths[index], align=alignments[index])
                for index, cell in enumerate(row)
            ).rstrip()
        )
    return lines


def separator_line(headers: Sequence[str], rows: Sequence[Sequence[str]], *, gap: str = "  ") -> str:
    """:func:`render_table` 과 같은 열 폭으로 구분선 한 줄을 만든다."""
    widths = [display_width(header) for header in headers]
    for row in rows:
        for index, cell in enumerate(row):
            widths[index] = max(widths[index], display_width(cell))
    return gap.join("-" * width for width in widths)


def harden_streams() -> None:
    """표준 출력·오류 스트림이 인코딩 오류로 죽지 않게 정책을 낮춘다.

    출력 문면 자체는 cp949 안전하게 작성하지만, 프리셋 메타데이터처럼 **외부에서
    들어온 문자열**이 cp949 에 없는 문자를 담을 수 있다. 그 한 글자 때문에
    ``UnicodeEncodeError`` 로 명령 전체가 죽는 것보다 ``\\uXXXX`` 로 흘려 보내고
    나머지를 보여 주는 편이 낫다. 인코딩 자체는 바꾸지 않는다(파이프 호환).
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


def _emit(lines: Iterable[str]) -> None:
    """사람용 출력을 stdout 에 줄 단위로 쓴다."""
    for line in lines:
        print(line)


def _emit_json(payload: Mapping[str, Any]) -> None:
    """기계가독 출력을 stdout 에 **단독으로** 쓴다(사람용 문구 혼입 금지).

    ``sort_keys=True`` 로 키 순서를 고정하고 ``ensure_ascii=False`` 로 한국어를
    보존한다. 끝 개행은 1개다.
    """
    sys.stdout.write(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    )


def _fail(message: str, *, hints: Sequence[str] = ()) -> None:
    """오류 메시지를 stderr 에 쓴다(정상 출력과 섞이지 않게 한다)."""
    print(message, file=sys.stderr)
    for hint in hints:
        print(f"  {hint}", file=sys.stderr)


def _number(value: int) -> str:
    """천 단위 구분 쉼표를 넣은 정수 표기."""
    return f"{value:,}"


# ---------------------------------------------------------------------------
# 인자 타입 검사기
# ---------------------------------------------------------------------------
def _day_arg(text: str) -> str:
    """``YYYY-MM-DD`` 형식의 KST 날짜 인자를 검증한다."""
    try:
        return date.fromisoformat(text.strip()).isoformat()
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            f"날짜 형식이 올바르지 않습니다(YYYY-MM-DD 여야 합니다): {text!r}"
        ) from exc


def _budget_arg(text: str) -> int:
    """1 이상의 정수인 일일 예산 상한 인자를 검증한다."""
    try:
        value = int(text.strip())
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            f"예산 상한은 정수여야 합니다: {text!r}"
        ) from exc
    if value < 1:
        raise argparse.ArgumentTypeError(
            f"예산 상한은 1 이상이어야 합니다: {value}"
        )
    return value


def _key_fp_arg(text: str) -> str:
    """키 지문 인자(sha256 hex 앞 12자)를 검증하고 소문자로 정규화한다."""
    candidate = text.strip()
    if not _KEY_FP_RE.match(candidate):
        raise argparse.ArgumentTypeError(
            f"키 지문은 16진수 12자여야 합니다(원장에 저장되는 형식): {text!r}"
        )
    return candidate.lower()


def _env_name_arg(text: str) -> str:
    """환경변수 이름 인자를 검증한다(값이 아니라 **이름**만 받는다).

    ``%`` 도 막는 이유: 포털 인증키의 URL 인코딩 표기(``...%2B...%3D%3D``)에는
    ``=``·공백·탭이 하나도 없어 이름으로 통과했고, 그러면 부재 오류 메시지가 키
    원문을 stderr 로 되울렸다(2026-08-09 Advisor 검증 V6).

    거부 메시지에 넘긴 값을 싣지 않는 이유: 그 값이 인증키라면 막으려던 노출을
    거부 메시지가 그대로 저지른다. 이 모듈은 "키 원문은 출력하지 않는다"를 지킨다.
    """
    candidate = text.strip()
    if not candidate or any(ch in candidate for ch in "= \t%"):
        raise argparse.ArgumentTypeError(
            "--key-env 에는 인증키 원문이 아니라 환경변수 '이름'을 넘겨야 합니다"
            "(넘긴 값이 인증키일 수 있어 출력하지 않습니다). "
            "예: --key-env DATA_GO_KR_SERVICE_KEY"
        )
    return candidate


# ---------------------------------------------------------------------------
# 파서
# ---------------------------------------------------------------------------
def _package_version() -> str:
    """설치된 mcportal 패키지 버전을 지연 조회한다."""
    import mcportal

    return str(getattr(mcportal, "__version__", "unknown"))


def build_parser() -> argparse.ArgumentParser:
    """서브커맨드 3종을 등록한 argparse 파서를 만든다.

    Returns:
        ``quota status`` · ``compile`` · ``presets`` 를 가진 최상위 파서.
    """
    parser = argparse.ArgumentParser(
        prog=PROGRAM,
        description=(
            "MCPortal 명령행 도구 - 쿼터 현황 조회, 프리셋 컴파일, 프리셋 목록."
        ),
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {_package_version()}",
        help="버전을 출력하고 종료한다.",
    )
    subparsers = parser.add_subparsers(dest="command", metavar="<명령>", required=True)

    quota = subparsers.add_parser(
        "quota",
        help="쿼터 관련 명령.",
        description="쿼터 원장을 읽기 전용으로 조회한다(원장을 만들거나 쓰지 않는다).",
    )
    quota_sub = quota.add_subparsers(
        dest="quota_command", metavar="<하위명령>", required=True
    )
    status = quota_sub.add_parser(
        "status",
        help="오늘(KST) 사용량·예산·잔여·상태를 표로 보여 준다.",
        description=(
            "오늘(KST) 사용량·예산·잔여·상태를 표로 보여 준다. 원장은 읽기 전용으로 "
            "열며 파일이 없어도 오류가 아니다."
        ),
    )
    status.add_argument(
        "--ledger",
        metavar="PATH",
        default=None,
        help="원장 경로. 생략하면 MCPORTAL_LEDGER 환경변수 또는 ~/.mcportal/ledger.db.",
    )
    status.add_argument(
        "--budget",
        metavar="N",
        type=_budget_arg,
        default=None,
        help=f"일일 예산 상한. 생략하면 {ENV_BUDGET} 환경변수, 그다음 프로파일 기본값.",
    )
    status.add_argument(
        "--day",
        metavar="YYYY-MM-DD",
        type=_day_arg,
        default=None,
        help="집계 대상 KST 날짜. 생략하면 오늘(KST).",
    )
    key_filter = status.add_mutually_exclusive_group()
    key_filter.add_argument(
        "--key-fp",
        metavar="FP",
        type=_key_fp_arg,
        default=None,
        help="특정 키 지문(16진수 12자)만 집계한다.",
    )
    key_filter.add_argument(
        "--key-env",
        metavar="VAR",
        type=_env_name_arg,
        default=None,
        help=(
            "환경변수 VAR 의 값을 인증키로 보고 지문만 로컬 계산해 필터한다. "
            "키 원문은 어디에도 출력하지 않는다."
        ),
    )
    status.add_argument("--json", action="store_true", help="기계가독 JSON 으로 출력한다.")
    status.set_defaults(handler="quota_status")

    compile_parser = subparsers.add_parser(
        "compile",
        help="프리셋 번들을 재컴파일해 openapi.json 을 갱신한다.",
        description=(
            "프리셋 번들을 재컴파일한다. 내용이 같으면 파일을 다시 쓰지 않는다. "
            "--check 는 파일을 쓰지 않고 바이트 비교만 하며 드리프트 시 종료 코드 3."
        ),
    )
    compile_parser.add_argument(
        "preset_id",
        nargs="*",
        metavar="PRESET_ID",
        help="컴파일할 프리셋 ID. 생략하면 전부.",
    )
    compile_parser.add_argument(
        "--presets-root", metavar="PATH", default=None, help="프리셋 루트 디렉터리."
    )
    compile_parser.add_argument(
        "--check",
        action="store_true",
        help="파일을 쓰지 않고 커밋본과 바이트 비교만 한다(CI 게이트용).",
    )
    compile_parser.add_argument("--json", action="store_true", help="기계가독 JSON 으로 출력한다.")
    compile_parser.set_defaults(handler="compile")

    presets_parser = subparsers.add_parser(
        "presets",
        help="설치된 프리셋 번들 목록을 보여 준다.",
        description="설치된 프리셋 번들 목록을 표로 보여 준다.",
    )
    presets_parser.add_argument(
        "--presets-root", metavar="PATH", default=None, help="프리셋 루트 디렉터리."
    )
    presets_parser.add_argument("--json", action="store_true", help="기계가독 JSON 으로 출력한다.")
    presets_parser.add_argument(
        "--verbose",
        action="store_true",
        help="각 프리셋 아래에 큐레이션 메모(service.notes)를 함께 보여 준다.",
    )
    presets_parser.set_defaults(handler="presets")

    return parser


# ---------------------------------------------------------------------------
# quota status
# ---------------------------------------------------------------------------
def _resolve_budget(argument: int | None) -> tuple["DailyBudget", str]:
    """예산 상한을 해석한다(인자 > 환경변수 > 프로파일 기본값).

    Args:
        argument: ``--budget`` 로 받은 값(없으면 None).

    Returns:
        ``(DailyBudget, 출처 식별자)``. 출처는 ``argument`` ·
        ``env:CALL_BUDGET`` · ``profile_default`` 중 하나다.

    Raises:
        CliError: 환경변수 값이 정수가 아니거나 1 미만일 때. ``--budget`` 은
            ``_budget_arg`` 가 1 이상을 강제하는데 환경변수 경로만 범위를 보지
            않아, ``CALL_BUDGET=-5`` 가 ``mcportal.quota.status/1`` 계약에
            ``limit: -5`` · 호출 0건인데 ``EXHAUSTED`` 로 실렸다(적대 리뷰 F10).
            같은 개념의 두 입력 경로가 같은 규칙을 갖도록 여기서 접는다.
    """
    from mcportal.profiles import DATA_GO_KR
    from mcportal.quota.budget import DailyBudget

    if argument is not None:
        return DailyBudget(argument), BUDGET_SOURCE_ARGUMENT
    raw = os.environ.get(ENV_BUDGET)
    if raw is not None and raw.strip() != "":
        try:
            budget = DailyBudget()
        except ValueError as exc:
            raise CliError(
                f"예산 상한을 해석하지 못했습니다: {exc}",
                hints=[f"{ENV_BUDGET} 를 정수로 고치거나 --budget 으로 직접 지정하세요."],
            ) from exc
        if int(budget.limit) < 1:
            raise CliError(
                f"예산 상한은 1 이상이어야 합니다: {ENV_BUDGET}={raw.strip()}",
                hints=[
                    f"{ENV_BUDGET} 를 1 이상의 정수로 고치거나 "
                    "--budget 으로 직접 지정하세요.",
                ],
            )
        return budget, BUDGET_SOURCE_ENV
    return DailyBudget(DATA_GO_KR.default_daily_budget), BUDGET_SOURCE_PROFILE


def _resolve_key_filter(key_fp_arg: str | None, key_env_arg: str | None) -> str | None:
    """집계에 적용할 키 지문 필터를 만든다.

    ``--key-env`` 는 환경변수 **이름**만 받고, 값은 :func:`~mcportal.runtime.keys.
    prepare_service_key` 로 정규화한 뒤 지문으로만 쓴다. 정규화를 거치는 이유는
    트랜스포트가 정규화한 키로 원장을 기록하기 때문이다 - 인코딩키를 그대로
    지문화하면 같은 키인데도 0건으로 보인다.

    Returns:
        키 지문 문자열 또는 필터 없음(None).

    Raises:
        CliError: 환경변수가 비어 있거나 키 형식이 잘못됐을 때. **키 원문은
            메시지에 절대 싣지 않는다.**
    """
    if key_env_arg is not None:
        from mcportal.quota.ledger import key_fp
        from mcportal.runtime.keys import prepare_service_key

        raw = os.environ.get(key_env_arg)
        if raw is None or not raw.strip():
            raise CliError(
                f"환경변수 {key_env_arg} 에 인증키가 없습니다.",
                hints=["키를 설정했는지 확인하세요. 키 원문은 출력하지 않습니다."],
            )
        try:
            prepared = prepare_service_key(raw)
        except ValueError as exc:  # pragma: no cover - 위에서 공백을 걸렀다
            raise CliError(f"환경변수 {key_env_arg} 의 인증키를 해석하지 못했습니다: {exc}") from exc
        return key_fp(prepared)
    return key_fp_arg


def _ledger_uri(path: Path, *, immutable: bool) -> str:
    """원장 파일을 읽기 전용으로 여는 SQLite URI 를 만든다.

    Windows 경로의 역슬래시·공백·한글을 :meth:`Path.as_uri` 가 퍼센트 인코딩해
    주므로 문자열 조립보다 안전하다.
    """
    query = "?mode=ro&immutable=1" if immutable else "?mode=ro"
    return path.resolve().as_uri() + query


def _connect_readonly(path: Path, *, immutable: bool) -> tuple[sqlite3.Connection, bool]:
    """원장을 읽기 전용으로 열고 ``calls`` 테이블 존재 여부까지 확인한다.

    SQLite 는 연결을 지연 평가하므로 ``connect`` 만으로는 손상된 파일을 걸러내지
    못한다. 그래서 여는 즉시 ``sqlite_master`` 를 한 번 읽어 파일 헤더까지
    실제로 만지게 한다.

    Returns:
        ``(커넥션, calls 테이블 존재 여부)``.

    Raises:
        sqlite3.Error: 열기 또는 스키마 조회에 실패했을 때(호출자가 재시도한다).
    """
    conn = sqlite3.connect(_ledger_uri(path, immutable=immutable), uri=True)
    try:
        row = conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'calls'"
        ).fetchone()
    except sqlite3.Error:
        conn.close()
        raise
    return conn, row is not None


def _read_ledger(
    path: Path, *, day: str, key_fp_filter: str | None
) -> tuple[tuple[tuple[str, int], ...], tuple[str, ...]]:
    """원장에서 해당 KST 날짜의 키 지문별 호출 건수를 읽는다.

    원장은 **읽기 전용**으로만 연다. :class:`~mcportal.quota.UsageLedger` 를
    생성하면 파일과 부모 디렉터리를 만들어 버리므로 CLI 는 절대 쓰지 않는다.
    커넥션은 ``try/finally`` 로 반드시 닫는다(Windows 파일 잠금).

    Args:
        path: 원장 파일 경로(존재가 확인된 상태여야 한다).
        day: 집계할 KST 날짜(``YYYY-MM-DD``).
        key_fp_filter: 특정 키 지문만 볼 때 그 지문.

    Returns:
        ``((키지문, 건수), ...)`` 와 사용자에게 덧붙일 안내 문구들.

    Raises:
        CliError: 원장을 읽을 수 없을 때(손상·권한·비-SQLite 파일).
    """
    notes: list[str] = []
    try:
        conn, has_table = _connect_readonly(path, immutable=False)
    except sqlite3.Error:
        try:
            conn, has_table = _connect_readonly(path, immutable=True)
        except sqlite3.Error as exc:
            raise CliError(
                f"원장을 읽지 못했습니다: {path}",
                hints=[
                    f"SQLite 오류: {exc}",
                    "MCPortal 원장이 맞는지, 읽기 권한이 있는지 확인하세요.",
                ],
            ) from exc
        notes.append(IMMUTABLE_NOTE)

    try:
        if not has_table:
            notes.append(NO_TABLE_NOTE)
            return (), tuple(notes)
        sql = "SELECT key_fp, COUNT(*) FROM calls WHERE day_kst = ?"
        params: list[Any] = [day]
        if key_fp_filter is not None:
            sql += " AND key_fp = ?"
            params.append(key_fp_filter)
        sql += " GROUP BY key_fp ORDER BY key_fp"
        try:
            rows = conn.execute(sql, params).fetchall()
        except sqlite3.Error as exc:
            raise CliError(
                f"원장 집계 질의에 실패했습니다: {path}",
                hints=[f"SQLite 오류: {exc}"],
            ) from exc
    finally:
        conn.close()

    return tuple((str(row[0]), int(row[1])) for row in rows), tuple(notes)


def _cmd_quota_status(args: argparse.Namespace) -> int:
    """``mcportal quota status`` 를 수행한다.

    원장이 없어도, 비어 있어도 정상(:data:`EXIT_OK`)이다. 빈 상태는 실패가 아니다.
    """
    from mcportal.quota.ledger import default_ledger_path, kst_day

    day = args.day if args.day is not None else kst_day()
    ledger_path = Path(args.ledger) if args.ledger else default_ledger_path()
    budget, budget_source = _resolve_budget(args.budget)
    key_filter = _resolve_key_filter(args.key_fp, args.key_env)

    notes: list[str] = [BEST_EFFORT_NOTE]
    exists = ledger_path.exists()
    rows: tuple[tuple[str, int], ...] = ()
    if exists:
        rows, extra_notes = _read_ledger(ledger_path, day=day, key_fp_filter=key_filter)
        notes.extend(extra_notes)

    limit = int(budget.limit)
    keys = [
        {
            "key_fp": fingerprint,
            "used": used,
            "remaining": max(limit - used, 0),
            "status": str(budget.status(used)),
        }
        for fingerprint, used in rows
    ]
    total_used = sum(used for _, used in rows)
    total = {
        "used": total_used,
        "remaining": max(limit - total_used, 0),
        "status": str(budget.status(total_used)),
    }

    if args.json:
        _emit_json(
            {
                "schema": SCHEMA_QUOTA_STATUS,
                "day_kst": day,
                "ledger_path": str(ledger_path),
                "ledger_exists": exists,
                "budget": {
                    "limit": limit,
                    "source": budget_source,
                    "soft_ratio": float(budget.soft_ratio),
                },
                "keys": keys,
                "total": total,
                "notes": notes,
            }
        )
        return EXIT_OK

    _emit(
        _render_quota_status(
            day=day,
            ledger_path=ledger_path,
            exists=exists,
            limit=limit,
            budget_source=budget_source,
            keys=keys,
            total=total,
            key_filter=key_filter,
            notes=notes,
        )
    )
    return EXIT_OK


def _render_quota_status(
    *,
    day: str,
    ledger_path: Path,
    exists: bool,
    limit: int,
    budget_source: str,
    keys: Sequence[Mapping[str, Any]],
    total: Mapping[str, Any],
    key_filter: str | None,
    notes: Sequence[str],
) -> list[str]:
    """쿼터 현황의 사람용 출력을 줄 목록으로 만든다(cp949 안전)."""
    budget_label = _BUDGET_SOURCE_LABELS.get(budget_source, budget_source)
    budget_line = f"예산: {_number(limit)} 회/일  (출처: {budget_label})"
    lines = [
        f"MCPortal 쿼터 현황 (KST {day})",
        f"원장: {ledger_path}" + ("" if exists else " (없음)"),
    ]

    if not keys:
        lines.append("")
        message = "호출 이력 없음 - 아직 MCPortal 경유 호출이 기록되지 않았습니다."
        if key_filter is not None:
            message = (
                f"호출 이력 없음 - 키 지문 {key_filter} 로 집계된 호출이 없습니다."
            )
        lines.append(message)
        lines.append(budget_line)
    else:
        lines.append(budget_line)
        lines.append("")
        headers = ["키 지문", "오늘 호출", "잔여", "상태"]
        aligns = ["left", "right", "right", "left"]
        rows = [
            [
                str(entry["key_fp"]),
                _number(int(entry["used"])),
                _number(int(entry["remaining"])),
                str(entry["status"]),
            ]
            for entry in keys
        ]
        total_row = [
            "합계",
            _number(int(total["used"])),
            _number(int(total["remaining"])),
            str(total["status"]),
        ]
        table_rows = [*rows, total_row]
        table = render_table(headers, table_rows, aligns)
        # 마지막 행(합계) 앞에 구분선을 한 줄 더 끼운다.
        body = table[:-1]
        body.append(separator_line(headers, table_rows))
        body.append(table[-1])
        lines.extend(body)

    for note in notes:
        lines.append("")
        lines.append(f"주의: {note}")
    return lines


# ---------------------------------------------------------------------------
# 프리셋 공통
# ---------------------------------------------------------------------------
def _import_curation() -> ModuleType:
    """큐레이션 모듈을 지연 임포트한다.

    ``--help`` 와 ``quota status`` 가 이 모듈의 존재 여부와 무관하게 동작하도록
    서브커맨드 안에서만 끌어온다.

    Raises:
        CliError: 모듈을 임포트할 수 없을 때(한국어 안내).
    """
    try:
        from mcportal.compiler import curation
    except ImportError as exc:
        raise CliError(
            "프리셋 기능에는 mcportal.compiler.curation 모듈이 필요한데 "
            "임포트하지 못했습니다.",
            hints=[f"임포트 오류: {exc}", "설치된 mcportal 버전을 확인하세요."],
        ) from exc
    return curation


def _root_search_hints(curation: ModuleType) -> tuple[str, ...]:
    """프리셋 루트를 찾지 못했을 때 보여 줄 탐색 후보 안내를 만든다."""
    env_name = str(getattr(curation, "ENV_PRESETS_ROOT", ENV_PRESETS_ROOT))
    env_value = os.environ.get(env_name)
    return (
        f"환경변수 {env_name}"
        + (f" (현재 값: {env_value})" if env_value else " (설정되지 않음)"),
        "설치된 mcportal 패키지 안의 presets 디렉터리",
        "리포지터리 루트의 presets 디렉터리",
        str(Path.cwd() / "presets"),
    )


def _explicit_root(explicit: PathLike | None) -> Path | None:
    """``--presets-root`` 로 명시된 경로를 검증한다.

    큐레이션 모듈을 임포트하기 **전에** 부른다 - 사용자가 넘긴 경로가 틀렸다면
    모듈 가용성과 무관하게 그 사실부터 정확히 알려야 한다.

    Returns:
        검증된 루트 경로. 명시되지 않았으면 None.

    Raises:
        CliError: 명시한 경로가 없거나 디렉터리가 아닐 때(명시 경로 부재는 실패다).
    """
    if explicit is None:
        return None
    root = Path(explicit)
    if not root.exists():
        raise CliError(
            f"지정한 프리셋 루트가 없습니다: {root}",
            hints=["--presets-root 경로를 확인하세요."],
        )
    if not root.is_dir():
        raise CliError(
            f"지정한 프리셋 루트가 디렉터리가 아닙니다: {root}",
            hints=["--presets-root 에는 프리셋 번들들을 담은 디렉터리를 넘기세요."],
        )
    return root


def _resolve_presets_root(
    curation: ModuleType, explicit: Path | None
) -> Path | None:
    """프리셋 루트를 정한다(명시 경로가 없으면 큐레이션 모듈의 탐색에 맡긴다)."""
    if explicit is not None:
        return explicit
    return curation.default_presets_root()


def _root_source(curation: ModuleType, explicit: Path | None, root: Path | None) -> str:
    """채택된 루트가 어느 후보에서 왔는지 기계가독 라벨로 돌려준다."""
    if explicit is not None:
        return "argument"
    if root is None:
        return "none"
    env_name = str(getattr(curation, "ENV_PRESETS_ROOT", ENV_PRESETS_ROOT))
    configured = os.environ.get(env_name)
    if configured:
        try:
            if Path(configured).resolve() == Path(root).resolve():
                return f"env:{env_name}"
        except OSError:  # pragma: no cover - 경로 해석 실패는 환경 문제다
            pass
    return "discovered"


def _warn_ignored_env_root(
    curation: ModuleType, explicit: Path | None, root: Path | None
) -> None:
    """``MCPORTAL_PRESETS`` 가 설정됐는데 채택되지 않았으면 stderr 로 알린다.

    명시 인자 ``--presets-root`` 는 부재·비디렉터리를 :data:`EXIT_ERROR` 로 막는데
    환경변수는 :func:`curation.default_presets_root` 가 조용히 건너뛴다. ``presets``
    는 읽기 전용이라 혼동에 그치지만 ``compile`` 은 **쓰기 명령**이라 사용자가
    의도하지 않은 트리의 ``openapi.json`` 을 재생성한다(적대 리뷰 F13).

    ``--presets-root`` 가 주어졌으면 아무 말도 하지 않는다. 명시 인자가 환경변수를
    이기는 것은 설계된 우선순위이지 사고가 아니며, 경고를 내면 **같은 유효 경로를
    양쪽에 준 경우에도** "번들이 없어 무시했다"는 거짓 진단이 나온다
    (2026-08-09 Advisor 검증 V7).

    Args:
        curation: 큐레이션 모듈(환경변수 이름의 정본).
        explicit: ``--presets-root`` 로 명시된 경로(있으면 환경변수는 애초에 무관).
        root: 실제로 채택된 루트.
    """
    if explicit is not None:
        return
    env_name = str(getattr(curation, "ENV_PRESETS_ROOT", ENV_PRESETS_ROOT))
    configured = os.environ.get(env_name)
    if not configured:
        return
    if _root_source(curation, explicit, root) == f"env:{env_name}":
        return
    where = str(root) if root is not None else "(루트 없음)"
    # 사유를 단정하지 않는다. 탐색이 건너뛴 이유는 경로 부재·비디렉터리·번들 0건
    # 어느 쪽이든 될 수 있고, CLI 는 그 셋을 구분해 알지 못한다.
    print(
        f"경고: 환경변수 {env_name} 의 경로를 프리셋 루트로 채택하지 않고 "
        f"{where} 를 씁니다(경로: {configured}). "
        "그 경로가 있는 디렉터리이고 그 아래에 번들이 있는지 확인하세요.",
        file=sys.stderr,
    )


def _load_preset_infos(curation: ModuleType, root: Path | None) -> tuple[Any, ...]:
    """프리셋 번들 요약 목록을 읽는다(오류는 한국어로 접는다)."""
    try:
        return tuple(curation.iter_presets(root))
    except (ValueError, OSError) as exc:
        raise CliError(
            "프리셋 번들을 읽는 중 오류가 발생했습니다.",
            hints=[f"{type(exc).__name__}: {exc}"],
        ) from exc


def _select_presets(
    infos: Sequence[Any],
    wanted: Sequence[str],
    *,
    search_hints: Sequence[str] = (),
) -> tuple[Any, ...]:
    """요청된 프리셋 ID 만 골라낸다(입력 순서 보존).

    Args:
        infos: 루트에서 읽은 프리셋 요약들(빈 튜플일 수 있다).
        wanted: 사용자가 지정한 ID 들(빈 시퀀스면 전체 선택).
        search_hints: 루트를 찾지 못했을 때 함께 보여 줄 탐색 경로 안내.

    Raises:
        CliError: 존재하지 않는 ID 가 있을 때. 사용 가능한 ID 목록을 함께 보여 준다.
            **번들이 하나도 없는 루트에서도 마찬가지다** - 지정한 ID 를 조용히
            무시하고 성공으로 끝내면 CI 의 드리프트 게이트가 아무것도 검사하지
            않고 초록을 낸다(적대 리뷰 F5).
    """
    if not wanted:
        return tuple(infos)
    index = {str(info.preset_id): info for info in infos}
    missing = [preset_id for preset_id in wanted if preset_id not in index]
    if missing:
        available = ", ".join(sorted(index)) or "(없음)"
        hints = [f"사용 가능한 ID: {available}"]
        if not index:
            hints.append(
                "이 루트에는 프리셋 번들이 하나도 없습니다. "
                "--presets-root 로 번들이 있는 경로를 지정하거나 "
                "MCPORTAL_PRESETS 를 설정하세요."
            )
            hints.extend(f"탐색 경로: {hint}" for hint in search_hints)
        raise CliError(
            f"프리셋 ID 를 찾지 못했습니다: {', '.join(missing)}",
            hints=hints,
        )
    picked: list[Any] = []
    seen: set[str] = set()
    for preset_id in wanted:
        if preset_id in seen:
            continue
        seen.add(preset_id)
        picked.append(index[preset_id])
    return tuple(picked)


def _info_to_json(info: Any) -> dict[str, Any]:
    """:class:`PresetInfo` 를 JSON 안전한 딕셔너리로 바꾼다(경로는 문자열).

    필드 목록을 :func:`dataclasses.fields` 로 읽으므로 큐레이션 모듈이 필드를
    늘려도 CLI 를 고칠 필요가 없다.
    """
    payload: dict[str, Any] = {}
    if not is_dataclass(info):  # pragma: no cover - 계약 위반 방어
        return {"repr": repr(info)}
    for field in dataclass_fields(info):
        value = getattr(info, field.name)
        payload[field.name] = _json_safe(value)
    return payload


def _json_safe(value: Any) -> Any:
    """경로·튜플·집합을 JSON 으로 옮길 수 있는 형태로 바꾼다(배열은 실제 배열)."""
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_json_safe(item) for item in value]
    if isinstance(value, Mapping):
        return {str(key): _json_safe(item) for key, item in value.items()}
    return value


def _no_presets_lines(root: Path | None, hints: Sequence[str]) -> list[str]:
    """프리셋을 찾지 못했을 때의 사람용 출력(빈 상태는 실패가 아니다)."""
    lines = ["프리셋을 찾지 못했습니다."]
    if root is not None:
        lines.append(f"루트: {root} (프리셋 번들 없음)")
    else:
        lines.append("탐색한 경로:")
        lines.extend(f"  - {hint}" for hint in hints)
    return lines


# ---------------------------------------------------------------------------
# compile
# ---------------------------------------------------------------------------
def _compile_one(curation: ModuleType, info: Any, *, check: bool) -> dict[str, Any]:
    """프리셋 1건을 컴파일하고 커밋본과 바이트 비교한다.

    같으면 파일을 다시 쓰지 않는다(mtime 흔들림 방지). ``check`` 면 어떤 경우에도
    쓰지 않는다.

    Returns:
        ``preset_id`` · ``status`` · ``operation_count`` · ``unresolved_count`` ·
        ``openapi_path`` 를 담은 딕셔너리.

    Raises:
        CliError: 컴파일·기록에 실패했을 때.
    """
    from mcportal.compiler.openapi import dumps, write_spec

    target = Path(info.openapi_path)
    try:
        compiled = curation.compile_preset(info.directory)
        payload = dumps(compiled.document).encode("utf-8")
    except (ValueError, OSError) as exc:
        raise CliError(
            f"프리셋 {info.preset_id} 컴파일에 실패했습니다.",
            hints=[f"{type(exc).__name__}: {exc}"],
        ) from exc

    current = target.read_bytes() if target.is_file() else None
    if current == payload:
        status = STATUS_UNCHANGED
    elif check:
        status = STATUS_DRIFT
    else:
        try:
            write_spec(compiled.document, target)
        except (ValueError, OSError) as exc:
            raise CliError(
                f"프리셋 {info.preset_id} 의 openapi.json 기록에 실패했습니다.",
                hints=[f"{type(exc).__name__}: {exc}"],
            ) from exc
        status = STATUS_WRITTEN

    return {
        "preset_id": str(info.preset_id),
        "status": status,
        "operation_count": int(info.operation_count),
        "unresolved_count": int(info.unresolved_count),
        "openapi_path": str(target),
    }


def _cmd_compile(args: argparse.Namespace) -> int:
    """``mcportal compile`` 을 수행한다."""
    explicit = _explicit_root(args.presets_root)
    curation = _import_curation()
    root = _resolve_presets_root(curation, explicit)
    hints = _root_search_hints(curation)
    _warn_ignored_env_root(curation, explicit, root)
    root_source = _root_source(curation, explicit, root)
    infos = _load_preset_infos(curation, root) if root is not None else ()
    if not infos:
        # 지정한 ID 는 번들이 0건이어도 검증한다. 먼저 접지 않으면 CI 의
        # `mcportal compile --check <ID>` 가 아무것도 검사하지 않고 exit 0 을
        # 낸다(적대 리뷰 F5: 게이트의 침묵 통과).
        _select_presets(
            infos,
            args.preset_id,
            search_hints=() if explicit is not None else hints,
        )
        if args.json:
            _emit_json(
                {
                    "schema": SCHEMA_COMPILE,
                    "root": str(root) if root is not None else None,
                    "root_source": root_source,
                    "presets": [],
                    "written": 0,
                    "unchanged": 0,
                    "drift": 0,
                }
            )
        else:
            _emit(_no_presets_lines(root, hints))
        return EXIT_OK

    selected = _select_presets(infos, args.preset_id)
    results = [_compile_one(curation, info, check=args.check) for info in selected]
    counts = {
        STATUS_WRITTEN: sum(1 for r in results if r["status"] == STATUS_WRITTEN),
        STATUS_UNCHANGED: sum(1 for r in results if r["status"] == STATUS_UNCHANGED),
        STATUS_DRIFT: sum(1 for r in results if r["status"] == STATUS_DRIFT),
    }

    if args.json:
        _emit_json(
            {
                "schema": SCHEMA_COMPILE,
                "root": str(root),
                "root_source": root_source,
                "presets": results,
                "written": counts[STATUS_WRITTEN],
                "unchanged": counts[STATUS_UNCHANGED],
                "drift": counts[STATUS_DRIFT],
            }
        )
    else:
        _emit(_render_compile(root, selected, results, check=args.check, counts=counts))

    return EXIT_DRIFT if counts[STATUS_DRIFT] else EXIT_OK


def _render_compile(
    root: Path,
    infos: Sequence[Any],
    results: Sequence[Mapping[str, Any]],
    *,
    check: bool,
    counts: Mapping[str, int],
) -> list[str]:
    """컴파일 진행·요약의 사람용 출력을 만든다."""
    heading = "프리셋 컴파일 점검" if check else "프리셋 컴파일"
    lines = [f"{heading} (루트: {root})"]
    total = len(results)
    names = [str(info.service_name) for info in infos]
    labels = [_STATUS_LABELS.get(str(r["status"]), str(r["status"])) for r in results]
    id_width = max((display_width(str(r["preset_id"])) for r in results), default=0)
    name_width = max((display_width(name) for name in names), default=0)
    label_width = max((display_width(label) for label in labels), default=0)
    for index, result in enumerate(results, start=1):
        counter = f"[{index}/{total}]"
        lines.append(
            "  "
            + counter
            + " "
            + _pad(str(result["preset_id"]), id_width)
            + "  "
            + _pad(names[index - 1], name_width)
            + "  "
            + _pad(labels[index - 1], label_width)
            + "  "
            + f"({result['operation_count']} 오퍼레이션 / 응답 미확정 "
            + f"{result['unresolved_count']})"
        )
    parts: list[str] = []
    if check:
        if counts[STATUS_DRIFT]:
            parts.append(f"{counts[STATUS_DRIFT]}건 드리프트")
        parts.append(f"{counts[STATUS_UNCHANGED]}건 일치")
        lines.append(f"점검: {total}건 중 " + ", ".join(parts))
        if counts[STATUS_DRIFT]:
            lines.append(
                "드리프트가 있습니다. `mcportal compile` 로 재생성한 뒤 커밋하세요."
            )
    else:
        if counts[STATUS_WRITTEN]:
            parts.append(f"{counts[STATUS_WRITTEN]}건 갱신")
        parts.append(f"{counts[STATUS_UNCHANGED]}건 변경 없음")
        lines.append(f"완료: {total}건 중 " + ", ".join(parts))
    return lines


# ---------------------------------------------------------------------------
# presets
# ---------------------------------------------------------------------------
def _cmd_presets(args: argparse.Namespace) -> int:
    """``mcportal presets`` 를 수행한다."""
    explicit = _explicit_root(args.presets_root)
    curation = _import_curation()
    root = _resolve_presets_root(curation, explicit)
    hints = _root_search_hints(curation)
    _warn_ignored_env_root(curation, explicit, root)
    infos = _load_preset_infos(curation, root) if root is not None else ()

    if args.json:
        _emit_json(
            {
                "schema": SCHEMA_PRESETS,
                "root": str(root) if root is not None else None,
                "root_source": _root_source(curation, explicit, root),
                "presets": [_info_to_json(info) for info in infos],
            }
        )
        return EXIT_OK

    if not infos:
        _emit(_no_presets_lines(root, hints))
        return EXIT_OK

    _emit(_render_presets(root, infos, verbose=args.verbose))
    return EXIT_OK


def _render_presets(root: Path, infos: Sequence[Any], *, verbose: bool) -> list[str]:
    """프리셋 목록의 사람용 출력을 만든다.

    머리글의 "N종(M데이터셋)" 은 큐레이션이 붙인 ``group`` 라벨의 **가짓수**와
    번들 개수에서 계산한다. 도메인 상수를 코드에 박지 않기 위해서다 - 프리셋을
    늘리면 숫자가 저절로 따라온다.
    """
    groups = {str(info.group) if info.group else str(info.preset_id) for info in infos}
    lines = [f"프리셋 {len(groups)}종({len(infos)}데이터셋) - 루트: {root}", ""]
    headers = ["ID", "서비스명", "종류", "오퍼레이션", "응답미확정", "이용허락범위"]
    aligns = ["left", "left", "left", "right", "right", "left"]
    rows = [
        [
            str(info.preset_id),
            str(info.service_name),
            str(info.source_kind),
            str(int(info.operation_count)),
            str(int(info.unresolved_count)),
            str(info.license_note) if info.license_note else "-",
        ]
        for info in infos
    ]
    if verbose:
        # 메모를 각 프리셋 아래에 끼워야 하므로 표를 직접 조립한다.
        table = render_table(headers, rows, aligns)
        lines.extend(table[:2])
        for info, row in zip(infos, table[2:]):
            lines.append(row)
            for note in getattr(info, "notes", ()) or ():
                lines.append(f"      - {note}")
    else:
        lines.extend(render_table(headers, rows, aligns))

    if any(int(info.unresolved_count) > 0 for info in infos):
        lines.append("")
        lines.append(UNRESOLVED_NOTE)
    return lines


# ---------------------------------------------------------------------------
# 진입점
# ---------------------------------------------------------------------------
def main(argv: Sequence[str] | None = None) -> int:
    """CLI 진입점. **예외를 밖으로 던지지 않고** 종료 코드로 되돌린다.

    Args:
        argv: 인자 목록(생략하면 ``sys.argv[1:]``).

    Returns:
        :data:`EXIT_OK` · :data:`EXIT_ERROR` · :data:`EXIT_USAGE` ·
        :data:`EXIT_DRIFT` · :data:`EXIT_INTERRUPTED` 중 하나.
    """
    harden_streams()
    parser = build_parser()
    try:
        args = parser.parse_args(list(argv) if argv is not None else None)
    except SystemExit as exc:  # --help/--version(0) 과 사용법 오류(2)를 코드로 받는다.
        code = exc.code
        if code is None:
            return EXIT_OK
        if isinstance(code, int):
            return code
        _fail(str(code))
        return EXIT_USAGE

    handler_name = HANDLERS.get(str(getattr(args, "handler", "")))
    if handler_name is None:  # pragma: no cover - required=True 가 먼저 막는다
        parser.print_usage(sys.stderr)
        return EXIT_USAGE
    handler = getattr(sys.modules[__name__], handler_name)

    try:
        return int(handler(args))
    except KeyboardInterrupt:
        _fail("사용자 중단(Ctrl+C)으로 종료합니다.")
        return EXIT_INTERRUPTED
    except CliError as exc:
        _fail(str(exc), hints=exc.hints)
        return exc.code
    except SystemExit as exc:  # pragma: no cover - 핸들러는 종료 코드를 반환한다
        code = exc.code
        return code if isinstance(code, int) else EXIT_ERROR
    except Exception as exc:  # noqa: BLE001 - 진입점은 무엇도 밖으로 흘리지 않는다
        if os.environ.get(ENV_DEBUG):
            import traceback

            traceback.print_exc()
        _fail(
            f"예상하지 못한 오류로 중단했습니다: {type(exc).__name__}: {exc}",
            hints=[f"자세한 추적이 필요하면 {ENV_DEBUG}=1 로 다시 실행하세요."],
        )
        return EXIT_ERROR


if __name__ == "__main__":  # pragma: no cover - 모듈 실행 경로
    raise SystemExit(main())
