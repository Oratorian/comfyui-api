"""Single source of truth for the ComfyUI host.

Resolution order:
  1. Whatever set_comfyui_host() last set (e.g. the `python server.py
     --comfyui-host ...` CLI).
  2. The COMFYUI_HOST environment variable (lets `uvicorn server:app` and
     `COMFYUI_HOST=ip:port uvicorn ...` work without code changes).
  3. The default 127.0.0.1:8188.

All websocket/HTTP calls read get_comfyui_host(), so changing it here changes
the target everywhere.
"""
import os
import sys

DEFAULT_HOST = "127.0.0.1:8188"


def _nuitka_meta():
    """The Nuitka `__compiled__` metadata object if running under Nuitka, else
    None. It is a module-level global in compiled modules and exposes
    `containing_dir` (the folder holding the real .exe) and `onefile`."""
    return globals().get("__compiled__")


def exe_dir() -> str:
    """Folder that actually contains the running .exe (frozen builds only); ""
    when running as plain source.

    IMPORTANT: under Nuitka --onefile, `sys.executable` is the *bootstrap*
    python.exe inside the volatile temp unpack dir (e.g.
    ...\\onefile_1234_xxx\\python.exe) — NOT where the user put the .exe. The
    real location comes from, in order:
      1. __compiled__.containing_dir  (Nuitka's authoritative value), else
      2. dirname(abspath(sys.argv[0])) (the launched .exe path), else
      3. (PyInstaller) sys.frozen -> dirname(sys.executable), which there *is*
         the real exe.
    """
    meta = _nuitka_meta()
    if meta is not None:
        cdir = getattr(meta, "containing_dir", None)
        if cdir:
            return os.path.abspath(cdir)
        # Fallback for older Nuitka without containing_dir: the launched exe.
        argv0 = sys.argv[0] if sys.argv else ""
        if argv0:
            return os.path.dirname(os.path.abspath(argv0))
        return ""
    if getattr(sys, "frozen", False):  # PyInstaller / py2exe
        return os.path.dirname(os.path.abspath(sys.executable))
    return ""


def resolve_data_dir(name: str, source_dir: str) -> str:
    """Locate a runtime data folder (e.g. 'workflows', 'input') for the app.

    - Compiled binary (Nuitka --standalone / --onefile): <exe_dir>/<name>, i.e.
      NEXT TO the real .exe. Nothing is bundled into the binary — workflows are
      environment-specific (they reference the model/checkpoint names installed
      in your ComfyUI), so they must be shipped as a folder beside the exe and
      edited there. Under --onefile this deliberately ignores the volatile temp
      unpack dir (see exe_dir).
    - Normal source run: <source_dir>/<name> (the project folder), where
      source_dir is the caller's os.path.dirname(__file__).
    """
    here = exe_dir()
    return os.path.join(here or source_dir, name)


# Env var so the workflow directory can be set without the CLI flag — e.g.
# `COMFYUI_WORKFLOW_DIR=D:\wf uvicorn server:app`.
WORKFLOW_DIR_ENV = "COMFYUI_WORKFLOW_DIR"


def resolve_workflow_dir(source_dir: str, cli_value: str | None = None) -> str:
    """Resolve the workflows directory. Precedence:

      1. cli_value          (an explicit --workflow-dir, highest priority)
      2. $COMFYUI_WORKFLOW_DIR
      3. resolve_data_dir("workflows", source_dir)   (exe-adjacent / project)

    Returns an absolute path. Does not validate existence — call
    validate_workflow_dir() for that where an error is wanted.
    """
    chosen = cli_value or os.environ.get(WORKFLOW_DIR_ENV) or \
        resolve_data_dir("workflows", source_dir)
    return os.path.abspath(os.path.expanduser(chosen))


def validate_workflow_dir(path: str) -> str | None:
    """Return an error string if `path` is not a usable workflow dir (missing,
    not a directory, or contains no *.json), else None."""
    if not os.path.exists(path):
        return f"workflow dir '{path}' does not exist"
    if not os.path.isdir(path):
        return f"workflow dir '{path}' is not a directory"
    try:
        has_json = any(f.endswith(".json") for f in os.listdir(path))
    except OSError as e:
        return f"workflow dir '{path}' is not readable: {e}"
    if not has_json:
        return f"workflow dir '{path}' contains no .json workflow files"
    return None

# In-process override (set by the CLI in server.py). None -> fall back to env/default.
_override: str | None = None


def set_comfyui_host(host: str | None) -> None:
    """Set the ComfyUI host (e.g. '192.168.1.50:8188'). Accepts a bare host or
    a host:port; also tolerates a leading scheme which it strips."""
    global _override
    if host:
        host = host.strip()
        for scheme in ("http://", "https://", "ws://", "wss://"):
            if host.startswith(scheme):
                host = host[len(scheme):]
        host = host.rstrip("/")
    _override = host or None
    # Mirror into the env so a later-spawned uvicorn worker inherits it.
    if _override:
        os.environ["COMFYUI_HOST"] = _override


def get_comfyui_host() -> str:
    return _override or os.environ.get("COMFYUI_HOST") or DEFAULT_HOST
