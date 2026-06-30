from api.api_helpers import generate_image_by_prompt
from utils.helpers.randomize_seed import generate_random_15_digit_number
import json


def _set_prompt_text(node, text):
  """Write a prompt into whatever text field(s) the conditioning node uses.

  Workflows differ: most CLIP encoders use `text`, SDXL-style ones use
  `text_g`/`text_l`. Returns True if anything was written (False for nodes with
  no text field, e.g. ConditioningZeroOut)."""
  inputs = node.get('inputs', {})
  wrote = False
  for field in ('text', 'text_g', 'text_l'):
    if field in inputs:
      inputs[field] = text
      wrote = True
  return wrote


def prompt_to_image(workflow, positive_prompt, negative_prompt='', save_previews=False):
  prompt = json.loads(workflow) if isinstance(workflow, str) else workflow
  id_to_class_type = {id: details['class_type'] for id, details in prompt.items()}
  k_sampler = [key for key, value in id_to_class_type.items() if value == 'KSampler'][0]
  prompt[k_sampler]['inputs']['seed'] = generate_random_15_digit_number()

  positive_input_id = prompt[k_sampler]['inputs']['positive'][0]
  _set_prompt_text(prompt[positive_input_id], positive_prompt)

  if negative_prompt:
    negative_input_id = prompt[k_sampler]['inputs']['negative'][0]
    # Some workflows wire the negative to ConditioningZeroOut (no text field);
    # only write if the node actually accepts text. (Was previously a bug that
    # copied the POSITIVE prompt into the negative slot and would KeyError here.)
    if not _set_prompt_text(prompt[negative_input_id], negative_prompt):
      print(f"Note: negative node '{negative_input_id}' "
            f"({id_to_class_type[negative_input_id]}) has no text field; "
            f"negative prompt ignored for this workflow.")

  return generate_image_by_prompt(prompt, './output/', save_previews)
