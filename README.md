<!-- mcp-name: io.github.yonghwan1106/mcportal -->

# MCPortal

**The missing bridge between data.go.kr and the global MCP ecosystem.** MCPortal
normalizes Korean public-API specifications into standard OpenAPI 3.1, compiles
them into MCP servers, and puts every outbound call behind a hard daily budget
with backoff and caching. Fully open source, fully self-hosted.

한국어 문서: [`README.ko.md`](README.ko.md) — this English README is the
canonical document and the Korean file is a translation of it.

> **Accuracy disclaimer**
>
> data.go.kr publishes no "remaining quota" endpoint. MCPortal's usage ledger
> therefore counts **only the calls that went through MCPortal**, which makes it
> a best-effort estimate. Calls that the same `serviceKey` spends outside
> MCPortal — another script, the portal console, a different tool — never reach
> the ledger, so the ledger value is always a lower-bound approximation of real
> consumption. The axis of trust is not that estimate but the **hard budget cap
> (`CALL_BUDGET`)**. Even when the ledger is wrong, the hard guard physically
> blocks calls beyond the daily cap, so the safety line against quota-related
> account sanctions always comes from `CALL_BUDGET`.

## The no-key boundary

| Works without any API key | Needs a data.go.kr service key |
| --- | --- |
| Replaying record/replay cassettes | Live API calls |
| The full test suite (`pytest`), including the record-mode tests, which run on synthetic transports | Live response sampling |
| Standing up an MCP server from a committed spec (spec-to-MCP) | Ad-hoc conversion of an API that has no cassette yet |
| Regenerating the compile demo (`examples/compile_demo.py`) | Recording new cassettes |
| Regenerating and listing the preset bundles (`mcportal compile` / `mcportal presets`) — bundled in the wheel since 0.2.0 (note 1) | Sampling a response schema that is still unresolved (`mcportal sample`) |
| Reading quota status (`mcportal quota status`) | The key-dependent benchmark items K1–K3 |
| Running the benchmark harness (the five key-free items in `benchmarks/PROTOCOL.md`) | — |

Every demo, development and CI path in MCPortal runs without a key. The
record/replay layer replays cassettes that were recorded earlier, so the same
response flow can be reproduced and the whole test suite can go green with no
`serviceKey` present. **Spec-to-MCP conversion has already happened at build
time** — the compiled artifacts are committed under `specs/` — so even standing
up an MCP server and answering tool calls needs no key. What does need a key is
narrow: live traffic to data.go.kr, and sampling or converting an API that has
no cassette yet.

> **Note 1 — the preset bundles ship inside the wheel as of 0.2.0.**
> Since 0.2.0, **the published wheel carries the four preset bundles** (16 bundle
> files plus the two `presets/` documents, measured on the built artifact), so a
> plain `pip install mcportal` can run `mcportal presets` and `mcportal compile`
> with no checkout. What the wheel deliberately leaves out is the sampling
> evidence — `cassettes/`, `samples/` and `sampled_schemas.json` stay in the
> repository only. To use a different bundle set, point MCPortal at a checkout
> with `--presets-root <path>` or the environment variable
> `MCPORTAL_PRESETS=<path>`. When `mcportal presets` finds no bundle it prints
> the paths it searched.

### Key-free reproduction walkthrough

```
# 1) Compile a spec from synthetic fixtures (zero network, zero credentials).
python examples/compile_demo.py
#    -> specs/demo/openapi.json + specs/demo/samples/*.json
#    Re-running produces byte-identical output; the determinism check is
#    built into the script.

# 2) Stand up an MCP server from a committed spec plus a cassette (no key).
python -c "from mcportal.mcp import build_server; \
build_server('specs/demo/openapi.json', mode='replay', \
cassette_path='<cassette path>')"
#    specs/demo/ commits the spec and the samples, not a cassette, so
#    <cassette path> must point at one you recorded yourself. That a server
#    really stands up and answers a tool call without a key is proven by the
#    last case in tests/test_mcp_wiring.py, which builds a synthetic cassette
#    in tmp_path. presets/<id>/openapi.json fits the same slot.

# 3) Regenerate the preset bundles from real published specs (no key).
#    Needs a repo checkout (note 1); on a PyPI install pass --presets-root
#    <path> or set MCPORTAL_PRESETS=<path>.
mcportal presets            # list
mcportal compile --check    # byte-compare committed vs regenerated (exit 3 on drift)

# 4) The whole test suite (no live network, no key, no real data).
pytest -q
```

## Install

```
pip install mcportal            # core runtime (single dependency: httpx)
pip install "mcportal[mcp]"     # + the MCP conversion layer (fastmcp)
```

