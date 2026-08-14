# requirements/ - pinned dependency graph

`lock-py311.txt` pins the **full transitive graph** of `mcportal[dev,mcp]`
(`dev` is a superset of `mcp`), with `--generate-hashes` so every artifact is
verified on install. The runtime declaration in `pyproject.toml` is unchanged
and stays `httpx` only: this lockfile is a *reproduction aid*, not a dependency.
The lock tool (uv) is a one-off developer tool and is never installed at
runtime.

**Since 2026-08-09 the file is produced by `uv pip compile --universal`**, not
by pip-tools. The platform-marker section below records why: a Windows-resolved
pip-tools lock both pinned `pywin32` unconditionally (breaks ubuntu) *and*
omitted linux-only transitive dependencies entirely (`secretstorage`,
`jeepney` via `keyring` - second CI run failed on exactly that). Universal
resolution keeps every environment marker in one file; pip's hash-checking
install mode accepts it as-is (verified with a clean-venv `--dry-run` on
Windows and by the CI ubuntu matrix).

All comments in this directory are ASCII on purpose. Files under
`requirements/` are read by pip, which decodes them with the locale codec on
Windows (cp949 here); a single non-ASCII byte makes `pip install -r` die with a
`UnicodeDecodeError` before it reads a single pin.

## Regenerate

Run from the repository root, with a Python 3.11 interpreter:

    python -m pip install uv
    python -m uv pip compile pyproject.toml \
        --extra dev --extra mcp \
        --universal \
        --generate-hashes \
        -o requirements/lock-py311.txt

`--universal` is the point of using uv here: it resolves for *all* platforms
and writes environment markers (`; sys_platform == 'win32'` etc.) instead of
baking in the resolving machine's answers. uv emits LF and strips extras by
default, so no post-processing is needed.

Historical note (pip-tools, used for the first cut of this file): it resolves
for the running platform only, which is unusable for a cross-OS CI matrix -
see the platform-marker section below for the measured failures.

## How to install from it

Use the lockfile as a **requirements** file, not a constraints file:

    python -m pip install -r requirements/lock-py311.txt
    python -m pip install -e . --no-deps

The second line needs `--no-deps` because the first already installed the whole
graph at pinned, hash-verified versions.

### Why not `pip install -e .[dev] -c requirements/lock-py311.txt`

That form cannot work with a hashed lockfile, and the failure is not
configuration-dependent. Hashes in a constraints file put pip into
hash-checking mode, and in that mode a local project directory has nothing to
hash. Both variants were measured against this lockfile (pip 26.1.2):

| Command | Result |
|---|---|
| `pip install -e .[dev] -c lock-py311.txt` | `ERROR: The editable requirement ... cannot be installed when requiring hashes, because there is no single file to hash` |
| `pip install .[dev] -c lock-py311.txt` | `ERROR: Can't verify hashes for these file:// requirements because they point to directories` |
| `pip install -r lock-py311.txt` | works |

The editable row was re-measured on 2026-08-15 against pip 24.0 and pip 26.2.1
and produced the identical error on both, so it is not an artefact of one pip
version. `CONTRIBUTING.md` gives the two-step form as the development install
for exactly this reason.

If a constraints-style install is genuinely wanted, regenerate a second
lockfile without `--generate-hashes`; a hashed lockfile and `-c` are mutually
exclusive by design.

## Platform markers: the defect this file used to have (fixed 2026-08-09)

This defect is fixed and the section is written in the past tense on purpose:
the current lockfile is a `uv pip compile --universal` output and carries
markers for every platform. It is kept because the failure is one regeneration
away - regenerate this file without `--universal` and all of it comes back.

**What went wrong.** pip-compile resolves for the interpreter and platform it
runs on, and it *drops environment markers* for dependencies that are
conditional upstream. `mcp` declares:

    pywin32>=310; sys_platform == 'win32' and python_version < '3.14'

and the first, Windows-resolved cut of this file pinned it unconditionally as
`pywin32==312`, with no marker, so `pip install -r requirements/lock-py311.txt`
**failed on Linux and macOS**: pywin32 publishes no distribution for those
platforms.

**What the file looks like now.** Every platform-conditional pin carries its
marker:

    colorama==0.4.6 ; sys_platform == 'win32'
    pywin32==312 ; sys_platform == 'win32'
    pywin32-ctypes==0.2.3 ; sys_platform == 'win32'
    tzdata==2026.3 ; sys_platform == 'win32'

The linux-only half of the graph, which a Windows resolution had omitted
outright, is present too:

    jeepney==0.9.0 ; sys_platform == 'linux'
    secretstorage==3.5.0 ; sys_platform == 'linux'

**How it got fixed.** Two remedies were considered and *neither* is what the
file uses today:

1. One lockfile per platform, generated on that platform
   (`lock-linux-py311.txt`, `lock-win-py311.txt`), selected by runner OS.
2. A single file with the `pywin32` marker re-attached by hand after every
   regeneration. Not reproducible from the regeneration command, so it would
   have needed a check that fails the build when the marker goes missing.

Remedy 2 was actually applied, and it was not enough. The first CI run had
failed on ubuntu exactly as described above; after the hand-attached marker,
the second CI run failed on the *other* half of the same defect - a
Windows-resolved lock omits linux-only transitive dependencies entirely
(`secretstorage`/`jeepney` via `keyring`), which pip's hash-checking mode
rejects as unpinned. The durable fix was neither remedy but a resolver change:
`uv pip compile --universal` (see Regenerate above), which emits every
platform's pins with markers in one file.

pip-tools 7.x has no universal-resolution mode that would have avoided the
marker loss; that capability exists only in other resolvers, which is why the
fix had to be a resolver swap rather than a flag.

The guard is still the CI matrix itself - a regeneration that loses markers or
drops a platform's pins turns the ubuntu jobs red on the next push. There is no
separate marker lint.

## Scope note

The pins are exact by intent, including `fastmcp==2.14.7`. See the
`[project.optional-dependencies]` comment in `pyproject.toml` for why the
compatibility ceiling is `<4` rather than the `<3` claimed before W4, and why
this release still pins a single measured version instead of a range.
