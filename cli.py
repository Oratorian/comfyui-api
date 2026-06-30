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
                        validate_workflow_dir, WORKFLOW_DIR_ENV)
from api.generate import generate
from utils.actions.load_workflow import load_workflow

# Directory this script lives in; used as the source-run fallback for workflows/.
HERE = os.path.dirname(os.path.abspath(__file__))


def _resolve_workflow(name: str, workflow_dir: str) -> str:
    """Accept a bare filename (looked up in workflow_dir) or an explicit path."""
    if os.path.isfile(name):
        return name
    candidate = os.path.join(workflow_dir, name)
    if os.path.isfile(candidate):
        return candidate
    sys.exit(f"error: workflow '{name}' not found (looked in cwd and {workflow_dir})")


def _list_workflows(workflow_dir: str) -> list[str]:
    return sorted(f for f in os.listdir(workflow_dir) if f.endswith(".json")) \
        if os.path.isdir(workflow_dir) else []


def _default_workflow(workflow_dir: str) -> str:
    files = _list_workflows(workflow_dir)
    return files[0] if files else "base_workflow.json"


def main(argv=None):
    # Stage 1: resolve --workflow-dir first (and handle --list-workflows), so the
    # full parser's --workflow default scans the correct directory. add_help=False
    # keeps -h for the real parser below; the known/unknown split lets the rest of
    # the args flow through untouched.
    # allow_abbrev=False is essential: otherwise argparse prefix-matches
    # "--workflow foo" (the full parser's arg) to "--workflow-dir foo" here.
    pre = argparse.ArgumentParser(add_help=False, allow_abbrev=False)
    pre.add_argument("--workflow-dir", default=None, dest="workflow_dir",
                     help="Directory to look up workflow files in.")
    pre.add_argument("--list-workflows", action="store_true", dest="list_workflows",
                     help="List available workflows in the workflow dir and exit.")
    pre_args, _ = pre.parse_known_args(argv)

    workflow_dir = resolve_workflow_dir(HERE, pre_args.workflow_dir)

    # --list-workflows always succeeds for an existing directory: it just shows
    # what's there (including nothing). A genuinely bad path (typo) still errors,
    # but an empty-yet-valid dir prints "(none)" rather than failing — this is the
    # first command a user runs on a fresh download, where workflows/ is empty.
    if pre_args.list_workflows:
        if not os.path.isdir(workflow_dir):
            sys.exit(f"error: workflow dir '{workflow_dir}' does not exist")
        files = _list_workflows(workflow_dir)
        print(f"workflow dir: {workflow_dir}")
        if files:
            for f in files:
                print(f"  {f}")
        else:
            print("  (none)")
        return

    # For a real run, an explicitly-given --workflow-dir must be usable (exist
    # AND contain at least one .json). When NOT explicitly given, an empty
    # default dir is tolerated — you can still pass --workflow as an explicit
    # path.
    if pre_args.workflow_dir is not None:
        err = validate_workflow_dir(workflow_dir)
        if err:
            sys.exit(f"error: {err}")

    p = argparse.ArgumentParser(
        description="Generate image(s) via a running ComfyUI instance (no server).",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("--prompt", required=True, help="Positive prompt.")
    p.add_argument("--negative", default="", help="Negative prompt.")
    p.add_argument("--workflow", default=_default_workflow(workflow_dir),
                   help="Workflow file (name in --workflow-dir, or a path). Must be Export(API) format.")
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
