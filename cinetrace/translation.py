"""Translate English visual observations locally into Simplified Chinese."""

import os
import re
from functools import lru_cache
from pathlib import Path


MODEL = "Helsinki-NLP/opus-mt-en-zh"
REVISION = "408d9bc410a388e1d9aef112a2daba955b945255"
LOCAL_MODEL = Path(__file__).resolve().parents[1] / "data" / "models" / "opus-mt-en-zh"
HAN = re.compile(r"[\u3400-\u9fff]")
QUOTED = re.compile(r'("[^"]*"|“[^”]*”|「[^」]*」|『[^』]*』)')


def needs_translation(text: str) -> bool:
    # A sign or a device name quoted in an observation can use another language.
    narration = QUOTED.sub(" ", text).strip() or text
    words = re.findall(r"[A-Za-z]+", narration)
    return len(words) > len(HAN.findall(narration))


@lru_cache(maxsize=1)
def _translator():
    from transformers import MarianMTModel, MarianTokenizer

    location = os.getenv("CINETRACE_TRANSLATION_MODEL") or (LOCAL_MODEL if LOCAL_MODEL.is_dir() else MODEL)
    options = {"revision": REVISION} if location == MODEL else {"local_files_only": True}
    tokenizer = MarianTokenizer.from_pretrained(location, **options)
    model = MarianMTModel.from_pretrained(location, **options).eval()
    return tokenizer, model


def _translate_english(text: str) -> str:
    import torch

    tokenizer, model = _translator()
    sentence = tokenizer(">>cmn_Hans<< " + text, return_tensors="pt")
    with torch.inference_mode():
        result = model.generate(**sentence, num_beams=1, max_new_tokens=256)
    translated = tokenizer.decode(result[0], skip_special_tokens=True).strip()
    if not HAN.search(translated):
        raise ValueError("英译中模型没有返回中文动作描述。")
    return translated


def translate_action(text: str) -> str:
    text = text.strip()
    if not needs_translation(text):
        return text
    translated = _translate_english(text)
    # Keep quoted Chinese signs intact, even if the English model reinterprets them.
    originals = QUOTED.findall(text)
    if any(HAN.search(quote) for quote in originals):
        matches = list(QUOTED.finditer(translated))
        if len(matches) == len(originals):
            for original, match in reversed(list(zip(originals, matches))):
                if HAN.search(original):
                    translated = translated[:match.start()] + original + translated[match.end():]
        elif any(q[1:-1] not in translated for q in originals if HAN.search(q)):
            raise ValueError("译文遗漏了动作描述中引用的中文文字。")
    return translated
