"""FastAPI server over the ComfyUI wrapper.

Async job model (image gen is slow, so requests never block):
  POST /generate            -> {job_id}        (txt2img)
  POST /generate/img2img    -> {job_id}        (multipart: image + fields)
  GET  /jobs/{job_id}       -> status + progress + image filenames
  GET  /jobs/{job_id}/image -> the PNG bytes (first output) when done
  GET  /workflows           -> available workflow files
  GET  /healthz             -> liveness

Interactive API docs:  /docs  (Swagger UI)  and  /redoc  (ReDoc)

Run:  uvicorn server:app --host 127.0.0.1 --port 8000
(ComfyUI must be running on 127.0.0.1:8188 — see api/open_websocket.py)
"""
import json
import os
import sys
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from enum import Enum
from typing import Optional

from fastapi import FastAPI, HTTPException, UploadFile, File, Form, Path, Query
from fastapi.responses import Response, HTMLResponse
from fastapi.openapi.docs import get_redoc_html
from pydantic import BaseModel, Field

from api.config import get_comfyui_host, resolve_data_dir

# FastAPI ships ReDoc from the `redoc@next` CDN tag, which periodically publishes
# builds that render as a blank white page. Pin a known-good stable bundle.
REDOC_JS = "https://cdn.jsdelivr.net/npm/redoc@2.1.5/bundles/redoc.standalone.js"

# --------------------------------------------------------------------------- #
# App metadata — populates /docs and /redoc headers and the OpenAPI schema.
# --------------------------------------------------------------------------- #
tags_metadata = [
    {"name": "generation", "description": "Submit text-to-image and image-to-image jobs."},
    {"name": "jobs", "description": "Poll job status/progress and retrieve the resulting PNG."},
    {"name": "meta", "description": "Service health and available workflows."},
]

app = FastAPI(
    title="ComfyUI API",
    version="1.0.0",
    summary="Async HTTP API over a headless ComfyUI instance.",
    description=(
        "Thin FastAPI layer over a running ComfyUI server. Image generation is "
        "asynchronous: submit a job, then poll `/jobs/{job_id}` for progress and "
        "fetch the PNG from `/jobs/{job_id}/image` once `status == \"done\"`.\n\n"
        f"**Prerequisite:** ComfyUI must be reachable at `{get_comfyui_host()}` "
        "(set with `--comfyui-host` or the `COMFYUI_HOST` env var)."
    ),
    contact={"name": "ComfyUI API", "url": "http://127.0.0.1:8000/docs"},
    license_info={"name": "MIT"},
    openapi_tags=tags_metadata,
    redoc_url=None,  # replaced by a pinned-version /redoc route below
)

from api.generate import generate  # noqa: E402  (after app so import errors surface clearly)

# workflows/ and input/ live NEXT TO the .exe in a compiled build (nothing is
# bundled in — workflows are ComfyUI-environment-specific), or next to this
# source file when run from source. See api.config.resolve_data_dir.
_HERE = os.path.dirname(os.path.abspath(__file__))
WORKFLOW_DIR = resolve_data_dir("workflows", _HERE)
INPUT_DIR = resolve_data_dir("input", _HERE)


def _pick_default_workflow() -> str:
    """First *.json in workflows/ (alphabetical), so the default tracks whatever
    workflow is actually present instead of a hardcoded filename."""
    try:
        files = sorted(f for f in os.listdir(WORKFLOW_DIR) if f.endswith(".json"))
        return files[0] if files else "base_workflow.json"
    except FileNotFoundError:
        return "base_workflow.json"


DEFAULT_WORKFLOW = _pick_default_workflow()

_executor = ThreadPoolExecutor(max_workers=1)  # ComfyUI runs one prompt at a time
_jobs: dict[str, dict] = {}
_jobs_lock = threading.Lock()


# --------------------------------------------------------------------------- #
# Schemas (drive the OpenAPI models shown in /docs and /redoc)
# --------------------------------------------------------------------------- #
class JobState(str, Enum):
    queued = "queued"
    running = "running"
    done = "done"
    error = "error"


