# MCPortal

> **이 문서는 한국어 번역본이다. 정본(canonical)은 영문 [`README.md`](README.md) 이며,
> 두 문서가 어긋나면 영문판이 정본이다.**
>
> English: [`README.md`](README.md) · 한국어: 이 문서 (`README.ko.md`)

**data.go.kr와 글로벌 MCP 생태계 사이의 빠진 다리 — 한국 공공 API 명세를 표준 OpenAPI 3.1로 정규화해 MCP로 컴파일하고, 일일 쿼터를 예산·백오프·캐시로 관리하는 완전 오픈·셀프호스트형 런타임 레이어**

> **정확도 한계 고지 (Accuracy Disclaimer)**
>
> data.go.kr는 잔여 쿼터(remaining quota) 조회 API를 제공하지 않는다. 따라서
> MCPortal의 사용량 원장(usage ledger)은 **MCPortal을 경유한 호출만** 집계하는
> 베스트에포트(best-effort) 추정치다. 같은 serviceKey가 MCPortal 밖(다른 스크립트,
> 포털 콘솔, 타 도구)에서 소비한 호출은 원장에 잡히지 않으므로, 원장 값은 언제나
> 실제 잔여량의 하한 근사일 뿐이다. 신뢰의 축은 이 추정치가 아니라 **하드 예산
> 상한(`CALL_BUDGET`)** 이다. 원장이 부정확하더라도 하드 가드가 일일 상한을 넘는
> 호출을 물리적으로 차단하므로, 쿼터 초과로 인한 계정 제재를 예방하는 안전선은
> 언제나 `CALL_BUDGET`에서 나온다.

## 설치

```
pip install mcportal            # 코어 런타임(의존성 httpx 단일)
pip install "mcportal[mcp]"     # + MCP 변환 계층(fastmcp)
```

**의존성 정책**: 코어 런타임의 의존성은 **httpx 단일**이다. 스펙 정규화 컴파일러
(`mcportal.compiler`)도 표준 라이브러리와 httpx만 쓴다. MCP 변환(`mcportal.mcp`)이
쓰는 **fastmcp는 선택적 추가 의존성(`[mcp]` extra)** 이며, 설치하지 않아도
`import mcportal` 과 전체 테스트 스위트는 그대로 돈다. 미설치 상태에서 MCP 변환을
호출하면 설치 방법을 알려 주는 한국어 `ImportError` 로 막힌다. `[mcp]` extra 는
fastmcp 와 함께 **anyio** 를 선언한다 — sync→async 브리지가 `anyio.to_thread` 를
직접 임포트하기 때문이다(httpx 가 전이 의존으로 끌어오더라도, 직접 임포트에는
직접 선언이 따라야 W4 의 정확 핀·락파일이 버전 드리프트를 잡는다).

