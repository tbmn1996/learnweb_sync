"""Independent requirements from the approved semester-repair plan."""
import importlib.util
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import textwrap
import unittest
from unittest import mock
from datetime import datetime, timezone

from apscheduler.triggers.cron import CronTrigger
from bs4 import BeautifulSoup
import requests
import learnweb_sync as lws
from test_learnweb_sync import FakeHttpResponse, FolderNotionFixture, make_activity, make_resource_file_result


def active_course_map():
    return {"1": {"name": "Course", "shortname": "Course_2026", "notion_page_id": "course-page",
                  "sync_content": True, "url": "https://learnweb.invalid/course/view.php?id=1", "conflict": False}}


class SyncRepairAcceptance(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "state.db"
        for name, value in [("DB_PATH", self.path), ("NOTION_TOKEN", "placeholder"),
                            ("NOTION_COURSES_DB_ID", "courses-db"), ("NOTION_LW_DB_ID", "content-db")]:
            patch = mock.patch.object(lws, name, value)
            patch.start()
            self.addCleanup(patch.stop)

    def insert(self, *, notion_id=None, status="new"):
        connection = lws.init_db()
        lws.upsert_activity(connection, make_activity(cmid="1", course_id="1", modtype="resource"))
        connection.execute("UPDATE resources SET notion_id=?, status=? WHERE cmid='1'", (notion_id, status))
        connection.commit()
        connection.row_factory = sqlite3.Row
        return connection, dict(connection.execute("SELECT * FROM resources WHERE cmid='1'").fetchone())

    def assert_run_fails(self):
        with mock.patch.object(sys, "argv", ["learnweb_sync.py", "run"]):
            with self.assertRaises(SystemExit) as result:
                lws.main()
        self.assertNotEqual(result.exception.code, 0)

    def test_course_load_failure_cannot_report_successful_run(self):
        empty = {"by_course_id": {}, "by_lw_id": {}, "duplicate_course_ids": set(), "duplicate_lw_ids": set()}
        with (
            mock.patch.object(lws, "login", return_value=True),
            mock.patch.object(lws, "get_courses", return_value=[{"course_id": "1", "name": "Course", "url": "https://learnweb.invalid/course/1"}]),
            mock.patch.object(lws, "notion_query_courses_db", return_value=empty),
            mock.patch.object(lws, "_load_course_page", side_effect=requests.ConnectionError("offline failure")),
        ):
            self.assert_run_fails()

    def test_scan_failure_cannot_report_successful_run(self):
        with (
            mock.patch.object(lws, "login", return_value=True),
            mock.patch.object(lws, "cmd_sync_courses", return_value=active_course_map()),
            mock.patch.object(lws, "_load_course_page", side_effect=requests.ConnectionError("offline failure")),
        ):
            self.assert_run_fails()

    def test_push_failure_cannot_report_successful_run(self):
        conn, _ = self.insert()
        conn.close()
        with (
            mock.patch.object(lws, "login", return_value=True),
            mock.patch.object(lws, "cmd_sync_courses", return_value=active_course_map()),
            mock.patch.object(lws, "cmd_scan", return_value={"tracked_only_courses": [], "errors": []}),
            mock.patch.object(lws, "_push_resource_activity", return_value={"status": "retryable_error", "modtype": "resource"}),
        ):
            self.assert_run_fails()

    def test_failed_upload_is_not_a_synced_resource(self):
        conn, row = self.insert()
        self.addCleanup(conn.close)
        response = mock.Mock()
        response.json.return_value = {"results": [], "id": "possible-pending-page"}
        with (
            mock.patch.object(lws, "_download_resource", return_value=make_resource_file_result()),
            mock.patch.object(lws, "_notion_request", return_value=response),
            mock.patch.object(lws, "notion_upload_file", side_effect=requests.ConnectionError("upload failed")) as upload,
            mock.patch.object(lws, "notion_create_lw_page", return_value="possible-pending-page"),
        ):
            result = lws._push_resource_activity(conn, mock.Mock(), row, active_course_map(), "course-page")
        upload.assert_called_once()
        self.assertNotEqual(conn.execute("SELECT status FROM resources WHERE cmid='1'").fetchone()[0], "synced")
        self.assertNotIn(result["status"], ("created", "synced"))

    def test_login_page_cannot_mark_existing_course_contents_removed(self):
        conn, _ = self.insert(notion_id="existing", status="synced")
        conn.close()
        login = BeautifulSoup('<html><body id="page-login-index"><form action="/login/index.php"><input name="logintoken"></form></body></html>', "html.parser")
        with mock.patch.object(lws, "_load_course_page", return_value=login):
            try:
                lws.cmd_scan(session=mock.Mock(), course_map=active_course_map())
            except (RuntimeError, SystemExit):
                pass
        with sqlite3.connect(self.path) as conn:
            self.assertNotEqual(conn.execute("SELECT status FROM resources WHERE cmid='1'").fetchone()[0], "removed")

    def test_actual_html_attachment_is_downloaded_as_a_file(self):
        payload = b"<!doctype html><html><title>Teaching dashboard</title><body>Interactive example</body></html>"
        response = FakeHttpResponse(
            content=payload,
            url="https://learnweb.invalid/pluginfile.php/7/mod_resource/content/0/dashboard.html",
            headers={"Content-Type": "text/html", "Content-Disposition": 'attachment; filename="dashboard.html"'},
        )
        session = mock.Mock()
        session.get.return_value = response
        result = lws._download_resource(session, "https://learnweb.invalid/mod/resource/view.php?id=1")
        self.assertEqual(result.kind, "file")
        self.assertEqual(result.file_name, "dashboard.html")

    def test_existing_identical_resource_with_notion_normalized_name_is_not_duplicated(self):
        conn, row = self.insert(notion_id="existing", status="synced")
        self.addCleanup(conn.close)
        payload = b"original presentation"
        row.update(file_name="Präsentation Kopie.pdf", file_hash=hashlib.md5(payload).hexdigest())
        page = {"properties": {"LW Download": {"files": [{"name": "Prasentation_Kopie.pdf", "type": "external", "external": {"url": "https://files.invalid/existing"}}]}}}
        with (
            mock.patch.object(lws, "_repair_lw_metadata", return_value=page),
            mock.patch.object(lws, "_remote_attachment_hash", return_value=row["file_hash"]),
            mock.patch.object(lws, "_download_resource", return_value=make_resource_file_result(row["file_name"], payload)),
            mock.patch.object(lws, "notion_upload_file", return_value="unwanted-upload") as upload,
            mock.patch.object(lws, "notion_update_lw_page") as update,
        ):
            result = lws._push_resource_activity(conn, mock.Mock(), row, active_course_map(), "course-page")
        upload.assert_not_called()
        update.assert_not_called()
        self.assertEqual(result["status"], "skipped")

    def test_recovered_remote_page_matches_identical_file_despite_normalized_name(self):
        digest = hashlib.md5(b"same PDF").hexdigest()
        resource = {"cmid": "1", "course_id": "1", "name": "Presentation", "file_name": "Präsentation Kopie.pdf", "file_hash": digest}
        page = {"id": "existing", "properties": {"Kurs": {"select": None}, "KurseLearnWeb": {"relation": [{"id": "course-page"}]}, "LW Download": {"files": [{"name": "Prasentation_Kopie.pdf", "type": "external", "external": {"url": "https://files.invalid/existing"}}]}}}
        with (
            mock.patch.object(lws, "_notion_find_lw_page", return_value=page),
            mock.patch.object(lws, "_notion_get_lw_page", return_value=page),
            mock.patch.object(lws, "_remote_attachment_hash", return_value=digest),
            mock.patch.object(lws, "_notion_request") as request,
        ):
            self.assertEqual(lws.notion_create_lw_page(resource, "unneeded-upload", "course-page"), "existing")
        self.assertFalse(any("LW Download" in call.kwargs.get("json", {}).get("properties", {}) for call in request.call_args_list))

    def test_owned_folder_file_update_preserves_manual_attachment(self):
        conn, row = self.insert(notion_id="existing", status="synced")
        self.addCleanup(conn.close)
        row.update(modtype="folder", section="Week", file_hash="old-inventory")
        url = "https://learnweb.invalid/pluginfile.php/own.pdf"
        old_hash = hashlib.md5(b"old original").hexdigest()
        conn.execute("UPDATE resources SET attachment_state=? WHERE cmid='1'", (json.dumps({url: {"name": "Own.pdf", "hash": old_hash}}),))
        conn.commit()
        page = {"properties": {"Kurs": {"select": None}, "KurseLearnWeb": {"relation": [{"id": "course-page"}]}, "LW Download": {"files": [
            {"name": "Own.pdf", "type": "external", "external": {"url": "https://files.invalid/old"}},
            {"name": "Manual.pdf", "type": "external", "external": {"url": "https://files.invalid/manual"}},
        ]}}}
        with (
            mock.patch.object(lws, "_fetch_activity_soup"),
            mock.patch.object(lws, "_extract_folder_files", return_value=[("Own.pdf", url)]),
            mock.patch.object(lws, "_download_file_url", return_value=(b"updated original", "Own.pdf")),
            mock.patch.object(lws, "_repair_lw_metadata", return_value=page),
            mock.patch.object(lws, "_notion_get_lw_page", return_value=page),
            mock.patch.object(lws, "_remote_attachment_hash", return_value=old_hash),
            mock.patch.object(lws, "notion_upload_file", return_value="updated-upload"),
            mock.patch.object(lws, "_notion_request") as request,
        ):
            result = lws._push_folder_activity(conn, mock.Mock(), row, active_course_map(), "course-page")
        self.assertEqual(result["status"], "updated")
        writes = [c.kwargs["json"]["properties"]["LW Download"]["files"] for c in request.call_args_list if "LW Download" in c.kwargs.get("json", {}).get("properties", {})]
        self.assertTrue(writes)
        files = writes[-1]
        self.assertEqual([f["name"] for f in files].count("Own.pdf"), 1)
        self.assertEqual([f["name"] for f in files].count("Manual.pdf"), 1)
        self.assertIn({"name": "Manual.pdf", "type": "external", "external": {"url": "https://files.invalid/manual"}}, files)
        self.assertTrue(any(f.get("file_upload", {}).get("id") == "updated-upload" for f in files))

    def test_partial_folder_keeps_success_and_does_not_retry_blocked_upload(self):
        conn, row = self.insert()
        self.addCleanup(conn.close)
        row["modtype"] = "folder"
        conn.execute("UPDATE resources SET modtype='folder' WHERE cmid='1'")
        conn.commit()
        files = [("good.py", "https://learnweb.invalid/good.py"), ("blocked.ipynb", "https://learnweb.invalid/blocked.ipynb")]
        fixture = FolderNotionFixture()
        def download(session, url):
            return (b"good" if url.endswith(".py") else b"notebook", url.rsplit("/", 1)[1])
        def upload(data, filename):
            if filename.endswith(".ipynb"):
                raise lws.UploadBlockedError("Provider rejects original extension")
            return "good-upload"
        with (
            mock.patch.object(lws, "NOTION_LW_DB_ID", "lw-db"),
            mock.patch.object(lws, "_fetch_activity_soup"),
            mock.patch.object(lws, "_extract_folder_files", return_value=files),
            mock.patch.object(lws, "_download_file_url", side_effect=download) as downloads,
            mock.patch.object(lws, "notion_upload_file", side_effect=upload) as uploads,
            mock.patch.object(lws, "_notion_request", side_effect=fixture.request),
        ):
            first = lws._push_folder_activity(conn, mock.Mock(), row, active_course_map(), "course-page")
            self.assertEqual(first["status"], "terminal_error")
            saved = dict(conn.execute("SELECT * FROM resources WHERE cmid='1'").fetchone())
            self.assertEqual(saved["notion_id"], "folder-page")
            self.assertEqual(saved["status"], "error")
            self.assertEqual([f["name"] for f in fixture.page["properties"]["LW Download"]["files"]], ["good.py"])
            previous_writes = len(fixture.writes)
            second = lws._push_folder_activity(conn, mock.Mock(), saved, active_course_map(), "course-page")
            self.assertEqual(second["status"], "terminal_error")
            self.assertEqual(len(fixture.writes), previous_writes)
            self.assertEqual(uploads.call_count, 2, "Repeat must retain good upload and original-format blocker")
            self.assertEqual(sum(call.args[1].endswith("blocked.ipynb") for call in downloads.call_args_list), 1,
                             "Unchanged permanent format blocker must not be downloaded on every run")
            files[1] = ("blocked.ipynb", "https://learnweb.invalid/version2/blocked.ipynb")
            revised = dict(conn.execute("SELECT * FROM resources WHERE cmid='1'").fetchone())
            lws._push_folder_activity(conn, mock.Mock(), revised, active_course_map(), "course-page")
            self.assertEqual(sum(call.args[1].endswith("blocked.ipynb") for call in downloads.call_args_list), 2,
                             "A new source URL must be evaluated independently of the old blocker")
            self.assertEqual(uploads.call_count, 3)

    def test_folder_rechecks_ownership_immediately_before_replacement(self):
        conn, row = self.insert(notion_id="existing", status="synced")
        self.addCleanup(conn.close)
        row.update(modtype="folder", file_hash="old-inventory")
        url = "https://learnweb.invalid/Own.pdf"
        old_hash = hashlib.md5(b"old original").hexdigest()
        conn.execute("UPDATE resources SET attachment_state=? WHERE cmid='1'", (json.dumps({url: {"name": "Own.pdf", "hash": old_hash}}),))
        conn.commit()
        page = {"properties": {"Kurs": {"select": None}, "KurseLearnWeb": {"relation": [{"id": "course-page"}]}, "LW Download": {"files": [
            {"name": "Own.pdf", "type": "external", "external": {"url": "https://files.invalid/own"}},
        ]}}}
        with (
            mock.patch.object(lws, "_fetch_activity_soup"),
            mock.patch.object(lws, "_extract_folder_files", return_value=[("Own.pdf", url)]),
            mock.patch.object(lws, "_download_file_url", return_value=(b"updated original", "Own.pdf")),
            mock.patch.object(lws, "_repair_lw_metadata", return_value=page),
            mock.patch.object(lws, "_notion_get_lw_page", return_value=page),
            mock.patch.object(lws, "_remote_attachment_hash", side_effect=[old_hash, hashlib.md5(b"manual replacement").hexdigest()]) as hashes,
            mock.patch.object(lws, "notion_upload_file", return_value="updated-upload"),
            mock.patch.object(lws, "_notion_request") as request,
        ):
            result = lws._push_folder_activity(conn, mock.Mock(), row, active_course_map(), "course-page")
        self.assertEqual(hashes.call_count, 2)
        self.assertIn(result["status"], ("error", "terminal_error", "retryable_error"))
        self.assertFalse(any("LW Download" in call.kwargs.get("json", {}).get("properties", {}) for call in request.call_args_list))

    def test_known_provider_blocker_has_distinct_incomplete_exit(self):
        conn, _ = self.insert()
        conn.close()
        with (
            mock.patch.object(lws, "login", return_value=True),
            mock.patch.object(lws, "cmd_sync_courses", return_value=active_course_map()),
            mock.patch.object(lws, "cmd_scan", return_value={"tracked_only_courses": [], "errors": []}),
            mock.patch.object(lws, "_download_resource", return_value=make_resource_file_result("blocked.ipynb", b"{}")),
            mock.patch.object(lws, "notion_upload_file", side_effect=lws.UploadBlockedError("Provider rejects original extension")),
            mock.patch.object(sys, "argv", ["learnweb_sync.py", "run"]),
        ):
            with self.assertRaises(SystemExit) as result:
                lws.main()
        self.assertEqual(result.exception.code, 2, "Known content blocker is incomplete, not an operational crash or success")

    def test_distinct_source_files_with_identical_bytes_remain_two_attachments(self):
        conn, row = self.insert()
        self.addCleanup(conn.close)
        row["modtype"] = "folder"
        files = [(name, "https://learnweb.invalid/" + name) for name in ("Template.pdf", "Solution.pdf")]
        fixture = FolderNotionFixture()
        with (
            mock.patch.object(lws, "NOTION_LW_DB_ID", "lw-db"),
            mock.patch.object(lws, "_fetch_activity_soup"),
            mock.patch.object(lws, "_extract_folder_files", return_value=files),
            mock.patch.object(lws, "_download_file_url", side_effect=lambda session, url: (b"same original bytes", url.rsplit("/", 1)[1])),
            mock.patch.object(lws, "_remote_attachment_hash", return_value=hashlib.md5(b"same original bytes").hexdigest()),
            mock.patch.object(lws, "notion_upload_file", side_effect=["template-upload", "solution-upload"]),
            mock.patch.object(lws, "_notion_request", side_effect=fixture.request),
        ):
            result = lws._push_folder_activity(conn, mock.Mock(), row, active_course_map(), "course-page")
        self.assertEqual(result["status"], "created")
        self.assertCountEqual([f["name"] for f in fixture.page["properties"]["LW Download"]["files"]], ["Template.pdf", "Solution.pdf"])

    def test_unreadable_renamed_attachment_does_not_cause_duplicate_upload(self):
        conn, row = self.insert(notion_id="existing", status="synced")
        self.addCleanup(conn.close)
        payload = b"original presentation"
        row.update(file_name="Moodle-name.pdf", file_hash=hashlib.md5(payload).hexdigest())
        page = {"properties": {"LW Download": {"files": [{"name": "Renamed.pdf", "type": "external", "external": {"url": "https://files.invalid/existing"}}]}}}
        with (
            mock.patch.object(lws, "_repair_lw_metadata", return_value=page),
            mock.patch.object(lws, "_remote_attachment_hash", side_effect=RuntimeError("CDN temporary error")),
            mock.patch.object(lws, "_download_resource", return_value=make_resource_file_result(row["file_name"], payload)),
            mock.patch.object(lws, "notion_upload_file", return_value="unwanted-upload") as upload,
            mock.patch.object(lws, "notion_update_lw_page") as update,
        ):
            result = lws._push_resource_activity(conn, mock.Mock(), row, active_course_map(), "course-page")
        upload.assert_not_called()
        update.assert_not_called()
        self.assertEqual(result["status"], "error")

    def test_resource_without_file_checkpoint_preserves_same_named_manual_file(self):
        conn, row = self.insert(notion_id="existing", status="error")
        self.addCleanup(conn.close)
        page = {"properties": {"Kurs": {"select": None}, "KurseLearnWeb": {"relation": [{"id": "course-page"}]}, "LW Download": {"files": [
            {"name": "Source.pdf", "type": "external", "external": {"url": "https://files.invalid/manual"}},
        ]}}}
        with (
            mock.patch.object(lws, "_repair_lw_metadata", return_value=page),
            mock.patch.object(lws, "_notion_get_lw_page", return_value=page),
            mock.patch.object(lws, "_remote_attachment_hash", return_value=hashlib.md5(b"manual bytes").hexdigest()),
            mock.patch.object(lws, "_download_resource", return_value=make_resource_file_result("Source.pdf", b"source bytes")),
            mock.patch.object(lws, "notion_upload_file", return_value="new-upload"),
            mock.patch.object(lws, "_notion_request") as request,
        ):
            result = lws._push_resource_activity(conn, mock.Mock(), row, active_course_map(), "course-page")
        writes = [call for call in request.call_args_list if "LW Download" in call.kwargs.get("json", {}).get("properties", {})]
        self.assertFalse(writes, "Missing ownership checkpoint must not replace same-named manual bytes")
        self.assertIn(result["status"], ("error", "terminal_error", "retryable_error"))

    def test_missing_course_metadata_is_backfilled_without_changing_manual_values(self):
        resource = {"cmid": "1", "course_id": "1", "course_shortname": "Course_2026"}
        empty = {"properties": {"Kurs": {"select": None}, "KurseLearnWeb": {"relation": []}}}
        manual = {"properties": {"Kurs": {"select": {"name": "Manual"}}, "KurseLearnWeb": {"relation": [{"id": "manual-course"}]}}}
        with mock.patch.object(lws, "COURSE_MAP", {"Course_2026": "Inf1"}):
            self.assertEqual(lws._missing_lw_metadata(empty, resource, "course-page"), {
                "Kurs": {"select": {"name": "Inf1"}}, "KurseLearnWeb": {"relation": [{"id": "course-page"}]},
            })
            self.assertEqual(lws._missing_lw_metadata(manual, resource, "course-page"), {})

    def test_ambiguous_or_failed_content_lookup_never_creates_page(self):
        resource = {"cmid": "1", "course_id": "1", "name": "Source"}
        for response in (RuntimeError("lookup offline"), {"results": [{"id": "one"}, {"id": "two"}], "has_more": False}):
            with self.subTest(response=type(response).__name__):
                calls = []
                def request(method, url, **kwargs):
                    calls.append(url)
                    if isinstance(response, Exception):
                        raise response
                    result = mock.Mock()
                    result.json.return_value = response
                    return result
                with mock.patch.object(lws, "_notion_request", side_effect=request):
                    with self.assertRaises(RuntimeError):
                        lws.notion_create_lw_page(resource, None, "course-page")
                self.assertFalse(any(url.endswith("/pages") for url in calls))

    def test_upload_create_includes_original_filename_and_specific_mime(self):
        calls = []
        def request(method, url, **kwargs):
            calls.append((method, url, kwargs))
            response = mock.Mock(status_code=200)
            response.json.return_value = {"id": "upload", "upload_url": "https://notion.invalid/send", "status": "uploaded",
                "bot": {"workspace_limits": {"max_file_upload_size_in_bytes": 5 * 1024**3}}}
            return response
        with mock.patch.object(lws, "_notion_request", side_effect=request):
            lws.notion_upload_file(b"print('example')\n", "example.py")
        create = next(kwargs for method, url, kwargs in calls if method == "POST" and url.endswith("/file_uploads"))
        self.assertEqual(create["json"]["filename"], "example.py")
        self.assertNotEqual(create["json"].get("content_type"), "application/octet-stream")

    def test_large_pdf_uploads_all_parts_before_completion(self):
        payload = b"x" * (20 * 1024 * 1024 + 17)
        parts, events = {}, []
        def request(method, url, **kwargs):
            response = mock.Mock(status_code=200)
            if method == "POST" and url.endswith("/file_uploads"):
                body = kwargs["json"]
                self.assertEqual(body["filename"], "large.pdf")
                self.assertEqual(body["mode"], "multi_part")
                events.append(("create", body["number_of_parts"]))
                response.json.return_value = {"id": "upload", "upload_url": "https://notion.invalid/send", "status": "pending"}
            elif "files" in kwargs:
                part = kwargs["files"]["file"][1]
                part = part.read() if hasattr(part, "read") else part
                number = int(kwargs.get("data", {}).get("part_number", 0))
                parts[number] = part
                events.append(("part", number))
                response.json.return_value = {"id": "upload", "status": "pending"}
            elif method == "POST" and url.endswith("/complete"):
                events.append(("complete", len(parts)))
                response.json.return_value = {"id": "upload", "status": "uploaded"}
            else:
                response.json.return_value = {"id": "upload", "status": "uploaded", "bot": {"workspace_limits": {"max_file_upload_size_in_bytes": 5 * 1024**3}}}
            return response
        with mock.patch.object(lws, "_notion_request", side_effect=request):
            result = lws.notion_upload_file(payload, "large.pdf")
        self.assertEqual(result, "upload")
        count = events[0][1]
        self.assertGreater(count, 1)
        self.assertEqual(set(parts), set(range(1, count + 1)))
        self.assertEqual(b"".join(parts[n] for n in range(1, count + 1)), payload)
        self.assertEqual(events[-1], ("complete", count))
        for n, content in parts.items():
            self.assertLessEqual(len(content), 20 * 1024 * 1024)
            if n < count:
                self.assertGreaterEqual(len(content), 5 * 1024 * 1024)


class ServerRepairAcceptance(unittest.TestCase):
    def load_server(self, *, missing=()):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        env = {"LEARNWEB_URL": "https://learnweb.invalid", "LEARNWEB_USERNAME": "dummy", "LEARNWEB_PASSWORD": "dummy",
               "NOTION_TOKEN": "dummy", "NOTION_COURSES_DB_ID": "11111111-1111-1111-1111-111111111111", "NOTION_LW_DB_ID": "22222222-2222-2222-2222-222222222222",
               "SYNC_WEBHOOK_SECRET": "dummy", "STATE_DB_PATH": str(Path(temp.name)/"state.db"),
               "STATE_LOCK_PATH": str(Path(temp.name)/"state.lock")}
        for key in missing:
            env.pop(key, None)
        patch = mock.patch.dict(os.environ, env, clear=True)
        patch.start()
        self.addCleanup(patch.stop)
        name = "server_requirement_" + str(id(temp))
        spec = importlib.util.spec_from_file_location(name, Path(lws.__file__).with_name("server.py"))
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        self.addCleanup(sys.modules.pop, name, None)
        with mock.patch("apscheduler.schedulers.background.BackgroundScheduler.start"):
            spec.loader.exec_module(module)
        return module

    def test_default_schedule_has_all_five_weekdays(self):
        server = self.load_server()
        trigger = CronTrigger.from_crontab(server.CRON_SCHEDULE, timezone="UTC")
        current = datetime(2026, 10, 12, 0, 0, tzinfo=timezone.utc)
        days, previous = [], None
        for _ in range(5):
            next_run = trigger.get_next_fire_time(previous, current)
            days.append(next_run.weekday())
            previous = current = next_run
        self.assertEqual(days, [0, 1, 2, 3, 4])

    def test_health_rejects_missing_notion_configuration(self):
        server = self.load_server(missing=("NOTION_TOKEN",))
        response = server.app.test_client().get("/health")
        self.assertEqual(response.status_code, 503)

    def test_valid_configuration_can_pass_readiness(self):
        server = self.load_server()
        self.assertEqual(server.app.test_client().get("/health").status_code, 200)

    def test_webhook_without_configured_secret_cannot_start_sync(self):
        server = self.load_server(missing=("SYNC_WEBHOOK_SECRET",))
        with mock.patch.object(server, "_run_sync") as run:
            response = server.app.test_client().post("/webhook/sync")
        self.assertIn(response.status_code, (401, 403, 503))
        run.assert_not_called()

    def test_subprocess_start_failure_releases_lock_and_counts_failure(self):
        server = self.load_server()
        server.FAILURE_THRESH = 100
        with mock.patch.object(server.subprocess, "Popen", side_effect=OSError("cannot spawn")):
            try:
                server._scheduled_sync()
            except OSError:
                pass
        self.assertFalse(server._sync_lock.locked())
        self.assertEqual(server._consecutive_failures, 1)

    def test_failure_threshold_really_exits_its_process(self):
        code = textwrap.dedent('''
            import os, sys, tempfile, threading
            from pathlib import Path
            from unittest import mock
            with tempfile.TemporaryDirectory() as tmp:
                os.environ.update(STATE_DB_PATH=str(Path(tmp)/'state.db'), STATE_LOCK_PATH=str(Path(tmp)/'state.lock'), SYNC_FAILURE_EXIT_THRESHOLD='1')
                os.environ.update(LEARNWEB_URL='https://learnweb.invalid', LEARNWEB_USERNAME='dummy', LEARNWEB_PASSWORD='dummy', NOTION_TOKEN='dummy', NOTION_COURSES_DB_ID='11111111-1111-1111-1111-111111111111', NOTION_LW_DB_ID='22222222-2222-2222-2222-222222222222', SYNC_WEBHOOK_SECRET='offline-webhook')
                with mock.patch('apscheduler.schedulers.background.BackgroundScheduler.start'):
                    import server
                print('ENTERED_FAILURE_PROBE', flush=True)
                real_popen = server.subprocess.Popen
                def failed_child(args, **kwargs):
                    return real_popen([sys.executable, '-u', '-c', 'raise SystemExit(1)'], **kwargs)
                with mock.patch.object(server.subprocess, 'Popen', side_effect=failed_child):
                    thread = threading.Thread(target=server._scheduled_sync)
                    thread.start()
                    thread.join(5)
                print('PROCESS_STILL_ALIVE', flush=True)
        ''')
        result = subprocess.run([sys.executable, "-c", code], cwd=Path(lws.__file__).parent,
                                capture_output=True, text=True, timeout=12, start_new_session=True)
        self.assertIn("ENTERED_FAILURE_PROBE", result.stdout)
        self.assertNotIn("Traceback", result.stderr)
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("PROCESS_STILL_ALIVE", result.stdout)


if __name__ == "__main__":
    unittest.main()
