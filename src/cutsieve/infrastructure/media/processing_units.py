"""One source version's frame, audio and text evidence; all input is local."""

from bisect import bisect_left
from pathlib import Path
from tempfile import NamedTemporaryFile
from hashlib import sha256
import json
import os
import shutil
import sys
from subprocess import TimeoutExpired
from PIL import Image

from cutsieve.application.media_sampling import (
    adaptive_points,
    coverage,
    identity,
    segment_bounds,
    segments,
    windows,
    static_duplicate,
    POLICY,
)
from cutsieve.application.media_memory import (
    ocr_occurrences,
    speech_flags,
    viewing_notes,
)
from cutsieve.infrastructure.edit_captions import Cue, write_srt
from cutsieve.infrastructure.persistence.processing_catalog import record
from .source_artifacts import command, digest_file
from .local_inference import asr_model_config, ocr_model_config
from cutsieve.infrastructure.persistence.media_visual_budget import billed_window
from .visual_observations import (
    visual_deployment,
    observe,
    validated_claims,
    PROMPT_VERSION,
)

US = 1_000_000
CAPABILITIES = ("asr", "ocr", "visual_observation", "text_embedding", "image_embedding")
OCR_PROVIDERS = frozenset(("paddleocr", "rapidocr-onnxruntime"))
SUBTITLES_COVER_ASR = "内嵌字幕已覆盖"
TEXT_INDEX_OPTIONAL = "默认由 FTS5 全文检索覆盖（未单独启用文本向量）"


def local_models(env=None):
    """Only explicitly provisioned local weights may be loaded by this pipeline."""
    env = os.environ if env is None else env
    root, interpreter = asr_model_config(env)
    ocr, ocr_python = ocr_model_config(env)
    return dict(python=ocr_python, asr=root, asr_python=interpreter, ocr=ocr)


def local_package_version(interpreter, package, module, stopped=None):
    probe = f"import {module}; from importlib.metadata import version; print(version({package!r}))"
    if package == "paddleocr":
        probe = 'import os; os.environ["PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK"] = "True"; import paddle; ' + probe
    try:
        version, _ = command([interpreter, "-I", "-c", probe], stopped, timeout=20)
    except InterruptedError:
        raise
    except (OSError, ValueError, TimeoutExpired):
        return None
    return version.decode().strip()


def asr_version(interpreter, stopped=None):
    return local_package_version(interpreter, "faster-whisper", "faster_whisper", stopped)


def ocr_version(root, interpreter, stopped=None):
    if root == "rapidocr":
        return local_package_version(interpreter, "rapidocr-onnxruntime", "rapidocr_onnxruntime", stopped)
    if root and all((Path(root) / model).is_dir() for model in ("PP-OCRv5_mobile_det", "PP-OCRv5_mobile_rec")):
        return local_package_version(interpreter, "paddleocr", "paddleocr", stopped)
    return None


def select_ocr_frames(frames, *, has_subtitles, stride=1, max_with_subs=60):
    frames = sorted(frames, key=lambda a: a.body["frame"]["actual_us"])[::stride]
    if not has_subtitles or not max_with_subs or len(frames) <= max_with_subs:
        return frames
    times = [a.body["frame"]["actual_us"] for a in frames]
    selected = {}
    for i in range(max_with_subs):
        fraction = i / (max_with_subs - 1) if max_with_subs > 1 else 0.5
        target = times[0] + fraction * (times[-1] - times[0])
        right = min(bisect_left(times, target), len(times) - 1)
        left = max(0, right - 1)
        nearest = min((left, right), key=lambda j: abs(times[j] - target))
        selected[nearest] = frames[nearest]
    return list(selected.values())


