# requirements/ - pinned dependency graph

`lock-py311.txt` pins the **full transitive graph** of `mcportal[dev,mcp]`
(`dev` is a superset of `mcp`), with `--generate-hashes` so every artifact is
verified on install. The runtime declaration in `pyproject.toml` is unchanged
and stays `httpx` only: this lockfile is a *reproduction aid*, not a dependency.
pip-tools itself is a one-off developer tool and is never installed at runtime.

All comments in this directory are ASCII on purpose. Files under
`requirements/` are read by pip, which decodes them with the locale codec on
Windows (cp949 here); a single non-ASCII byte makes `pip install -r` die with a
`UnicodeDecodeError` before it reads a single pin.

## Regenerate

Run from the repository root, with a Python 3.11 interpreter:

    python -m pip install pip-tools
    python -m piptools compile \
        --extra dev --extra mcp \
        --strip-extras \
        --generate-hashes \
        --output-file requirements/lock-py311.txt \
        pyproject.toml

Then normalize the output to LF endings with exactly one trailing newline
(pip-compile emits CRLF on Windows; the repo is LF via `.gitattributes`).

`--strip-extras` is required, not cosmetic. Without it the output contains
`pydantic[email]==...` and pip refuses the file as a constraints file with
`ERROR: Constraints cannot have extras`. It is also the announced future
default of pip-tools. Nothing is lost: the extra's own dependencies
(`email-validator`, ...) are pinned as first-class entries in the same file.

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

If a constraints-style install is genuinely wanted, regenerate a second
lockfile without `--generate-hashes`; a hashed lockfile and `-c` are mutually
exclusive by design.

## Known limitation: this lockfile is resolved on Windows

pip-compile resolves for the interpreter and platform it runs on, and it
**drops environment markers** for dependencies that are conditional upstream.
Concretely, `mcp` declares:

    pywin32>=310; sys_platform == 'win32' and python_version < '3.14'

but the lockfile pins it unconditionally as `pywin32==312`, with no marker.
`pip install -r requirements/lock-py311.txt` therefore **fails on Linux and
macOS**: pywin32 publishes no distribution for those platforms.

This matters for any CI matrix that spans operating systems. Two workable
remedies, in order of preference:

1. Generate one lockfile per platform on that platform
   (`lock-linux-py311.txt`, `lock-win-py311.txt`) and select by runner OS.
2. Keep a single file and re-attach the marker to the `pywin32` block by hand
   after every regeneration, i.e. `pywin32==312 ; sys_platform == "win32"`.
   This is not reproducible from the command above, so it needs a check that
   fails the build when the marker goes missing.

**Applied: remedy 2, on 2026-08-09** (the first CI run failed on ubuntu
exactly as predicted above). The marker is hand-attached in
`lock-py311.txt`; the failing check is the CI matrix itself -- an ubuntu
job cannot install an unconditional `pywin32` pin, so a regeneration that
drops the marker turns the whole `test` job red on the next push.

pip-tools 7.x has no universal-resolution mode that would avoid this; that
capability exists only in other resolvers.

## Scope note

The pins are exact by intent, including `fastmcp==2.14.7`. See the
`[project.optional-dependencies]` comment in `pyproject.toml` for why the
compatibility ceiling is `<4` rather than the `<3` claimed before W4, and why
this release still pins a single measured version instead of a range.