**Dependency policy: the core runtime depends on httpx and nothing else.** The
spec-normalizing compiler (`mcportal.compiler`) uses only the standard library
and httpx. [fastmcp](https://github.com/PrefectHQ/fastmcp) is required solely by
`mcportal.mcp` and ships as the optional `[mcp]` extra — without it,
`import mcportal` and the entire test suite still work, and calling into the MCP
layer raises a Korean `ImportError` that explains how to install it. The `[mcp]`
extra also declares **anyio**, because the sync-to-async bridge imports
`anyio.to_thread` directly; httpx pulls anyio in transitively, but a direct
import deserves a direct declaration so that pins and lockfiles constrain it.

`import mcportal` does **not** import `mcportal.mcp` — that is what keeps the
import working without fastmcp installed. The MCP symbols are resolved lazily on
first attribute access through a module `__getattr__`
([PEP 562](https://peps.python.org/pep-0562/)), so `from mcportal.mcp import
build_server` and `mcportal.build_server` refer to the same object. Either
spelling is fine.

### Single-key principle (no multi-key rotation)

MCPortal's data.go.kr profile **does not support multi-key rotation.**
data.go.kr issues one key per development account and meters a daily call limit
against it; cycling several keys to escape that limit risks violating the
service's operating policy and inviting account sanctions. MCPortal respects the
structure as it is and accepts a single key. When the limit is too low, the
supported answer is data.go.kr's own path — registering a use case and applying
for the operational tier — not more keys.

## CLI

The CLI uses the standard library `argparse` only. **Zero new runtime
dependencies** is a binding rule for this project, so even the terminal tables
are laid out by hand (Hangul counted as double width, ASCII rules, safe on a
Windows cp949 console).

```
mcportal quota status [--ledger PATH] [--budget N] [--day YYYY-MM-DD]
                      [--key-fp FP | --key-env VAR] [--json]
mcportal compile [PRESET_ID ...] [--presets-root PATH] [--check] [--json]
mcportal presets [--presets-root PATH] [--json] [--verbose]
mcportal sample PRESET_ID ... --key-env VAR [--budget N] [--count N]
                [--ledger PATH] [--presets-root PATH] [--json]
```

| Subcommand | What it does |
| --- | --- |
| `quota status` | Shows today's (KST) usage, budget, remainder and state per key fingerprint. **The ledger is opened read-only (`mode=ro`) and never created.** Budget resolution order is `--budget` > `CALL_BUDGET` > profile default, and the output states which path was taken |
| `compile` | Regenerates preset bundles. Files whose content is unchanged are not rewritten. `--check` writes nothing and only byte-compares |
| `presets` | Lists the bundles as a table; `--verbose` expands the curation notes |
| `sample` | Live sampling that fills in response schemas still marked unresolved, writing the inferred schema, the response bodies and a replayable cassette. It is the one subcommand that needs a key, and the key is taken **only** from the environment variable named by `--key-env VAR`, never as a literal argument |

Exit codes: **0** success (including "no ledger", "no presets" and "nothing
changed" — an empty state is not a failure) / **1** execution failure / **2**
usage error / **3** drift found by `compile --check` / **130** user interrupt.

`--json` prints JSON alone on stdout (no human prose mixed in, so it is
pipe-safe) and sends every error to stderr. **A raw service key is never printed
on any path** — even `--key-env` reads the environment variable and computes the
fingerprint locally. The ledger stores no raw key, so a fingerprint is the only
thing the CLI ever had available to show.

## Presets — three services, four datasets

`presets/` holds bundles built from **real published specifications** on
data.go.kr. Their purpose is to demonstrate, **in data rather than in code**,
the fix for the failure mode of naive spec conversion, where every generated
tool ends up described as "list query".

| ID | Service | Domain | Source kind | Operations | Data licence as published |
| --- | --- | --- | --- | --- | --- |
| `15000115` | Ministry of Government Legislation — national law information sharing service | law | `rest_doc_manual` | 8 | KOGL Type 1 (attribution) |
| `15081808` | National Tax Service — business registration validity and status lookup | business registration | `odcloud_swagger` | 2 | no restriction stated |
| `15101612` | Korea Customs Service — trade by country | customs | `gw_swagger` | 1 | no restriction stated |
| `15102108` | Korea Customs Service — import/export summary | customs | `gw_swagger` | 1 | no restriction stated |

Licence wording is what data.go.kr displayed on the acquisition date recorded in
each bundle. By domain there are **three services**, and only customs has two
datasets, which is why every document writes **"three services (four
datasets)"**.

**Ten of the twelve response schemas were unresolved; live sampling on
2026-08-09 settled all ten** (`15000115` eight, `15101612` one, `15102108` one —
one call per operation, ten calls total). The inferred schemas are persisted in
each bundle's `sampled_schemas.json`, the response bodies in `samples/` and the
request/response pairs in `cassettes/`, so the result **replays offline with no
key**. Sampled bundles report `generation_mode: "sampled"` in
`info.x-mcportal`.

The remaining two operations (`15081808`) were never unresolved — that source
declares its response schema. That bundle was deliberately left out of sampling
because its request body carries a business registration number, so its schema is
declared but not measured. MCPortal does not hide either state: the live count is
written into `info.x-mcportal.schema_inference.unresolved` in the generated
document, and what each bundle still does not know is listed in its own
`presets/<id>/README.md`. Writing down something unverified as if it were
verified is against the rules of this project.

A bundle is four committed files, plus the sampling evidence where it exists:

```
presets/<id>/
├─ source.json           <- the spec document plus source URL, acquisition date, sha256
├─ curation.json         <- human-checked descriptions, examples, hints
├─ openapi.json          <- the merge of both layers (the committed artifact)
├─ README.md             <- provenance and open questions for this dataset
├─ sampled_schemas.json  <- schemas inferred from live samples (sampled bundles only)
├─ samples/              <- scrubbed response bodies from those calls
└─ cassettes/            <- request/response pairs for offline replay
```

The four files at the top are what ships in the wheel. The sampling evidence
stays in the repository.

- **The lower layer carries zero lines of domain knowledge.**
  `mcportal.compiler.curation` is a general engine that reads, validates and
  merges curation data; no institution or dataset name appears in the code, and
  a test enforces that by scanning the source strings.
- **Curation does not change spec facts.** It adds descriptions, examples, tags
  and hints. Parameter type, location and requiredness, and operation path and
  method, remain whatever the source spec declares. Only two channels can
  correct a fact, and both demand a written `reason`: downgrading a response
  schema to unresolved, and removing a parameter.
- Given the same inputs, `openapi.json` regenerates **byte-identically**. Use
  `mcportal compile --check` as a CI gate (exit code 3 on drift). One caveat:
  `info.x-mcportal.tool_version` carries the package version, so **bumping the
  version changes all four artifacts**, and regenerating is the convention when
  that happens.

Conventions and open items are governed by
[`presets/README.md`](presets/README.md); provenance and terms of use for the
spec metadata are governed by
[`presets/NOTICE-DATA.md`](presets/NOTICE-DATA.md).

## Architecture

Two layers at compile time, one guarded chain at run time.

```
COMPILE TIME  (offline: no key, no network)

  spec documents                      +--------------------------------+
   - odcloud OAS (JSON)               | lower layer: the compiler      |
   - gateway Swagger 2.0 / 3.x  --->  | zero domain knowledge          |
   - hand-mapped usage guide          | sources -> SourceSpec -> IR    |
                                      +---------------+----------------+
                                                      |
  curation.json                                       |
  (human-checked descriptions,  --------------->    merge
   examples, hints)                    upper layer: data, not code
                                                      |
                                                      v
                                         openapi.json (committed;
                                         byte-identical on rebuild)

RUN TIME  (one MCP tool call)

  MCP client
      |  tool call
      v
  FastMCP server            <- built by FastMCP.from_openapi() from openapi.json
      |
      v
  sync/async bridge  ->  MCPortalTransport
                            |-- quota guard      token bucket + SQLite ledger
                            |                    + CALL_BUDGET hard cap + backoff
                            |-- key injection    the key never enters the spec
                            |-- TTL cache
                            |-- record / replay  cassettes, scrubbed on write
                            |-- normalization    XML -> JSON, EUC-KR, error codes
                            v
                     data.go.kr      (or the cassette, in replay mode)
```

Two consequences of that shape are worth stating explicitly.

- **Tool definitions are not hand-generated.** `FastMCP.from_openapi()` owns the
  spec-to-tool conversion; MCPortal contributes the stage before it (spec
  normalization) and the stage after it (quota and hygiene). Version-family
  differences are absorbed by runtime signature introspection: it uses
  `FastMCP.from_openapi` where the class exposes it and otherwise falls back to
  building `FastMCP(providers=[OpenAPIProvider(...)])`. That is a capability
  check rather than a version check — `from_openapi` is **not** a 2.x-only entry
  point, it exists in the 3.x line too. The dependency is pinned to
  `fastmcp==2.14.7` because that is the combination actually exercised under
  cassette replay; the known hard boundary is **4.0**, which moves the HTTP stack
  to `httpx2>=2.5` and therefore breaks the `httpx.AsyncBaseTransport` bridge the
  transport is built on (4.0 also re-splits the distribution into
  `fastmcp-slim`). The rationale is recorded next to the pin in `pyproject.toml`.
- **The service key has no place to leak into.** The compiler emits no
  `security` or `securitySchemes` and strips key parameters out of the source,
  so the key is never an MCP tool argument, never in a spec file, never in a
  prompt log. Only the fact of transport-side injection survives, as
  `info.x-mcportal.key_injection: "transport"`.

The guard is wired on every default path. Budget resolution is
`create_client(budget=...)` > the `CALL_BUDGET` environment variable > the
profile default, and omitting the argument still wires the guard — a README that
declares the hard cap to be the axis of trust cannot let the guard quietly
vanish. An in-flight reservation is taken at `before_call` and released at
`after_call`, so the cap holds even when an MCP server issues concurrent tool
calls.

## Benchmarks

The measurement plan is pre-registered.
[`benchmarks/PROTOCOL.md`](benchmarks/PROTOCOL.md) fixes the items, repeat
counts, statistical definitions and limitations before the harness existed, and
the harness measures nothing that is not in that document. Result files embed a
fingerprint of the protocol, so which revision produced a number stays checkable
after the fact.

```
python benchmarks/harness.py --label <label>
```

Five key-free items are measured (replay round trip, scrubbing, compile plus
determinism, quota-guard overhead, FastMCP build). Zero network calls; inputs
are either fully synthetic or the committed presets. Outliers are not removed —
the raw samples ship inside the result file so anyone can recompute.

**Headline: quota-guard overhead is a median of +0.90 ms per call**
(+901,050 ns; guarded median 1.04 ms against a bare median 0.14 ms; N = 200
after 20 warmup rounds; measured 2026-08-09 on Windows 10, CPython 3.11.9,
httpx 0.28.1, SQLite 3.45.1).

Read that as an **absolute increment**, and read it with two facts attached.
The number **includes the SQLite ledger write** (WAL journal mode), because that
write is part of the real cost. The baseline it is subtracted from is a bare
`httpx.Client` over an in-memory `httpx.MockTransport` — no socket, no I/O — so
the same measurement expressed as a percentage is large by construction and is
not meaningful on its own. Against a real data.go.kr round trip the comparison
looks different, and MCPortal does not claim that comparison here because it has
not been measured.

**These numbers are the cost of the layer MCPortal adds to itself, not a
comparison against competing libraries.** The item that would judge whether the
two-layer design pays off (K2 — tool-call success rate of naive generation
versus curation) is **defined only** in the protocol and **has not been run.**
Writing the definition down in advance is deliberate: it prevents picking a
favourable criterion after the results are in. The live key used in 0.2.0 went to
response-schema sampling only; the key-dependent benchmark items K1–K3 are out of
scope for this release.

## Machine-readable preset root

`mcportal presets --json` reports a `root_source` key alongside `root`, labelling
where the adopted preset root came from: `argument` (an explicit
`--presets-root`), `env:MCPORTAL_PRESETS`, `discovered` (found by the default
search), or `none` (no root at all) — so a script can tell a deliberate root from
an accidental one.

## Licence

Apache-2.0. See [`LICENSE`](LICENSE) and [`NOTICE`](NOTICE).

Provenance and terms of use for **data-derived files** (test fixtures, response
samples, compiler artifacts) are tracked separately in
[`NOTICE-DATA.md`](NOTICE-DATA.md).

- **Zero response payloads obtained by calling an API are committed.** Test
  fixtures, cassettes and demo artifacts are all synthetic, and the key-free
  reproduction path stands on those synthetic fixtures alone.
- **Spec metadata from public APIs** (Swagger documents, request/response
  tables) is committed under `presets/` with its source URL and acquisition date
  recorded. Acquisition was entirely unauthenticated: zero uses of a service
  key, zero gateway data calls. Spec documents contain example values the portal
  wrote for documentation purposes, and those are all placeholders — the
  per-file list of sources, terms and example values is governed by
  [`presets/NOTICE-DATA.md`](presets/NOTICE-DATA.md).
- **Zero personally identifying information** (real names, real business
  registration numbers, personal phone numbers, personal email addresses) is
  present. The scope of that statement is the bundle artifacts — `source.json`,
  `curation.json`, `openapi.json` and each `README.md`. The portal page
  snapshots under `presets/_raw/` do retain the operating agency's public help
  desk email (`opendata_help@nia.or.kr`), its main phone number (`1566-0025`)
  and the representative numbers of each dataset's managing department, as they
  appeared in the original; those are institutional contact points, not personal
  ones. The evidence and the full list are governed by
  [`presets/NOTICE-DATA.md`](presets/NOTICE-DATA.md) §2-1.
