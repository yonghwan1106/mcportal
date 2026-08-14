# SBOM provenance — how `mcportal-0.2.0.cdx.json` was made, and what is stale

This note exists because the filename in this directory does not match the
current release, and that mismatch is easy to misread in either direction.
Recorded 2026-08-15 from direct measurement of the checked-out tree.

## 1. What is here

| File | Size | What it is |
| --- | --- | --- |
| `mcportal-0.2.0.cdx.json` | 106,392 B | CycloneDX 1.6 JSON, 87 components, 87 dependency entries |
| `ML-BOM.md` | — | Demonstration-stack placeholder, unrelated to the dependency graph |

## 2. How the committed snapshot was produced

**By hand, once, on a Windows machine — not by CI.**

Evidence:

- `sbom/` has been touched by exactly one commit, `c049d11 "Release prep 0.2.0"`.
  Nothing has rewritten it since.
- The component list contains `pywin32` and does **not** contain `jeepney` or
  `secretstorage`. Those three are `keyring`'s platform-conditional
  dependencies: the Windows one is present, the two Linux ones are absent. The
  CI job runs on `ubuntu-latest`, so CI could not have produced this file.

The `sbom` job in `.github/workflows/ci.yml` (lines 374–423) *does* generate a
CycloneDX SBOM, but it is a different artifact with a different lifecycle:

- it triggers on `push` to `main` and `pull_request` to `main` — **not** on
  release tags;
- it writes `mcportal.cdx.json` (no version in the filename) and uploads it as
  a build artifact with `retention-days: 30`;
- it never writes into `sbom/` and never commits.

`ci.yml` says so itself at line 415: *"리포에 커밋되는 sbom/ 스냅샷(릴리스 시점
산출물)과는 별개다."* `release.yml` contains **no** SBOM step at all.

So there is no mechanism — and never was one — that refreshes this directory on
a release. The 0.2.1 release did not fail to update it; nothing was ever wired
to update it.

## 3. What is actually stale, and what is not

The document carries no `metadata.component` block and no timestamp or
`serialNumber` (`--output-reproducible` strips the latter two). The project
version therefore appears in exactly **one** place: the self-component,
`mcportal 0.2.0`, purl `mcportal==0.2.0`. That single field is stale.

Everything else still holds. Comparing all 87 components against the current
`requirements/lock-py311.txt` (91 pins):

- **version disagreements among shared packages: 0.** Every dependency version
  recorded here still matches the lockfile pin exactly.
- present here but not pinned in the lockfile: `pip`, `setuptools` — venv
  bootstrap packages, expected from an `environment` scan.
- pinned in the lockfile but absent here: `pytest`, `respx`, `iniconfig`,
  `pluggy`, `jeepney`, `secretstorage`, `async-timeout` (7).

That last group is the reason this file was **not** regenerated for 0.2.1.

## 4. Why it was not regenerated (decision, 2026-08-15)

`requirements/lock-py311.txt` was regenerated after this snapshot was taken
(`879d547 "Regenerate lockfile with uv universal resolution"`), which folded the
test dependencies into the same lockfile. Re-running the CI recipe today would
therefore yield roughly **94** components on Linux — 87 plus those 7 — and would
list `pytest` and `respx` inside a document that is supposed to describe what
ships to users. Re-running it on Windows would produce a third, different set
again (`pywin32` in, the Linux pair out).

Regenerating would thus have made the artifact less faithful to the distribution,
not more, while moving a component count that is cited in submitted documents.
`cyclonedx-bom` is also pinned nowhere in this repository — CI installs it
ad hoc into a throwaway venv — so a regeneration five days before code freeze
would have meant an unpinned network install as well.

The snapshot was left as-is. Its filename is an honest record of the commit it
was taken at.

## 5. If this is refreshed later

Regenerating for a future release should reuse the CI recipe verbatim so the
environment matches (`ci.yml`, `sbom` job), and should decide explicitly whether
the target venv is built from the full lockfile (which now includes test
dependencies) or from runtime dependencies only. The two choices give materially
different component counts, and any count quoted elsewhere has to move with it.
