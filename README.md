# MCPortal

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
#    W2 시점에 specs\demo\ 에는 스펙과 샘플만 커밋돼 있고 카세트는 없다.
#    <카세트 경로>는 record 로 직접 녹화한 카세트를 가리켜야 한다.
#    데모 카세트 커밋은 W3 예정이며, 무키로 서버가 실제로 서고 도구 호출까지
#    응답하는 것은 tests\test_mcp_wiring.py 의 마지막 케이스가 증명한다
#    (그 테스트는 합성 카세트를 tmp_path 에 만들어 쓴다).

# 3) 전체 테스트 스위트(실네트워크·실키·실데이터 0건).
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

## 로드맵

- **W3**: 프리셋 3종 큐레이션 · CLI(`mcportal quota status`) · 벤치마크 하네스
- **W4**: 의존성 정확 핀 + 락파일 · PyPI 배포 · **v0.1 공개(public release)**

## License

Apache-2.0. 자세한 내용은 [`LICENSE`](LICENSE) 및 [`NOTICE`](NOTICE)를 참고하라.

코드가 아닌 **데이터 유래 파일**(테스트 픽스처·응답 샘플·컴파일 산출물)의 출처와
이용조건은 [`NOTICE-DATA.md`](NOTICE-DATA.md)가 별도로 추적한다. 현재 이 리포에
실데이터 유래 파일은 0건이며, 무키 재현 경로는 전부 합성 픽스처로 성립한다.