class ProcessingUnits:
    def __init__(self, catalog, run, source, reader, temp):
        self.catalog, self.run, self.source, self.reader, self.temp = (
            catalog,
            run,
            source,
            reader,
            Path(temp),
        )
        self.spans = windows(source.probe["duration_us"])
        self.folder = catalog.root / run.asset
        self.files = self.folder / "files"
        self.files.mkdir(parents=True, exist_ok=True)
        self.config = run.config
        self.models = self.config["_local"]
        self.caps = {
            name: dict(name=name, state="UNCONFIGURED", reason="本次未启用") for name in CAPABILITIES
        }
        for name in self.config["requested_capabilities"]:
            self.caps[name]["reason"] = "本机未配置对应的处理模型"
        self.caps["text_embedding"].update(state="SKIPPED", reason=TEXT_INDEX_OPTIONAL)
        self.problems = []
        self.proxy = {}
        self.units = {r.unit: r.body for r in catalog.entries(run, "CHECKPOINT")}
        self.total = len(self.spans) + 5
        self.visual_budget = min(32, max(0, int(os.getenv("CUTSIEVE_MEDIA_VISUAL_MAX_WINDOWS", "8"))))
        if "visual_observation" in self.config["requested_capabilities"]:
            self.total += min(self.visual_budget, len(self.spans))

    def keep_file(self, path, kind, mime, unit):
        digest = digest_file(path)
        destination = self.files / (digest + path.suffix)
        if not destination.exists():
            # Producers reuse scratch filenames. Detach the bytes before publishing an immutable digest.
            with NamedTemporaryFile(dir=self.files, delete=False) as tmp:
                staged = Path(tmp.name)
            shutil.copyfile(path, staged)
            try:
                os.link(staged, destination)
            except FileExistsError:
                pass
            finally:
                staged.unlink(missing_ok=True)
        artifact_id = identity(self.run.id, "artifact:" + unit)
        public = dict(
            artifact_id=artifact_id,
            workspace_id=self.run.workspace,
            kind=kind,
            sha256=digest,
            size_bytes=destination.stat().st_size,
            mime_type=mime,
            media_revision_id=self.run.revision,
            timeline_revision_id=None,
            url=None,
            url_expires_at=None,
        )
        rel = destination.relative_to(self.catalog.root).as_posix()
        return record(self.run, "ARTIFACT", unit, dict(artifact=public, file=rel), rid=artifact_id)

    def json_file(self, name, body, kind, unit):
        path = self.temp / name
        path.write_text(json.dumps(body, ensure_ascii=False), encoding="utf8")
        return self.keep_file(path, kind, "application/json", unit)

    def evidence(
        self,
        unit,
        modality,
        provider,
        version,
        claim,
        start,
        end,
        artifacts,
        *,
        flags=(),
        frame=None,
        ref_file=None,
        support="SUPPORTED",
        language=None,
    ):
        obs_id = identity(self.run.id, "obs:" + unit)
        ev_id = identity(self.run.id, "evidence:" + unit)
        span = dict(start_us=start, end_us=end)
        observed = provider != "sampled-frame"
        refs = [a["id"] for a in artifacts]
        ev = dict(
            evidence_id=ev_id,
            workspace_id=self.run.workspace,
            project_id=self.run.project,
            media_revision_id=self.run.revision,
            range=span,
            claim=claim[:12000],
            support=support,
            observation_ids=[obs_id] if observed else [],
            artifact_ids=refs,
            verification_protocol=None,
            index_generation_id=self.run.id,
        )
        # Stable membership for the overlapping 10-second semantic segments.
        segment_start, _ = segment_bounds(start, self.source.probe["duration_us"])
        segment = identity(self.run.revision, f"segment:{segment_start}")
        body = dict(
            evidence=ev,
            modality=modality,
            source=provider,
            segment_id=segment,
            frame=frame,
            file=ref_file,
        )
        items = [record(self.run, "EVIDENCE", unit, body, start, rid=ev_id)]
        if observed:
            obs = dict(
                observation_id=obs_id,
                workspace_id=self.run.workspace,
                media_revision_id=self.run.revision,
                range=span,
                modality=modality,
                text=claim[:12000],
                language=language,
                claim_type="OBSERVED",
                support=support,
                artifact_ids=refs,
                model_id=provider,
                model_version=version,
                prompt_version="none",
                pipeline_version=self.config["pipeline_version"],
                quality_flags=list(flags),
            )
            items.append(record(self.run, "OBSERVATION", unit, obs, start, rid=obs_id))
        return items

    def save(self, unit, stage, items=(), state=None):
        body = state or {}
        self.catalog.commit(
            self.run,
            [*items, record(self.run, "CHECKPOINT", unit, body)],
            stage=stage,
            total=self.total,
            completed=len(self.units) + 1,
        )
        self.units[unit] = body

    def decode_frame(self, at, previous_signature):
        pts = self.reader.frame_pts(at)
        token = f"frame-{pts}"
        artifact_id = identity(self.run.id, "artifact:" + token)
        previous = self.catalog.get_record(self.run.workspace, artifact_id, "ARTIFACT")
        path = self.temp / "frame.jpg"
        path.unlink(missing_ok=True)
        cache = self.folder / "cache"
        cache.mkdir(exist_ok=True)
        cache_key = sha256(
            f"{self.source.digest}:frame-v1:{self.reader.version}:{pts}".encode()
        ).hexdigest()
        cached = cache / (cache_key + ".jpg")
        if previous:
            path = self.catalog.derived_path(previous.body["file"])
            actual = previous.body["frame"]["actual_us"]
        elif cached.is_file():
            path = cached
            actual = round((pts * self.reader.raw_timebase - self.reader.origin) * US)
        else:
            actual = self.reader.save_frame(pts, path)
        with Image.open(path) as image:
            signature = image.convert("RGB").resize((8, 8), Image.Resampling.BOX).tobytes()
        if static_duplicate(previous_signature, signature):
            return None, [], previous_signature
        if previous:
            return previous.body["frame"], [], signature
        if path != cached:
            try:
                os.link(path, cached)
            except FileExistsError:
                pass
        item = self.keep_file(path, "FRAME", "image/jpeg", token)
        tb = self.reader.raw_timebase
        frame = dict(
            artifact_id=item["id"],
            workspace_id=self.run.workspace,
            media_revision_id=self.run.revision,
            requested_us=at,
            actual_us=actual,
            actual_pts=pts,
            time_base_num=tb.numerator,
            time_base_den=tb.denominator,
            source_frame_index=None,
            width=self.source.probe["width"],
            height=self.source.probe["height"],
            sampling_policy_version=POLICY,
            sha256=item["body"]["artifact"]["sha256"],
            source_origin=self.source.probe["source_origin"],
            decoder_version=self.reader.version,
        )
        item["body"]["frame"] = frame
        ev = self.evidence(
            token,
            "VISUAL",
            "sampled-frame",
            self.reader.version,
            "原片采样画面",
            actual,
            min(actual + 1, self.source.probe["duration_us"]),
            [item],
            frame=frame,
            ref_file=item["body"]["file"],
            support="NOT_CHECKED",
        )
        return frame, [item, *ev], signature

    def frames(self):
        plan_key = "adaptive-frame-plan"
        if plan_key not in self.units:
            try:
                cuts = self.reader.scene_points()
            except (ValueError, TimeoutExpired):
                cuts = []
            duration = self.source.probe["duration_us"]
            points = adaptive_points(duration, cuts, self.config.get("_frame_budget", 2048))
            self.save(plan_key, "SAMPLING", state={"points": points})
        points = iter(self.units[plan_key]["points"])
        at = next(points, None)
        signature = None
        for i, span in enumerate(self.spans):
            cov = coverage(self.run, i, span, "PLANNED")
            self.catalog.commit(
                self.run,
                [
                    record(
                        self.run,
                        "COVERAGE",
                        f"{i}:plan",
                        cov,
                        span[0],
                        rid=cov["coverage_id"],
                    )
                ],
                stage="SAMPLING",
                total=self.total,
            )
            candidates = []
            while at is not None and at < span[1]:
                candidates.append(at)
                at = next(points, None)
            unit = f"frames:{i}:0"
            if unit in self.units and "signature" not in self.units[unit]:
                unit = f"frames:{i}:adaptive"
            if unit not in self.units:
                frames, items, failures = [], [], 0
                for point in candidates:
                    try:
                        frame, derived, signature = self.decode_frame(point, signature)
                    except (ValueError, OSError, TimeoutExpired) as exc:
                        if isinstance(exc, InterruptedError):
                            raise
                        failures += 1
                        continue
                    if frame:
                        frames.append(frame)
                        items.extend(derived)
                state = dict(frames=frames, signature=signature.hex() if signature is not None else None)
                if failures:
                    state["failure"] = "当前窗口无法完整解码"
                self.save(unit, "SAMPLING", items, state)
            unit_state = self.units[unit]
            signature = bytes.fromhex(unit_state["signature"]) if unit_state["signature"] is not None else None
            unique = {f["artifact_id"]: f for f in self.window_frames(i)}
            reason = unit_state.get("failure")
            stage = "FAILED" if reason else "DECODED"
            if reason:
                self.problems.append(reason)
            c = coverage(self.run, i, span, stage, unique.values(), reason)
            self.catalog.commit(
                self.run,
                [
                    record(
                        self.run,
                        "COVERAGE",
                        f"{i}:decoded",
                        c,
                        span[0],
                        rid=c["coverage_id"],
                    )
                ],
            )

    def window_frames(self, index):
        return [frame for slot in (0, 1, 2, 3, 4, "adaptive")
                for frame in self.units.get(f"frames:{index}:{slot}", {}).get("frames", [])]

    def ensure_proxy(self):
        if "proxy" in self.units:
            self.proxy = self.units["proxy"]
            return
        if self.source.probe.get("browser_playable"):
            self.save("proxy", "PLAYBACK")
            return
        if self.source.probe.get("proxy_encoder") == self.reader.version:
            self.proxy = {
                k: self.source.probe[k]
                for k in (
                    "playback_artifact_id",
                    "proxy_time_mapping_artifact_id",
                    "proxy_encoder",
                )
            }
            self.save("proxy", "PLAYBACK", state=self.proxy)
            return
        path = self.temp / "playback.mp4"
        mapping = self.reader.proxy(path)
        proxy = self.keep_file(path, "PREVIEW", "video/mp4", "proxy")
        doc = self.json_file("proxy-map.json", mapping, "REPORT", "proxy-map")
        self.proxy = dict(
            playback_artifact_id=proxy["id"],
            proxy_time_mapping_artifact_id=doc["id"],
            proxy_encoder=self.reader.version,
        )
        self.save("proxy", "PLAYBACK", [proxy, doc], self.proxy)

    def subtitles(self):
        if "subtitles" in self.units:
            return
        data, cues = self.reader.subtitle_text()
        if data is None:
            self.save("subtitles", "SUBTITLES", state={"complete": False})
            return
        path = self.temp / "subtitles.srt"
        path.write_bytes(data)
        raw = self.keep_file(path, "TRANSCRIPT", "application/x-subrip", "embedded")
        items = [raw]
        for i, cue in enumerate(cues):
            items.extend(
                self.evidence(
                    f"sub-{i}",
                    "OCR",
                    "embedded-subtitle",
                    self.reader.version,
                    cue.text,
                    cue.start_us,
                    cue.end_us,
                    [raw],
                    flags=["EMBEDDED_SUBTITLE"],
                )
            )
        self.save("subtitles", "SUBTITLES", items, {"complete": True, "has_text": bool(cues)})

    def has_subtitle_text(self):
        subtitles = self.units.get("subtitles", {})
        if "has_text" in subtitles:
            return subtitles["has_text"]
        # Older checkpoints recorded successful extraction even when it produced no cues.
        return bool(subtitles.get("complete")) and any(
            entry.body["source"] == "embedded-subtitle" for entry in self.catalog.entries(self.run, "EVIDENCE")
        )

    def local_batch(self, mode):
        root = self.models.get(mode)
        if not root or (root != "rapidocr" and not Path(root).is_dir()):
            return None
        interpreter = (self.models.get("asr_python") if mode == "asr" else None) or self.models["python"] or sys.executable
        if mode == "asr":
            package = "faster-whisper"
            version = asr_version(interpreter, self.reader.stopped)
        else:
            package = "rapidocr-onnxruntime" if root == "rapidocr" else "paddleocr"
            version = ocr_version(root, interpreter, self.reader.stopped)
        if not version:
            return None
        model = root if root == "rapidocr" else str(Path(root).resolve())
        adapter = Path(__file__).with_name("local_inference.py")
        model_stamp = sha256(f"{model}:{package}:{version}".encode()).hexdigest()
        # Cache against the source revision, input bytes and configured model installation.
        cache = self.folder / "cache"
        cache.mkdir(parents=True, exist_ok=True)
        if mode == "ocr":
            frames = (a for a in self.catalog.entries(self.run, "ARTIFACT") if "frame" in a.body)
            selected = select_ocr_frames(
                frames, has_subtitles=self.has_subtitle_text(),
                **self.config.get("_ocr_sampling", {}),
            )
            batch = [(self.catalog.derived_path(a.body["file"]), a) for a in selected]
        else:
            batch = []
            for i, (start, end) in enumerate(self.spans):
                left = max(0, start - 2 * US)
                right = min(self.source.probe["duration_us"], end + 2 * US)
                path = self.temp / f"speech-{i}.wav"
                self.reader.audio(left, right, path)
                batch.append((path, (i, start, end, left)))
        tasks = []
        for path, _ in batch:
            key = sha256(f"{mode}:{model_stamp}:{digest_file(path)}".encode()).hexdigest()
            tasks.append(dict(path=str(path), output=str(cache / (key + ".json"))))
        spec = self.temp / (mode + ".json")
        spec.write_text(
            json.dumps(dict(mode=mode, model=model, engine=package, checkpoint=model_stamp, inputs=tasks)),
            encoding="utf8",
        )
        command([interpreter, "-I", adapter, spec], self.reader.stopped, timeout=7200)
        return [(path, info, Path(entry["output"])) for (path, info), entry in zip(batch, tasks)]

    def recognize(self, mode):
        if mode in self.units:
            self.caps[mode] = {k: self.units[mode][k] for k in ("name", "state", "reason")}
            return
        if mode not in self.config["requested_capabilities"]:
            self.save(mode, mode.upper(), state=self.caps[mode])
            return
        if mode == "asr" and self.has_subtitle_text():
            self.caps[mode].update(state="SKIPPED", reason=SUBTITLES_COVER_ASR)
            self.save(mode, "ASR", state=self.caps[mode])
            return
        if mode == "asr" and self.source.probe["audio_index"] is None:
            self.caps[mode] = dict(name=mode, state="AVAILABLE", reason="原片没有音轨")
            self.save(mode, "ASR", state=self.caps[mode])
            return
        batch = self.local_batch(mode)
        if batch is None:
            self.caps[mode]["reason"] = "本机未配置专用模型"
            self.save(mode, mode.upper(), state=self.caps[mode])
            return
        ocr_frames = []
        spoken = {}
        for path, info, out in batch:
            raw = json.loads(out.read_text(encoding="utf8"))
            prefix = mode + "-" + (info.id if mode == "ocr" else str(info[0]))
            report = self.keep_file(out, "REPORT", "application/json", prefix)
            items = [report]
            if mode == "ocr":
                frame = info.body["frame"]
                ocr_frames.append((frame["actual_us"], raw["lines"], info, report, raw))
            else:
                _, core_start, core_end, left = info
                audio = self.keep_file(path, "TRANSCRIPT", "audio/wav", prefix + ":wav")
                items.append(audio)
                for i, s in enumerate(raw["spans"]):
                    start = max(0, left + round(s["start"] * US))
                    end = min(self.source.probe["duration_us"], left + round(s["end"] * US))
                    if not start < end or not core_start <= (start + end) // 2 < core_end:
                        continue
                    flags = speech_flags(s, start, end, spoken)
                    if flags is None:
                        continue
                    if s.get("word_timing") == "whisper-acoustic-alignment":
                        flags.append("WHISPER_ACOUSTIC_ALIGNMENT")
                    items.extend(
                        self.evidence(
                            f"{prefix}:{i}",
                            "ASR",
                            raw["model"],
                            raw["version"],
                            s["text"],
                            start,
                            end,
                            [audio, report],
                            flags=flags,
                            language=s.get("language") or raw["language"],
                            support="UNCERTAIN" if "LOW_SPEECH_CONFIDENCE" in flags else "SUPPORTED",
                        )
                    )
            self.catalog.commit(self.run, items, stage=mode.upper())
        if ocr_frames:
            ocr_frames.sort(key=lambda entry: entry[0])
            for i, group in enumerate(ocr_occurrences((e[:2] for e in ocr_frames))):
                anchor = ocr_frames[group["frames"][0][0]]
                refs = []
                for pos, _ in group["frames"]:
                    refs.extend([{"id": ocr_frames[pos][2].id}, ocr_frames[pos][3]])
                roi_file = self.temp / "ocr-region.png"
                with Image.open(
                    self.catalog.derived_path(anchor[2].body["file"])
                ) as image:
                    x1, y1, x2, y2 = group["first_box"]
                    box = (
                        max(0, int(x1)),
                        max(0, int(y1)),
                        min(image.width, int(x2) + 1),
                        min(image.height, int(y2) + 1),
                    )
                    if box[0] >= box[2] or box[1] >= box[3]:
                        continue
                    image.crop(box).save(roi_file)
                roi = self.keep_file(roi_file, "FRAME", "image/png", f"ocr-roi:{i}")
                roi["body"]["roi"] = dict(source_frame_artifact_id=anchor[2].id, display_rect=list(box))
                refs.append(roi)
                items = self.evidence(
                    f"ocr-occurrence:{i}",
                    "OCR",
                    anchor[4]["model"],
                    anchor[4]["version"],
                    group["text"],
                    group["start_us"],
                    min(group["end_us"] + 1, self.source.probe["duration_us"]),
                    refs,
                    frame=anchor[2].body["frame"],
                    ref_file=anchor[2].body["file"],
                    support=(
                        "UNCERTAIN" if max(score for _, score in group["frames"]) < 0.65 else "SUPPORTED"
                    ),
                    flags=["ROI_IN_RAW_RESPONSE", "SAMPLED_BOUNDARIES"],
                )
                self.catalog.commit(self.run, [roi, *items])
        reason = "本次 VAD/转写未提取到台词" if mode == "asr" and not spoken else None
        self.caps[mode] = dict(name=mode, state="AVAILABLE", reason=reason)
        state = dict(self.caps[mode])
        if mode == "ocr":
            state["frame_artifact_ids"] = [info.id for _, info, _ in batch]
        self.save(mode, mode.upper(), state=state)

    def visual_report(self, deployment, unit, window, inputs):
        # Frame identifiers change between generations. The actual input pixels and times do not.
        dependencies = [
            self.source.digest,
            self.config["pipeline_version"],
            PROMPT_VERSION,
            deployment.model,
            deployment.config.model,
            deployment.config.chat_url,
            deployment.config.transport,
            list(deployment.config.provider_only),
            [(r.body["frame"]["actual_us"], r.body["artifact"]["sha256"]) for r in window],
        ]
        key = sha256(json.dumps(dependencies).encode()).hexdigest()
        previous = None
        if self.run.predecessor:
            previous = self.catalog.get_record(
                self.run.workspace,
                identity(self.run.predecessor, "artifact:" + unit),
                "ARTIFACT",
            )
        frame_ids = [r.id for r in window]
        if previous and previous.body.get("visual_dependency") == key:
            path = self.catalog.derived_path(previous.body["file"])
            if digest_file(path) != previous.body["artifact"]["sha256"]:
                raise ValueError("视觉观察缓存校验失败")
            report = json.loads(path.read_text(encoding="utf8"))
            original = report["input_frame_ids"]
            mapped = dict(zip(original, frame_ids, strict=True))
            # Preserve the exact response and its original IDs alongside the replay mapping.
            report["replay_frame_ids"] = mapped
            claims = [dict(row, frames=[mapped[f] for f in row["frames"]]) for row in report["response"]]
            claims = validated_claims({"observations": claims}, {f: t for f, t, _ in inputs})
        else:

            def charge(step, size, image_count, send):
                return billed_window(
                    self.catalog,
                    self.run,
                    f"{unit}:{step}",
                    size,
                    image_count,
                    os.environ,
                    send,
                )

            claims, report = observe(deployment, inputs, charge)
            report.update(producing_run_id=self.run.id, input_frame_ids=frame_ids)
        raw = self.json_file("observations.json", report, "REPORT", unit)
        raw["body"]["visual_dependency"] = key
        return claims, report, raw

    def visual(self):
        name = "visual_observation"
        config = visual_deployment(os.environ)
        if not config:
            self.caps[name]["reason"] = "未启用通过图像预检的 Flash 服务；可回看采样帧"
            return
        count = min(self.visual_budget, len(self.spans))
        chosen = {int((i + 0.5) * len(self.spans) / count) for i in range(count)}
        frames = [r for r in self.catalog.entries(self.run, "ARTIFACT") if "frame" in r.body]
        completed = {
            r.unit
            for r in self.catalog.entries(self.run, "COVERAGE")
            if r.unit.startswith("visual-window:") and r.body["stage"] == "MODEL_OBSERVED"
        }
        finished = len(completed)
        for i in sorted(chosen):
            unit = f"visual-window:{i}"
            if unit in self.units:
                continue
            start, end = self.spans[i]
            window = sorted(
                (r for r in frames if start <= r.body["frame"]["actual_us"] < end),
                key=lambda r: r.body["frame"]["actual_us"],
            )
            if len(window) > 8:
                window = [window[(2 * j + 1) * len(window) // 16] for j in range(8)]
            if not window:
                continue
            by_id = {r.id: r for r in window}
            inputs = [
                (
                    r.id,
                    r.body["frame"]["actual_us"],
                    self.catalog.derived_path(r.body["file"]),
                )
                for r in window
            ]
            # Reserve before the external process sees bytes. A crash leaves this window unresolved.
            self.save(unit, "VISUAL", state={"reserved": True})
            try:
                claims, report, raw = self.visual_report(config, unit, window, inputs)
                items = [raw]
                ids = []
                for n, c in enumerate(claims):
                    refs = [by_id[id] for id in c["frames"]]
                    positions = [r.body["frame"]["actual_us"] for r in refs]
                    new = self.evidence(
                        f"{unit}:{n}",
                        "VISUAL",
                        "flash" if config.billed else "local-flash",
                        report["model"],
                        c["text"],
                        min(positions),
                        min(max(positions) + 1, self.source.probe["duration_us"]),
                        [{"id": r.id} for r in refs] + [raw],
                        frame=refs[0].body["frame"],
                        ref_file=refs[0].body["file"],
                        support="UNCERTAIN" if c["claim_type"] == "INFERRED" else "SUPPORTED",
                    )
                    for row in new:
                        if row["kind"] == "OBSERVATION":
                            row["body"].update(
                                claim_type=c["claim_type"],
                                prompt_version=PROMPT_VERSION,
                            )
                        else:
                            ids.append(row["id"])
                    items.extend(new)
                cov = coverage(
                    self.run,
                    i,
                    self.spans[i],
                    "MODEL_OBSERVED",
                    [r.body["frame"] for r in window],
                    evidence=ids,
                )
                cov["protocol_id"] = PROMPT_VERSION
                items.append(record(self.run, "COVERAGE", unit, cov, start, rid=cov["coverage_id"]))
                self.catalog.commit(self.run, items, stage="VISUAL")
                finished += 1
            except (ValueError, TypeError, IndexError, KeyError, OSError):
                self.problems.append("部分视觉窗口未得到有效的模型响应")
                failed = coverage(
                    self.run,
                    i,
                    self.spans[i],
                    "FAILED",
                    reason="本窗口没有得到可验证的模型观察，请检查视觉配置、调用或预算记录",
                )
                failed.update(
                    coverage_id=identity(self.run.id, unit + ":failed"),
                    protocol_id=PROMPT_VERSION,
                )
                self.catalog.commit(
                    self.run,
                    [
                        record(
                            self.run,
                            "COVERAGE",
                            unit + ":failed",
                            failed,
                            start,
                            rid=failed["coverage_id"],
                        )
                    ],
                )
        self.caps[name] = dict(
            name=name,
            state="AVAILABLE" if finished == len(self.spans) else ("PARTIAL" if finished else "FAILED"),
            reason=None if finished == len(self.spans) else f"已观察 {finished}/{len(self.spans)} 个窗口",
        )

    def run_all(self):
        self.ensure_proxy()
        self.frames()
        try:
            self.subtitles()
        except (ValueError, TimeoutExpired):
            self.problems.append("原片字幕轨无法转换为文本")
            self.save("subtitles", "SUBTITLES", state={"complete": False})
        for mode in ("asr", "ocr"):
            try:
                self.recognize(mode)
            except (OSError, ValueError, TimeoutExpired) as exc:
                if isinstance(exc, InterruptedError):
                    raise
                self.caps[mode] = dict(name=mode, state="FAILED", reason=f"本地 {mode.upper()} 推理未完成")
                self.save(mode, mode.upper(), state=self.caps[mode])
        if "visual_observation" in self.config["requested_capabilities"]:
            self.visual()
        self.validate_artifacts()
        items = (r.body for r in self.catalog.entries(self.run, "EVIDENCE"))
        rows = self.catalog.search_rows(items, self.run.id)
        duration = self.source.probe["duration_us"]
        notes = viewing_notes(duration, rows, segments(duration))
        notes["media_revision_id"] = self.run.revision
        note_artifact = self.json_file("notes.json", notes, "REPORT", "evidence-notes")
        artifacts = [note_artifact]
        self.proxy["caption_track_id"] = None
        dialogue = [r for r in rows if r["source"] == "embedded-subtitle"]
        if not dialogue:
            dialogue = [r for r in rows if r["modality"] == "ASR" and r["support"] == "SUPPORTED"]
        if dialogue:
            cues = [Cue(r["range"]["start_us"], r["range"]["end_us"], r["claim"]) for r in dialogue]
            path = self.temp / "aligned-captions.srt"
            write_srt(path, sorted(cues, key=lambda cue: (cue.start_us, cue.end_us)))
            track = self.keep_file(path, "TRANSCRIPT", "application/x-subrip", "aligned-captions")
            self.proxy["caption_track_id"] = track["id"]
            artifacts.append(track)
        self.catalog.commit(self.run, artifacts, stage="NOTES")
        self.proxy["notes_artifact_id"] = note_artifact["id"]
        prepare = getattr(self.catalog, "prepare_search_rows", None)
        if prepare:
            self.catalog.commit(self.run, stage="INDEXING")
            spaces = prepare(self.run.workspace, rows)
            self.catalog.commit(self.run, [record(self.run, "CHECKPOINT", "search-spaces", spaces)])
            for space, name in (
                ("text", "text_embedding"),
                ("vision", "image_embedding"),
            ):
                if space in spaces:
                    self.caps[name] = dict(
                        name=name, state="AVAILABLE", reason=None,
                        storage=getattr(self.catalog, "search_vector_storage", None),
                    )
        self.modality_coverage(rows)
        for name in self.config["requested_capabilities"]:
            if self.caps[name]["state"] not in {"AVAILABLE", "SKIPPED"}:
                self.problems.append(name + "：" + self.caps[name]["reason"])
        self.catalog.publish(
            self.run,
            capabilities=list(self.caps.values()),
            proxy=self.proxy,
            problems=self.problems,
            total=self.total,
        )

    def modality_coverage(self, rows):
        """Each protocol keeps its own coverage; frame decoding can't verify an OCR/ASR claim."""
        routed = {"asr": "ASR", "ocr": "OCR", "image_embedding": "IMAGE_EMBEDDING"}
        for name, modality in routed.items():
            state = self.caps[name]
            if state["state"] == "SKIPPED":
                continue
            if (
                name not in self.config["requested_capabilities"]
                and state["state"] != "AVAILABLE"
            ):
                continue
            providers = {
                "asr": {"faster-whisper"},
                "ocr": OCR_PROVIDERS,
                "image_embedding": {"sampled-frame"},
            }[name]
            relevant = [r for r in rows if r["source"] in providers]
            scanned = self.units.get("ocr", {}).get("frame_artifact_ids") if name == "ocr" else None
            scanned = set(scanned) if scanned is not None else None
            for i, span in enumerate(self.spans):
                found = [
                    r
                    for r in relevant
                    if r["range"]["start_us"] < span[1] and r["range"]["end_us"] > span[0]
                ]
                ok = state["state"] == "AVAILABLE"
                stage = "MODEL_OBSERVED" if ok else "FAILED"
                if ok and name == "asr" and self.source.probe["audio_index"] is None:
                    stage = "DECODED"
                frame_data = (
                    self.window_frames(i)
                    if name != "asr"
                    else []
                )
                reason = state["reason"] if not ok else None
                if scanned is not None and frame_data:
                    frame_data = [frame for frame in frame_data if frame["artifact_id"] in scanned]
                    if not frame_data:
                        stage = "PLANNED"
                elif name != "asr" and not frame_data:
                    stage, reason = "FAILED", "此窗口没有可用的实际采样帧"
                c = coverage(
                    self.run,
                    i,
                    span,
                    stage,
                    frame_data,
                    reason,
                    modality=modality,
                    evidence=[r["evidence_id"] for r in found],
                )
                c["protocol_id"] = name + "-source-v1"
                self.catalog.commit(
                    self.run,
                    [
                        record(
                            self.run,
                            "COVERAGE",
                            f"{i}:{name}",
                            c,
                            span[0],
                            rid=c["coverage_id"],
                        )
                    ],
                )

    def validate_artifacts(self):
        artifacts = {r.id: r.body for r in self.catalog.entries(self.run, "ARTIFACT")}
        for r in artifacts.values():
            path = self.catalog.derived_path(r["file"])
            if not path.is_file() or not path.stat().st_size:
                raise ValueError("派生产物校验失败")
        observations = {r.id for r in self.catalog.entries(self.run, "OBSERVATION")}
        for item in self.catalog.entries(self.run, "EVIDENCE"):
            ev = item.body["evidence"]
            if (
                ev["workspace_id"] != self.run.workspace
                or ev["media_revision_id"] != self.run.revision
                or not set(ev["artifact_ids"]) <= artifacts.keys()
                or not set(ev["observation_ids"]) <= observations
                or not 0
                <= ev["range"]["start_us"]
                < ev["range"]["end_us"]
                <= self.source.probe["duration_us"]
            ):
                raise ValueError("证据引用或时间校验失败")
