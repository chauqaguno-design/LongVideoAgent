"""Describe a representative frame of a short shot with the small local SmolVLM2."""

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


def representative_frame(clip):
    """Return the middle frame; suppress action guesses on visually still footage."""
    import av
    import numpy as np

    with av.open(str(clip)) as video:
        frames = list(video.decode(video=0))
    if not frames:
        raise ValueError("片段中没有可解码的画面。")

    def thumbnail(frame):
        return frame.reformat(width=96, height=64, format="rgb24").to_ndarray().astype(np.int16)

    first = thumbnail(frames[0])
    # Ignore small codec variations on title cards and repeated frames.
    if not any(np.any(np.abs(thumbnail(frame) - first) > 6) for frame in frames[1:]):
        return None
    return frames[len(frames) // 2].to_image()


def describe(clip, directory):
    frame = representative_frame(clip)
    if frame is None:
        return "采样画面基本静止，未观察到可确认的动作。"

    import torch

    processor, model = load_model(directory)
    message = [{"role": "user", "content": [
        {"type": "image", "image": frame},
        {"type": "text", "text": "Who or what is visible? Describe the subject's position "
                                "and which way they are facing in one short sentence."},
    ]}]
    inputs = processor.apply_chat_template(message, add_generation_prompt=True, tokenize=True,
                                           return_dict=True, return_tensors="pt")
    inputs = inputs.to(model.device, dtype=model.dtype)
    with torch.inference_mode():
        output = model.generate(**inputs, max_new_tokens=40, do_sample=False)
    new_tokens = output[0, inputs["input_ids"].shape[1]:]
    return processor.decode(new_tokens, skip_special_tokens=True).strip()
