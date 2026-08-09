# Contributing to MCPortal

Thank you for looking at MCPortal. Issues, questions and pull requests are all
welcome. Please read the merge-hold policy first — it affects timing, not
whether your contribution is wanted.

## Merge hold until 2026-08-27

**External PRs are welcome but will not be merged before 2026-08-27.** MCPortal
is an entry in the 20th Korea Open Source Developer Contest, and the maintainer
treats the submitted work as having to be his own authorship; merging
third-party commits before the review deadline would put that in question. The
hold is about submission integrity, not about the quality of your patch.

What that means in practice:

- **Open the pull request anyway.** It will be read, reviewed and discussed
  during the hold, exactly as it would be afterwards.
- **You are credited while it waits.** The tracking issue for the PR names the
  contributor and describes the contribution, so the record exists from the day
  you open it.
- **Merges resume in September 2026.** Held PRs are taken first, in the order
  they were opened, once the review period has closed.
- **Issues are unaffected.** Bug reports, reproduction cases, questions and
  design discussion are handled normally throughout.

If a security problem is involved, please raise it as an issue marked as such
rather than sitting on it until September — the hold applies to merging code,
not to disclosure.

## Developer Certificate of Origin (DCO)

Every commit must be signed off. MCPortal uses the
[Developer Certificate of Origin 1.1](https://developercertificate.org/): by
signing off, you state that you wrote the contribution or otherwise have the
right to submit it under the project's licence (Apache-2.0).

Add the sign-off line automatically with `-s`:

```
git commit -s -m "compiler: absorb the empty basePath case"
```

That appends a trailer to the commit message:

```
Signed-off-by: Your Name <your.email@example.com>
```

The name and address must be real and must match your `user.name` and
`user.email`. To fix commits you already made:

```
git commit --amend -s --no-edit          # the most recent commit
git rebase --signoff <base>              # a whole branch
```

Unsigned commits are not merged. There is no CLA to sign beyond the DCO.

## Development environment

Python 3.11 is the baseline (`requires-python = ">=3.11"`); 3.12 is declared
supported in the package classifiers.

```
python -m venv .venv
.venv\Scripts\activate                                     # Windows
. .venv/bin/activate                                       # POSIX
python -m pip install -e ".[dev]" -c requirements/lock-py311.txt
```

The `-c` constraint file pins the full development and MCP dependency graph so
that everyone resolves the same versions. It is introduced by the packaging work
in the same release as this document; if your checkout predates it and the file
is absent, install without `-c` and expect free resolution instead.

Two rules govern dependencies:

- **The core runtime depends on httpx and nothing else.** A patch that adds a
  runtime dependency will not be accepted without a separate discussion first.
  `fastmcp` and `anyio` belong to the optional `[mcp]` extra; test-only tools
  belong to `[dev]`.
- **Comments in any `requirements/*.txt` file must be ASCII only.** pip decodes
  requirements files with the platform default codec, which is cp949 on a Korean
  Windows install, and a non-ASCII comment kills the install there.

## Tests

```
python -m pytest -q            # the whole suite
mcportal compile --check       # preset drift gate (exit code 3 on drift)
```

The suite must stay green with **no network access and no service key**. Those
are not incidental properties, they are the contract: record/replay cassettes
and synthetic fixtures cover everything, and a test that reaches the internet or
reads a real credential will be rejected.

What a pull request needs:

- **A regression test travels with every behaviour change.** A fix without a
  test that fails before it and passes after it is not finished. New behaviour
  needs the test that describes it.
- **Determinism holds.** Compiler output must regenerate byte-identically from
  the same input; `mcportal compile --check` must stay quiet. If you bump the
  package version, regenerate the preset artifacts, because
  `info.x-mcportal.tool_version` carries the version into all four files.
- **No real credentials, ever.** Never commit a service key, a live response
  captured with one, or a cassette that has not been through scrubbing. Test
  keys are synthetic strings.

## Code conventions

These are inherited across the whole source tree; match the file you are
editing.

- **SPDX header, two lines**, at the top of every Python file:

  ```python
  # SPDX-License-Identifier: Apache-2.0
  # Copyright 2026 Yong Park
  ```

- **`from __future__ import annotations`** immediately after the module
  docstring.
- **Docstrings are written in Korean**, Google style, with `Args:` / `Returns:`
  / `Raises:` sections. Prose that explains *why* a non-obvious decision was made
  belongs in the docstring or a comment, not in the commit message alone.
- **Value objects are frozen dataclasses** (`@dataclass(frozen=True)`).
- **Text files are UTF-8 without BOM, LF line endings, exactly one trailing
  newline.** Generated JSON additionally uses `sort_keys=True`.
- Markdown files carry no SPDX header.

## Reporting bugs

A good report contains the MCPortal version (`mcportal --version`), the Python
version and OS, the exact command or code, and the full error output with any
key material removed. If a preset is involved, name the dataset ID. If you can
reproduce it with a synthetic fixture or a scrubbed cassette, attach that —
it usually turns a report straight into a regression test.
