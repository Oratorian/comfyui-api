"""API-friendly generation core.

Mirrors api_helpers.generate_image_by_prompt(_and_image) but, instead of
printing progress and only saving to disk, it:
  - reports progress through a callback (current_step, max_steps, fraction)
  - returns the generated image bytes (list of {file_name, type, image_data})

This lets the FastAPI layer surface live progress and serve the PNG without the
CLI's print/disk coupling. Reuses the existing websocket + node-injection code.
"""
import json

import os

from api.open_websocket import open_websocket_connection
from api.websocket_api import queue_prompt, get_history, get_image, upload_image
from utils.actions.prompt_to_image import _set_prompt_text
from utils.helpers.randomize_seed import generate_random_15_digit_number


def _track_progress(ws, prompt, prompt_id, progress_cb=None):
    """Drain the websocket until execution finishes, forwarding progress."""
    node_ids = list(prompt.keys())
    finished = set()
    while True:
        out = ws.recv()
        if not isinstance(out, str):
            continue  # binary preview frames
        msg = json.loads(out)
        mtype, data = msg.get('type'), msg.get('data', {})

        if mtype == 'progress' and progress_cb:
            cur, mx = data.get('value', 0), data.get('max', 0) or 1
            progress_cb({'phase': 'sampling', 'step': cur, 'max': mx,
                         'fraction': round(cur / mx, 4)})
        elif mtype == 'execution_cached':
            finished.update(data.get('nodes', []))
            if progress_cb:
                progress_cb({'phase': 'nodes', 'done': len(finished), 'total': len(node_ids)})
        elif mtype == 'executing':
            node = data.get('node')
            if node is not None:
                finished.add(node)
                if progress_cb:
                    progress_cb({'phase': 'nodes', 'done': len(finished), 'total': len(node_ids)})
            elif data.get('prompt_id') == prompt_id:
                break  # done


def _collect_images(prompt_id, server_address, allow_preview=False):
    images = []
    history = get_history(prompt_id, server_address)[prompt_id]
    for node_id in history['outputs']:
        for image in history['outputs'][node_id].get('images', []):
            if image['type'] == 'temp' and not allow_preview:
                continue
            if image['type'] not in ('output', 'temp'):
                continue
            images.append({
                'file_name': image['filename'],
                'type': image['type'],
                'image_data': get_image(image['filename'], image['subfolder'],
                                        image['type'], server_address),
            })
    return images


def _override_dimensions(prompt, width=None, height=None, batch_size=None):
    """Override image size on every node whose width/height are LITERAL numbers.

    Targets latent nodes (EmptyLatentImage, EmptySD3LatentImage, custom sizers)
    generically by field shape, not class name. Skips nodes whose width/height
    are links (e.g. metadata/saver nodes that read size from upstream) so we
    never clobber a computed value. Returns the list of node ids changed."""
    if width is None and height is None and batch_size is None:
        return []
    changed = []
    for nid, node in prompt.items():
        inputs = node.get('inputs', {})
        # only literal numeric width/height are settable; links are [node, slot]
        is_literal = lambda v: isinstance(v, (int, float)) and not isinstance(v, bool)
        if 'width' in inputs and 'height' in inputs and \
           is_literal(inputs['width']) and is_literal(inputs['height']):
            if width is not None:
                inputs['width'] = width
            if height is not None:
                inputs['height'] = height
            if batch_size is not None and 'batch_size' in inputs:
                inputs['batch_size'] = batch_size
            changed.append(nid)
    return changed


def _prepare_prompt(workflow, positive_prompt, negative_prompt='',
                    width=None, height=None, batch_size=None):
    """Inject prompts, seed, and (optional) size into a copy of the workflow graph."""
    prompt = json.loads(workflow) if isinstance(workflow, str) else json.loads(json.dumps(workflow))
    class_of = {nid: n['class_type'] for nid, n in prompt.items()}
    ksampler = next(nid for nid, c in class_of.items() if c == 'KSampler')
    prompt[ksampler]['inputs']['seed'] = generate_random_15_digit_number()

    pos_id = prompt[ksampler]['inputs']['positive'][0]
    _set_prompt_text(prompt[pos_id], positive_prompt)

    if negative_prompt:
        neg_id = prompt[ksampler]['inputs']['negative'][0]
        _set_prompt_text(prompt[neg_id], negative_prompt)  # no-op for ConditioningZeroOut

    _override_dimensions(prompt, width, height, batch_size)

    return prompt, class_of


def generate(workflow, positive_prompt, negative_prompt='', input_image_path=None,
             allow_preview=False, progress_cb=None,
             width=None, height=None, batch_size=None):
    """Generate image(s) and return their bytes.

    workflow: dict or JSON string of an API-format ComfyUI workflow.
    input_image_path: if set, runs img2img (a LoadImage node must exist).
    width/height/batch_size: optional overrides for the latent size node(s);
        when None the workflow's own values are used.
    progress_cb: optional callable receiving progress dicts.
    Returns: list of {file_name, type, image_data(bytes)}.
    """
    prompt, class_of = _prepare_prompt(workflow, positive_prompt, negative_prompt,
                                       width=width, height=height, batch_size=batch_size)

    if input_image_path:
        loader = next(nid for nid, c in class_of.items() if c == 'LoadImage')
        filename = os.path.basename(input_image_path)
        prompt[loader]['inputs']['image'] = filename

    ws, server_address, client_id = open_websocket_connection()
    try:
        if input_image_path:
            upload_image(input_image_path, os.path.basename(input_image_path),
                         server_address, image_type="input")
        prompt_id = queue_prompt(prompt, client_id, server_address)['prompt_id']
        _track_progress(ws, prompt, prompt_id, progress_cb)
        return _collect_images(prompt_id, server_address, allow_preview)
    finally:
        ws.close()
