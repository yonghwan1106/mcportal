# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Yong Park
"""무키(no-key) 컴파일 데모 — 합성 픽스처만으로 OpenAPI 3.1 산출물을 만든다.

이 스크립트는 **네트워크 호출 0건 · 인증키 0건**으로 MCPortal 컴파일러 전 구간을
통과시킨다. 실기관명·실 서비스ID·실 URL·실키를 쓰지 않으며, 도메인은 RFC 2606
예약 TLD 인 ``.invalid`` 를 써서 실수로도 실호출이 나가지 않게 한다.

흐름::

    합성 Swagger(OAS 3.x) → load_source → SourceSpec
                                 ↓
    합성 응답 샘플 3건 → infer_schema_with_report → JSON Schema + 리포트
                                 ↓
    build_openapi → write_spec("specs/demo/openapi.json")
                  + write_samples("specs/demo/samples/")

실행::

    python examples\\compile_demo.py

같은 입력이면 같은 바이트가 나와야 한다. 재실행 결과가 달라지면 결정론 회귀이며
버그다(스크립트가 그 사실을 스스로 검사해 알려 준다).
"""

from __future__ import annotations

import hashlib
import sys
from pathlib import Path
from typing import Any

from mcportal.compiler import (
    CompileOptions,
    build_openapi,
    infer_schema_with_report,
    load_source,
    write_spec,
)
from mcportal.compiler.sampler import SampleResult, write_samples

#: 산출물이 저장되는 디렉터리(리포 루트 기준 ``specs/demo``).
DEMO_DIR = Path(__file__).resolve().parents[1] / "specs" / "demo"

#: 합성 스펙 소스가 흉내 내는 가상 서비스 이름.
SERVICE_NAME = "가상행정연구원 개방자료 서비스"

#: 가상 데이터셋 ID(포털에 존재하지 않는 번호대).
SERVICE_ID = "99900001"

#: 합성 취득 시각. 산출물에는 절대 실리지 않아야 한다(결정론 요건).
FETCHED_AT = "2026-08-05T09:00:00+09:00"

#: 합성 Swagger 문서(OpenAPI 3.x 형). ``serviceKey`` 파라미터를 일부러 넣어 두었다 —
#: 컴파일 결과에서 사라지는 것이 인증키 비노출 원칙(IR 불변식 I3)의 증거다.
DEMO_SWAGGER: dict[str, Any] = {
    "openapi": "3.0.1",
    "info": {"title": SERVICE_NAME, "version": "1.0"},
    "servers": [{"url": "https://apis.example.invalid/9990000/demo"}],
    "paths": {
        "/getDemoList": {
            "get": {
                "operationId": "getDemoList",
                "summary": "가상 자료 목록 조회",
                "description": "가상행정연구원이 공개한다고 가정한 합성 목록 자료다.",
                "parameters": [
                    {
                        "name": "serviceKey",
                        "in": "query",
                        "required": True,
                        "description": "인증키(트랜스포트가 주입하므로 도구 인자가 아니다)",
                        "schema": {"type": "string"},
                    },
                    {
                        "name": "pageNo",
                        "in": "query",
                        "required": True,
                        "description": "페이지 번호",
                        "example": "1",
                        "schema": {"type": "integer"},
                    },
                    {
                        "name": "numOfRows",
                        "in": "query",
                        "required": True,
                        "description": "한 페이지 결과 수",
                        "example": "10",
                        "schema": {"type": "integer"},
                    },
                    {
                        "name": "sido",
                        "in": "query",
                        "required": False,
                        "description": "시도 필터(가상 값)",
                        "schema": {"type": "string", "enum": ["가상시", "무명군"]},
                    },
                ],
                "responses": {
                    "200": {"description": "정상", "content": {"application/json": {}}}
                },
            }
        },
        "/getDemoItem": {
            "get": {
                "operationId": "getDemoItem",
                "summary": "가상 자료 상세 조회",
                "parameters": [
                    {
                        "name": "serviceKey",
                        "in": "query",
                        "required": True,
                        "schema": {"type": "string"},
                    },
                    {
                        "name": "itemId",
                        "in": "query",
                        "required": True,
                        "description": "자료 식별자",
                        "example": "A-001",
                        "schema": {"type": "string"},
                    },
                ],
                "responses": {
                    "200": {"description": "정상", "content": {"application/json": {}}}
                },
            }
        },
    },
}

#: 합성 응답 샘플 3건(표준형 봉투). 실제 호출로 얻은 것이 아니라 이 파일의 리터럴이다.
#:
#: - 1건은 ``item`` 이 1개, 2건은 여러 개 → 배열 병합 규칙을 밟는다.
#: - ``note`` 는 3건 중 2건에만 있어 optional 판정(required 제외)을 밟는다.
#: - ``updatedAt`` 은 전부 ``YYYY-MM-DD`` 라 ``format: date`` 판정을 밟는다.
DEMO_SAMPLES: list[dict[str, Any]] = [
    {
        "response": {
            "header": {"resultCode": "00", "resultMsg": "정상"},
            "body": {
                "pageNo": 1,
                "numOfRows": 10,
                "totalCount": 3,
                "items": {
                    "item": [
                        {
                            "itemId": "A-001",
                            "title": "가상 자료 하나",
                            "updatedAt": "2026-01-02",
                            "note": "첫 번째 합성 항목",
                        }
                    ]
                },
            },
        }
    },
    {
        "response": {
            "header": {"resultCode": "00", "resultMsg": "정상"},
            "body": {
                "pageNo": 2,
                "numOfRows": 10,
                "totalCount": 3,
                "items": {
                    "item": [
                        {
                            "itemId": "A-002",
                            "title": "가상 자료 둘",
                            "updatedAt": "2026-01-03",
                        },
                        {
                            "itemId": "A-003",
                            "title": "가상 자료 셋",
                            "updatedAt": "2026-01-04",
                            "note": "세 번째 합성 항목",
                        },
                    ]
                },
            },
        }
    },
    {
        "response": {
            "header": {"resultCode": "00", "resultMsg": "정상"},
            "body": {
                "pageNo": 3,
                "numOfRows": 10,
                "totalCount": 3,
                "items": {"item": []},
            },
        }
    },
]