class GenerateRequest(BaseModel):
    prompt: str = Field(..., description="Positive prompt.",
                        examples=["a knight in a volcanic caldera, dramatic light, high detail"])
    negative_prompt: str = Field("", description="Negative prompt (ignored by workflows whose "
                                                 "negative is a ConditioningZeroOut node).",
                                 examples=["lowres, text, watermark, blurry"])
    workflow: str = Field(DEFAULT_WORKFLOW, description="Workflow filename in workflows/.",
                          examples=[DEFAULT_WORKFLOW])
    width: Optional[int] = Field(None, ge=64, description="Override output width (px). "
                                 "Null uses the workflow's own size.", examples=[1024])
    height: Optional[int] = Field(None, ge=64, description="Override output height (px). "
                                  "Null uses the workflow's own size.", examples=[1024])
    batch_size: Optional[int] = Field(None, ge=1, description="Override number of images per run. "
                                      "Null uses the workflow's own batch size.", examples=[1])
    allow_preview: bool = Field(False, description="Also collect intermediate preview frames.")


class JobAccepted(BaseModel):
    """Returned when a job is accepted (HTTP 202). Poll /jobs/{job_id} next."""
    job_id: str = Field(..., description="Use to poll status and fetch the image.",
                        examples=["3f2a1c9e8b7d4a6f"])
    status: JobState = Field(JobState.queued, description="Always 'queued' on submit.")


class ProgressInfo(BaseModel):
    """Latest progress event. During sampling, `phase="sampling"` with
    step/max/fraction; between nodes, `phase="nodes"` with done/total."""
    phase: Optional[str] = Field(None, description="'sampling' or 'nodes'.", examples=["sampling"])
    step: Optional[int] = Field(None, description="Current KSampler step (sampling phase).", examples=[12])
    max: Optional[int] = Field(None, description="Total KSampler steps (sampling phase).", examples=[20])
    fraction: Optional[float] = Field(None, description="step/max, 0..1 (sampling phase).", examples=[0.6])
    done: Optional[int] = Field(None, description="Nodes completed (nodes phase).")
    total: Optional[int] = Field(None, description="Total nodes in the graph (nodes phase).")


class ImageRef(BaseModel):
    """A produced image, as listed in ComfyUI execution order."""
    file_name: str = Field(..., description="ComfyUI's filename for the image.", examples=["ComfyUI_00123_.png"])
    type: str = Field(..., description="'output' (SaveImage) or 'temp' (PreviewImage).", examples=["output"])


class JobStatus(BaseModel):
    """Current state of a generation job."""
    status: JobState = Field(..., description="queued → running → done | error.")
    progress: Optional[ProgressInfo] = Field(None, description="Latest progress event (null until running).")
    images: list[ImageRef] = Field([], description="Output images once done, in execution order. "
                                                   "Fetch bytes via /jobs/{id}/image.")
    error: Optional[str] = Field(None, description="Error message if status == 'error'.")


class WorkflowList(BaseModel):
    workflows: list[str] = Field(..., description="Available workflow filenames in workflows/.",
                                 examples=[[DEFAULT_WORKFLOW]])
    default: str = Field(..., description="Workflow used when none is specified.", examples=[DEFAULT_WORKFLOW])


class Health(BaseModel):
    ok: bool = Field(True, description="True when the service is up.")


class Config(BaseModel):
    comfyui_host: str = Field(..., description="The ComfyUI host this API targets.",
                              examples=["127.0.0.1:8188"])


# --------------------------------------------------------------------------- #
# Internals
# --------------------------------------------------------------------------- #
def _load_workflow(name: str) -> dict:
    safe = os.path.basename(name)  # prevent path traversal
    path = os.path.join(WORKFLOW_DIR, safe)
    if not os.path.isfile(path):
        raise HTTPException(404, f"workflow '{safe}' not found in workflows/")
    with open(path, "r", encoding="utf-8") as f:
        wf = json.load(f)

    # Reject UI-format exports with a clear message instead of a deep KeyError.
    # API format is a flat {node_id: {"class_type", "inputs"}} map; the normal
    # "Export" produces a UI graph with top-level "nodes"/"links" arrays.
    if isinstance(wf, dict) and ("nodes" in wf or "links" in wf):
        raise HTTPException(
            422,
            f"workflow '{safe}' looks like a normal ComfyUI Export (UI graph). "
            f"Re-export it with **Export (API)** / Save (API Format) and replace "
            f"the file.",
        )
    if not isinstance(wf, dict) or not all(
        isinstance(v, dict) and "class_type" in v for v in wf.values()
    ):
        raise HTTPException(
            422,
            f"workflow '{safe}' is not a valid API-format ComfyUI workflow "
            f"(expected a map of node_id -> {{class_type, inputs}}). Use Export (API).",
        )
    return wf


