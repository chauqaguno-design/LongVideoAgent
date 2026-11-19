"""Describe ordered frames locally with SmolVLM2. One caption covers one window."""

from functools import lru_cache


@lru_cache(maxsize=1)
def load_model(directory):
    import torch
    from transformers import AutoModelForImageTextToText, AutoProcessor

    device = "cuda" if torch.cuda.is_available() else "cpu"
    if device == "cpu":
        torch.set_num_threads(min(torch.get_num_threads(), 4))
    processor = AutoProcessor.from_pretrained(directory, local_files_only=True)
    model = AutoModelForImageTextToText.from_pretrained(
        directory, local_files_only=True, dtype=torch.float16 if device == "cuda" else torch.float32,
        attn_implementation="sdpa",
    ).to(device).eval()
    return processor, model


def describe(clip, directory):
    import torch

    processor, model = load_model(directory)
    message = [{"role": "user", "content": [
        {"type": "video", "path": str(clip)},
        {"type": "text", "text": "What physical movement or interaction can you see? "
                                "Briefly describe how the scene changes, using only what is visible."},
    ]}]
    inputs = processor.apply_chat_template(message, add_generation_prompt=True, tokenize=True,
                                           return_dict=True, return_tensors="pt", num_frames=6, fps=None)
    inputs = inputs.to(model.device, dtype=model.dtype)
    with torch.inference_mode():
        output = model.generate(**inputs, max_new_tokens=40, do_sample=False)
    new_tokens = output[0, inputs["input_ids"].shape[1]:]
    return processor.decode(new_tokens, skip_special_tokens=True).strip()