`import mcportal` 은 `mcportal.mcp` 를 **임포트하지 않는다.** fastmcp 없이도
`import mcportal` 이 성공해야 하기 때문이다. MCP 변환 심볼은 모듈 `__getattr__`
([PEP 562](https://peps.python.org/pep-0562/))로 **처음 참조할 때** 지연 해석되므로,
`from mcportal.mcp import build_server` 와 `mcportal.build_server` 는 같은 객체를
가리킨다. 어느 쪽으로 써도 무방하다.

## 무키 경계 (serviceKey 없이 되는 것 vs 실키가 필요한 것)

| serviceKey 없이 되는 것 (No key required) | 실키가 필요한 것 (Real key required) |
| --- | --- |
| record/replay 카세트(cassette) 재생 데모 | 라이브 API 호출 (live API calls) |
| 전체 테스트 스위트 실행 (`pytest`) | 라이브 응답 샘플링 (live sampling) |
| 커밋된 스펙에서 MCP 서버 세우기 (spec-to-MCP) | 아직 카세트가 없는 API의 즉석 변환 |
| 컴파일 데모 재생성 (`examples/compile_demo.py`) | 카세트 신규 녹화 (record) |
| 프리셋 3종(4데이터셋) 재생성·조회 (`mcportal compile` / `presets`) — 0.2.0 부터 **wheel 동봉**(주1) | 미확정 응답 스키마 채우기 (`mcportal sample`) |
| 쿼터 현황 조회 (`mcportal quota status`) | 벤치마크 실키 항목 K1~K3 |

> **주1 — 0.2.0 부터 프리셋 4종이 wheel 에 동봉된다.** 번들 16파일과 `presets/`
> 문서 2개가 wheel 에 들어간다(빌드 산출물 실측). 따라서 `pip install mcportal`
> 만으로 `mcportal presets`·`mcportal compile` 이 돈다. **동봉하지 않는 것은
> 샘플링 증거**다 — `cassettes/`·`samples/`·`sampled_schemas.json` 은 리포에만
> 둔다. 다른 번들을 쓰려면 `--presets-root <경로>` 또는 환경변수
> `MCPORTAL_PRESETS=<경로>` 로 위치를 알려 주면 된다. `mcportal presets` 는
> 프리셋을 찾지 못하면 탐색한 경로를 그대로 출력한다.

MCPortal의 데모·개발·CI 경로는 전부 무키로 돈다. 미리 녹화해 둔 카세트를 재생하는
record/replay 계층 덕분에, serviceKey가 없어도 실제 API와 동일한 응답 흐름을 재현하고
전체 테스트를 초록불로 통과시킬 수 있다. **스펙→MCP 변환은 설치·빌드 시점에 끝나
있으므로**(산출물이 `specs/` 에 커밋된다) MCP 서버를 세워 도구를 호출하는 데까지도
실키가 필요 없다. 실키를 요구하는 것은 실제 data.go.kr로 나가는 라이브 호출과,
아직 카세트가 없는 API를 즉석에서 샘플링·변환하는 경로뿐이다.

### 무키 재현 데모 흐름

```
# 1) 합성 픽스처로 스펙을 컴파일한다(네트워크 0건·인증키 0건).
python examples\compile_demo.py
#    → specs\demo\openapi.json + specs\demo\samples\*.json
#    재실행하면 바이트 동일한 산출물이 나온다(결정론 검증이 스크립트에 내장).

# 2) 커밋된 스펙 + 카세트로 MCP 서버를 세운다(실키 불요).
python -c "from mcportal.mcp import build_server; \
build_server('specs/demo/openapi.json', mode='replay', \
cassette_path='<카세트 경로>')"
#    specs\demo\ 에는 스펙과 샘플만 커밋돼 있고 카세트는 없다(W3 시점에도 그렇다).
#    <카세트 경로>는 record 로 직접 녹화한 카세트를 가리켜야 한다.
#    무키로 서버가 실제로 서고 도구 호출까지 응답하는 것은
#    tests\test_mcp_wiring.py 의 마지막 케이스가 증명한다
#    (그 테스트는 합성 카세트를 tmp_path 에 만들어 쓴다).
#    presets\<id>\openapi.json 도 같은 자리에 넣을 수 있다(15000115 -> 도구 8개).

# 3) 실제 공개 스펙으로 만든 프리셋을 재생성한다(네트워크 0건·인증키 0건).
#    ⚠️ 이 두 명령은 **리포를 체크아웃한 상태**를 전제한다. 프리셋은 아직 wheel 에
#    동봉되지 않으므로(주1) `pip install mcportal` 환경에서는 "프리셋을 찾지
#    못했습니다"가 나온다. 그 경우 --presets-root <경로> 또는
#    환경변수 MCPORTAL_PRESETS=<경로> 로 번들 위치를 알려 주면 된다.
mcportal presets            # 목록
mcportal compile --check    # 커밋본과 재생성본의 바이트 비교(드리프트가 있으면 종료 코드 3)

# 4) 전체 테스트 스위트(실네트워크·실키·실데이터 0건).
pytest -q
```

## 단일 키 원칙 (멀티키 로테이션 미지원)

MCPortal의 data.go.kr 프로파일은 **멀티키 로테이션(multi-key rotation)을 지원하지 않는다.**
data.go.kr는 개발계정 1인당 1키에 일일 호출 한도를 부과하는 1인 1키 구조이며, 여러 키를
번갈아 써서 한도를 우회하는 것은 서비스 운영정책 위반 소지가 있고 계정 제재 위험을 키운다.
MCPortal은 이 구조를 그대로 존중해 단일 키만 주입받는다. 한도가 부족하면 키를 늘리는 대신,
data.go.kr가 제공하는 **운영계정(운영단계) 전환**이라는 공식 경로 — 활용사례 등록 후 한도
상향 심사 — 를 통해 정식으로 상한을 올리는 것을 안내한다.

## 구현 상태

### W1 — 쿼터·런타임·재현 골격

- **쿼터가드 코어 (Quota-guard core)**
  - [x] 토큰 버킷(token bucket) 레이트리미터
  - [x] SQLite 사용량 원장(usage ledger)
  - [x] `CALL_BUDGET` 하드 가드(hard budget cap)
  - [x] 지수 백오프(exponential backoff)
- **런타임 공통 레이어 (Runtime common layer)**
  - [x] serviceKey 자동 주입(key injection)
  - [x] XML / EUC-KR 정규화(normalization)
  - [x] 오류코드 한국어 매핑(error-code mapping)
  - [x] TTL 캐시(TTL cache)
- **record-replay 골격 (record-replay skeleton)**
  - [x] 카세트 녹화/재생(cassette record & replay)
  - [x] serviceKey 자동 스크러빙(auto-scrubbing, 기본값 on)

**예산 해석 우선순위**: `create_client(budget=...)` 명시 인자 > 환경변수
`CALL_BUDGET` > 프로파일 기본 예산(`ProviderProfile.default_daily_budget`).
인자를 생략해도 **쿼터가드는 항상 배선된다** — README가 "신뢰의 축은 하드 예산
상한"이라고 선언한 이상, 기본 경로에서 가드가 조용히 사라지면 안 되기 때문이다.
가드 없는 트랜스포트가 필요하면 `MCPortalTransport(guard=None)` 을 직접 만든다.

가드가 항상 배선되면서 따라오는 두 가지 결과를 명시해 둔다.

- **`CALL_BUDGET`이 정수가 아니면 클라이언트 생성 자체가 실패한다**(한국어
  `ValueError`). 오염된 환경변수를 조용히 무시하고 다른 한도로 도는 것보다,
  즉시 멈추고 고치게 하는 편이 하드 상한의 취지에 맞다.
- **원장 파일이 만들어진다.** `ledger_path` 를 생략하면
  `~/.mcportal/ledger.db` 를 쓰며, 위치는 `MCPORTAL_LEDGER` 환경변수로 바꿀 수
  있다. 클라이언트를 닫으면(`client.close()` / `await client.aclose()`) 원장
  커넥션도 함께 회수된다.

**동시 호출에서의 하드 상한**: 쿼터가드는 `before_call` 시점에 in-flight 예약을
선점하고 `after_call` 에서 반납한다. 판정 모수가 `원장 기록 + 예약`이므로,
MCP 서버처럼 동시 tool call 이 들어오는 실행 형태에서도 실제 상위 호출 수가
상한을 넘지 않는다(예약이 없으면 동시 요청 전부가 기록 이전에 판정을 통과한다).

### W2 — 스펙 정규화 컴파일러 (`mcportal.compiler`)

data.go.kr는 스펙을 한 가지 형태로 주지 않는다. odcloud OAS(JSON형), 게이트웨이
Swagger(2.0/3.x), 그리고 스펙 파일 없이 활용가이드 문서만 있는 경우가 뒤섞여 있다.
컴파일러는 이 셋과 목록조회 메타를 **단일 중간표현으로 흡수**한 뒤 표준 OpenAPI
3.1로 산출한다.

- [x] `sources` — 3가지 제공 방식 + 목록조회 메타 → 중간표현 `SourceSpec`
- [x] `inference` — 정규화 응답 샘플 3~5개 → JSON Schema (오프라인·순수 함수)
- [x] `openapi` — `SourceSpec` → OpenAPI 3.1 문서 + 결정론 직렬화
- [x] `sampler` — 라이브 샘플링(쿼터가드 경유 강제 · 하드캡 5회 · 스크러빙 강제)

**결정론**: 같은 입력이면 재생성 결과가 바이트 단위로 같다. 샘플 입력 순서가 달라도
추론 결과가 같고(카운터 누적 기반 병합), 산출 JSON은 `sort_keys=True` · UTF-8 · LF ·
끝 개행 1개로 고정된다. 생성 시각·호스트명·취득 시각은 산출물에 싣지 않는다 —
그것들이 들어가는 순간 "재생성하면 바이트 동일"이라는 주장이 무너지기 때문이다.

**공개 표면**: `mcportal` 최상위와 `mcportal.compiler` 는 **사용자가 실제로 조립하는
이름**만 재수출한다. 진단용 헬퍼·타입 별칭·내부 기본값은 원 모듈에서 직접
임포트한다 — 예컨대 추론기의 세부 설정은 `mcportal.compiler.inference` 에서,
프리셋 드리프트 검사 함수는 `mcportal.compiler.curation` 에서 가져온다.

**인증키 비노출**: 컴파일된 스펙에는 `security`·`securitySchemes` 를 넣지 않고,
소스에 인증키 파라미터가 있어도 제거한다. 인증키는 트랜스포트가 주입하므로
**MCP 도구 인자로 노출될 자리가 없다**(스펙 파일·프롬프트 로그 유출 차단).
그 사실은 `info.x-mcportal.key_injection: "transport"` 로만 남는다.

### W2 — MCP 변환 (`mcportal.mcp`)

```python
from mcportal.mcp import build_server

server = build_server(
    "specs/demo/openapi.json",   # 컴파일러가 만들어 커밋해 둔 스펙
    mode="replay",               # 무키 경로: 카세트에서 응답을 재생한다
    cassette_path="<녹화해 둔 카세트 경로>",
)
```

`mode="live"` 로 바꾸고 `service_key=...` 를 주면 실제 API로 나간다. 이때도 요청은
쿼터가드를 지나므로 하드 예산 상한이 그대로 적용된다.

- [x] `FastMCP.from_openapi()` **위임** — 도구 정의·인자 변환·핸들러 생성 같은
      자체 코드젠은 하지 않는다. 스펙→도구 변환의 정확도와 유지보수 책임은
      fastmcp에 두고, MCPortal의 기여는 그 앞단(스펙 정규화)과 뒷단(쿼터·위생)에
      집중한다.
- [x] 계열 차이 흡수 — fastmcp 2.x(`from_openapi`)와 3.x(`OpenAPIProvider`)를
      런타임 시그니처 introspection으로 골라 호출한다.
- [x] sync→async 트랜스포트 브리지 — fastmcp는 `httpx.AsyncClient` 를 요구하지만
      MCPortal 런타임은 sync다. 쿼터 로직을 async로 재구현하지 않고 워커 스레드로
      위임해, **async 경로에서도 하드 예산 상한·키 주입·캐시·record/replay가 그대로
      살아 있다**(동시 tool call 에서도 예약 카운터가 상한을 지킨다).
- [x] **XML 응답 정규화** — 브리지가 XML 본문을 정규화 JSON으로 재직렬화해 올려
      보낸다. 컴파일러는 XML→dict 변환 결과에서 스키마를 추론하므로, 런타임이
      원본 XML을 그대로 넘기면 선언과 실제가 어긋나 도구 호출이 전부 실패한다.
      `_type=json`을 무시하고 XML을 돌려주는 data.go.kr 게이트웨이 오류도 같은
      경로로 흡수된다. 컴파일된 스펙의 200 `content` 키가 항상
      `application/json`인 것이 이 규약의 짝이며, 원 선언은
      `responses.200.x-mcportal.upstream_media_type`에 남는다.
- [x] 임포트 가드 — fastmcp 미설치 시 설치 방법을 담은 한국어 `ImportError`.

### W3 — 프리셋 3종(4데이터셋)과 큐레이션 2층 구조

`presets/` 에 공공데이터포털의 **실제 공개 스펙**으로 만든 번들이 들어 있다. 자동
변환만으로는 도구 설명이 전부 "목록 조회"로 보이는 문제를 **코드가 아니라
데이터로** 푸는 것이 이 계층의 목적이다.

| ID | 서비스 | 소스 종류 | 오퍼레이션 | 응답 미확정 | 이용허락범위 |
|---|---|---|---|---|---|
| `15000115` | 법제처 국가법령정보 공유서비스 | `rest_doc_manual` | 8 | 0 | 공공누리 제1유형(출처표시) |
| `15081808` | 국세청 사업자등록정보 진위확인·상태조회 | `odcloud_swagger` | 2 | 0 | 제한 없음 |
| `15101612` | 관세청 국가별 수출입실적 | `gw_swagger` | 1 | 0 | 제한 없음 |
| `15102108` | 관세청 수출입총괄 | `gw_swagger` | 1 | 0 | 제한 없음 |

도메인 기준으로 **3종**(법령 / 사업자등록 / 관세)이고 관세만 데이터셋이 2개라
모든 문서에서 **"3종(4데이터셋)"** 으로 표기한다.

번들 하나는 파일 넷이다.

```
presets/<id>/
├─ source.json     ← 스펙 원문 + 출처 URL·취득일·sha256 (아래층 입력, 원문 무손상)
├─ curation.json   ← 사람이 확인한 설명·예시·힌트    (위층 입력)
├─ openapi.json    ← 두 층을 병합한 산출물            (커밋 대상)
├─ README.md       ← 이 데이터셋의 출처·미해결 항목
├─ sampled_schemas.json ← 실키 샘플에서 추론한 응답 스키마(샘플링한 번들만)
├─ samples/        ← 실호출 응답 본문(스크러빙 완료)
└─ cassettes/      ← 요청·응답 쌍(오프라인 재현용)
```

위 네 파일이 wheel 에 동봉되는 범위이고, 샘플링 증거는 리포에만 둔다.

- **아래층(엔진)에는 도메인 지식이 0줄이다.** `mcportal.compiler.curation` 은
  큐레이션 데이터를 읽고 검증하고 병합하는 일반 엔진이며, 특정 기관·데이터셋의
  이름이 코드에 등장하지 않는다(테스트가 소스 문자열 스캔으로 회귀 검증한다).
- **큐레이션은 스펙 사실을 바꾸지 않는다.** 설명·예시·태그·힌트만 얹고, 파라미터의
  타입·위치·필수 여부와 경로·메서드는 원 스펙 선언이 정본이다. 사실 교정 통로는
  근거(`reason`)를 요구하는 두 가지뿐이다 — 응답 스키마 미확정 강등과 파라미터 제거.
- **응답 스키마 12건 중 10건이 미확정이었고, 2026-08-09 실키 샘플링으로 10건을
  전부 확정했다**(`15000115` 8건 · `15101612` 1건 · `15102108` 1건, 오퍼레이션당
  1회씩 총 10회 호출). 추론 결과는 번들마다 `sampled_schemas.json` 에, 응답 본문은
  `samples/` 에, 요청·응답 쌍은 `cassettes/` 에 남아 **무키로 재현**된다. 확정된
  번들은 `info.x-mcportal.generation_mode` 가 `sampled` 다. 나머지 2건
  (`15081808`)은 원래부터 소스가 응답 스키마를 선언해 미확정이 아니었고, 요청
  본문에 사업자등록번호가 실리는 개인정보 축이라 **샘플링에서 의도적으로
  뺐다**(선언은 있으나 실측되지 않았다). 어느 쪽이든 숨기지 않고 산출 문서의
  `info.x-mcportal.schema_inference.unresolved` 에 숫자로 남긴다.
- 같은 입력이면 `openapi.json` 은 **바이트 동일**하게 재생성된다. `mcportal compile
  --check` 를 CI 게이트로 쓴다(드리프트가 있으면 종료 코드 3). 다만
  `info.x-mcportal.tool_version` 이 패키지 버전을 담으므로 **버전을 올리면 4개
  산출물이 전부 바뀌며**, 그때는 재생성이 규약이다.

규약·미해결 항목은 [`presets/README.md`](presets/README.md), 스펙 메타데이터의 출처와
이용조건은 [`presets/NOTICE-DATA.md`](presets/NOTICE-DATA.md)가 정본이다.

### W3 — CLI (`mcportal`)

표준 라이브러리 `argparse` 만 쓴다. **신규 런타임 의존성 0** 이 이 프로젝트의
구속 규칙이라, 터미널 표도 자체 폭 계산으로 그린다(한글 2폭 · ASCII 구분선 ·
Windows cp949 콘솔 안전).

```
mcportal quota status [--ledger PATH] [--budget N] [--day YYYY-MM-DD]
                      [--key-fp FP | --key-env VAR] [--json]
mcportal compile [PRESET_ID ...] [--presets-root PATH] [--check] [--json]
mcportal presets [--presets-root PATH] [--json] [--verbose]
mcportal sample PRESET_ID ... --key-env VAR [--budget N] [--count N]
                [--ledger PATH] [--presets-root PATH] [--json]
```

| 서브커맨드 | 하는 일 |
|---|---|
| `quota status` | 오늘(KST) 사용량·예산·잔여·상태를 키 지문별로 보여 준다. **원장은 읽기 전용(`mode=ro`)으로만 열고 만들지 않는다.** 예산 해석 우선순위는 `--budget` > `CALL_BUDGET` > 프로파일 기본값이며 어느 경로였는지 출력에 밝힌다 |
| `compile` | 프리셋을 재생성한다. 내용이 같으면 파일을 다시 쓰지 않는다. `--check` 는 쓰지 않고 바이트 비교만 한다 |
| `presets` | 번들 목록을 표로 보여 준다. `--verbose` 는 큐레이션 메모까지 펼친다 |
| `sample` | 응답 스키마가 미확정인 오퍼레이션만 골라 실제로 호출하고, 추론한 스키마·응답 본문·재현용 카세트를 함께 남긴다. **키가 필요한 유일한 서브커맨드**이며 키는 `--key-env VAR` 가 가리키는 환경변수에서만 읽는다(원문을 인자로 받는 옵션은 없다) |

종료 코드: **0** 정상(원장 없음·프리셋 없음·변경 없음을 포함한다 — 빈 상태는
실패가 아니다) / **1** 실행 실패 / **2** 사용법 오류 / **3** `compile --check`
드리프트 / **130** 사용자 중단.

`--json` 은 stdout에 JSON 단독으로 나가고(사람용 문구가 섞이지 않아 파이프 안전),
오류는 전부 stderr로 간다. **인증키 원문은 어떤 경로로도 출력되지 않는다** —
`--key-env` 도 환경변수 값을 읽어 지문만 로컬에서 계산한다(원장에 키 원문 자체가
없으므로 CLI가 다룰 수 있는 것이 애초에 지문뿐이다).

### W3 — 벤치마크 (`benchmarks/`)

측정 계획을 **실행 코드보다 먼저** 확정하는 선등록(pre-registration) 방식이다.
[`benchmarks/PROTOCOL.md`](benchmarks/PROTOCOL.md)가 항목·반복 수·통계 정의·한계를
먼저 못박고, 하네스는 그 문서에 없는 항목을 재지 않는다. 결과 파일에는
PROTOCOL.md의 지문이 박히므로 어느 판본으로 쟀는지 사후에 확인할 수 있다.

```
python benchmarks/harness.py --label <라벨>     # 결과: benchmarks/results/bench_<날짜>_<라벨>.json
```

무키 5종(replay 왕복 · 스크러빙 · 컴파일+결정론 · 쿼터가드 오버헤드 · FastMCP 빌드)을
잰다. 네트워크 호출 0건이며 입력은 100% 합성이거나 커밋된 프리셋이다. 이상치를
제거하지 않고 원자료 표본을 결과 파일에 그대로 싣는다(재검산 가능).

**이 수치는 경쟁 라이브러리와의 비교가 아니라 MCPortal이 스스로 얹은 계층의
비용이다.** 2층 구조의 효용을 판정하는 항목(K2 — 자동생성 단독 vs 큐레이션의 툴콜
성공률)은 프로토콜에 **정의만** 되어 있고 **아직 실행하지 않았다.** 정의를 미리
적어 둔 이유는 키를 얻은 뒤에 유리한 판정 기준을 고르는 일을 막기 위해서다.
v0.2.0 에서 실키를 쓴 것은 응답 스키마 샘플링뿐이며, 실키 벤치 항목 K1~K3 은
이번 릴리스 범위가 아니다.

## 로드맵

- **v0.2.0**(현재): 실키 샘플링으로 미확정 응답 스키마 10건 확정 · 의존성 정확 핀
  + 락파일 · 프리셋 wheel 동봉 · CI(테스트·라이선스 게이트·시크릿 스캔·SBOM)
- **다음**: 벤치마크 실키 항목 K1~K3 실행 · 큐레이션 근거를 산출 문서에 남길 벤더
  확장 통로(정찰 F-07) · `15081808` 의 실호출 확인(개인정보 축이라 신중히 검토)

## License

Apache-2.0. 자세한 내용은 [`LICENSE`](LICENSE) 및 [`NOTICE`](NOTICE)를 참고하라.

코드가 아닌 **데이터 유래 파일**(테스트 픽스처·응답 샘플·컴파일 산출물)의 출처와
이용조건은 [`NOTICE-DATA.md`](NOTICE-DATA.md)가 별도로 추적한다.

- **API를 호출해 받은 실응답 데이터는 0건이다.** 테스트 픽스처·카세트·데모 산출물은
  전부 합성이며, 무키 재현 경로는 그 합성 픽스처만으로 성립한다.
- **W3부터 공공 API의 스펙 메타데이터**(스웨거 문서·요청변수/출력결과 표)는 출처
  URL과 취득일을 명기한 뒤 `presets/` 에 커밋한다. 취득은 전부 무인증이며 인증키
  사용 0회·게이트웨이 데이터 호출 0회다. 스펙 문서에 포털이 문서화용으로 적어 둔
  예시값이 섞여 있고 그 값은 전부 자리표시자다 — 파일별 출처·이용조건·예시값
  목록은 [`presets/NOTICE-DATA.md`](presets/NOTICE-DATA.md)가 정본이다.
- **개인을 식별하는 정보**(실명·실 사업자등록번호·개인 전화·개인 이메일)는 0건이다.
  범위는 **번들 산출물**(`source.json` · `curation.json` · `openapi.json` · 각
  `README.md`) 기준이다. `presets/_raw/` 의 포털 페이지 스냅샷에는 NIA 운영기관
  창구 이메일(`opendata_help@nia.or.kr`)·대표전화(`1566-0025`)와 각 데이터셋의
  관리부서 대표번호가 원문 그대로 남아 있으며, 기관 창구 정보이지 개인 연락처가
  아니다 — 근거와 목록은 [`presets/NOTICE-DATA.md`](presets/NOTICE-DATA.md) §2-1
  이 정본이다.
