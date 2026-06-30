# comfyui-api

A thin Python wrapper over a running ComfyUI instance, plus a **FastAPI** HTTP
server on top of it. Use it from Python, as an HTTP API, via a CLI, or as
**prebuilt Windows binaries** — no Python install required.

## Prebuilt binaries (Windows)

Each tagged release ships standalone one-file executables, compiled with
[Nuitka](https://nuitka.net/) by GitHub Actions (see
[`.github/workflows/build.yml`](.github/workflows/build.yml)):

- **`cli.exe`** — the command-line generator
- **`server.exe`** — the FastAPI server

Download the zip for the target you want from the
[Releases](../../releases) page and unzip it. Each contains the `.exe` plus an
(empty) `workflows/` folder beside it — drop your own Export(API) workflow JSONs
in there:

```text
cli.exe
workflows/      <- put your *.json workflows here
```

Run it directly — no Python needed:

```powershell
cli.exe --prompt "a knight in a volcanic caldera" --out knight.png
server.exe --comfyui-host 192.168.1.50:8188 --port 8000
```

Workflows are **not** baked into the exe (they reference the model/checkpoint
names installed in *your* ComfyUI). Edit/replace the JSONs in the `workflows/`
folder beside the exe, point elsewhere with `cli.exe --workflow-dir <path>` (or
the `COMFYUI_WORKFLOW_DIR` env var), and list what's available with
`cli.exe --list-workflows`.

To build them yourself, see [`BUILD.md`](BUILD.md).

## Install (from source)

```bash
pip install -r requirements.txt
```

You need a **ComfyUI server running** with its `/ws` websocket reachable -
locally that's usually `127.0.0.1:8188`. Point elsewhere with `--comfyui-host`
or the `COMFYUI_HOST` env var (see below); no code edits needed.

### ⚠️ Workflows must be in **API format** -- use **Export (API)**, not Export

Workflows live in `workflows/`. They MUST be exported from the ComfyUI UI via
**Export (API)** (sometimes shown as *Save (API Format)* after enabling
Settings → **Dev mode**) - **not** the normal **Export**.

The two formats look similar but are incompatible:

- **Export (API)** → a flat `{ "node_id": { "class_type": ..., "inputs": ... } }`
  map. This is what this server requires.
- **Export** (normal) → a UI graph with `nodes`/`links` arrays and canvas
  positions. **This will not work** - prompt/seed/size injection can't find the
  nodes, and the job fails.

Put the exported `*.json` into `workflows/`. The default workflow is the first
file there (alphabetically); pick a specific one per request with the `workflow`
field.

---

## FastAPI server (recommended)

Two ways to launch -- both let you point at a non-local ComfyUI:

```bash
# A) python entrypoint (has --comfyui-host)
python server.py --comfyui-host 192.168.1.50:8188 --host 127.0.0.1 --port 8000 --reload

# B) uvicorn directly (set the host via env var)
COMFYUI_HOST=192.168.1.50:8188 uvicorn server:app --host 127.0.0.1 --port 8000
```

The ComfyUI target defaults to `127.0.0.1:8188`. Override it with the
`--comfyui-host` flag (method A) or the `COMFYUI_HOST` env var (works for both).
`GET /config` shows the active target. `--host`/`--port` control where **this
API** binds, not ComfyUI.

Then open the interactive docs:

- **Swagger UI:** http://127.0.0.1:8000/docs
- **ReDoc:** http://127.0.0.1:8000/redoc (served from a pinned redoc bundle;
  FastAPI's default `redoc@next` CDN tag can 404 and render a blank page)
- **OpenAPI JSON:** http://127.0.0.1:8000/openapi.json

Generation is **asynchronous** (image gen is slow): you submit a job, poll its
status, then download the PNG.

### Endpoints

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/generate` | Submit a text-to-image job → `{job_id}` |
| `POST` | `/generate/img2img` | Submit an image-to-image job (multipart: `image` + fields) → `{job_id}` |
| `GET` | `/jobs/{job_id}` | Status, progress, output filenames |
| `GET` | `/jobs/{job_id}/image` | The generated PNG (when `status == "done"`). Defaults to `index=-1` = the last image (final result after all detailer/upscale passes). |
| `GET` | `/workflows` | List available workflow files |
| `GET` | `/healthz` | Liveness |

### Example (txt2img)

Optional `width`, `height`, `batch_size` override the workflow's fixed latent
size (any node with literal width/height inputs); omit them to use the
workflow's own values.

```bash
# submit (custom size; drop width/height to use the workflow default)
JOB=$(curl -s -X POST http://127.0.0.1:8000/generate \
  -H "Content-Type: application/json" \
  -d '{"prompt":"a knight in a volcanic caldera, dramatic light","negative_prompt":"lowres, watermark","width":832,"height":1216}' \
  | python -c "import sys,json;print(json.load(sys.stdin)['job_id'])")

# poll
curl -s http://127.0.0.1:8000/jobs/$JOB        # -> {"status":"running","progress":{...}}

