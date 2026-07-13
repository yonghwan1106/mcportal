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

## 무키 경계 (serviceKey 없이 되는 것 vs 실키가 필요한 것)

| serviceKey 없이 되는 것 (No key required) | 실키가 필요한 것 (Real key required) |
| --- | --- |
| record/replay 카세트(cassette) 재생 데모 | 라이브 API 호출 (live API calls) |
| 전체 테스트 스위트 실행 (`pytest`) | 임의 API의 라이브 변환 (live spec-to-MCP) |

MCPortal의 데모·개발·CI 경로는 전부 무키로 돈다. 미리 녹화해 둔 카세트를 재생하는
record/replay 계층 덕분에, serviceKey가 없어도 실제 API와 동일한 응답 흐름을 재현하고
전체 테스트를 초록불로 통과시킬 수 있다. 실제 data.go.kr로 나가는 라이브 호출과,
아직 카세트가 없는 임의 API를 즉석에서 MCP로 변환하는 라이브 변환만이 실키를 요구한다.

## 단일 키 원칙 (멀티키 로테이션 미지원)

MCPortal의 data.go.kr 프로파일은 **멀티키 로테이션(multi-key rotation)을 지원하지 않는다.**
data.go.kr는 개발계정 1인당 1키에 일일 호출 한도를 부과하는 1인 1키 구조이며, 여러 키를
번갈아 써서 한도를 우회하는 것은 서비스 운영정책 위반 소지가 있고 계정 제재 위험을 키운다.
MCPortal은 이 구조를 그대로 존중해 단일 키만 주입받는다. 한도가 부족하면 키를 늘리는 대신,
data.go.kr가 제공하는 **운영계정(운영단계) 전환**이라는 공식 경로 — 활용사례 등록 후 한도
상향 심사 — 를 통해 정식으로 상한을 올리는 것을 안내한다.

## W1 구현 상태

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

## 로드맵

- **W2**: 스펙 정규화 컴파일러(spec-normalization compiler) + 응답 스키마 추론(response-schema
  inference) + FastMCP 변환(FastMCP compilation) + **v0.1 공개(public release)**

## License

Apache-2.0. 자세한 내용은 [`LICENSE`](LICENSE) 및 [`NOTICE`](NOTICE)를 참고하라.
