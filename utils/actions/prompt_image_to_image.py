from api.api_helpers import generate_image_by_prompt_and_image
from utils.helpers.randomize_seed import generate_random_15_digit_number
from utils.actions.prompt_to_image import _set_prompt_text
import os
import json


def prompt_image_to_image(workflow, input_path, positive_prompt, negative_prompt='', save_previews=False):
  prompt = json.loads(workflow) if isinstance(workflow, str) else workflow
  id_to_class_type = {id: details['class_type'] for id, details in prompt.items()}
  k_sampler = [key for key, value in id_to_class_type.items() if value == 'KSampler'][0]
  prompt[k_sampler]['inputs']['seed'] = generate_random_15_digit_number()

  positive_input_id = prompt[k_sampler]['inputs']['positive'][0]
  _set_prompt_text(prompt[positive_input_id], positive_prompt)

  if negative_prompt:
    # Was a bug: previously wrote into id_to_class_type (the class-name map)
    # instead of `prompt`, so the negative prompt never reached the graph.
    negative_input_id = prompt[k_sampler]['inputs']['negative'][0]
    if not _set_prompt_text(prompt[negative_input_id], negative_prompt):
      print(f"Note: negative node '{negative_input_id}' "
            f"({id_to_class_type[negative_input_id]}) has no text field; "
            f"negative prompt ignored for this workflow.")

  image_loader = [key for key, value in id_to_class_type.items() if value == 'LoadImage'][0]
  filename = os.path.basename(input_path)
  prompt[image_loader]['inputs']['image'] = filename

  return generate_image_by_prompt_and_image(prompt, './output/', input_path, filename, save_previews)
