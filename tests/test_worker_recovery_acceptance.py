"""Worker acceptance from user requirements, using a small in-memory Notion protocol."""
import copy
import io
import json
from pathlib import Path
import sqlite3
import subprocess
import tempfile
import unittest
from contextlib import ExitStack, redirect_stdout
from unittest import mock
from urllib.parse import urlparse

from bs4 import BeautifulSoup
import requests
import learnweb_sync as lws
from transcription import downloader, manifest, recordings, transcriber
from transcription.types import Recording, Segment


class SimulatedCrash(BaseException):
    pass


class NotionMemory:
    def __init__(self):
        self.pages = {}
        self.counter = 0
        self.crash_after_append = False
        self.deleted = []

    def append(self, page, children):
        for child in children:
            self.counter += 1
            self.pages[page]["children"].append({**copy.deepcopy(child), "id": f"block-{self.counter}", "has_children": False})

    def request(self, method, url, **kwargs):
        path = urlparse(url).path
        body = kwargs.get("json", {})
        if method == "POST" and path.endswith("/query"):
            database = path.split("/")[-2]
            wanted = body["filter"]["rich_text"]["equals"]
            found = []
            for page in self.pages.values():
                value = "".join(x.get("plain_text", x.get("text", {}).get("content", "")) for x in page["properties"].get("Quelle", {}).get("rich_text", []))
                if page["parent"]["database_id"] == database and value == wanted:
                    found.append(copy.deepcopy(page))
            data = {"results": found, "has_more": False}
        elif method == "POST" and path.endswith("/pages"):
            ident = f"page-{len(self.pages)+1}"
            self.pages[ident] = {"id": ident, "parent": copy.deepcopy(body["parent"]), "properties": copy.deepcopy(body["properties"]), "children": []}
            data = {"id": ident}
        elif method == "PATCH" and "/pages/" in path:
            ident = path.split("/")[-1]
            self.pages[ident]["properties"].update(copy.deepcopy(body["properties"]))
            data = copy.deepcopy(self.pages[ident])
        elif path.endswith("/children"):
            ident = path.split("/")[-2]
            if method == "PATCH":
                before = len(self.pages[ident]["children"])
                self.append(ident, body["children"])
                if self.crash_after_append:
                    self.crash_after_append = False
                    raise SimulatedCrash("remote append committed before local checkpoint")
                data = {"results": copy.deepcopy(self.pages[ident]["children"][before:]), "has_more": False}
            elif method == "GET":
                data = {"results": copy.deepcopy(self.pages[ident]["children"]), "has_more": False}
            else:
                raise AssertionError((method, path))
        elif method == "DELETE" and "/blocks/" in path:
            ident = path.split("/")[-1]
            self.deleted.append(ident)
            for page in self.pages.values():
                page["children"] = [x for x in page["children"] if x["id"] != ident]
            data = {"id": ident, "archived": True}
        else:
            raise AssertionError((method, path))
        response = mock.Mock(status_code=200)
        response.json.return_value = data
        return response

    def meeting(self):
        return next(x for x in self.pages.values() if x["parent"]["database_id"] == lws.NOTION_MEETING_DB_ID)

    def text(self):
        return "\n".join("".join(t.get("text", {}).get("content", "") for t in b.get(b["type"], {}).get("rich_text", [])) for b in self.meeting()["children"])


