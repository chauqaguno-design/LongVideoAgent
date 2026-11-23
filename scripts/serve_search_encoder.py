"""Serve local, read-only Qwen VL embedding and BGE-M3 checkpoints on loopback."""

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
    def __init__(self, vision, text, *, joint_text=False):
        import torch

        self.spaces = {}
        self.joint_text = joint_text
        if vision:
            folder = Path(vision).resolve(strict=True)
            helper = folder / "scripts/qwen3_vl_embedding.py"
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

            self.vision = Encoder(str(folder), dtype="auto", max_pixels=384 * 384)
            self.spaces["vision"] = dict(version="Qwen3-VL-Embedding-" + fingerprint(folder), dimensions=2048)
            if joint_text:
                self.spaces["text"] = dict(
                    version=self.spaces["vision"]["version"] + "-text", dimensions=2048
                )
        if text:
            from transformers import AutoModel, AutoTokenizer

            folder = Path(text).resolve(strict=True)
            self.bge = AutoModel.from_pretrained(folder, local_files_only=True).eval()
            self.tokenizer = AutoTokenizer.from_pretrained(folder, local_files_only=True)
            self.spaces["text"] = dict(
                version=folder.name + "-" + fingerprint(folder), dimensions=self.bge.config.hidden_size
            )
        self.torch = torch

    def encode(self, payload):
        from PIL import Image

        space = payload["space"]
        model = self.spaces[space]
        if payload["version"] != model["version"] or not 1 <= len(payload["items"]) <= 4:
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
                )
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
                vectors = self.vision.process(samples)
        return dict(version=model["version"], vectors=vectors.float().tolist())


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--vision", help="Local Qwen3-VL-Embedding-2B model folder")
    parser.add_argument("--text", help="Local BGE-M3 model folder")
    parser.add_argument(
        "--joint-text",
        action="store_true",
        help="Explicitly use the supplied Qwen embedding checkpoint for text",
    )
    parser.add_argument("--port", type=int, default=8174)
    args = parser.parse_args()
    if not (args.vision or args.text):
        parser.error("supply at least one local checkpoint")
    if args.joint_text and (not args.vision or args.text):
        parser.error(
            "--joint-text requires a vision checkpoint and a dedicated text checkpoint must be omitted"
        )
    encoders = Encoders(args.vision, args.text, joint_text=args.joint_text)

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
            if not 0 < size <= 4 * 1024**2:
                return self.send_error(413)
            try:
                result = encoders.encode(json.loads(self.rfile.read(size)))
            except (ValueError, KeyError, OSError, RuntimeError) as exc:
                return self.reply(422, {"error": type(exc).__name__})
            self.reply(200, result)

    HTTPServer(("127.0.0.1", args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
