"""Containers supported by the timeline exporter. Previews always use MP4."""

EXPORT_MIME = {"mp4": "video/mp4", "webm": "video/webm", "mkv": "video/x-matroska"}
EXPORT_FORMATS = tuple(EXPORT_MIME)
