"""Describe a single silent shot in Chinese with local Qwen3-VL-Instruct weights."""

from functools import lru_cache


PROMPT = (
    "观察这个无声镜头。只用一个简短的中文句子记录最明显的一项事实：哪个主体在做什么。"
    "若有人移动，用衣着或画面位置指明是谁；如果基本静止，就写可见的姿态或朝向。"
    "看不清的部分省略。不要推测人物身份、对白、情绪或动作结果。"
)


@lru_cache(maxsize=1)
def load_model(directory):
    import torch
    from transformers import AutoProcessor, Qwen3VLForConditionalGeneration

    device = "cuda" if torch.cuda.is_available() else "cpu"
    if device == "cpu":
        torch.set_num_threads(min(torch.get_num_threads(), 6))
    processor = AutoProcessor.from_pretrained(directory, local_files_only=True)
    # Bound the visual tokens for local inference while preserving the shot's aspect ratio.
    processor.video_processor.size = {"shortest_edge": 4096, "longest_edge": 8 * 384 * 320}
    model = Qwen3VLForConditionalGeneration.from_pretrained(
        directory, local_files_only=True, dtype="auto",
        attn_implementation="sdpa",
    ).to(device).eval()
    return processor, model


def describe(clip, directory):
    import torch

    processor, model = load_model(directory)
    content = [{"type": "video", "path": str(clip)}, {"type": "text", "text": PROMPT}]
    inputs = processor.apply_chat_template(
        [{"role": "user", "content": content}], add_generation_prompt=True, tokenize=True,
        return_dict=True, return_tensors="pt", num_frames=8, fps=None,
    ).to(model.device)
    with torch.inference_mode():
        tokens = model.generate(**inputs, max_new_tokens=64, do_sample=False)
    answer = processor.decode(tokens[0, inputs["input_ids"].shape[1]:], skip_special_tokens=True)
    return answer.strip()
