# Building standalone binaries (Nuitka)

Compiles `server.py` and `cli.py` into self-contained Windows binaries — no
Python install needed on the target machine. ComfyUI itself must still be
running and reachable (default `127.0.0.1:8188`).

## Prerequisites

- The project venv (`.venv`) with `requirements.txt` installed.
- Nuitka in the venv: `./.venv/Scripts/python.exe -m pip install nuitka ordered-set zstandard`
- A C compiler. Nuitka auto-detects MSVC (Visual Studio Build Tools) via the
  registry; if none is found it offers to download a bundled MinGW64.

## Build

```powershell
.\build_nuitka.ps1                    # both, standalone folders
.\build_nuitka.ps1 -OneFile           # both, single-file exes
.\build_nuitka.ps1 -Target cli        # one target only
.\build_nuitka.ps1 -Target server -OneFile
.\build_nuitka.ps1 -Clean             # wipe dist\ and build\ first
```

In both modes the build also drops a `workflows\` folder next to the produced
exe(s) (seeded from the source `workflows\`, never overwriting an existing one).
Workflows are not compiled in — see "Where workflows\ and input\ live" below.

### Standalone (default) — a folder per binary

Copy the whole `*.dist\` folder to deploy; every file in it is required. The
`workflows\` folder is inside each `*.dist\`.

```
dist\cli.dist\cli.exe         (~46 MB folder, + workflows\)
dist\server.dist\server.exe   (~77 MB folder, + workflows\)
```

### One-file (`-OneFile`) — a single self-contained exe

```
dist\cli.exe      (~14 MB)
dist\server.exe   (~21 MB)
dist\workflows\   (shipped beside the exes, NOT inside them)
```

Each exe is one file you can copy anywhere — but copy the `workflows\` folder
alongside it. On launch the exe unpacks to a temp dir (`%TEMP%\onefile_*`), so
cold start is a bit slower than standalone.

## Where workflows\ and input\ live

**Workflows are NOT bundled into the exe.** They are environment-specific — each
JSON references the exact model/checkpoint names installed in *your* ComfyUI — so
freezing them into the binary would ship defaults that break on another machine.
Instead they live in a `workflows\` folder **next to the .exe**, which you edit
freely. The build seeds that folder from the source `workflows\` (only if it
isn't already there, so a rebuild never clobbers your customized set):

```
dist\
  cli.exe
  server.exe
  workflows\          <- shipped beside the exes; edit/replace these
    anima.json
    illustrious.json
```

At runtime, a compiled binary always resolves `workflows\` and `input\` to the
folder containing the **real** .exe. For `cli.exe` you can also pass
`--workflow <path>` to point anywhere.

> One-file caveat that bit us: under `--onefile`, `sys.executable` is the
> *bootstrap* `python.exe` inside the volatile temp unpack dir
> (`%TEMP%\onefile_*`), **not** where you put the .exe. The real location comes
> from `__compiled__.containing_dir` / `sys.argv[0]`. `api/config.py:exe_dir()`
> uses those, so the lookup correctly lands beside the .exe, not in temp.

`input\` (img2img uploads, server only) is likewise created next to the .exe,
never in the temp unpack dir. (See `api/config.py:resolve_data_dir`.)

Deploy: copy the one-file `.exe` **and** a `workflows\` folder beside it. A bare
exe with no `workflows\` next to it will report no workflows (and `--workflow`
will only accept explicit paths).

### Choosing the workflow directory

Instead of (or in addition to) the `workflows\` folder beside the exe, you can
point at any directory:

- `cli.exe --workflow-dir D:\my\workflows ...`
- `set COMFYUI_WORKFLOW_DIR=D:\my\workflows` (env var; lower priority than the
  flag)

Precedence: `--workflow-dir` flag → `COMFYUI_WORKFLOW_DIR` → `workflows\` beside
the exe. An explicitly-given dir that is missing or has no `*.json` is an error.

`cli.exe --list-workflows [--workflow-dir D:\...]` prints the workflows in the
resolved directory and exits — handy for checking what a deployment will see.

> Note: `--workflow-dir` / `--list-workflows` / `COMFYUI_WORKFLOW_DIR` are on
> `cli.exe` today. The server still reads the `workflows\` folder beside it
> (or its source `workflows\`); a matching `--workflow-dir` for the server is
> not built yet.

## Run

Paths below show the one-file output (`dist\cli.exe`, `dist\server.exe`); for a
standalone build use `dist\cli.dist\cli.exe` / `dist\server.dist\server.exe`.

```powershell
# CLI
dist\cli.exe --prompt "a knight in a volcanic caldera" --out knight.png
dist\cli.exe --prompt "..." --comfyui-host 192.168.1.50:8188 --out shot.png

# Server
dist\server.exe                                  # 127.0.0.1:8000
dist\server.exe --comfyui-host 192.168.1.50:8188 --port 8000
```

## Notes

- **`server.py` was made frozen-aware.** When run normally, `python server.py`
  still launches uvicorn with the `"server:app"` string (and supports `--reload`).
  When compiled, there is no importable `server` module, so it passes the `app`
  object directly to `uvicorn.run(...)`. `--reload` is therefore unavailable in
  the binary (reload needs to re-spawn and re-import by module name) and is
  ignored with a printed note. The `uvicorn server:app` invocation is unchanged
  for source runs.
- `--include-package=uvicorn` (and starlette/anyio/h11/websockets/httptools/
  watchfiles/multipart) is required because uvicorn imports its loop, protocol,
  and lifespan implementations by string at runtime — static analysis misses them.
- `email_validator` is intentionally not bundled (no `EmailStr` fields are used).