def _new_job() -> str:
    job_id = uuid.uuid4().hex
    with _jobs_lock:
        _jobs[job_id] = {"status": "queued", "progress": None, "images": [], "error": None}
    return job_id


def _update(job_id: str, **fields):
    with _jobs_lock:
        if job_id in _jobs:
            _jobs[job_id].update(fields)


def _run_job(job_id, workflow, prompt, negative, input_path, allow_preview,
             width=None, height=None, batch_size=None):
    _update(job_id, status="running")
    try:
        images = generate(
            workflow, prompt, negative,
            input_image_path=input_path,
            allow_preview=allow_preview,
            width=width, height=height, batch_size=batch_size,
            progress_cb=lambda p: _update(job_id, progress=p),
        )
        with _jobs_lock:
            _jobs[job_id]["_bytes"] = images
            _jobs[job_id]["images"] = [
                {"file_name": im["file_name"], "type": im["type"]} for im in images
            ]
            _jobs[job_id]["status"] = "done"
    except Exception as e:
        _update(job_id, status="error", error=f"{type(e).__name__}: {e}")
    finally:
        if input_path and os.path.exists(input_path):
            try:
                os.remove(input_path)
            except OSError:
                pass


# --------------------------------------------------------------------------- #
# Endpoints
# --------------------------------------------------------------------------- #
@app.get("/redoc", include_in_schema=False)
def redoc_html():
    # Custom ReDoc page pinned to a stable redoc bundle (FastAPI's default uses
    # `redoc@next`, which can render a blank page).
    return get_redoc_html(
        openapi_url=app.openapi_url,
        title=app.title + " — ReDoc",
        redoc_js_url=REDOC_JS,
    )


@app.get("/healthz", response_model=Health, tags=["meta"], summary="Liveness check")
def healthz():
    return Health(ok=True)


@app.get("/config", response_model=Config, tags=["meta"],
         summary="Show the active ComfyUI host this API targets")
def config():
    return Config(comfyui_host=get_comfyui_host())


@app.get("/workflows", response_model=WorkflowList, tags=["meta"],
         summary="List available workflow files")
def list_workflows():
    files = [f for f in os.listdir(WORKFLOW_DIR) if f.endswith(".json")]
    return WorkflowList(workflows=files, default=DEFAULT_WORKFLOW)


@app.post("/generate", response_model=JobAccepted, status_code=202, tags=["generation"],
          summary="Submit a text-to-image job",
          responses={404: {"description": "Workflow file not found"}})
def generate_txt2img(req: GenerateRequest):
    workflow = _load_workflow(req.workflow)
    job_id = _new_job()
    _executor.submit(_run_job, job_id, workflow, req.prompt, req.negative_prompt,
                     None, req.allow_preview,
                     req.width, req.height, req.batch_size)
    return JobAccepted(job_id=job_id, status=JobState.queued)


@app.post("/generate/img2img", response_model=JobAccepted, status_code=202, tags=["generation"],
          summary="Submit an image-to-image job",
          responses={404: {"description": "Workflow file not found"}})
async def generate_img2img(
    image: UploadFile = File(..., description="Input image (PNG)."),
    prompt: str = Form(..., description="Positive prompt.",
                       examples=["repaint as a stormy night scene, cinematic"]),
    negative_prompt: str = Form("", description="Negative prompt."),
    workflow: str = Form(DEFAULT_WORKFLOW, description="Workflow filename in workflows/."),
    width: Optional[int] = Form(None, ge=64, description="Override output width (px). "
                                "Null uses the workflow's own size."),
    height: Optional[int] = Form(None, ge=64, description="Override output height (px). "
                                 "Null uses the workflow's own size."),
    batch_size: Optional[int] = Form(None, ge=1, description="Override images per run. "
                                     "Null uses the workflow's own batch size."),
    allow_preview: bool = Form(False, description="Also collect intermediate preview frames."),
):
    wf = _load_workflow(workflow)
    os.makedirs(INPUT_DIR, exist_ok=True)
    safe_name = f"{uuid.uuid4().hex}_{os.path.basename(image.filename or 'upload.png')}"
    dest = os.path.join(INPUT_DIR, safe_name)
    with open(dest, "wb") as f:
        f.write(await image.read())

    job_id = _new_job()
    _executor.submit(_run_job, job_id, wf, prompt, negative_prompt, dest, allow_preview,
                     width, height, batch_size)
    return JobAccepted(job_id=job_id, status=JobState.queued)


