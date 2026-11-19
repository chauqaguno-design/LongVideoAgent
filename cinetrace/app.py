"""Local HTTP API and a single worker that consumes the persistent queue."""

import logging
import os
import threading
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from . import pipeline
from .dispatch import kafka_notifications
from .storage import PART_SIZE, Store


ROOT = Path(__file__).resolve().parent.parent
Hash = Annotated[str, Field(pattern=r"^[a-fA-F0-9]{64}$")]
log = logging.getLogger("uvicorn.error")


class Upload(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    size: int = Field(gt=0, le=2**63 - 1)
    fingerprint: Hash


class Complete(BaseModel):
    sha256: Hash | None = None


class Worker:
    def __init__(self, store):
        self.store = store
        self.wake = threading.Event()
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self.run, name="context-worker", daemon=True)

    def run(self):
        while not self.stop.is_set():
            video_id = self.store.claim()
            if video_id is None:
                self.wake.wait(2)
                self.wake.clear()
                continue
            self.process(video_id)

    def process(self, video_id):
        context = self.store.video(video_id)
        original = self.store.media_path(video_id)
        work_dir = original.parent / "work"

        def publish(patch):
            if self.stop.is_set():
                raise InterruptedError("服务正在关闭")
            context.update(patch)
            self.store.save_context(video_id, context)

        try:
            result = pipeline.build_context(original, work_dir, dict(context), publish)
            status = result["status"]
            publish({**result, "progress": 100, "stage": "处理完成" if status == "ready" else "部分完成"})
        except InterruptedError:
            context.update(status="queued", stage="等待恢复")
            self.store.save_context(video_id, context)
        except Exception as exc:
            log.exception("Video processing failed for %s", video_id)
            context.update(status="partial" if context["evidence"] else "failed",
                           stage="处理中断", error=str(exc))
            self.store.save_context(video_id, context)


@asynccontextmanager
async def lifespan(app):
    store = Store(Path(os.environ.get("CINETRACE_DATA", ROOT / "data")))
    worker = Worker(store)
    app.state.store = store
    app.state.worker = worker
    with kafka_notifications(store, worker.wake):
        worker.thread.start()
        try:
            yield
        finally:
            worker.stop.set()
            worker.wake.set()
            worker.thread.join(timeout=3)


app = FastAPI(title="CineTrace", lifespan=lifespan)


@app.get("/api/videos")
def list_videos(request: Request):
    return request.app.state.store.list_videos()


@app.post("/api/uploads")
def start_upload(body: Upload, request: Request):
    name = body.name.strip()
    if not name.lower().endswith(".mp4"):
        raise HTTPException(422, "首版可上传 MP4 视频")
    return request.app.state.store.start_upload(name, body.size, body.fingerprint.lower())


@app.put("/api/uploads/{video_id}/parts/{index}")
async def put_part(video_id: str, index: int, request: Request, x_part_sha256: Annotated[Hash, Header()]):
    data = bytearray()
    async for block in request.stream():
        if len(data) + len(block) > PART_SIZE:
            raise HTTPException(413, "分片大于服务端允许的长度")
        data.extend(block)
    await run_in_threadpool(request.app.state.store.put_part, video_id, index, data, x_part_sha256.lower())
    return {"index": index}


@app.post("/api/uploads/{video_id}/complete")
def complete(video_id: str, request: Request, body: Complete | None = None):
    store = request.app.state.store
    expected = body.sha256.lower() if body and body.sha256 else None
    store.finish_upload(video_id, expected, pipeline.probe)
    request.app.state.worker.wake.set()
    return store.video(video_id)


@app.get("/api/videos/{video_id}")
def get_video(video_id: str, request: Request):
    return request.app.state.store.video(video_id)


@app.api_route("/api/videos/{video_id}/media", methods=["GET", "HEAD"])
def media(video_id: str, request: Request):
    store = request.app.state.store
    if store.video(video_id)["status"] == "uploading":
        raise HTTPException(409, "上传完成后可播放")
    path = store.media_path(video_id)
    if not path.is_file():
        raise HTTPException(404, "找不到原片")
    return FileResponse(path, media_type="video/mp4")


@app.post("/api/videos/{video_id}/retry")
def retry(video_id: str, request: Request):
    store = request.app.state.store
    store.retry(video_id)
    request.app.state.worker.wake.set()
    return store.video(video_id)


app.mount("/web", StaticFiles(directory=ROOT / "web", check_dir=False))
app.mount("/", StaticFiles(directory=ROOT / "web", html=True, check_dir=False), name="web")
