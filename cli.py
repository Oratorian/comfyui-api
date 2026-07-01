"""Command-line image generation — the library path, no FastAPI server.

Talks directly to a running ComfyUI instance and writes the result to disk.
Reuses api.generate.generate(), so it gets the same prompt/seed/size injection,
bug fixes, and host configuration as the server.

Examples:
  python cli.py --prompt "a knight in a volcanic caldera, dramatic light" \
                --out knight.png

  python cli.py --prompt "..." --negative "lowres, watermark" \
                --width 832 --height 1216 --batch 2 \
                --comfyui-host 192.168.1.50:8188 --out shot.png

  python cli.py --workflow illustrious.json --prompt "..." \
                --img2img ./input/base.png --out edit.png

  # previews + selection are independent, like the API:
  python cli.py --prompt "..." --preview --all --out frame.png   # save every frame
  python cli.py --prompt "..." --preview --index 3 --out f3.png  # collect, save #3 only

A custom workflow must be the Export(API) format (see README) and live in
workflows/ (or pass an absolute/relative path with --workflow).
"""
import argparse
import os
import sys

from api.config import (set_comfyui_host, get_comfyui_host, resolve_workflow_dir,
                        validate_workflow_dir, is_workflow_file, WORKFLOW_DIR_ENV)
from api.generate import generate
from utils.actions.load_workflow import load_workflow

__version__ = "1.0.2"

# Directory this script lives in; used as the source-run fallback for workflows/.
HERE = os.path.dirname(os.path.abspath(__file__))

# Sentinel for the --workflow default: we can't compute the real default until
# the workflow dir is known, and we must avoid scanning the dir eagerly during
# parser construction (that can crash --help on an unreadable dir). Resolved in
# main() after parsing.
_WF_DEFAULT = object()


def _resolve_workflow(name: str, workflow_dir: str) -> str:
    """Resolve a --workflow value to a path.

    A bare filename (no path separator) is looked up in workflow_dir first, so a
    same-named file in the current directory can't silently shadow it. A value
    containing a separator (or that isn't found in workflow_dir) is treated as an
    explicit cwd/relative/absolute path.
    """
    has_sep = os.sep in name or (os.altsep and os.altsep in name)
    if not has_sep:
        candidate = os.path.join(workflow_dir, name)
        if os.path.isfile(candidate):
            return candidate
    if os.path.isfile(name):
        return name
    sys.exit(f"error: workflow '{name}' not found "
             f"(looked in workflow dir {workflow_dir} and as a path from cwd)")


def _list_workflows(workflow_dir: str) -> list[str]:
    """Sorted *.json (case-insensitive) in workflow_dir; [] if missing/unreadable."""
    try:
        return sorted(f for f in os.listdir(workflow_dir) if is_workflow_file(f))
    except OSError:
        return []


def _default_workflow(workflow_dir: str) -> str | None:
    """First workflow in the dir, or None if there are none (no phantom name)."""
    files = _list_workflows(workflow_dir)
    return files[0] if files else None