def _digest(path: Path) -> str:
    """파일 바이트의 sha256 지문을 ``sha256:<64hex>`` 로 돌려준다."""
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def _make_stdout_safe() -> None:
    """Windows 콘솔(cp949)에서 출력이 인코딩 오류로 죽지 않게 한다.

    한국어는 cp949 에서도 그대로 나오지만, cp949 에 없는 기호(예: em dash)가 하나만
    섞여도 ``UnicodeEncodeError`` 로 스크립트가 죽는다. 콘솔 인코딩은 그대로 두고
    치환 정책만 느슨하게 바꿔, 한국어 가독성을 잃지 않으면서 죽지도 않게 한다.
    """
    reconfigure = getattr(sys.stdout, "reconfigure", None)
    if reconfigure is None:  # pragma: no cover - 표준 스트림이 아닌 경우 방어
        return
    try:
        reconfigure(errors="backslashreplace")
    except (ValueError, OSError):  # pragma: no cover - 재설정 불가 스트림 방어
        pass


def main() -> int:
    """합성 픽스처로 데모 산출물을 만들고 결과를 한국어로 보고한다.

    Returns:
        프로세스 종료 코드(0=성공, 1=결정론 회귀 감지).
    """
    _make_stdout_safe()

    # ① 합성 Swagger → 중간표현.
    source = load_source(
        DEMO_SWAGGER,
        service_id=SERVICE_ID,
        service_name=SERVICE_NAME,
        source_url="https://example.invalid/demo/openapi.json",
        fetched_at=FETCHED_AT,
    )

    # ② 합성 샘플 → 응답 스키마 추론(오프라인·결정론). getDemoItem 은 일부러
    #    샘플을 주지 않아 폴백 스키마와 unresolved 카운트를 보여 준다.
    schema, report = infer_schema_with_report(DEMO_SAMPLES)
    response_schemas = {"getDemoList": schema}
    reports = {"getDemoList": report}

    # ③ OpenAPI 3.1 산출.
    compiled = build_openapi(
        source,
        response_schemas,
        options=CompileOptions(generation_mode="sampled"),
        reports=reports,
    )

    spec_path = DEMO_DIR / "openapi.json"
    previous = spec_path.read_bytes() if spec_path.exists() else None
    write_spec(compiled.document, spec_path)
    current = spec_path.read_bytes()

    # ④ 샘플 저장(시크릿 치환 게이트를 그대로 통과시킨다. 합성 샘플이라 치환 대상은 없다).
    results = {
        "getDemoList": tuple(
            SampleResult(
                operation_id="getDemoList",
                status_code=200,
                ok=True,
                result_code="00",
                source_format="json",
                payload=payload,
            )
            for payload in DEMO_SAMPLES
        )
    }
    # secrets 는 키워드 필수 인자다. 이 데모는 100% 합성 픽스처라 지울 시크릿이
    # 없지만, "없음"을 호출자가 명시적으로 선언하게 해서 실키 경로가 조용히
    # 무방비로 돌아가는 조합을 없앤다(S7).
    sample_paths = write_samples(results, DEMO_DIR / "samples", secrets=[])

    meta = compiled.document["info"]["x-mcportal"]
    print("MCPortal 무키 컴파일 데모 - 합성 픽스처 전용(네트워크 0건, 인증키 0건)")
    print(f"  서비스        : {source.service_name} ({source.service_id})")
    print(f"  소스 종류     : {source.source_kind.value}")
    print(f"  소스 지문     : {source.fingerprint}")
    print(f"  오퍼레이션    : {', '.join(compiled.operation_ids)}")
    print(f"  스키마        : {', '.join(compiled.schema_names)}")
    print(f"  추론 샘플 수  : {meta['sample_count']}건 / 생성 모드 {meta['generation_mode']}")
    print(f"  미확정 스키마 : {meta['schema_inference'].get('unresolved', 0)}건(샘플 미제공 오퍼레이션)")
    print(f"  산출 스펙     : {spec_path}")
    print(f"  스펙 지문     : {_digest(spec_path)}")
    for path in sample_paths:
        print(f"  샘플          : {path.name}  {_digest(path)}")

    text = current.decode("utf-8")
    if "serviceKey" in text:
        print("  [실패] 산출물에 인증키 파라미터 이름이 남았습니다.")
        return 1
    if FETCHED_AT in text or "09:00:00" in text:
        print("  [실패] 산출물에 취득 시각이 실렸습니다(결정론 파괴).")
        return 1
    print("  점검          : 인증키 파라미터 비노출 OK · 취득 시각 비유출 OK")

    if previous is not None and previous != current:
        print("  [실패] 재생성 결과가 이전 산출물과 다릅니다. 결정론 회귀입니다.")
        return 1
    if previous is not None:
        print("  재현성        : 이전 산출물과 바이트 동일 OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