# download when done — the API returns PNG bytes; -o writes them wherever you
# run curl (the server does NOT save to disk in the API path).
curl -s http://127.0.0.1:8000/jobs/$JOB/image -o out.png
```

### Example (img2img)

```bash
curl -s -X POST http://127.0.0.1:8000/generate/img2img \
  -F image=@./input/your.png \
  -F prompt="repaint as a stormy night scene, cinematic" \
  -F negative_prompt="blurry, lowres"
```

Notes:
- One job runs at a time (ComfyUI processes a single prompt at a time); extra
  jobs queue.
- Jobs and their image bytes are kept **in memory** (lost on restart) - fine for
  local/dev use.
- If a workflow's negative conditioning is a `ConditioningZeroOut` node, the negative prompt is ignored - there's
  no text field to write to.

---

## CLI use (no server)

`cli.py` generates straight to a file — no FastAPI, no polling. It reuses the
same generation core as the server, so it gets the same prompt/seed/size
handling and the `--comfyui-host` override.

The flags are identical whether you run it from source (`python cli.py …`) or as
the prebuilt binary (`cli.exe …`).

### Parameters

| Flag | Default | Description |
|---|---|---|
| `--prompt` | *(required)* | Positive prompt. |
| `--negative` | `""` | Negative prompt. Ignored if the workflow's negative is a `ConditioningZeroOut` node. |
| `--workflow` | first `*.json` in the workflow dir | Workflow file: a bare name (looked up in `--workflow-dir`) or an explicit path. Must be **Export (API)** format. |
| `--workflow-dir` | `workflows/` beside the exe, or `$COMFYUI_WORKFLOW_DIR` | Directory to look up workflow files in. |
| `--list-workflows` | `false` | Print the workflows in the resolved dir and exit. |
| `--img2img IMAGE` | `None` | Run image-to-image with this input image (the workflow needs a `LoadImage` node). |
| `--width` | workflow's own | Override output width (px). |
| `--height` | workflow's own | Override output height (px). |
| `--batch` | workflow's own | Override images per run (batch size). |
| `--out` | `output.png` | Output file. With `--all`, an index is inserted (`out.png` → `out_0.png`, `out_1.png`, …). |
| `--preview` | `false` | Also collect intermediate preview frames (= API `allow_preview`). Combine with `--index`/`--all` to actually save them. |
| `--index N` | `-1` | Which image to save, in execution order. `-1` is the last/final image; negative indexing works. Ignored with `--all`. |
| `--all` | `false` | Save every collected image (with an index suffix) instead of one. |
| `--comfyui-host` | `$COMFYUI_HOST` or `127.0.0.1:8188` | Target ComfyUI as `ip:port`. |
| `-h`, `--help` | | Show all flags and exit. |

Workflow directory resolution: `--workflow-dir` → `COMFYUI_WORKFLOW_DIR` env var
→ `workflows/` next to the script/exe.

```bash
# basic
python cli.py --prompt "a knight in a volcanic caldera, dramatic light" --out knight.png

# custom size, negative, batch, and a remote ComfyUI
python cli.py --prompt "..." --negative "lowres, watermark" \
  --width 832 --height 1216 --batch 2 \
  --comfyui-host 192.168.1.50:8188 --out shot.png

# image-to-image (workflow needs a LoadImage node)
python cli.py --workflow my_img2img.json --prompt "repaint as winter" \
  --img2img ./input/base.png --out edit.png

# use a different workflow directory, or just list what's available
python cli.py --list-workflows --workflow-dir /path/to/workflows
python cli.py --prompt "..." --workflow-dir /path/to/workflows --workflow my.json
```

Image collection and selection are **independent, mirroring the API**:

- `--preview` — collect intermediate preview frames (= API `allow_preview`). On
  its own it just collects; combine with `--index`/`--all` to save them.
- `--index N` — which image to save, in execution order. `-1` (default) is the
  last/final image; negative indexing works.
- `--all` — save every collected image with an index suffix (`out_0.png`, …).

So `--preview --index 3` collects previews but saves only image #3, and `--all`
without `--preview` saves all the real outputs without preview frames. The output
path is **wherever you point `--out`** — it does not force `output/`.

---

## Library use (no server)

```python
from utils.actions.load_workflow import load_workflow
from utils.actions.prompt_to_image import prompt_to_image
from utils.actions.prompt_image_to_image import prompt_image_to_image

wf = load_workflow('./workflows/illustrious.json')  # any Export(API) workflow in workflows/
prompt_to_image(wf, 'a mountain lake at dawn', 'lowres, watermark', save_previews=True)
# img2img: put your image in input/, then:
# prompt_image_to_image(wf, './input/your.png', 'repaint as winter', save_previews=True)
```

In this **library path only**, images are written to `./output/` (relative to
your current working directory). The **CLI** (`cli.py`) and the **API/server**
do not use `output/` — the CLI writes to `--out`, and the API returns PNG bytes
the caller saves wherever it likes.