class WorkerRecoveryAcceptance(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.temp.name)
        self.store = NotionMemory()
        self.rec = Recording(cmid="123", course_id="1", title="Lecture", source_url="https://learnweb.invalid/opencast/123", episode_id="episode-1", media_url="https://learnweb.invalid/media.mp4")
        course = {"1": {"name": "Course", "shortname": "Course_2026", "notion_page_id": "course-page", "sync_content": True, "url": "https://learnweb.invalid/course/view.php?id=1", "conflict": False}}
        patches = [
            mock.patch.object(lws, "DB_PATH", self.root / "state.db"),
            mock.patch.object(lws, "TRANSCRIBE_LOCK_PATH", self.root / "lock"),
            mock.patch.object(lws, "TRANSCRIBE_WORK_DIR", self.root / "work"),
            mock.patch.object(lws, "NOTION_TOKEN", "placeholder"),
            mock.patch.object(lws, "NOTION_LW_DB_ID", "content-db"),
            mock.patch.object(lws, "NOTION_MEETING_DB_ID", "meeting-db"),
            mock.patch.object(lws, "login", return_value=True),
            mock.patch.object(lws, "cmd_sync_courses", return_value=course),
            mock.patch.object(lws, "_load_course_page", return_value=BeautifulSoup('<ol class="breadcrumb"><li>Course_2026</li></ol>', "html.parser")),
            mock.patch.object(lws, "_extract_activities", return_value=[{"cmid": "123", "modtype": "opencast", "view_url": self.rec.source_url, "name": "Lecture"}]),
            mock.patch.object(recordings, "discover_recordings", return_value=[self.rec]),
            mock.patch.object(downloader, "download_media", return_value=self.root / "media.mp4"),
            mock.patch.object(downloader, "extract_audio", return_value=self.root / "audio.wav"),
            mock.patch.object(downloader, "probe_duration", return_value=1.0),
            mock.patch.object(lws, "_notion_request", side_effect=self.store.request),
        ]
        for patch in patches:
            self.stack.enter_context(patch)
        self.transcribe = self.stack.enter_context(mock.patch.object(transcriber, "transcribe", return_value=([Segment(0, 1, "original lecture text")], "fixture")))

    def run_worker(self, **kwargs):
        with redirect_stdout(io.StringIO()) as output:
            lws.cmd_transcribe(course="1", **kwargs)
        return output.getvalue()

    def test_append_committed_before_crash_is_resumed_without_duplicates(self):
        self.store.crash_after_append = True
        with self.assertRaises(SimulatedCrash):
            self.run_worker()
        self.assertEqual(len(self.store.pages), 2)
        self.run_worker()
        self.assertEqual(len(self.store.pages), 2)
        self.assertEqual(self.store.text().count("original lecture text"), 1)
        with sqlite3.connect(self.root / "state.db") as conn:
            self.assertEqual(conn.execute("SELECT status FROM transcripts").fetchone()[0], "done")

    def test_force_preserves_manual_note_when_append_counter_is_zero(self):
        self.run_worker()
        meeting = self.store.meeting()
        self.store.append(meeting["id"], [{"type": "paragraph", "paragraph": {"rich_text": [{"type": "text", "text": {"content": "MY MANUAL NOTE"}}]}}])
        manual_id = meeting["children"][-1]["id"]
        with sqlite3.connect(self.root / "state.db") as conn:
            conn.execute("UPDATE transcripts SET appended_block_count=0")
        self.transcribe.return_value = ([Segment(0, 1, "replacement lecture text")], "fixture")
        self.run_worker(force=True)
        text = self.store.text()
        self.assertEqual(len(self.store.pages), 2)
        self.assertEqual(text.count("MY MANUAL NOTE"), 1)
        self.assertEqual(text.count("replacement lecture text"), 1)
        self.assertNotIn("original lecture text", text)
        self.assertNotIn(manual_id, self.store.deleted)

    def test_lookup_error_does_not_become_a_negative_match(self):
        with mock.patch.object(lws, "_notion_request", side_effect=requests.HTTPError("lookup failed")):
            with self.assertRaises(Exception):
                lws._notion_find_page_by_quelle("content-db", "recording-key")
        self.assertEqual(self.store.pages, {})

    def test_limit_cannot_hide_an_earlier_recording_failure(self):
        with (
            mock.patch.object(recordings, "discover_recordings", return_value=[self.rec, self.rec, self.rec]),
            mock.patch.object(lws, "_process_recording", side_effect=[{"key": "failed", "stage": "download", "reason": "fixture", "title": "Failed lecture"}, "processed"]) as process,
        ):
            with self.assertRaises(SystemExit) as result:
                self.run_worker(limit=1)
        self.assertEqual(result.exception.code, 2)
        self.assertEqual(process.call_count, 2)

    def test_dry_run_uses_readonly_course_lookup(self):
        indexed = {"1": {"page_id": "course-page", "lw_id": "Course_2026", "sync_content": True, "url": "https://learnweb.invalid/course/view.php?id=1", "course_id": "1"}}
        with (
            mock.patch.object(lws, "notion_query_courses_db", return_value={"by_course_id": indexed, "by_lw_id": {}, "duplicate_course_ids": set(), "duplicate_lw_ids": set()}),
            mock.patch.object(lws, "get_courses", return_value=[{"course_id": "1", "name": "Course", "url": "https://learnweb.invalid/course/view.php?id=1"}]),
            mock.patch.object(lws, "cmd_sync_courses", side_effect=AssertionError("write-capable sync not allowed in dry-run")),
        ):
            self.run_worker(dry_run=True)
        self.assertEqual(self.store.pages, {})
        self.assertFalse((self.root / "state.db").exists())


class YoutubeRetryAcceptance(unittest.TestCase):
    def test_transient_challenge_retries_are_bounded(self):
        failed = subprocess.CompletedProcess([], 1, "", "ERROR: HTTP Error 403: Forbidden; n challenge solving failed")
        success = subprocess.CompletedProcess([], 0, "ok", "")
        with mock.patch.object(downloader.subprocess, "run", side_effect=[failed, failed, success]) as run, mock.patch.object(downloader.time, "sleep"):
            result = downloader._run_youtube_command(["yt-dlp"], timeout=10)
        self.assertEqual(run.call_count, 3)
        self.assertEqual(result.returncode, 0)

    def test_unavailable_video_does_not_retry(self):
        failed = subprocess.CompletedProcess([], 1, "", "ERROR: Video unavailable: video has been removed")
        with mock.patch.object(downloader.subprocess, "run", return_value=failed) as run, mock.patch.object(downloader.time, "sleep"):
            downloader._run_youtube_command(["yt-dlp"], timeout=10)
        self.assertEqual(run.call_count, 1)

    def test_duplicate_cmid_is_resolved_once(self):
        activity = {"cmid": "55", "modtype": "url", "name": "Lecture video", "view_url": "https://learnweb.invalid/mod/url/view.php?id=55"}
        with (
            mock.patch.object(lws, "_extract_activities", return_value=[activity, activity]),
            mock.patch.object(lws, "_extract_url_target", return_value="https://www.youtube.com/watch?v=abcdefghijk") as resolve,
            mock.patch.object(lws, "_youtube_title_needs_probe", return_value=False),
        ):
            result = lws.discover_youtube_recordings(mock.Mock(), BeautifulSoup("<html></html>", "html.parser"), "", course_id="1")
        self.assertEqual(resolve.call_count, 1)
        self.assertEqual(len(result), 1)
