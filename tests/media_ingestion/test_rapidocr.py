import json
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

from cutsieve.infrastructure.media import local_inference, processing_units
from cutsieve.infrastructure.persistence.processing_worker import ProcessingWorker
from cutsieve.presentation.api.capabilities import capabilities


@pytest.mark.parametrize("via_engine", [False, True])
def test_rapidocr_adapter_preserves_the_local_ocr_report(monkeypatch, tmp_path, via_engine):
    monkeypatch.delenv("CUTSIEVE_OCR_ENGINE", raising=False)
    if via_engine:
        monkeypatch.setenv("CUTSIEVE_OCR_ENGINE", " RapidOCR ")
    path = tmp_path / "frame.png"

    class RapidOCR:
        def __call__(self, filename):
            assert filename == str(path)
            polygon = [(1, 2), (3, 2), (3, 4), (1, 4)]
            return [(polygon, "站台 A", "0.95"), (polygon, " ", 0.99)], [0.2]

    monkeypatch.setitem(sys.modules, "rapidocr_onnxruntime", SimpleNamespace(RapidOCR=RapidOCR))
    monkeypatch.setattr(local_inference, "version", lambda name: {"rapidocr-onnxruntime": "1.4.4"}[name])
    provider, version, infer = local_inference.build_ocr(tmp_path if via_engine else Path("RapidOCR"))
    assert (provider, version) == ("rapidocr-onnxruntime", "1.4.4")
    assert infer(path) == {"lines": [{
        "text": "站台 A", "confidence": 0.95,
        "polygon": [[1.0, 2.0], [3.0, 2.0], [3.0, 4.0], [1.0, 4.0]],
    }]}
    monkeypatch.setattr(RapidOCR, "__call__", lambda *args: (None, [0.1]))
    assert infer(path) == {"lines": []}


@pytest.mark.parametrize("setting", ["CUTSIEVE_OCR_ENGINE", "CUTSIEVE_PADDLE_OCR_DIR"])
def test_rapidocr_capability_checks_the_selected_python(monkeypatch, setting):
    calls = []

    def command(args, stopped=None, **options):
        calls.append(args)
        assert args[1:3] == ["-I", "-c"]
        assert "import rapidocr_onnxruntime" in args[3]
        if args[0] == "missing-python":
            raise FileNotFoundError
        return b"1.4.4\n", b""

    monkeypatch.setattr(processing_units, "command", command)

    def ocr(env):
        return next(service for service in capabilities(env)["dedicated_services"] if service["name"] == "OCR")

    assert not ocr({})["configured"] and not calls
    env = {setting: " RapidOCR ", "CUTSIEVE_ASR_PYTHON": "asr-python"}
    assert ocr(env) == dict(name="OCR", configured=True, ready=True, verified=True, version="1.4.4")
    assert calls[-1][0] == sys.executable
    assert ocr(dict(env, CUTSIEVE_MEDIA_PYTHON="missing-python")) == dict(
        name="OCR", configured=True, ready=False, verified=False, version=None,
    )
    assert calls[-1][0] == "missing-python"


def test_worker_caps_ocr_on_long_subtitled_media(monkeypatch, tmp_path):
    monkeypatch.setenv("CUTSIEVE_OCR_ENGINE", "rapidocr")
    monkeypatch.setenv("CUTSIEVE_MEDIA_PYTHON", "media-python")
    monkeypatch.delenv("CUTSIEVE_OCR_STRIDE", raising=False)
    monkeypatch.delenv("CUTSIEVE_OCR_MAX_FRAMES_WITH_SUBS", raising=False)
    config = ProcessingWorker.config({})
    units = processing_units.ProcessingUnits.__new__(processing_units.ProcessingUnits)
    units.config, units.models = config, config["_local"]
    units.folder = units.temp = tmp_path
    units.reader, units.run = SimpleNamespace(stopped=None), object()
    frames = []
    for index in range(270):
        filename = str(index) + ".jpg"
        (tmp_path / filename).write_bytes(filename.encode())
        frames.append(SimpleNamespace(id=index, body={"frame": {"actual_us": index * 6_000_000}, "file": filename}))
    units.catalog = SimpleNamespace(entries=lambda *_: reversed(frames), derived_path=lambda file: tmp_path / file)

    def command(args, stopped, **options):
        assert args[:2] == ["media-python", "-I"]
        spec = json.loads(args[-1].read_text(encoding="utf-8"))
        assert spec["model"] == "rapidocr" and spec["engine"] == "rapidocr-onnxruntime"
        return b"", b""

    monkeypatch.setattr(processing_units, "command", command)
    monkeypatch.setattr(processing_units, "ocr_version", lambda *_: "1.4.4")
    units.units = {"subtitles": {"complete": True, "has_text": True}}
    selected = [frame.id for _, frame, _ in units.local_batch("ocr")]
    assert len(selected) == 60 and selected[0] == 0 and selected[-1] == 269
    assert all(4 <= right - left <= 5 for left, right in zip(selected, selected[1:]))
    units.units["subtitles"] = {"complete": True, "has_text": False}
    assert len(units.local_batch("ocr")) == 270
    monkeypatch.setenv("CUTSIEVE_OCR_STRIDE", "3")
    monkeypatch.setenv("CUTSIEVE_OCR_MAX_FRAMES_WITH_SUBS", "20")
    assert ProcessingWorker.config({})["_ocr_sampling"] == {"stride": 3, "max_with_subs": 20}
