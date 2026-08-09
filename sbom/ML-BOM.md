# ML-BOM — demonstration stack (PLACEHOLDER SKELETON)

> ## ⚠ THIS IS A PLACEHOLDER, NOT A RELEASED BILL OF MATERIALS.
>
> Every version, digest and size below is **unmeasured**. Nothing in this file
> has been verified against an installed artifact, and no component listed here
> is bundled with, depended on, or shipped by the `mcportal` package. The file
> exists so that the demonstration stack is *declared* before it is *built*, and
> so that the fields that must be measured are visible as holes rather than
> filled in from memory later.
>
> **Fill-in trigger:** once the W5 demo stack is fixed. Until then, treat every
> row as a proposal.

## 1. Scope

| Item | Value |
| --- | --- |
| Subject | The **local demonstration stack** used to show an MCPortal-compiled MCP server being driven by a model — not the `mcportal` distribution |
| Not in scope | The `mcportal` package's own dependency graph. That is a software BOM, produced separately in CycloneDX form by the release tooling, and it is the authoritative document for what ships to PyPI |
| Runtime dependency impact | **None.** Nothing here becomes a dependency of `mcportal`; the core runtime dependency stays httpx alone |
| Network | The stack is intended to run locally with open-weight models. Whether any component reaches the network at demo time is itself [TO BE VERIFIED — W5] |
| Status | PLACEHOLDER SKELETON. Not published, not attached to a release |

## 2. Components

Legend for the Status column: **PLACEHOLDER** = proposed, nothing measured;
**LICENCE VERIFIED** = the licence field was read from the upstream source on
the date given, while versions and digests remain unmeasured.

| # | Component | Type | Version | Licence | Source | Digest / hash | Status |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 1 | Open-weight LLM, Qwen3 family (exact variant not chosen) | model (weights) | [TO BE FIXED — W5 demo stack decision] | Apache-2.0 — verified 2026-08-09 against the `Qwen/Qwen3-8B` model card metadata. **Re-verify per variant**, since a family is not a licence | Hugging Face `Qwen/Qwen3-*`, pulled through the runner in row 2 | [TO BE MEASURED — record with `ollama show` once the W5 demo stack is fixed] | PLACEHOLDER · LICENCE VERIFIED for the reference variant only |
| 2 | Ollama | local model runner | [TO BE MEASURED — `ollama --version`] | MIT — verified 2026-08-09 from the repository `LICENSE` ("Copyright (c) Ollama") | https://github.com/ollama/ollama | [TO BE MEASURED — installer artifact hash] | PLACEHOLDER · LICENCE VERIFIED |
| 3 | mcphost | MCP host / demo runner | [TO BE MEASURED — release tag actually used] | MIT — verified 2026-08-09 from the repository `LICENSE` ("Copyright (c) 2024 Mark III Labs, LLC.") | https://github.com/mark3labs/mcphost | [TO BE MEASURED — release binary hash] | PLACEHOLDER · LICENCE VERIFIED |
| 4 | MCPortal preset bundles used in the demo | data (spec metadata) | tracks the repository | Per dataset, as published on data.go.kr | `presets/<id>/`; provenance is governed by [`../presets/NOTICE-DATA.md`](../presets/NOTICE-DATA.md) | The per-file `sha256` values already recorded in `presets/_MANIFEST_*.json` and in each `source.json` | PLACEHOLDER — the *selection* for the demo is undecided; the files themselves are already tracked |
| 5 | Demo fixtures: response samples and replay cassettes | data (fixtures) | [TO BE FIXED — W5] | Derived from row 4; scrubbed before commit | `presets/<id>/samples/`, `presets/<id>/cassettes/`, plus synthetic fixtures under `tests/` | [TO BE MEASURED] | PLACEHOLDER — which fixtures the demo replays is undecided |

## 3. Fields that must be measured before this file is real

1. **Model variant and weights digest.** Both are blank on purpose. Record the
   digest that the runner reports for the exact tag that was pulled, not a
   digest copied from a model page — a re-quantised or re-uploaded tag changes
   it.
2. **Runner and host versions.** Record what was actually executed for the demo,
   including the platform the binary was built for.
3. **Fixture selection.** Row 5 is only meaningful once the demo script exists
   and names the cassettes it replays.
4. **Per-variant licence re-check.** Row 1's licence is verified for one
   reference variant. Model families do not carry a single licence by
   construction; the chosen variant's own model card is the authority.

## 4. Rules this file inherits

- **No real service key, no real personal data, anywhere in the demo stack.**
  A fixture that has not been through scrubbing does not enter this BOM.
- **Data provenance is not restated here.** For anything under `presets/`,
  [`../presets/NOTICE-DATA.md`](../presets/NOTICE-DATA.md) is the governing
  document and this file only points at it.
- **Unverified is written as unverified.** Any field that cannot be checked at
  the time of writing carries a bracketed marker (`[TO BE MEASURED …]`,
  `[TO BE FIXED …]`, `[TO BE VERIFIED …]`) or `[UNVERIFIED]`. Removing a marker
  without doing the measurement is the failure mode this file is designed to
  make visible.
