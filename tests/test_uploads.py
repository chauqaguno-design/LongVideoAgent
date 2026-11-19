import hashlib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import HTTPException

from cinetrace.storage import Store


def digest(data):
    return hashlib.sha256(data).hexdigest()


class UploadTests(unittest.TestCase):
    @patch("cinetrace.storage.PART_SIZE", 4)
    def test_resume_after_restart_and_complete_once(self):
        with tempfile.TemporaryDirectory() as data:
            chunks = [b"abcd", b"ef"]
            fingerprint = digest(b"".join(hashlib.sha256(chunk).digest() for chunk in chunks))
            store = Store(Path(data))
            upload = store.start_upload("a.mp4", 6, fingerprint)
            video_id = upload["id"]
            store.put_part(video_id, 1, chunks[1], digest(chunks[1]))

            store = Store(Path(data))
            self.assertEqual(store.start_upload("a.mp4", 6, fingerprint)["received"], [1])
            with self.assertRaises(HTTPException):
                store.finish_upload(video_id, None, None)
            store.put_part(video_id, 0, chunks[0], digest(chunks[0]))
            store.put_part(video_id, 0, chunks[0], digest(chunks[0]))
            with self.assertRaises(HTTPException):
                store.put_part(video_id, 0, b"oops", digest(b"oops"))
            store.finish_upload(video_id, digest(b"abcdef"), lambda path: {"duration_ms": 1500})
            self.assertEqual(store.media_path(video_id).read_bytes(), b"abcdef")
            self.assertEqual(store.claim(), video_id)
            store.finish_upload(video_id, None, None)
            self.assertIsNone(store.claim())
            self.assertEqual(store.pending_notifications(), [video_id])
            store.mark_notified(video_id)
            self.assertEqual(store.pending_notifications(), [])

            # Simulate a worker interrupted after saving the first real evidence.
            context = store.video(video_id)
            context["evidence"].append({"text": "first"})
            context["index"] = {"status": "pending", "model": "test"}
            store.save_context(video_id, context)
            store = Store(Path(data))
            self.assertEqual(store.claim(), video_id)
            self.assertEqual(store.video(video_id)["evidence"], [{"text": "first"}])
            self.assertEqual(store.video(video_id)["index"]["model"], "test")
            other = store.start_upload("b.mp4", 6, fingerprint)
            self.assertEqual(store.video(other["id"])["evidence"], [])

    def test_incorrect_manifest_can_be_reuploaded(self):
        with tempfile.TemporaryDirectory() as data:
            store = Store(Path(data))
            fingerprint = digest(hashlib.sha256(b"good").digest())
            upload = store.start_upload("movie.mp4", 4, fingerprint)
            store.put_part(upload["id"], 0, b"oops", digest(b"oops"))
            with self.assertRaises(HTTPException):
                store.finish_upload(upload["id"], None, None)
            self.assertEqual(store.start_upload("movie.mp4", 4, fingerprint)["received"], [])
            store.put_part(upload["id"], 0, b"good", digest(b"good"))
            store.finish_upload(upload["id"], None, lambda path: {"duration_ms": 100})
            self.assertEqual(store.media_path(upload["id"]).read_bytes(), b"good")
