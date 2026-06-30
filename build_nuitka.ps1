<#
.SYNOPSIS
  Build standalone Nuitka binaries for the ComfyUI API (server.py + cli.py).

.DESCRIPTION
  Produces two standalone folders under .\dist :
      dist\server.dist\server.exe   (the FastAPI/uvicorn HTTP server)
      dist\cli.dist\cli.exe         (the command-line generator)

  Each folder is self-contained (Python runtime + all deps + workflows\ bundled
  in). Copy the whole *.dist folder to the target machine; no Python required.

  ComfyUI itself still has to be running and reachable (default 127.0.0.1:8188,
  override with --comfyui-host or the COMFYUI_HOST env var) — these binaries are
  only the thin API/CLI layer.

.EXAMPLE
  .\build_nuitka.ps1                # build both
  .\build_nuitka.ps1 -Target server # build only the server
  .\build_nuitka.ps1 -Target cli    # build only the cli
  .\build_nuitka.ps1 -Clean         # remove build artifacts first
#>
[CmdletBinding()]
param(
    [ValidateSet('both', 'server', 'cli')]
    [string]$Target = 'both',
    [switch]$OneFile,   # produce a single self-contained .exe instead of a folder
    [switch]$Clean
)

$ErrorActionPreference = 'Stop'
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $here

# Use the project venv's Python so Nuitka sees the right interpreter + packages.
$py = Join-Path $here '.venv\Scripts\python.exe'
if (-not (Test-Path $py)) { throw "venv python not found at $py — create .venv and pip install -r requirements.txt first." }

$dist  = Join-Path $here 'dist'
$build = Join-Path $here 'build'

if ($Clean) {
    Write-Host '== Cleaning dist\ and build\ ==' -ForegroundColor Yellow
    foreach ($d in @($dist, $build)) { if (Test-Path $d) { Remove-Item $d -Recurse -Force } }
}

# Flags shared by both targets.
#   --include-package=uvicorn : uvicorn loads its loop/protocol/lifespan impls by
#       string at runtime, so they must be force-included (server only, but cheap).
#   --include-data-dir=workflows : both scripts os.listdir() workflows\ at runtime
#       relative to the executable, so the JSONs must ship alongside the binary.
# --onefile implies --standalone and emits a single packed .exe. The exe unpacks
# to a temp dir at launch; the app resolves workflows\/input\ next to the REAL
# .exe (api.config.exe_dir / resolve_data_dir).
#
# Workflows are NOT bundled: they are environment-specific (they reference the
# exact model/checkpoint names installed in *your* ComfyUI), so freezing them in
# would ship broken defaults. Instead, ship a `workflows\` folder NEXT TO the exe
# (see the post-build copy below) and edit/replace those JSONs freely.
$mode = if ($OneFile) { '--onefile' } else { '--standalone' }
$common = @(
    '-m', 'nuitka',
    $mode,
    '--assume-yes-for-downloads',
    "--output-dir=$dist",
    '--include-package=api',
    '--include-package=utils',
    '--company-name=comfyui-api',
    '--product-version=1.0.0',  # required by Nuitka whenever product/company info is set
    '--show-progress'
    # NOTE: --remove-output is intentionally omitted. On Windows it makes Nuitka
    # try to delete the intermediate .build/.dist folders immediately after the
    # build, which Defender often still has open -> a FATAL "Failed to delete"
    # that fails an otherwise-successful build. We clean those up ourselves below
    # (best-effort, never fatal) after the artifact exists.
)

# Server needs the full ASGI/uvicorn runtime stack; many of these submodules are
# imported via strings and would otherwise be missed by static analysis.
$serverExtra = @(
    '--include-package=uvicorn',
    '--include-package=fastapi',
    '--include-package=starlette',
    '--include-package=anyio',
    '--include-package=h11',
    '--include-package=websockets',
    '--include-package=httptools',
    '--include-package=watchfiles',
    '--include-package=multipart',          # python-multipart imports as `multipart`
    '--product-name=ComfyUI API Server'
)

# CLI is lean: api/utils + Pillow + requests_toolbelt + websocket-client.
$cliExtra = @(
    '--include-package=PIL',
    '--include-package=requests_toolbelt',
    '--include-package=websocket',
    '--product-name=ComfyUI API CLI'
)

function Remove-IfPossible {
    # Best-effort recursive delete that tolerates AV/indexer locks on freshly
    # built binaries. Retries briefly, then gives up without failing the build.
    param([string]$Path)
    if (-not (Test-Path $Path)) { return }
    foreach ($i in 1..5) {
        try { Remove-Item $Path -Recurse -Force -ErrorAction Stop; return }
        catch { Start-Sleep -Milliseconds 600 }
    }
    Write-Host "  (note: couldn't remove intermediate '$Path' — it's locked, e.g. by Defender; safe to delete later)" -ForegroundColor DarkYellow
}

function Build-Target {
    param([string]$Script, [string[]]$Extra)
    $name = [IO.Path]::GetFileNameWithoutExtension($Script)
    Write-Host "== Building $Script ==" -ForegroundColor Cyan
    $args = $common + $Extra + @($Script)
    & $py @args

    # Expected artifact: onefile -> dist\<name>.exe ; standalone -> dist\<name>.dist\<name>.exe
    $artifact = if ($OneFile) { Join-Path $dist "$name.exe" } else { Join-Path $dist "$name.dist\$name.exe" }
    if (-not (Test-Path $artifact)) {
        throw "Nuitka build for $Script did not produce '$artifact' (exit $LASTEXITCODE)."
    }
    # Clean intermediates ourselves so an AV lock can't fail a good build.
    Remove-IfPossible (Join-Path $dist "$name.build")
    Remove-IfPossible (Join-Path $dist "$name.onefile-build")
    if ($OneFile) { Remove-IfPossible (Join-Path $dist "$name.dist") }  # onefile leaves a staging .dist

    # Ship workflows NEXT TO the exe (not bundled). Seed the folder from the
    # source workflows\ only if it doesn't already exist there, so a customized
    # set placed beside the exe is never clobbered by a rebuild.
    $exeFolder = Split-Path -Parent $artifact     # onefile: dist\ ; standalone: dist\<name>.dist\
    $destWf = Join-Path $exeFolder 'workflows'
    $srcWf  = Join-Path $here 'workflows'
    if ((Test-Path $srcWf) -and -not (Test-Path $destWf)) {
        Copy-Item $srcWf $destWf -Recurse
        Write-Host "  seeded workflows\ -> $destWf" -ForegroundColor DarkGray
    } elseif (Test-Path $destWf) {
        Write-Host "  workflows\ already present at $destWf (left as-is)" -ForegroundColor DarkGray
    }
}

if ($Target -in 'both', 'cli')    { Build-Target -Script 'cli.py'    -Extra $cliExtra }
if ($Target -in 'both', 'server') { Build-Target -Script 'server.py' -Extra $serverExtra }

Write-Host ''
Write-Host '== Done ==' -ForegroundColor Green
# onefile -> dist\<name>.exe ; standalone -> dist\<name>.dist\<name>.exe
foreach ($n in 'cli', 'server') {
    $one = Join-Path $dist "$n.exe"
    $std = Join-Path $dist "$n.dist\$n.exe"
    if     (Test-Path $one) { Write-Host ("  {0,-7}-> dist\{1}.exe (onefile)" -f $n, $n) }
    elseif (Test-Path $std) { Write-Host ("  {0,-7}-> dist\{1}.dist\{1}.exe" -f $n, $n) }
}
