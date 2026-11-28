"""Serve local, read-only Qwen VL, CLIP, SigLIP, and BGE-M3 checkpoints on loopback."""

import argparse
import base64
import hashlib
import importlib.util
from http.server import BaseHTTPRequestHandler, HTTPServer
from io import BytesIO
import json
import os
from pathlib import Path

os.environ["HF_HUB_OFFLINE"] = os.environ["TRANSFORMERS_OFFLINE"] = "1"


def fingerprint(folder):
    digest = hashlib.sha256(b"search-encoder-v1:rgb384:cls-l2")
    for f in sorted(
        p
        for p in folder.rglob("*")
        if p.is_file()
        and p.suffix in {".json", ".safetensors", ".bin", ".model", ".py", ".txt"}
        and ".git" not in p.parts
    ):
        digest.update(f.relative_to(folder).as_posix().encode())
        with f.open("rb") as source:
            digest.update(hashlib.file_digest(source, "sha256").digest())
    return digest.hexdigest()


class Encoders:
    def __init__(self, vision, text, *, joint_text=False, device="auto"):
        import torch

        if device == "auto":
            device = "cuda" if torch.cuda.is_available() else "cpu"
        if device == "cuda" and not torch.cuda.is_available():
            raise ValueError("CUDA 不可用")
        self.device, self.torch = device, torch
        self.spaces = {}
        self.joint_text = joint_text
        if vision:
            folder = Path(vision).resolve(strict=True)
            helper = folder / "scripts/qwen3_vl_embedding.py"
            if helper.is_file():
                dimensions = self.load_qwen(folder, helper)
                name = "Qwen3-VL-Embedding"
            else:
                dimensions = self.load_clip(folder)
                name = folder.name
            self.spaces["vision"] = dict(version=f"{name}-{fingerprint(folder)}", dimensions=dimensions)
            if joint_text:
                self.spaces["text"] = dict(
                    version=self.spaces["vision"]["version"] + "-text", dimensions=dimensions
                )
        if text:
            from transformers import AutoModel, AutoTokenizer

            folder = Path(text).resolve(strict=True)
            self.bge = AutoModel.from_pretrained(folder, local_files_only=True).to(device).eval()
            self.tokenizer = AutoTokenizer.from_pretrained(folder, local_files_only=True)
            self.spaces["text"] = dict(
                version=folder.name + "-" + fingerprint(folder), dimensions=self.bge.config.hidden_size
            )

    def load_qwen(self, folder, helper):
        spec = importlib.util.spec_from_file_location("local_qwen_embedding", helper)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)

        class Encoder(module.Qwen3VLEmbedder):
            def _preprocess_inputs(self, conversations):
                batch = super()._preprocess_inputs(conversations)
                expects_image = any(
                    item["type"] == "image"
                    for chat in conversations
                    for message in chat
                    for item in message["content"]
                )
                if expects_image and "pixel_values" not in batch:
                    raise ValueError("图像解码失败")
                return batch

        model = Encoder(str(folder), dtype="auto", max_pixels=384 * 384, local_files_only=True)
        model.model.to(self.device)
        self.encode_vision = model.process
        return model.model.config.text_config.hidden_size

    def load_clip(self, folder):
        model_type = json.loads((folder / "config.json").read_text(encoding="utf-8"))["model_type"]
        if model_type == "chinese_clip":
            from transformers import ChineseCLIPModel as Model, ChineseCLIPProcessor as Processor
        elif model_type in {"clip", "siglip"}:
            from transformers import AutoModel as Model, AutoProcessor as Processor
        else:
            raise ValueError(f"不支持的图像编码模型: {model_type}")
        self.clip = Model.from_pretrained(folder, local_files_only=True).to(self.device).eval()
        if model_type == "chinese_clip":
            # Some Transformers 4.x releases read pooler_output in get_text_features
            # even though Chinese-CLIP disables its pooler. Use the checkpoint's CLS state.
            def pool_cls(_model, _args, output):
                if output.pooler_output is None:
                    output.pooler_output = output.last_hidden_state[:, 0, :]

            self.clip.text_model.register_forward_hook(pool_cls)
        self.processor = Processor.from_pretrained(folder, local_files_only=True)
        self.encode_vision = self.clip_features
        config = self.clip.config
        self.clip_text_options = dict(
            padding="max_length" if model_type == "siglip" else True,
            truncation=True,
            max_length=config.text_config.max_position_embeddings,
        )
        return config.vision_config.hidden_size if model_type == "siglip" else config.projection_dim

    def clip_features(self, samples):
        vectors = [None] * len(samples)
        for key, input_name, get_features, options in (
            ("image", "images", self.clip.get_image_features, {}),
            ("text", "text", self.clip.get_text_features, self.clip_text_options),
        ):
            indices = [i for i, sample in enumerate(samples) if key in sample]
            if not indices:
                continue
            inputs = self.processor(
                **{input_name: [samples[i][key] for i in indices]}, **options, return_tensors="pt"
            ).to(self.device)
            output = get_features(**inputs)
            # Transformers releases return either the feature tensor or a model output.
            features = getattr(output, "pooler_output", output)
            features = self.torch.nn.functional.normalize(features.float(), dim=-1)
            for index, vector in zip(indices, features, strict=True):
                vectors[index] = vector
        return self.torch.stack(vectors)

    def encode(self, payload):
        from PIL import Image

        space = payload["space"]
        model = self.spaces[space]
        if payload["version"] != model["version"] or not 1 <= len(payload["items"]) <= 16:
            raise ValueError("空间或批次错误")
        items = payload["items"]
        with self.torch.inference_mode():
            if space == "text" and not self.joint_text:
                args = self.tokenizer(
                    [item["text"] for item in items],
                    padding=True,
                    truncation=True,
                    max_length=512,
                    return_tensors="pt",
                ).to(self.device)
                vectors = self.torch.nn.functional.normalize(self.bge(**args).last_hidden_state[:, 0], dim=1)
            else:
                samples = []
                for item in items:
                    if "image" in item:
                        with Image.open(BytesIO(base64.b64decode(item["image"], validate=True))) as image:
                            if image.width * image.height > 384 * 384:
                                raise ValueError("输入像素超限")
                            samples.append({"image": image.convert("RGB")})
                    else:
                        samples.append({"text": item["text"]})
                vectors = self.encode_vision(samples)
        if tuple(vectors.shape) != (len(items), model["dimensions"]):
            raise ValueError("编码维度有误")
        return dict(version=model["version"], vectors=vectors.float().tolist())


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--vision", help="Local Qwen3-VL, Chinese-CLIP, CLIP, or SigLIP model folder")
    parser.add_argument("--text", help="Local BGE-M3 model folder")
    parser.add_argument("--device", choices=("auto", "cpu", "cuda"), default="auto")
    parser.add_argument(
        "--joint-text",
        action="store_true",
        help="Use the supplied cross-modal checkpoint for the dedicated text space",
    )
    parser.add_argument("--port", type=int, default=8174)
    args = parser.parse_args()
    if not (args.vision or args.text):
        parser.error("supply at least one local checkpoint")
    if args.joint_text and (not args.vision or args.text):
        parser.error(
            "--joint-text requires a vision checkpoint and a dedicated text checkpoint must be omitted"
        )
    if args.device == "cpu":
        # The Qwen checkpoint helper also chooses a device when it loads the model.
        os.environ["CUDA_VISIBLE_DEVICES"] = ""
    encoders = Encoders(args.vision, args.text, joint_text=args.joint_text, device=args.device)

    class Handler(BaseHTTPRequestHandler):
        def reply(self, status, body):
            data = json.dumps(body).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            self.reply(200, encoders.spaces) if self.path == "/spaces" else self.send_error(404)

        def do_POST(self):
            if self.path != "/encode":
                return self.send_error(404)
            size = int(self.headers.get("Content-Length", 0))
            if not 0 < size <= 12 * 1024**2:
                return self.send_error(413)
            try:
                result = encoders.encode(json.loads(self.rfile.read(size)))
            except (ValueError, KeyError, OSError, RuntimeError) as exc:
                return self.reply(422, {"error": type(exc).__name__})
            self.reply(200, result)

    HTTPServer(("127.0.0.1", args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