@app.get("/jobs/{job_id}", response_model=JobStatus, tags=["jobs"],
         summary="Get job status and progress",
         responses={404: {"description": "Unknown job_id"}})
def job_status(job_id: str = Path(..., description="Job id returned by a /generate call.")):
    with _jobs_lock:
        job = _jobs.get(job_id)
        if not job:
            raise HTTPException(404, "unknown job_id")
        return {k: v for k, v in job.items() if k != "_bytes"}


@app.get("/jobs/{job_id}/image", tags=["jobs"], summary="Download a result image (PNG)",
         response_class=Response,
         responses={
             200: {"content": {"image/png": {}}, "description": "The generated PNG."},
             404: {"description": "Unknown job, no images, or index out of range"},
             409: {"description": "Job not finished yet"},
         })
def job_image(
    job_id: str = Path(..., description="Job id."),
    index: int = Query(
        -1,
        description="Which image, in ComfyUI execution order. Negative indexes from "
                    "the end: -1 (default) is the LAST image produced — i.e. the final "
                    "result after all detailer/upscale passes, whether it's a SaveImage "
                    "or a PreviewImage. Use 0 for the first, or a specific positive index.",
    ),
):
    with _jobs_lock:
        job = _jobs.get(job_id)
        if not job:
            raise HTTPException(404, "unknown job_id")
        if job["status"] != "done":
            raise HTTPException(409, f"job not finished (status: {job['status']})")
        imgs = job.get("_bytes", [])
        if not imgs:
            raise HTTPException(404, "job produced no images")
        n = len(imgs)
        # accept Python-style negative indexing (-1 = last)
        if index < -n or index >= n:
            raise HTTPException(404, f"image index {index} out of range "
                                     f"(valid: -{n}..{n - 1})")
        data = imgs[index]
    return Response(content=data["image_data"], media_type="image/png")


# --------------------------------------------------------------------------- #
# CLI entry point — `python server.py [--comfyui-host ip:port] [--host] [--port]`
#
# The ComfyUI host also works under `uvicorn server:app` via the COMFYUI_HOST
# env var (e.g. `COMFYUI_HOST=192.168.1.50:8188 uvicorn server:app`), because
# api.config reads that variable. The CLI below just sets it before launching.
# --------------------------------------------------------------------------- #
if __name__ == "__main__":
    import argparse
    import uvicorn
    from api.config import set_comfyui_host, get_comfyui_host

    parser = argparse.ArgumentParser(description="Run the ComfyUI API server.")
    parser.add_argument("--comfyui-host", default=None,
                        help="ComfyUI host as ip:port (default: COMFYUI_HOST env or 127.0.0.1:8188).")
    parser.add_argument("--host", default="127.0.0.1", help="Bind address for THIS API (default 127.0.0.1).")
    parser.add_argument("--port", type=int, default=8000, help="Port for THIS API (default 8000).")
    parser.add_argument("--reload", action="store_true", help="Auto-reload on code changes (dev).")
    args = parser.parse_args()

    # Setting the host also writes COMFYUI_HOST into the environment, so a
    # reload subprocess (which re-imports this module fresh) still sees it.
    set_comfyui_host(args.comfyui_host)
    print(f"ComfyUI target: {get_comfyui_host()}")

    # In a Nuitka/PyInstaller binary there is no importable "server" module, so
    # the "server:app" string-import form (needed for --reload) cannot work.
    # Pass the app object directly and force reload off when frozen.
    frozen = getattr(sys, "frozen", False) or "__compiled__" in globals()
    if args.reload and not frozen:
        uvicorn.run("server:app", host=args.host, port=args.port, reload=True)
    else:
        if args.reload and frozen:
            print("note: --reload is unavailable in the compiled build; ignoring it.")
        uvicorn.run(app, host=args.host, port=args.port)