def main(argv=None):
    # Single parser, single grammar. allow_abbrev=False prevents argparse from
    # prefix-matching "--workflow" onto "--workflow-dir" (and vice versa), so we
    # don't need a two-stage parse — that earlier design let abbreviations be
    # honored by one stage but silently dropped by the other.
    p = argparse.ArgumentParser(
        description="Generate image(s) via a running ComfyUI instance (no server).",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
        allow_abbrev=False,
    )
    p.add_argument("--version", action="version",
                   version=f"comfyui-api cli {__version__}",
                   help="Show the version and exit.")
    p.add_argument("--prompt", help="Positive prompt. (required unless --list-workflows)")
    p.add_argument("--negative", default="", help="Negative prompt.")
    # --workflow's real default depends on --workflow-dir and requires scanning
    # the dir; we must NOT do that during parser construction (it would crash
    # --help on an unreadable dir), so default to a sentinel and resolve later.
    p.add_argument("--workflow", default=_WF_DEFAULT,
                   help="Workflow file (name in --workflow-dir, or a path). "
                        "Must be Export(API) format. (default: first in the workflow dir)")
    p.add_argument("--workflow-dir", default=None, dest="workflow_dir",
                   help=f"Directory to look up workflow files in "
                        f"(default: workflows/ beside the exe; or ${WORKFLOW_DIR_ENV}).")
    p.add_argument("--list-workflows", action="store_true", dest="list_workflows",
                   help="List available workflows in the workflow dir and exit.")
    p.add_argument("--img2img", metavar="IMAGE", default=None,
                   help="Run image-to-image with this input image (workflow needs a LoadImage node).")
    p.add_argument("--width", type=int, default=None, help="Override output width (px).")
    p.add_argument("--height", type=int, default=None, help="Override output height (px).")
    p.add_argument("--batch", type=int, default=None, dest="batch_size",
                   help="Override images per run.")
    p.add_argument("--out", default="output.png",
                   help="Output file. With --all, an index is inserted "
                        "(out.png -> out_0.png, out_1.png, ...).")
    # These three mirror the API and are INDEPENDENT, exactly like the server:
    #   --preview  == API allow_preview (collect intermediate preview frames)
    #   --index    == API ?index        (which image to save; -1 = last/final)
    #   --all      == save every collected image instead of one
    p.add_argument("--preview", action="store_true",
                   help="Collect intermediate preview frames too (API allow_preview). "
                        "On its own it does not save them — combine with --all or --index.")
    p.add_argument("--index", type=int, default=-1,
                   help="Which image to save, in ComfyUI execution order. -1 (default) "
                        "is the LAST/final image. Ignored when --all is set.")
    p.add_argument("--all", action="store_true",
                   help="Save every collected image (with an index suffix), not just one.")
    p.add_argument("--comfyui-host", default=None,
                   help="ComfyUI host as ip:port (default: COMFYUI_HOST env or 127.0.0.1:8188).")
    args = p.parse_args(argv)

    # Resolve the workflow dir from the parsed args (single source of truth):
    # --workflow-dir flag > $COMFYUI_WORKFLOW_DIR > default. `explicit` is True
    # when it came from the flag or env (then it must be a usable dir).
    workflow_dir, explicit = resolve_workflow_dir(HERE, args.workflow_dir)

    # --list-workflows: succeeds for any existing dir (prints "(none)" if empty),
    # the first command a user runs on a fresh download. A bad path still errors.
    if args.list_workflows:
        err = validate_workflow_dir(workflow_dir)
        # Tolerate "empty" only for the implicit default; an explicit bad/empty
        # dir is reported. "does not exist"/"not readable" always error.
        if err and (explicit or "contains no .json" not in err):
            sys.exit(f"error: {err}")
        files = _list_workflows(workflow_dir)
        print(f"workflow dir: {workflow_dir}")
        for f in files:
            print(f"  {f}")
        if not files:
            print("  (none)")
        return

    # A real run needs a prompt.
    if not args.prompt:
        p.error("the following arguments are required: --prompt")

    # An explicitly-configured workflow dir (flag OR env) must be usable.
    if explicit:
        err = validate_workflow_dir(workflow_dir)
        if err:
            sys.exit(f"error: {err}")

    # Resolve the --workflow default now that the dir is known.
    if args.workflow is _WF_DEFAULT:
        default_wf = _default_workflow(workflow_dir)
        if default_wf is None:
            sys.exit(f"error: no workflows found in {workflow_dir} — add a "
                     f"workflow JSON there, or pass --workflow PATH "
                     f"(or --workflow-dir).")
        args.workflow = default_wf

    set_comfyui_host(args.comfyui_host)
    print(f"ComfyUI target: {get_comfyui_host()}")

    workflow_path = _resolve_workflow(args.workflow, workflow_dir)
    workflow = load_workflow(workflow_path)
    if workflow is None:
        sys.exit(f"error: could not load workflow '{workflow_path}'")

    if args.img2img and not os.path.isfile(args.img2img):
        sys.exit(f"error: --img2img image '{args.img2img}' not found")

    def on_progress(ev):
        if ev.get("phase") == "sampling":
            print(f"\rsampling step {ev['step']}/{ev['max']}", end="", flush=True)

    print(f"workflow: {os.path.basename(workflow_path)}"
          + (f"  (img2img: {os.path.basename(args.img2img)})" if args.img2img else ""))
    images = generate(
        workflow, args.prompt, args.negative,
        input_image_path=args.img2img,
        allow_preview=args.preview,        # independent: collect previews (API allow_preview)
        width=args.width, height=args.height, batch_size=args.batch_size,
        progress_cb=on_progress,
    )
    print()  # newline after the progress line

    if not images:
        sys.exit("error: ComfyUI produced no images (does the workflow have a "
                 "SaveImage or PreviewImage node?)")

    # Selection mirrors the API: --all saves everything; otherwise --index picks
    # one (default -1 = last/final), with Python-style negative indexing.
    if args.all:
        to_save = images
    else:
        n = len(images)
        if args.index < -n or args.index >= n:
            sys.exit(f"error: --index {args.index} out of range (valid: -{n}..{n - 1})")
        to_save = [images[args.index]]

    base, ext = os.path.splitext(args.out)
    ext = ext or ".png"

    saved = []
    for i, img in enumerate(to_save):
        path = args.out if len(to_save) == 1 else f"{base}_{i}{ext}"
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        with open(path, "wb") as f:
            f.write(img["image_data"])
        saved.append(path)

    print(f"saved {len(saved)} image(s): {', '.join(saved)}")


if __name__ == "__main__":
    main()
