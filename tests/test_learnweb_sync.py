import io
import sqlite3
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

import requests
from bs4 import BeautifulSoup

import learnweb_sync as lws


class FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload


class FakeHttpResponse:
    def __init__(
        self,
        *,
        text: str = "",
        url: str = "https://example.com",
        headers: dict | None = None,
        status_code: int = 200,
        content: bytes | None = None,
    ):
        self.text = text
        self.url = url
        self.headers = headers or {}
        self.status_code = status_code
        self._content = content if content is not None else text.encode("utf-8")

    def iter_content(self, chunk_size=8192):
        for start in range(0, len(self._content), chunk_size):
            yield self._content[start:start + chunk_size]

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}")

    def close(self):
        return None


def make_course_page(shortname: str) -> BeautifulSoup:
    html = f"""
    <html>
      <body>
        <ol class="breadcrumb">
          <li>Dashboard</li>
          <li>{shortname}</li>
        </ol>
      </body>
    </html>
    """
    return BeautifulSoup(html, "html.parser")


def make_notion_page(title: str, url: str | None, sync_content: bool = False, page_id: str = "page-1"):
    return {
        "id": page_id,
        "properties": {
            "LW-ID": {"title": [{"plain_text": title}]},
            "SyncContent": {"checkbox": sync_content},
            "URL": {"url": url},
        },
    }


def make_resource_file_result(
    file_name: str = "file.pdf",
    file_bytes: bytes = b"pdf-bytes",
    final_url: str = "https://example.com/pluginfile.php/file.pdf",
):
    return lws.ResourceDownloadResult(
        kind="file",
        file_name=file_name,
        file_bytes=file_bytes,
        final_url=final_url,
    )


def make_activity(**overrides):
    activity = {
        "cmid": "cm-1",
        "course_id": "1",
        "course_name": "Course 1",
        "course_shortname": "Course_1",
        "modtype": "resource",
        "name": "Lecture 1",
        "section": "General",
        "view_url": "https://example.com/mod/resource/view.php?id=cm-1",
        "restricted": False,
        "availability_text": None,
    }
    activity.update(overrides)
    return activity


def make_course_page_with_activity(
    *,
    cmid: str = "cm-1",
    modtype: str = "resource",
    name: str = "Lecture 1",
    href: str | None = "https://example.com/mod/resource/view.php?id=cm-1",
    restriction_text: str | None = None,
    restriction_with_data_region: bool = True,
    restriction_classes: str = "activity-availability availabilityinfo isrestricted",
) -> BeautifulSoup:
    link_html = ""
    if href:
        link_html = (
            f'<a class="aalink stretched-link" href="{href}">'
            f'<span class="instancename">{name}</span>'
            "</a>"
        )

    restriction_html = ""
    if restriction_text is not None:
        data_region = ' data-region="availabilityinfo"' if restriction_with_data_region else ""
        restriction_html = (
            f'<div class="{restriction_classes}"{data_region}>{restriction_text}</div>'
        )

    html = f"""
    <html>
      <body>
        <li class="section course-section" data-sectionname="General">
          <ul data-for="cmlist">
            <li data-for="cmitem" data-id="{cmid}" class="activity {modtype} modtype_{modtype}">
              <div data-activityname="{name}">
                {link_html}
              </div>
              {restriction_html}
            </li>
          </ul>
        </li>
      </body>
    </html>
    """
    return BeautifulSoup(html, "html.parser")


class LearnwebSyncTests(unittest.TestCase):
    def test_semester_label_uses_summer_dates(self):
        label = lws._semester_label_for_datetime(
            datetime(2026, 4, 21, 12, 0, tzinfo=timezone.utc)
        )
        self.assertEqual(label, "SoSe 26")

    def test_semester_label_uses_winter_dates_before_april(self):
        label = lws._semester_label_for_datetime(
            datetime(2026, 2, 15, 12, 0, tzinfo=timezone.utc)
        )
        self.assertEqual(label, "WS 25/26")

    def test_semester_label_uses_berlin_timezone_for_boundaries(self):
        label = lws._semester_label_for_datetime(
            datetime(2026, 3, 31, 22, 30, tzinfo=timezone.utc)
        )
        self.assertEqual(label, "SoSe 26")

    def test_semester_label_honors_explicit_override(self):
        with mock.patch.object(lws, "CURRENT_SEMESTER_OVERRIDE", "Archiv 2024"):
            label = lws._semester_label_for_datetime(
                datetime(2026, 4, 21, 12, 0, tzinfo=timezone.utc)
            )
        self.assertEqual(label, "Archiv 2024")

    def test_parse_course_id_from_url(self):
        self.assertEqual(
            lws.parse_course_id_from_url("https://example.com/course/view.php?id=123"),
            "123",
        )
        self.assertEqual(
            lws.parse_course_id_from_url("https://example.com/course/view.php?foo=1&id=456&bar=2"),
            "456",
        )
        self.assertIsNone(lws.parse_course_id_from_url(""))
        self.assertIsNone(lws.parse_course_id_from_url("https://example.com/course/view.php?foo=1"))

    def test_get_courses_ignores_course_urls_outside_enrolled_course_navigation(self):
        session = mock.Mock()
        session.get.return_value = FakeHttpResponse(
            text="""
            <html>
              <body>
                <li class="sub-sub-menu-item">
                  <a href="https://example.com/course/view.php?id=123" title="Real Course">RC-2026</a>
                </li>
                <div class="townsquareletter_body postletter_body">
                  <p class="MsoPlainText">
                    <a href="https://example.com/course/view.php?id=76689">
                      https://example.com/course/view.php?id=76689
                    </a>
                  </p>
                </div>
              </body>
            </html>
            """
        )

        with mock.patch.object(lws, "BASE_URL", "https://example.com"):
            courses = lws.get_courses(session)

        self.assertEqual(
            courses,
            [
                {
                    "course_id": "123",
                    "name": "Real Course",
                    "url": "https://example.com/course/view.php?id=123",
                }
            ],
        )

    def test_load_course_page_rejects_enrolment_options_redirect(self):
        session = mock.Mock()
        session.get.return_value = FakeHttpResponse(
            url="https://example.com/enrol/index.php?id=76689",
            text="""
            <html>
              <body>
                <ol class="breadcrumb">
                  <li>Home</li>
                  <li>Enrolment options</li>
                </ol>
                <h2>Enrolment options</h2>
              </body>
            </html>
            """,
        )

        with self.assertRaisesRegex(RuntimeError, "Keine belegte Kursseite"):
            lws._load_course_page(
                session,
                "https://example.com/course/view.php?id=76689",
            )

    def test_init_db_adds_resource_diagnostic_columns(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "state.db"
            with mock.patch.object(lws, "DB_PATH", db_path):
                conn = lws.init_db()
                try:
                    columns = {
                        row[1]: row for row in conn.execute("PRAGMA table_info(resources)")
                    }
                finally:
                    conn.close()

        self.assertIn("failure_reason", columns)
        self.assertIn("failure_detail", columns)
        self.assertIn("last_attempt_at", columns)
        self.assertIn("retryable", columns)

    def test_download_resource_returns_file_for_direct_redirect(self):
        session = mock.Mock()
        session.get.return_value = FakeHttpResponse(
            url="https://example.com/pluginfile.php/42/file.pdf",
            headers={
                "Content-Type": "application/pdf",
                "Content-Disposition": 'attachment; filename="file.pdf"',
            },
            content=b"pdf-bytes",
        )

        result = lws._download_resource(session, "https://example.com/mod/resource/view.php?id=cm-1")

        self.assertEqual(result.kind, "file")
        self.assertEqual(result.file_name, "file.pdf")
        self.assertEqual(result.file_bytes, b"pdf-bytes")
        self.assertEqual(result.final_url, "https://example.com/pluginfile.php/42/file.pdf")

    def test_download_resource_follows_html_pluginfile_link(self):
        session = mock.Mock()
        session.get.side_effect = [
            FakeHttpResponse(
                url="https://example.com/mod/resource/view.php?id=cm-1",
                headers={"Content-Type": "text/html"},
                text="""
                <html>
                  <body>
                    <a href="/pluginfile.php/42/mod_resource/content/1/file.pdf">Download</a>
                  </body>
                </html>
                """,
            ),
            FakeHttpResponse(
                url="https://example.com/pluginfile.php/42/mod_resource/content/1/file.pdf",
                headers={"Content-Type": "application/pdf"},
                content=b"pdf-bytes",
            ),
        ]

        with mock.patch.object(lws, "BASE_URL", "https://example.com"):
            result = lws._download_resource(session, "https://example.com/mod/resource/view.php?id=cm-1")

        self.assertEqual(result.kind, "file")
        self.assertEqual(result.file_bytes, b"pdf-bytes")
        self.assertEqual(
            result.final_url,
            "https://example.com/pluginfile.php/42/mod_resource/content/1/file.pdf",
        )

    def test_download_resource_marks_invalid_module_as_terminal(self):
        session = mock.Mock()
        session.get.return_value = FakeHttpResponse(
            url="https://example.com/mod/resource/view.php?id=cm-1",
            headers={"Content-Type": "text/html"},
            text="""
            <html>
              <head><title>Error | Learnweb</title></head>
              <body>
                <div class="box py-3 errorbox alert alert-danger">Invalid course module ID</div>
              </body>
            </html>
            """,
        )

        result = lws._download_resource(session, "https://example.com/mod/resource/view.php?id=cm-1")

        self.assertEqual(result.kind, "terminal_error")
        self.assertEqual(result.failure_reason, "invalid_module")
        self.assertIn("final_url=https://example.com/mod/resource/view.php?id=cm-1", result.failure_detail)

    def test_download_resource_marks_redirect_to_course_as_terminal(self):
        session = mock.Mock()
        session.get.return_value = FakeHttpResponse(
            url="https://example.com/course/view.php?id=123",
            headers={"Content-Type": "text/html"},
            text="<html><body>Course page</body></html>",
        )

        result = lws._download_resource(session, "https://example.com/mod/resource/view.php?id=cm-1")

        self.assertEqual(result.kind, "terminal_error")
        self.assertEqual(result.failure_reason, "redirected_to_course")
        self.assertEqual(result.final_url, "https://example.com/course/view.php?id=123")

    def test_download_resource_marks_html_without_download_link_as_retryable(self):
        session = mock.Mock()
        session.get.return_value = FakeHttpResponse(
            url="https://example.com/mod/resource/view.php?id=cm-1",
            headers={"Content-Type": "text/html"},
            text="""
            <html>
              <head><title>Resource Wrapper</title></head>
              <body><p>No downloadable file here.</p></body>
            </html>
            """,
        )

        result = lws._download_resource(session, "https://example.com/mod/resource/view.php?id=cm-1")

        self.assertEqual(result.kind, "retryable_error")
        self.assertEqual(result.failure_reason, "html_no_download_link")
        self.assertIn("title=Resource Wrapper", result.failure_detail)

    def test_download_resource_marks_request_exception_as_retryable(self):
        session = mock.Mock()
        session.get.side_effect = requests.Timeout("boom")

        result = lws._download_resource(session, "https://example.com/mod/resource/view.php?id=cm-1")

        self.assertEqual(result.kind, "retryable_error")
        self.assertEqual(result.failure_reason, "download_exception")
        self.assertIn("exception=Timeout: boom", result.failure_detail)

    def test_extract_activities_marks_restricted_resource_as_deferred(self):
        soup = make_course_page_with_activity(
            href=None,
            restriction_text="Available from 15 May 2026, 10:00 AM",
        )

        with mock.patch.object(lws, "BASE_URL", "https://example.com"):
            activities = lws._extract_activities(soup, {"course_id": "1", "name": "Course 1"})

        self.assertEqual(len(activities), 1)
        self.assertTrue(activities[0]["restricted"])
        self.assertEqual(activities[0]["availability_text"], "Available from 15 May 2026, 10:00 AM")
        self.assertIsNone(activities[0]["view_url"])

    def test_extract_activities_detects_restriction_via_css_class_fallback(self):
        soup = make_course_page_with_activity(
            href=None,
            restriction_text="Available next week",
            restriction_with_data_region=False,
            restriction_classes="availabilityinfo",
        )

        with mock.patch.object(lws, "BASE_URL", "https://example.com"):
            activities = lws._extract_activities(soup, {"course_id": "1", "name": "Course 1"})

        self.assertEqual(len(activities), 1)
        self.assertTrue(activities[0]["restricted"])
        self.assertEqual(activities[0]["availability_text"], "Available next week")
        self.assertIsNone(activities[0]["view_url"])

    def test_extract_activities_keeps_legacy_fallback_for_linkless_unrestricted_activity(self):
        soup = make_course_page_with_activity(href=None, restriction_text=None)

        with mock.patch.object(lws, "BASE_URL", "https://example.com"):
            activities = lws._extract_activities(soup, {"course_id": "1", "name": "Course 1"})

        self.assertEqual(len(activities), 1)
        self.assertFalse(activities[0]["restricted"])
        self.assertIsNone(activities[0]["availability_text"])
        self.assertEqual(
            activities[0]["view_url"],
            "https://example.com/mod/resource/view.php?id=cm-1",
        )

    def test_extract_activities_marks_restricted_folder_as_deferred(self):
        soup = make_course_page_with_activity(
            modtype="folder",
            href=None,
            restriction_text="Available from 1 June 2026",
        )

        with mock.patch.object(lws, "BASE_URL", "https://example.com"):
            activities = lws._extract_activities(soup, {"course_id": "1", "name": "Course 1"})

        self.assertEqual(len(activities), 1)
        self.assertEqual(activities[0]["modtype"], "folder")
        self.assertTrue(activities[0]["restricted"])
        self.assertIsNone(activities[0]["view_url"])

    def test_notion_query_courses_db_builds_unique_indexes_and_duplicates(self):
        payload = {
            "results": [
                make_notion_page("Known A", "https://example.com/course/view.php?id=111", page_id="page-a"),
                make_notion_page("Known B", "https://example.com/course/view.php?id=222", page_id="page-b"),
                make_notion_page("Dup 1", "https://example.com/course/view.php?id=333", page_id="page-c"),
                make_notion_page("Dup 2", "https://example.com/course/view.php?id=333", page_id="page-d"),
                make_notion_page("Same Name", "https://example.com/course/view.php?id=444", page_id="page-e"),
                make_notion_page("Same Name", "https://example.com/course/view.php?id=555", page_id="page-f"),
                make_notion_page("No Url", None, page_id="page-g"),
            ],
            "has_more": False,
        }
        with mock.patch.object(lws, "_notion_request", return_value=FakeResponse(payload)):
            result = lws.notion_query_courses_db()

        self.assertEqual(result["by_course_id"]["111"]["page_id"], "page-a")
        self.assertEqual(result["by_course_id"]["222"]["page_id"], "page-b")
        self.assertNotIn("333", result["by_course_id"])
        self.assertIn("333", result["duplicate_course_ids"])
        self.assertNotIn("Same_Name", result["by_lw_id"])
        self.assertIn("Same_Name", result["duplicate_lw_ids"])
        self.assertEqual(result["by_lw_id"]["No_Url"]["course_id"], None)
        self.assertNotIn(None, result["duplicate_course_ids"])

    def test_cmd_sync_courses_known_unique_course_does_not_scrape_course_page(self):
        course = {"course_id": "123", "name": "Known Course", "url": "https://example.com/course/view.php?id=123"}
        notion_index = {
            "by_course_id": {
                "123": {
                    "page_id": "page-123",
                    "lw_id": "Known_Course",
                    "sync_content": True,
                    "url": course["url"],
                    "course_id": "123",
                }
            },
            "duplicate_course_ids": set(),
            "by_lw_id": {},
            "duplicate_lw_ids": set(),
        }

        with (
            mock.patch.object(lws, "NOTION_TOKEN", "token"),
            mock.patch.object(lws, "NOTION_COURSES_DB_ID", "db"),
            mock.patch.object(lws, "get_courses", return_value=[course]),
            mock.patch.object(lws, "notion_query_courses_db", return_value=notion_index),
            mock.patch.object(lws, "_load_course_page", side_effect=AssertionError("unexpected scrape")),
        ):
            result = lws.cmd_sync_courses(session=mock.Mock())

        self.assertEqual(result["123"]["shortname"], "Known_Course")
        self.assertTrue(result["123"]["sync_content"])
        self.assertFalse(result["123"]["conflict"])
        self.assertNotIn("activities", result["123"])

    def test_cmd_sync_courses_unknown_course_creates_notion_page_after_single_scrape(self):
        course = {"course_id": "123", "name": "Unknown Course", "url": "https://example.com/course/view.php?id=123"}
        notion_index = {
            "by_course_id": {},
            "duplicate_course_ids": set(),
            "by_lw_id": {},
            "duplicate_lw_ids": set(),
        }

        with (
            mock.patch.object(lws, "NOTION_TOKEN", "token"),
            mock.patch.object(lws, "NOTION_COURSES_DB_ID", "db"),
            mock.patch.object(lws, "get_courses", return_value=[course]),
            mock.patch.object(lws, "notion_query_courses_db", return_value=notion_index),
            mock.patch.object(lws, "_load_course_page", return_value=make_course_page("Unknown Course")),
            mock.patch.object(lws, "notion_create_course", return_value="page-new") as create_course,
        ):
            result = lws.cmd_sync_courses(session=mock.Mock())

        create_course.assert_called_once_with("Unknown_Course", course["url"])
        self.assertEqual(result["123"]["shortname"], "Unknown_Course")
        self.assertEqual(result["123"]["notion_page_id"], "page-new")
        self.assertFalse(result["123"]["sync_content"])
        self.assertFalse(result["123"]["conflict"])

    def test_cmd_sync_courses_unknown_course_reuses_existing_lw_id_and_updates_url(self):
        course = {"course_id": "123", "name": "Known Course", "url": "https://example.com/course/view.php?id=123"}
        notion_row = {
            "page_id": "page-123",
            "lw_id": "Known_Course",
            "sync_content": True,
            "url": "https://example.com/course/view.php?id=999",
            "course_id": "999",
        }
        notion_index = {
            "by_course_id": {"999": notion_row},
            "duplicate_course_ids": set(),
            "by_lw_id": {"Known_Course": notion_row},
            "duplicate_lw_ids": set(),
        }

        with (
            mock.patch.object(lws, "NOTION_TOKEN", "token"),
            mock.patch.object(lws, "NOTION_COURSES_DB_ID", "db"),
            mock.patch.object(lws, "get_courses", return_value=[course]),
            mock.patch.object(lws, "notion_query_courses_db", return_value=notion_index),
            mock.patch.object(lws, "_load_course_page", return_value=make_course_page("Known Course")),
            mock.patch.object(lws, "notion_update_course") as update_course,
            mock.patch.object(lws, "notion_create_course") as create_course,
        ):
            result = lws.cmd_sync_courses(session=mock.Mock())

        update_course.assert_called_once_with("page-123", course_url=course["url"])
        create_course.assert_not_called()
        self.assertEqual(result["123"]["notion_page_id"], "page-123")
        self.assertTrue(result["123"]["sync_content"])
        self.assertFalse(result["123"]["conflict"])

    def test_cmd_sync_courses_blocks_duplicate_course_id(self):
        course = {"course_id": "123", "name": "Duplicate Course", "url": "https://example.com/course/view.php?id=123"}
        notion_index = {
            "by_course_id": {},
            "duplicate_course_ids": {"123"},
            "by_lw_id": {},
            "duplicate_lw_ids": set(),
        }

        with (
            mock.patch.object(lws, "NOTION_TOKEN", "token"),
            mock.patch.object(lws, "NOTION_COURSES_DB_ID", "db"),
            mock.patch.object(lws, "get_courses", return_value=[course]),
            mock.patch.object(lws, "notion_query_courses_db", return_value=notion_index),
        ):
            result = lws.cmd_sync_courses(session=mock.Mock())

        self.assertTrue(result["123"]["conflict"])
        self.assertEqual(result["123"]["blocked_reason"], "duplicate_course_id")
        self.assertFalse(result["123"]["sync_content"])

    def test_cmd_sync_courses_blocks_duplicate_lw_id(self):
        course = {"course_id": "123", "name": "Duplicate Name", "url": "https://example.com/course/view.php?id=123"}
        notion_index = {
            "by_course_id": {},
            "duplicate_course_ids": set(),
            "by_lw_id": {},
            "duplicate_lw_ids": {"Duplicate_Name"},
        }

        with (
            mock.patch.object(lws, "NOTION_TOKEN", "token"),
            mock.patch.object(lws, "NOTION_COURSES_DB_ID", "db"),
            mock.patch.object(lws, "get_courses", return_value=[course]),
            mock.patch.object(lws, "notion_query_courses_db", return_value=notion_index),
            mock.patch.object(lws, "_load_course_page", return_value=make_course_page("Duplicate Name")),
        ):
            result = lws.cmd_sync_courses(session=mock.Mock())

        self.assertTrue(result["123"]["conflict"])
        self.assertEqual(result["123"]["blocked_reason"], "duplicate_lw_id")
        self.assertEqual(result["123"]["shortname"], "Duplicate_Name")

    def test_cmd_scan_scrapes_only_active_conflict_free_courses_and_updates_shortname(self):
        course_map = {
            "1": {
                "name": "Active Course",
                "shortname": "Stale_Shortname",
                "notion_page_id": "page-1",
                "sync_content": True,
                "url": "https://example.com/course/view.php?id=1",
                "conflict": False,
            },
            "2": {
                "name": "Inactive Course",
                "shortname": "Inactive",
                "notion_page_id": "page-2",
                "sync_content": False,
                "url": "https://example.com/course/view.php?id=2",
                "conflict": False,
            },
            "3": {
                "name": "Blocked Course",
                "shortname": "Blocked",
                "notion_page_id": None,
                "sync_content": True,
                "url": "https://example.com/course/view.php?id=3",
                "conflict": True,
            },
        }
        activity = {
            "cmid": "cm-1",
            "course_id": "1",
            "course_name": "Active Course",
            "modtype": "resource",
            "name": "Lecture 1",
            "section": "General",
            "view_url": "https://example.com/mod/resource/view.php?id=cm-1",
        }

        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "state.db"
            with (
                mock.patch.object(lws, "DB_PATH", db_path),
                mock.patch.object(lws, "_load_course_page", return_value=make_course_page("Fresh Shortname")) as load_page,
                mock.patch.object(lws, "_extract_activities", return_value=[activity]),
            ):
                result = lws.cmd_scan(session=mock.Mock(), course_map=course_map)

            self.assertEqual(load_page.call_count, 1)
            self.assertEqual(course_map["1"]["shortname"], "Fresh_Shortname")
            conn = sqlite3.connect(db_path)
            try:
                row = conn.execute(
                    "SELECT cmid, course_shortname FROM resources WHERE course_id = ?",
                    ("1",),
                ).fetchone()
            finally:
                conn.close()

        self.assertEqual(result["tracked_only_courses"], [])
        self.assertEqual(row, ("cm-1", "Fresh_Shortname"))

    def test_cmd_scan_marks_missing_activities_as_removed(self):
        course_map = {
            "1": {
                "name": "Active Course",
                "shortname": "Course_1",
                "notion_page_id": "page-1",
                "sync_content": True,
                "url": "https://example.com/course/view.php?id=1",
                "conflict": False,
            }
        }
        live_activity = {
            "cmid": "cm-live",
            "course_id": "1",
            "course_name": "Active Course",
            "modtype": "resource",
            "name": "Lecture 1",
            "section": "General",
            "view_url": "https://example.com/mod/resource/view.php?id=cm-live",
        }

        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "state.db"
            with mock.patch.object(lws, "DB_PATH", db_path):
                conn = lws.init_db()
                try:
                    conn.execute(
                        """
                        INSERT INTO resources
                            (cmid, course_id, course_name, course_shortname, modtype, name,
                             section, view_url, first_seen, last_seen)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            "cm-old",
                            "1",
                            "Active Course",
                            "Course_1",
                            "resource",
                            "Old Lecture",
                            "General",
                            "https://example.com/mod/resource/view.php?id=cm-old",
                            "2026-01-01T00:00:00+00:00",
                            "2026-01-01T00:00:00+00:00",
                        ),
                    )
                    conn.commit()
                finally:
                    conn.close()

                with (
                    mock.patch.object(lws, "_load_course_page", return_value=make_course_page("Course 1")),
                    mock.patch.object(lws, "_extract_activities", return_value=[live_activity]),
                ):
                    lws.cmd_scan(session=mock.Mock(), course_map=course_map)

            conn = sqlite3.connect(db_path)
            try:
                row = conn.execute(
                    "SELECT status FROM resources WHERE cmid = ?",
                    ("cm-old",),
                ).fetchone()
            finally:
                conn.close()

        self.assertEqual(row, ("removed",))

    def test_cmd_scan_persists_restricted_activity_as_deferred(self):
        course_map = {
            "1": {
                "name": "Active Course",
                "shortname": "Course_1",
                "notion_page_id": "page-1",
                "sync_content": True,
                "url": "https://example.com/course/view.php?id=1",
                "conflict": False,
            }
        }
        restricted_activity = make_activity(
            view_url=None,
            restricted=True,
            availability_text="Available from 15 May 2026, 10:00 AM",
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "state.db"
            with mock.patch.object(lws, "DB_PATH", db_path):
                with (
                    mock.patch.object(lws, "_load_course_page", return_value=make_course_page("Course 1")),
                    mock.patch.object(lws, "_extract_activities", return_value=[restricted_activity]),
                ):
                    result = lws.cmd_scan(session=mock.Mock(), course_map=course_map)

                conn = sqlite3.connect(db_path)
                try:
                    row = conn.execute(
                        """
                        SELECT status, retryable, failure_reason, failure_detail, view_url
                        FROM resources WHERE cmid = ?
                        """,
                        ("cm-1",),
                    ).fetchone()
                finally:
                    conn.close()

        self.assertEqual(result["total_new"], 1)
        self.assertEqual(
            row,
            (
                lws.RESOURCE_STATUS_DEFERRED,
                1,
                lws.DEFERRED_FAILURE_REASON,
                "availability=Available from 15 May 2026, 10:00 AM",
                None,
            ),
        )

    def test_upsert_activity_updates_existing_metadata(self):
        activity = {
            "cmid": "cm-1",
            "course_id": "1",
            "course_name": "Course 1",
            "course_shortname": "Old_Shortname",
            "modtype": "resource",
            "name": "Lecture 1",
            "section": "General",
            "view_url": "https://example.com/mod/resource/view.php?id=cm-1",
        }
        updated_activity = {
            **activity,
            "course_name": "Course 1 updated",
            "course_shortname": "Fresh_Shortname",
            "name": "Lecture 1 revised",
            "section": "Week 1",
            "view_url": "https://example.com/mod/resource/view.php?id=cm-1&redirect=1",
        }

        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "state.db"
            with mock.patch.object(lws, "DB_PATH", db_path):
                conn = lws.init_db()
                try:
                    self.assertTrue(lws.upsert_activity(conn, activity))
                    self.assertFalse(lws.upsert_activity(conn, updated_activity))
                    conn.commit()
                    row = conn.execute(
                        """
                        SELECT course_name, course_shortname, modtype, name, section, view_url
                        FROM resources WHERE cmid = ?
                        """,
                        ("cm-1",),
                    ).fetchone()
                finally:
                    conn.close()

        self.assertEqual(
            row,
            (
                "Course 1 updated",
                "Fresh_Shortname",
                "resource",
                "Lecture 1 revised",
                "Week 1",
                "https://example.com/mod/resource/view.php?id=cm-1&redirect=1",
            ),
        )

    def test_upsert_activity_revives_removed_item_as_new_and_clears_failures(self):
        activity = {
            "cmid": "cm-1",
            "course_id": "1",
            "course_name": "Course 1",
            "course_shortname": "Fresh_Shortname",
            "modtype": "resource",
            "name": "Lecture 1",
            "section": "General",
            "view_url": "https://example.com/mod/resource/view.php?id=cm-1",
        }

        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "state.db"
            with mock.patch.object(lws, "DB_PATH", db_path):
                conn = lws.init_db()
                try:
                    conn.execute(
                        """
                        INSERT INTO resources
                            (cmid, course_id, course_name, course_shortname, modtype, name,
                             section, view_url, first_seen, last_seen, status, failure_reason,
                             failure_detail, retryable)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            "cm-1",
                            "1",
                            "Course 1",
                            "Old_Shortname",
                            "resource",
                            "Old Lecture",
                            "General",
                            "https://example.com/mod/resource/view.php?id=cm-1",
                            "2026-01-01T00:00:00+00:00",
                            "2026-01-01T00:00:00+00:00",
                            "removed",
                            "invalid_module",
                            "final_url=https://example.com/mod/resource/view.php?id=cm-1",
                            0,
                        ),
                    )
                    conn.commit()
                    self.assertFalse(lws.upsert_activity(conn, activity))
                    row = conn.execute(
                        """
                        SELECT status, retryable, failure_reason, failure_detail, course_shortname
                        FROM resources WHERE cmid = ?
                        """,
                        ("cm-1",),
                    ).fetchone()
                finally:
                    conn.close()

        self.assertEqual(row, ("new", 1, None, None, "Fresh_Shortname"))

    def test_upsert_activity_inserts_new_restricted_as_deferred(self):
        activity = make_activity(
            view_url=None,
            restricted=True,
            availability_text="Available from 1 June 2026",
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "state.db"
            with mock.patch.object(lws, "DB_PATH", db_path):
                conn = lws.init_db()
                try:
                    self.assertTrue(lws.upsert_activity(conn, activity))
                    conn.commit()
                    row = conn.execute(
                        """
                        SELECT status, retryable, failure_reason, failure_detail, view_url
                        FROM resources WHERE cmid = ?
                        """,
                        ("cm-1",),
                    ).fetchone()
                finally:
                    conn.close()

        self.assertEqual(
            row,
            (
                lws.RESOURCE_STATUS_DEFERRED,
                1,
                lws.DEFERRED_FAILURE_REASON,
                "availability=Available from 1 June 2026",
                None,
            ),
        )

    def test_upsert_activity_heals_existing_redirected_to_course_row_to_deferred(self):
        activity = make_activity(
            view_url=None,
            restricted=True,
            availability_text="Available from 1 June 2026",
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "state.db"
            with mock.patch.object(lws, "DB_PATH", db_path):
                conn = lws.init_db()
                try:
                    conn.execute(
                        """
                        INSERT INTO resources
                            (cmid, course_id, course_name, course_shortname, modtype, name,
                             section, view_url, first_seen, last_seen, status, failure_reason,
                             failure_detail, retryable)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            "cm-1",
                            "1",
                            "Course 1",
                            "Course_1",
                            "resource",
                            "Lecture 1",
                            "General",
                            "https://example.com/mod/resource/view.php?id=cm-1",
                            "2026-01-01T00:00:00+00:00",
                            "2026-01-01T00:00:00+00:00",
                            "error",
                            "redirected_to_course",
                            "final_url=https://example.com/course/view.php?id=1",
                            0,
                        ),
                    )
                    conn.commit()
                    self.assertFalse(lws.upsert_activity(conn, activity))
                    row = conn.execute(
                        """
                        SELECT status, retryable, failure_reason, failure_detail, view_url
                        FROM resources WHERE cmid = ?
                        """,
                        ("cm-1",),
                    ).fetchone()
                finally:
                    conn.close()

        self.assertEqual(
            row,
            (
                lws.RESOURCE_STATUS_DEFERRED,
                1,
                lws.DEFERRED_FAILURE_REASON,
                "availability=Available from 1 June 2026",
                None,
            ),
        )

    def test_upsert_activity_heals_removed_row_to_deferred_when_restricted(self):
        activity = make_activity(
            view_url=None,
            restricted=True,
            availability_text="Available from 1 June 2026",
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "state.db"
            with mock.patch.object(lws, "DB_PATH", db_path):
                conn = lws.init_db()
                try:
                    conn.execute(
                        """
                        INSERT INTO resources
                            (cmid, course_id, course_name, course_shortname, modtype, name,
                             section, view_url, first_seen, last_seen, status)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            "cm-1",
                            "1",
                            "Course 1",
                            "Course_1",
                            "resource",
                            "Lecture 1",
                            "General",
                            "https://example.com/mod/resource/view.php?id=cm-1",
                            "2026-01-01T00:00:00+00:00",
                            "2026-01-01T00:00:00+00:00",
                            "removed",
                        ),
                    )
                    conn.commit()
                    self.assertFalse(lws.upsert_activity(conn, activity))
                    row = conn.execute(
                        """
                        SELECT status, retryable, failure_reason, failure_detail, view_url
                        FROM resources WHERE cmid = ?
                        """,
                        ("cm-1",),
                    ).fetchone()
                finally:
                    conn.close()

        self.assertEqual(
            row,
            (
                lws.RESOURCE_STATUS_DEFERRED,
                1,
                lws.DEFERRED_FAILURE_REASON,
                "availability=Available from 1 June 2026",
                None,
            ),
        )

    def test_upsert_activity_truncates_long_availability_text_in_failure_detail(self):
        activity = make_activity(
            view_url=None,
            restricted=True,
            availability_text="x" * 600,
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "state.db"
            with mock.patch.object(lws, "DB_PATH", db_path):
                conn = lws.init_db()
                try:
                    lws.upsert_activity(conn, activity)
                    conn.commit()
                    failure_detail = conn.execute(
                        "SELECT failure_detail FROM resources WHERE cmid = ?",
                        ("cm-1",),
                    ).fetchone()[0]
                finally:
                    conn.close()

        self.assertEqual(len(failure_detail), lws.RESOURCE_FAILURE_DETAIL_LIMIT)
        self.assertTrue(failure_detail.startswith("availability="))
        self.assertTrue(failure_detail.endswith("..."))

    def test_upsert_activity_transitions_deferred_back_to_new_when_link_appears(self):
        activity = make_activity()

        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "state.db"
            with mock.patch.object(lws, "DB_PATH", db_path):
                conn = lws.init_db()
                try:
                    conn.execute(
                        """
                        INSERT INTO resources
                            (cmid, course_id, course_name, course_shortname, modtype, name,
                             section, view_url, first_seen, last_seen, status, failure_reason,
                             failure_detail, retryable)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            "cm-1",
                            "1",
                            "Course 1",
                            "Course_1",
                            "resource",
                            "Lecture 1",
                            "General",
                            None,
                            "2026-01-01T00:00:00+00:00",
                            "2026-01-01T00:00:00+00:00",
                            lws.RESOURCE_STATUS_DEFERRED,
                            lws.DEFERRED_FAILURE_REASON,
                            "availability=Available from 1 June 2026",
                            1,
                        ),
                    )
                    conn.commit()
                    self.assertFalse(lws.upsert_activity(conn, activity))
                    row = conn.execute(
                        """
                        SELECT status, retryable, failure_reason, failure_detail, view_url
                        FROM resources WHERE cmid = ?
                        """,
                        ("cm-1",),
                    ).fetchone()
                finally:
                    conn.close()

        self.assertEqual(
            row,
            (
                "new",
                1,
                None,
                None,
                "https://example.com/mod/resource/view.php?id=cm-1",
            ),
        )

    def test_upsert_activity_chained_heal_error_to_deferred_to_new(self):
        restricted_activity = make_activity(
            view_url=None,
            restricted=True,
            availability_text="Available from 1 June 2026",
        )
        live_activity = make_activity()

        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "state.db"
            with mock.patch.object(lws, "DB_PATH", db_path):
                conn = lws.init_db()
                try:
                    conn.execute(
                        """
                        INSERT INTO resources
                            (cmid, course_id, course_name, course_shortname, modtype, name,
                             section, view_url, first_seen, last_seen, status, failure_reason,
                             failure_detail, retryable)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            "cm-1",
                            "1",
                            "Course 1",
                            "Course_1",
                            "resource",
                            "Lecture 1",
                            "General",
                            "https://example.com/mod/resource/view.php?id=cm-1",
                            "2026-01-01T00:00:00+00:00",
                            "2026-01-01T00:00:00+00:00",
                            "error",
                            "redirected_to_course",
                            "final_url=https://example.com/course/view.php?id=1",
                            0,
                        ),
                    )
                    conn.commit()
                    self.assertFalse(lws.upsert_activity(conn, restricted_activity))
                    self.assertFalse(lws.upsert_activity(conn, live_activity))
                    row = conn.execute(
                        """
                        SELECT status, retryable, failure_reason, failure_detail, view_url
                        FROM resources WHERE cmid = ?
                        """,
                        ("cm-1",),
                    ).fetchone()
                finally:
                    conn.close()

        self.assertEqual(
            row,
            (
                "new",
                1,
                None,
                None,
                "https://example.com/mod/resource/view.php?id=cm-1",
            ),
        )

    def test_upsert_activity_keeps_synced_row_when_later_restricted(self):
        activity = make_activity(
            name="Lecture 1 updated",
            view_url=None,
            restricted=True,
            availability_text="Available from 1 June 2026",
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "state.db"
            with mock.patch.object(lws, "DB_PATH", db_path):
                conn = lws.init_db()
                try:
                    conn.execute(
                        """
                        INSERT INTO resources
                            (cmid, course_id, course_name, course_shortname, modtype, name,
                             section, view_url, first_seen, last_seen, notion_id, status)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            "cm-1",
                            "1",
                            "Course 1",
                            "Course_1",
                            "resource",
                            "Lecture 1",
                            "General",
                            "https://example.com/mod/resource/view.php?id=cm-1",
                            "2026-01-01T00:00:00+00:00",
                            "2026-01-01T00:00:00+00:00",
                            "notion-1",
                            "synced",
                        ),
                    )
                    conn.commit()
                    self.assertFalse(lws.upsert_activity(conn, activity))
                    row = conn.execute(
                        """
                        SELECT status, notion_id, failure_reason, failure_detail, view_url, name
                        FROM resources WHERE cmid = ?
                        """,
                        ("cm-1",),
                    ).fetchone()
                finally:
                    conn.close()

        self.assertEqual(
            row,
            (
                "synced",
                "notion-1",
                None,
                None,
                "https://example.com/mod/resource/view.php?id=cm-1",
                "Lecture 1 updated",
            ),
        )

    def test_extract_folder_files_returns_sorted_pluginfile_links_only(self):
        soup = BeautifulSoup(
            """
            <div>
              <a href="/pluginfile.php/42/mod_folder/content/0/Zeta.pdf">Zeta.pdf</a>
              <a href="https://example.com/pluginfile.php/42/mod_folder/content/0/Alpha.pdf">Alpha.pdf</a>
              <a href="/mod/folder/view.php?id=123">Folder overview</a>
              <a href="/pluginfile.php/42/mod_folder/content/0/Alpha.pdf">Alpha.pdf</a>
            </div>
            """,
            "html.parser",
        )

        with mock.patch.object(lws, "BASE_URL", "https://example.com"):
            result = lws._extract_folder_files(soup)

        self.assertEqual(
            result,
            [
                ("Alpha.pdf", "https://example.com/pluginfile.php/42/mod_folder/content/0/Alpha.pdf"),
                ("Zeta.pdf", "https://example.com/pluginfile.php/42/mod_folder/content/0/Zeta.pdf"),
            ],
        )

    def test_extract_url_target_prefers_redirect_target(self):
        session = mock.Mock()
        session.get.return_value = FakeHttpResponse(
            url="https://external.example.com/resource",
            headers={"Content-Type": "text/html"},
            text="<html></html>",
        )

        target = lws._extract_url_target(
            session,
            "https://example.com/mod/url/view.php?id=cm-1",
        )

        self.assertEqual(target, "https://external.example.com/resource")

    def test_extract_page_content_normalizes_text_and_ignores_chrome(self):
        session = mock.Mock()
        session.get.return_value = FakeHttpResponse(
            url="https://example.com/mod/page/view.php?id=cm-1",
            headers={"Content-Type": "text/html"},
            text="""
            <html>
              <body>
                <div id="region-main">
                  <div class="activity-header">Header</div>
                  <div class="box generalbox">
                    <p>First paragraph.</p>
                    <script>ignored()</script>
                    <p>Second   paragraph.</p>
                  </div>
                </div>
              </body>
            </html>
            """,
        )

        content = lws._extract_page_content(
            session,
            "https://example.com/mod/page/view.php?id=cm-1",
        )

        self.assertEqual(content, "First paragraph.\nSecond paragraph.")

    def test_build_lw_page_properties_sets_auto_semester_label(self):
        with mock.patch.object(lws, "_semester_label_for_datetime", return_value="SoSe 26"):
            properties = lws._build_lw_page_properties(
                {
                    "cmid": "cm-1",
                    "course_id": "1",
                    "name": "Lecture 1",
                    "file_name": "slides.pdf",
                    "course_shortname": "Course_1",
                },
                "course-page",
                file_upload_ids="upload-1",
            )

        self.assertEqual(properties["Quell-Semester"], {"select": {"name": "SoSe 26"}})

    def test_build_lw_page_properties_sets_target_url_when_present(self):
        properties = lws._build_lw_page_properties(
            {
                "cmid": "cm-1",
                "course_id": "1",
                "name": "External Material",
            },
            None,
            target_url="https://external.example.com/doc",
        )

        self.assertEqual(
            properties[lws.LW_TARGET_URL_PROPERTY],
            {"url": "https://external.example.com/doc"},
        )

    def test_build_lw_page_properties_omits_target_url_when_absent(self):
        properties = lws._build_lw_page_properties(
            {
                "cmid": "cm-1",
                "course_id": "1",
                "name": "External Material",
            },
            None,
        )

        self.assertNotIn(lws.LW_TARGET_URL_PROPERTY, properties)

    def test_notion_lw_db_has_target_url_property_detects_url_field(self):
        with mock.patch.object(
            lws,
            "_notion_request",
            return_value=FakeResponse(
                {
                    "properties": {
                        lws.LW_TARGET_URL_PROPERTY: {
                            "name": lws.LW_TARGET_URL_PROPERTY,
                            "type": "url",
                        }
                    }
                }
            ),
        ) as notion_request:
            self.assertTrue(lws.notion_lw_db_has_target_url_property())

        notion_request.assert_called_once()

    def test_notion_lw_db_has_target_url_property_returns_false_when_missing(self):
        with mock.patch.object(
            lws,
            "_notion_request",
            return_value=FakeResponse(
                {
                    "properties": {
                        "Name": {"name": "Name", "type": "title"},
                        "URL": {"name": "URL", "type": "url"},
                    }
                }
            ),
        ):
            self.assertFalse(lws.notion_lw_db_has_target_url_property())

    def test_cmd_scan_reports_active_course_without_pushable_resources(self):
        course_map = {
            "1": {
                "name": "Active Course",
                "shortname": "AFW-2026_1",
                "notion_page_id": "page-1",
                "sync_content": True,
                "url": "https://example.com/course/view.php?id=1",
                "conflict": False,
            }
        }
        activities = [
            {
                "cmid": "cm-1",
                "course_id": "1",
                "course_name": "Active Course",
                "modtype": "quiz",
                "name": "Quiz 1",
                "section": "General",
                "view_url": "https://example.com/mod/quiz/view.php?id=cm-1",
            },
            {
                "cmid": "cm-2",
                "course_id": "1",
                "course_name": "Active Course",
                "modtype": "forum",
                "name": "Forum 1",
                "section": "General",
                "view_url": "https://example.com/mod/forum/view.php?id=cm-2",
            },
        ]

        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "state.db"
            with (
                mock.patch.object(lws, "DB_PATH", db_path),
                mock.patch.object(lws, "_load_course_page", return_value=make_course_page("AFW-2026_1")),
                mock.patch.object(lws, "_extract_activities", return_value=activities),
            ):
                result = lws.cmd_scan(session=mock.Mock(), course_map=course_map)

        self.assertEqual(
            result["tracked_only_courses"],
            [
                {
                    "course_id": "1",
                    "shortname": "AFW-2026_1",
                    "total_activities": 2,
                    "modtype_counts": {"forum": 1, "quiz": 1},
                }
            ],
        )

    def test_cmd_push_uses_db_shortname_fallback_and_skips_conflicts(self):
        course_map = {
            "1": {
                "shortname": "",
                "notion_page_id": "page-1",
                "sync_content": True,
                "url": "https://example.com/course/view.php?id=1",
                "conflict": False,
            },
            "2": {
                "shortname": "Blocked",
                "notion_page_id": "page-2",
                "sync_content": True,
                "url": "https://example.com/course/view.php?id=2",
                "conflict": True,
            },
        }

        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "state.db"
            with mock.patch.object(lws, "DB_PATH", db_path):
                conn = lws.init_db()
                try:
                    conn.execute(
                        """
                        INSERT INTO resources
                            (cmid, course_id, course_name, course_shortname, modtype, name,
                             section, view_url, first_seen, last_seen)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            "cm-1",
                            "1",
                            "Course 1",
                            "DB_Shortname",
                            "resource",
                            "Lecture 1",
                            "General",
                            "https://example.com/mod/resource/view.php?id=cm-1",
                            "2026-01-01T00:00:00+00:00",
                            "2026-01-01T00:00:00+00:00",
                        ),
                    )
                    conn.execute(
                        """
                        INSERT INTO resources
                            (cmid, course_id, course_name, course_shortname, modtype, name,
                             section, view_url, first_seen, last_seen)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            "cm-2",
                            "2",
                            "Course 2",
                            "Blocked_Shortname",
                            "resource",
                            "Lecture 2",
                            "General",
                            "https://example.com/mod/resource/view.php?id=cm-2",
                            "2026-01-01T00:00:00+00:00",
                            "2026-01-01T00:00:00+00:00",
                        ),
                    )
                    conn.commit()
                finally:
                    conn.close()

                captured_resources = []

                def fake_create_lw_page(resource, upload_id, course_page_id):
                    captured_resources.append((resource, upload_id, course_page_id))
                    return "notion-page"

                with (
                    mock.patch.object(lws, "NOTION_TOKEN", "token"),
                    mock.patch.object(lws, "NOTION_LW_DB_ID", "lw-db"),
                    mock.patch.object(lws, "_download_resource", return_value=make_resource_file_result()),
                    mock.patch.object(lws, "notion_upload_file", return_value=None),
                    mock.patch.object(lws, "notion_create_lw_page", side_effect=fake_create_lw_page),
                ):
                    lws.cmd_push(session=mock.Mock(), course_map=course_map)

            conn = sqlite3.connect(db_path)
            try:
                synced_rows = conn.execute(
                    "SELECT course_id, notion_id FROM resources WHERE notion_id IS NOT NULL ORDER BY course_id"
                ).fetchall()
            finally:
                conn.close()

        self.assertEqual(len(captured_resources), 1)
        self.assertEqual(captured_resources[0][0]["course_shortname"], "DB_Shortname")
        self.assertEqual(captured_resources[0][2], "page-1")
        self.assertEqual(synced_rows, [("1", "notion-page")])

    def test_cmd_push_marks_terminal_resource_error_and_does_not_create_page(self):
        course_map = {
            "1": {
                "shortname": "Course_1",
                "notion_page_id": "page-1",
                "sync_content": True,
                "url": "https://example.com/course/view.php?id=1",
                "conflict": False,
            }
        }

        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "state.db"
            with mock.patch.object(lws, "DB_PATH", db_path):
                conn = lws.init_db()
                try:
                    conn.execute(
                        """
                        INSERT INTO resources
                            (cmid, course_id, course_name, course_shortname, modtype, name,
                             section, view_url, first_seen, last_seen)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            "cm-1",
                            "1",
                            "Course 1",
                            "DB_Shortname",
                            "resource",
                            "Lecture 1",
                            "General",
                            "https://example.com/mod/resource/view.php?id=cm-1",
                            "2026-01-01T00:00:00+00:00",
                            "2026-01-01T00:00:00+00:00",
                        ),
                    )
                    conn.commit()
                finally:
                    conn.close()

                with (
                    mock.patch.object(lws, "NOTION_TOKEN", "token"),
                    mock.patch.object(lws, "NOTION_LW_DB_ID", "lw-db"),
                    mock.patch.object(
                        lws,
                        "_download_resource",
                        return_value=lws.ResourceDownloadResult(
                            kind="terminal_error",
                            failure_reason="invalid_module",
                            failure_detail="final_url=https://example.com/mod/resource/view.php?id=cm-1",
                            final_url="https://example.com/mod/resource/view.php?id=cm-1",
                        ),
                    ),
                    mock.patch.object(lws, "notion_create_lw_page") as create_page,
                ):
                    lws.cmd_push(session=mock.Mock(), course_map=course_map)

            conn = sqlite3.connect(db_path)
            try:
                row = conn.execute(
                    """
                    SELECT notion_id, status, retryable, failure_reason
                    FROM resources WHERE cmid = ?
                    """,
                    ("cm-1",),
                ).fetchone()
            finally:
                conn.close()

        create_page.assert_not_called()
        self.assertEqual(row, (None, "error", 0, "invalid_module"))

    def test_cmd_push_skips_terminal_unsynced_resource_rows(self):
        course_map = {
            "1": {
                "shortname": "Course_1",
                "notion_page_id": "page-1",
                "sync_content": True,
                "url": "https://example.com/course/view.php?id=1",
                "conflict": False,
            }
        }

        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "state.db"
            with mock.patch.object(lws, "DB_PATH", db_path):
                conn = lws.init_db()
                try:
                    conn.execute(
                        """
                        INSERT INTO resources
                            (cmid, course_id, course_name, course_shortname, modtype, name,
                             section, view_url, first_seen, last_seen, status, retryable,
                             failure_reason)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            "cm-1",
                            "1",
                            "Course 1",
                            "DB_Shortname",
                            "resource",
                            "Lecture 1",
                            "General",
                            "https://example.com/mod/resource/view.php?id=cm-1",
                            "2026-01-01T00:00:00+00:00",
                            "2026-01-01T00:00:00+00:00",
                            "error",
                            0,
                            "invalid_module",
                        ),
                    )
                    conn.commit()
                finally:
                    conn.close()

                with (
                    mock.patch.object(lws, "NOTION_TOKEN", "token"),
                    mock.patch.object(lws, "NOTION_LW_DB_ID", "lw-db"),
                    mock.patch.object(
                        lws,
                        "_download_resource",
                        side_effect=AssertionError("terminal resources should be filtered"),
                    ),
                ):
                    lws.cmd_push(session=mock.Mock(), course_map=course_map)

            conn = sqlite3.connect(db_path)
            try:
                row = conn.execute(
                    "SELECT status, retryable, failure_reason FROM resources WHERE cmid = ?",
                    ("cm-1",),
                ).fetchone()
            finally:
                conn.close()

        self.assertEqual(row, ("error", 0, "invalid_module"))

    def test_cmd_push_skips_deferred_rows_across_modtypes(self):
        course_map = {
            "1": {
                "shortname": "Course_1",
                "notion_page_id": "page-1",
                "sync_content": True,
                "url": "https://example.com/course/view.php?id=1",
                "conflict": False,
            }
        }

        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "state.db"
            with mock.patch.object(lws, "DB_PATH", db_path):
                conn = lws.init_db()
                try:
                    conn.execute(
                        """
                        INSERT INTO resources
                            (cmid, course_id, course_name, course_shortname, modtype, name,
                             section, view_url, first_seen, last_seen, status, retryable,
                             failure_reason)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            "cm-1",
                            "1",
                            "Course 1",
                            "Course_1",
                            "folder",
                            "Folder 1",
                            "General",
                            None,
                            "2026-01-01T00:00:00+00:00",
                            "2026-01-01T00:00:00+00:00",
                            lws.RESOURCE_STATUS_DEFERRED,
                            1,
                            lws.DEFERRED_FAILURE_REASON,
                        ),
                    )
                    conn.commit()
                finally:
                    conn.close()

                with (
                    mock.patch.object(lws, "NOTION_TOKEN", "token"),
                    mock.patch.object(lws, "NOTION_LW_DB_ID", "lw-db"),
                    mock.patch.object(
                        lws,
                        "_fetch_activity_soup",
                        side_effect=AssertionError("deferred rows should be filtered"),
                    ) as fetch_soup,
                ):
                    lws.cmd_push(session=mock.Mock(), course_map=course_map)

            conn = sqlite3.connect(db_path)
            try:
                row = conn.execute(
                    "SELECT status, failure_reason FROM resources WHERE cmid = ?",
                    ("cm-1",),
                ).fetchone()
            finally:
                conn.close()

        fetch_soup.assert_not_called()
        self.assertEqual(row, (lws.RESOURCE_STATUS_DEFERRED, lws.DEFERRED_FAILURE_REASON))

    def test_push_resource_activity_skips_deferred_row_without_view_url(self):
        row = {
            "cmid": "cm-1",
            "course_id": "1",
            "name": "Lecture 1",
            "view_url": None,
            "course_shortname": "Course_1",
            "modtype": "resource",
            "notion_id": None,
            "status": lws.RESOURCE_STATUS_DEFERRED,
        }

        with mock.patch.object(
            lws,
            "_download_resource",
            side_effect=AssertionError("deferred rows should not download"),
        ):
            result = lws._push_resource_activity(
                conn=mock.Mock(),
                session=mock.Mock(),
                row=row,
                course_map={},
                course_notion_page_id=None,
            )

        self.assertEqual(result["status"], "skipped")
        self.assertEqual(result["modtype"], "resource")

    def test_push_resource_activity_marks_missing_view_url_as_error_for_nondeferred_row(self):
        row = {
            "cmid": "cm-1",
            "course_id": "1",
            "name": "Lecture 1",
            "view_url": None,
            "course_shortname": "Course_1",
            "modtype": "resource",
            "notion_id": None,
            "status": "error",
        }

        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "state.db"
            with mock.patch.object(lws, "DB_PATH", db_path):
                conn = lws.init_db()
                try:
                    conn.execute(
                        """
                        INSERT INTO resources
                            (cmid, course_id, course_name, course_shortname, modtype, name,
                             section, view_url, first_seen, last_seen, status)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            "cm-1",
                            "1",
                            "Course 1",
                            "Course_1",
                            "resource",
                            "Lecture 1",
                            "General",
                            None,
                            "2026-01-01T00:00:00+00:00",
                            "2026-01-01T00:00:00+00:00",
                            "error",
                        ),
                    )
                    conn.commit()
                    with mock.patch.object(
                        lws,
                        "_download_resource",
                        side_effect=AssertionError("missing view_url should guard before download"),
                    ):
                        result = lws._push_resource_activity(
                            conn=conn,
                            session=mock.Mock(),
                            row=row,
                            course_map={},
                            course_notion_page_id=None,
                        )
                    saved_row = conn.execute(
                        "SELECT status, retryable, failure_reason, failure_detail FROM resources WHERE cmid = ?",
                        ("cm-1",),
                    ).fetchone()
                finally:
                    conn.close()

        self.assertEqual(result["status"], "error")
        self.assertEqual(result["failure_reason"], lws.MISSING_VIEW_URL_FAILURE_REASON)
        self.assertEqual(
            saved_row,
            ("error", 0, lws.MISSING_VIEW_URL_FAILURE_REASON, "view_url is NULL"),
        )

    def test_cmd_push_creates_folder_page_with_multiple_uploads(self):
        course_map = {
            "1": {
                "shortname": "Course_1",
                "notion_page_id": "course-page",
                "sync_content": True,
                "url": "https://example.com/course/view.php?id=1",
                "conflict": False,
            }
        }
        folder_files = [
            ("Alpha.pdf", "https://example.com/pluginfile.php/alpha"),
            ("Beta.pdf", "https://example.com/pluginfile.php/beta"),
        ]

        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "state.db"
            with mock.patch.object(lws, "DB_PATH", db_path):
                conn = lws.init_db()
                try:
                    conn.execute(
                        """
                        INSERT INTO resources
                            (cmid, course_id, course_name, course_shortname, modtype, name,
                             section, view_url, first_seen, last_seen)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            "folder-1",
                            "1",
                            "Course 1",
                            "DB_Shortname",
                            "folder",
                            "Literatur",
                            "Week 1",
                            "https://example.com/mod/folder/view.php?id=folder-1",
                            "2026-01-01T00:00:00+00:00",
                            "2026-01-01T00:00:00+00:00",
                        ),
                    )
                    conn.commit()
                finally:
                    conn.close()

                captured_calls = []

                def fake_create_lw_page(resource, upload_ids, course_page_id):
                    captured_calls.append((resource, upload_ids, course_page_id))
                    return "folder-page"

                with (
                    mock.patch.object(lws, "NOTION_TOKEN", "token"),
                    mock.patch.object(lws, "NOTION_LW_DB_ID", "lw-db"),
                    mock.patch.object(lws, "_fetch_activity_soup", return_value=BeautifulSoup("<html></html>", "html.parser")),
                    mock.patch.object(lws, "_extract_folder_files", return_value=folder_files),
                    mock.patch.object(
                        lws,
                        "_download_file_url",
                        side_effect=[(b"alpha", "Alpha.pdf"), (b"beta", "Beta.pdf")],
                    ),
                    mock.patch.object(lws, "notion_upload_file", side_effect=["upload-1", "upload-2"]),
                    mock.patch.object(lws, "notion_create_lw_page", side_effect=fake_create_lw_page),
                ):
                    lws.cmd_push(session=mock.Mock(), course_map=course_map)

            conn = sqlite3.connect(db_path)
            try:
                row = conn.execute(
                    "SELECT notion_id, file_hash, file_name, status FROM resources WHERE cmid = ?",
                    ("folder-1",),
                ).fetchone()
            finally:
                conn.close()

        self.assertEqual(len(captured_calls), 1)
        self.assertEqual(captured_calls[0][0]["display_name"], "Week 1 — Literatur")
        self.assertEqual(captured_calls[0][1], ["upload-1", "upload-2"])
        self.assertEqual(captured_calls[0][2], "course-page")
        self.assertEqual(row[0], "folder-page")
        self.assertEqual(row[1], lws._folder_fingerprint(folder_files))
        self.assertEqual(row[2], '["Alpha.pdf", "Beta.pdf"]')
        self.assertEqual(row[3], "synced")

    def test_cmd_push_updates_existing_folder_page_when_fingerprint_changes(self):
        course_map = {
            "1": {
                "shortname": "Course_1",
                "notion_page_id": "course-page",
                "sync_content": True,
                "url": "https://example.com/course/view.php?id=1",
                "conflict": False,
            }
        }
        folder_files = [
            ("Alpha.pdf", "https://example.com/pluginfile.php/alpha"),
            ("Beta.pdf", "https://example.com/pluginfile.php/beta"),
        ]

        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "state.db"
            with mock.patch.object(lws, "DB_PATH", db_path):
                conn = lws.init_db()
                try:
                    conn.execute(
                        """
                        INSERT INTO resources
                            (cmid, course_id, course_name, course_shortname, modtype, name,
                             section, view_url, first_seen, last_seen, notion_id, file_hash, status)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            "folder-1",
                            "1",
                            "Course 1",
                            "DB_Shortname",
                            "folder",
                            "Literatur",
                            "Week 1",
                            "https://example.com/mod/folder/view.php?id=folder-1",
                            "2026-01-01T00:00:00+00:00",
                            "2026-01-01T00:00:00+00:00",
                            "folder-page",
                            "old-hash",
                            "synced",
                        ),
                    )
                    conn.commit()
                finally:
                    conn.close()

                with (
                    mock.patch.object(lws, "NOTION_TOKEN", "token"),
                    mock.patch.object(lws, "NOTION_LW_DB_ID", "lw-db"),
                    mock.patch.object(lws, "_fetch_activity_soup", return_value=BeautifulSoup("<html></html>", "html.parser")),
                    mock.patch.object(lws, "_extract_folder_files", return_value=folder_files),
                    mock.patch.object(
                        lws,
                        "_download_file_url",
                        side_effect=[(b"alpha", "Alpha.pdf"), (b"beta", "Beta.pdf")],
                    ),
                    mock.patch.object(lws, "notion_upload_file", side_effect=["upload-1", "upload-2"]),
                    mock.patch.object(lws, "notion_update_lw_page") as update_page,
                    mock.patch.object(lws, "notion_create_lw_page") as create_page,
                ):
                    lws.cmd_push(session=mock.Mock(), course_map=course_map)

            conn = sqlite3.connect(db_path)
            try:
                row = conn.execute(
                    "SELECT notion_id, file_hash, status FROM resources WHERE cmid = ?",
                    ("folder-1",),
                ).fetchone()
            finally:
                conn.close()

        create_page.assert_not_called()
        update_page.assert_called_once()
        self.assertEqual(update_page.call_args.args[0], "folder-page")
        self.assertEqual(update_page.call_args.args[2], ["upload-1", "upload-2"])
        self.assertEqual(row, ("folder-page", lws._folder_fingerprint(folder_files), "synced"))

    def test_cmd_push_skips_unchanged_folder_page(self):
        course_map = {
            "1": {
                "shortname": "Course_1",
                "notion_page_id": "course-page",
                "sync_content": True,
                "url": "https://example.com/course/view.php?id=1",
                "conflict": False,
            }
        }
        folder_files = [("Alpha.pdf", "https://example.com/pluginfile.php/alpha")]
        fingerprint = lws._folder_fingerprint(folder_files)

        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "state.db"
            with mock.patch.object(lws, "DB_PATH", db_path):
                conn = lws.init_db()
                try:
                    conn.execute(
                        """
                        INSERT INTO resources
                            (cmid, course_id, course_name, course_shortname, modtype, name,
                             section, view_url, first_seen, last_seen, notion_id, file_hash, status)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            "folder-1",
                            "1",
                            "Course 1",
                            "DB_Shortname",
                            "folder",
                            "Literatur",
                            "Week 1",
                            "https://example.com/mod/folder/view.php?id=folder-1",
                            "2026-01-01T00:00:00+00:00",
                            "2026-01-01T00:00:00+00:00",
                            "folder-page",
                            fingerprint,
                            "synced",
                        ),
                    )
                    conn.commit()
                finally:
                    conn.close()

                with (
                    mock.patch.object(lws, "NOTION_TOKEN", "token"),
                    mock.patch.object(lws, "NOTION_LW_DB_ID", "lw-db"),
                    mock.patch.object(lws, "_fetch_activity_soup", return_value=BeautifulSoup("<html></html>", "html.parser")),
                    mock.patch.object(lws, "_extract_folder_files", return_value=folder_files),
                    mock.patch.object(lws, "_download_file_url", side_effect=AssertionError("unexpected download")),
                    mock.patch.object(lws, "notion_update_lw_page") as update_page,
                    mock.patch.object(lws, "notion_create_lw_page") as create_page,
                ):
                    lws.cmd_push(session=mock.Mock(), course_map=course_map)

        create_page.assert_not_called()
        update_page.assert_not_called()

    def test_cmd_push_marks_folder_error_without_changing_page_on_failed_upload(self):
        course_map = {
            "1": {
                "shortname": "Course_1",
                "notion_page_id": "course-page",
                "sync_content": True,
                "url": "https://example.com/course/view.php?id=1",
                "conflict": False,
            }
        }
        folder_files = [("Alpha.pdf", "https://example.com/pluginfile.php/alpha")]

        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "state.db"
            with mock.patch.object(lws, "DB_PATH", db_path):
                conn = lws.init_db()
                try:
                    conn.execute(
                        """
                        INSERT INTO resources
                            (cmid, course_id, course_name, course_shortname, modtype, name,
                             section, view_url, first_seen, last_seen, notion_id, file_hash, status)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            "folder-1",
                            "1",
                            "Course 1",
                            "DB_Shortname",
                            "folder",
                            "Literatur",
                            "Week 1",
                            "https://example.com/mod/folder/view.php?id=folder-1",
                            "2026-01-01T00:00:00+00:00",
                            "2026-01-01T00:00:00+00:00",
                            "folder-page",
                            "old-hash",
                            "synced",
                        ),
                    )
                    conn.commit()
                finally:
                    conn.close()

                with (
                    mock.patch.object(lws, "NOTION_TOKEN", "token"),
                    mock.patch.object(lws, "NOTION_LW_DB_ID", "lw-db"),
                    mock.patch.object(lws, "_fetch_activity_soup", return_value=BeautifulSoup("<html></html>", "html.parser")),
                    mock.patch.object(lws, "_extract_folder_files", return_value=folder_files),
                    mock.patch.object(lws, "_download_file_url", return_value=(b"alpha", "Alpha.pdf")),
                    mock.patch.object(lws, "notion_upload_file", return_value=None),
                    mock.patch.object(lws, "notion_update_lw_page") as update_page,
                    mock.patch.object(lws, "notion_create_lw_page") as create_page,
                ):
                    lws.cmd_push(session=mock.Mock(), course_map=course_map)

            conn = sqlite3.connect(db_path)
            try:
                row = conn.execute(
                    "SELECT notion_id, status FROM resources WHERE cmid = ?",
                    ("folder-1",),
                ).fetchone()
            finally:
                conn.close()

        create_page.assert_not_called()
        update_page.assert_not_called()
        self.assertEqual(row, ("folder-page", "error"))

    def test_cmd_push_creates_url_page_with_target_url_property(self):
        course_map = {
            "1": {
                "shortname": "Course_1",
                "notion_page_id": "course-page",
                "sync_content": True,
                "url": "https://example.com/course/view.php?id=1",
                "conflict": False,
            }
        }

        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "state.db"
            with mock.patch.object(lws, "DB_PATH", db_path):
                conn = lws.init_db()
                try:
                    conn.execute(
                        """
                        INSERT INTO resources
                            (cmid, course_id, course_name, course_shortname, modtype, name,
                             section, view_url, first_seen, last_seen)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            "url-1",
                            "1",
                            "Course 1",
                            "DB_Shortname",
                            "url",
                            "External Material",
                            "General",
                            "https://example.com/mod/url/view.php?id=url-1",
                            "2026-01-01T00:00:00+00:00",
                            "2026-01-01T00:00:00+00:00",
                        ),
                    )
                    conn.commit()
                finally:
                    conn.close()

                with (
                    mock.patch.object(lws, "NOTION_TOKEN", "token"),
                    mock.patch.object(lws, "NOTION_LW_DB_ID", "lw-db"),
                    mock.patch.object(lws, "notion_lw_db_has_target_url_property", return_value=True),
                    mock.patch.object(lws, "_extract_url_target", return_value="https://external.example.com/doc"),
                    mock.patch.object(lws, "notion_create_lw_page", return_value="url-page") as create_page,
                    mock.patch.object(lws, "notion_append_page_children") as append_children,
                ):
                    lws.cmd_push(session=mock.Mock(), course_map=course_map)

            conn = sqlite3.connect(db_path)
            try:
                row = conn.execute(
                    "SELECT notion_id, status FROM resources WHERE cmid = ?",
                    ("url-1",),
                ).fetchone()
            finally:
                conn.close()

        create_page.assert_called_once()
        self.assertEqual(
            create_page.call_args.kwargs["target_url"],
            "https://external.example.com/doc",
        )
        append_children.assert_not_called()
        self.assertEqual(row, ("url-page", "synced"))

    def test_cmd_push_marks_unsynced_url_error_when_target_url_property_missing(self):
        course_map = {
            "1": {
                "shortname": "Course_1",
                "notion_page_id": "course-page",
                "sync_content": True,
                "url": "https://example.com/course/view.php?id=1",
                "conflict": False,
            }
        }

        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "state.db"
            with mock.patch.object(lws, "DB_PATH", db_path):
                conn = lws.init_db()
                try:
                    conn.execute(
                        """
                        INSERT INTO resources
                            (cmid, course_id, course_name, course_shortname, modtype, name,
                             section, view_url, first_seen, last_seen)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            "url-1",
                            "1",
                            "Course 1",
                            "DB_Shortname",
                            "url",
                            "External Material",
                            "General",
                            "https://example.com/mod/url/view.php?id=url-1",
                            "2026-01-01T00:00:00+00:00",
                            "2026-01-01T00:00:00+00:00",
                        ),
                    )
                    conn.execute(
                        """
                        INSERT INTO resources
                            (cmid, course_id, course_name, course_shortname, modtype, name,
                             section, view_url, first_seen, last_seen)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            "page-1",
                            "1",
                            "Course 1",
                            "DB_Shortname",
                            "page",
                            "Overview",
                            "General",
                            "https://example.com/mod/page/view.php?id=page-1",
                            "2026-01-01T00:00:01+00:00",
                            "2026-01-01T00:00:01+00:00",
                        ),
                    )
                    conn.commit()
                finally:
                    conn.close()

                with (
                    mock.patch.object(lws, "NOTION_TOKEN", "token"),
                    mock.patch.object(lws, "NOTION_LW_DB_ID", "lw-db"),
                    mock.patch.object(lws, "notion_lw_db_has_target_url_property", return_value=False),
                    mock.patch.object(lws, "_extract_url_target") as extract_url_target,
                    mock.patch.object(lws, "_extract_page_content", return_value="Page content"),
                    mock.patch.object(lws, "notion_create_lw_page", return_value="page-page") as create_page,
                    mock.patch.object(lws, "notion_append_page_children") as append_children,
                ):
                    lws.cmd_push(session=mock.Mock(), course_map=course_map)

            conn = sqlite3.connect(db_path)
            try:
                rows = conn.execute(
                    """
                    SELECT cmid, notion_id, status, failure_reason
                    FROM resources
                    ORDER BY cmid
                    """
                ).fetchall()
            finally:
                conn.close()

        extract_url_target.assert_not_called()
        create_page.assert_called_once_with(
            {
                "cmid": "page-1",
                "course_id": "1",
                "name": "Overview",
                "course_shortname": "Course_1",
            },
            None,
            "course-page",
        )
        append_children.assert_called_once()
        self.assertEqual(
            rows,
            [
                ("page-1", "page-page", "synced", None),
                ("url-1", None, "error", "missing_target_url_property"),
            ],
        )

    def test_cmd_push_marks_url_error_when_create_fails_and_continues(self):
        course_map = {
            "1": {
                "shortname": "Course_1",
                "notion_page_id": "course-page",
                "sync_content": True,
                "url": "https://example.com/course/view.php?id=1",
                "conflict": False,
            }
        }

        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "state.db"
            with mock.patch.object(lws, "DB_PATH", db_path):
                conn = lws.init_db()
                try:
                    conn.execute(
                        """
                        INSERT INTO resources
                            (cmid, course_id, course_name, course_shortname, modtype, name,
                             section, view_url, first_seen, last_seen)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            "url-1",
                            "1",
                            "Course 1",
                            "DB_Shortname",
                            "url",
                            "External Material",
                            "General",
                            "https://example.com/mod/url/view.php?id=url-1",
                            "2026-01-01T00:00:00+00:00",
                            "2026-01-01T00:00:00+00:00",
                        ),
                    )
                    conn.execute(
                        """
                        INSERT INTO resources
                            (cmid, course_id, course_name, course_shortname, modtype, name,
                             section, view_url, first_seen, last_seen)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            "page-1",
                            "1",
                            "Course 1",
                            "DB_Shortname",
                            "page",
                            "Overview",
                            "General",
                            "https://example.com/mod/page/view.php?id=page-1",
                            "2026-01-01T00:00:01+00:00",
                            "2026-01-01T00:00:01+00:00",
                        ),
                    )
                    conn.commit()
                finally:
                    conn.close()

                def fake_create_lw_page(resource, upload_id, course_page_id, *, target_url=None):
                    if target_url:
                        raise requests.HTTPError("HTTP 400")
                    return "page-page"

                with (
                    mock.patch.object(lws, "NOTION_TOKEN", "token"),
                    mock.patch.object(lws, "NOTION_LW_DB_ID", "lw-db"),
                    mock.patch.object(lws, "notion_lw_db_has_target_url_property", return_value=True),
                    mock.patch.object(lws, "_extract_url_target", return_value="https://external.example.com/doc"),
                    mock.patch.object(lws, "_extract_page_content", return_value="Page content"),
                    mock.patch.object(lws, "notion_create_lw_page", side_effect=fake_create_lw_page),
                    mock.patch.object(lws, "notion_append_page_children") as append_children,
                ):
                    lws.cmd_push(session=mock.Mock(), course_map=course_map)

            conn = sqlite3.connect(db_path)
            try:
                rows = conn.execute(
                    "SELECT cmid, notion_id, status FROM resources ORDER BY cmid"
                ).fetchall()
            finally:
                conn.close()

        append_children.assert_called_once()
        self.assertEqual(
            rows,
            [
                ("page-1", "page-page", "synced"),
                ("url-1", None, "error"),
            ],
        )

    def test_cmd_push_creates_page_with_chunked_paragraph_blocks(self):
        course_map = {
            "1": {
                "shortname": "Course_1",
                "notion_page_id": "course-page",
                "sync_content": True,
                "url": "https://example.com/course/view.php?id=1",
                "conflict": False,
            }
        }
        long_text = " ".join(["paragraph"] * 500)

        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "state.db"
            with mock.patch.object(lws, "DB_PATH", db_path):
                conn = lws.init_db()
                try:
                    conn.execute(
                        """
                        INSERT INTO resources
                            (cmid, course_id, course_name, course_shortname, modtype, name,
                             section, view_url, first_seen, last_seen)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            "page-1",
                            "1",
                            "Course 1",
                            "DB_Shortname",
                            "page",
                            "Overview",
                            "General",
                            "https://example.com/mod/page/view.php?id=page-1",
                            "2026-01-01T00:00:00+00:00",
                            "2026-01-01T00:00:00+00:00",
                        ),
                    )
                    conn.commit()
                finally:
                    conn.close()

                with (
                    mock.patch.object(lws, "NOTION_TOKEN", "token"),
                    mock.patch.object(lws, "NOTION_LW_DB_ID", "lw-db"),
                    mock.patch.object(lws, "_extract_page_content", return_value=long_text),
                    mock.patch.object(lws, "notion_create_lw_page", return_value="page-page"),
                    mock.patch.object(lws, "notion_append_page_children") as append_children,
                ):
                    lws.cmd_push(session=mock.Mock(), course_map=course_map)

            conn = sqlite3.connect(db_path)
            try:
                row = conn.execute(
                    "SELECT notion_id, status FROM resources WHERE cmid = ?",
                    ("page-1",),
                ).fetchone()
            finally:
                conn.close()

        blocks = append_children.call_args.args[1]
        self.assertGreater(len(blocks), 1)
        self.assertTrue(all(block["type"] == "paragraph" for block in blocks))
        self.assertTrue(
            all(
                len(block["paragraph"]["rich_text"][0]["text"]["content"]) <= lws.MAX_NOTION_RICH_TEXT_CHARS
                for block in blocks
            )
        )
        self.assertEqual(row, ("page-page", "synced"))

    def test_cmd_push_marks_page_error_when_text_is_empty(self):
        course_map = {
            "1": {
                "shortname": "Course_1",
                "notion_page_id": "course-page",
                "sync_content": True,
                "url": "https://example.com/course/view.php?id=1",
                "conflict": False,
            }
        }

        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "state.db"
            with mock.patch.object(lws, "DB_PATH", db_path):
                conn = lws.init_db()
                try:
                    conn.execute(
                        """
                        INSERT INTO resources
                            (cmid, course_id, course_name, course_shortname, modtype, name,
                             section, view_url, first_seen, last_seen)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            "page-1",
                            "1",
                            "Course 1",
                            "DB_Shortname",
                            "page",
                            "Overview",
                            "General",
                            "https://example.com/mod/page/view.php?id=page-1",
                            "2026-01-01T00:00:00+00:00",
                            "2026-01-01T00:00:00+00:00",
                        ),
                    )
                    conn.commit()
                finally:
                    conn.close()

                with (
                    mock.patch.object(lws, "NOTION_TOKEN", "token"),
                    mock.patch.object(lws, "NOTION_LW_DB_ID", "lw-db"),
                    mock.patch.object(lws, "_extract_page_content", return_value=None),
                    mock.patch.object(lws, "notion_create_lw_page") as create_page,
                    mock.patch.object(lws, "notion_append_page_children") as append_children,
                ):
                    lws.cmd_push(session=mock.Mock(), course_map=course_map)

            conn = sqlite3.connect(db_path)
            try:
                row = conn.execute(
                    "SELECT notion_id, status FROM resources WHERE cmid = ?",
                    ("page-1",),
                ).fetchone()
            finally:
                conn.close()

        create_page.assert_not_called()
        append_children.assert_not_called()
        self.assertEqual(row, (None, "error"))

    def test_cmd_diagnose_resource_errors_groups_results_and_stays_read_only(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "state.db"
            with mock.patch.object(lws, "DB_PATH", db_path):
                conn = lws.init_db()
                try:
                    conn.execute(
                        """
                        INSERT INTO resources
                            (cmid, course_id, course_name, course_shortname, modtype, name,
                             section, view_url, first_seen, last_seen, status)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            "cm-1",
                            "1",
                            "Course 1",
                            "Course_1",
                            "resource",
                            "Lecture 1",
                            "General",
                            "https://example.com/mod/resource/view.php?id=cm-1",
                            "2026-01-01T00:00:00+00:00",
                            "2026-01-01T00:00:00+00:00",
                            "error",
                        ),
                    )
                    conn.execute(
                        """
                        INSERT INTO resources
                            (cmid, course_id, course_name, course_shortname, modtype, name,
                             section, view_url, first_seen, last_seen, status, retryable,
                             failure_reason)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            "cm-2",
                            "1",
                            "Course 1",
                            "Course_1",
                            "resource",
                            "Lecture 2",
                            "General",
                            "https://example.com/mod/resource/view.php?id=cm-2",
                            "2026-01-01T00:00:00+00:00",
                            "2026-01-01T00:00:00+00:00",
                            "error",
                            0,
                            "invalid_module",
                        ),
                    )
                    conn.commit()
                finally:
                    conn.close()

                stdout = io.StringIO()
                with (
                    redirect_stdout(stdout),
                    mock.patch.object(
                        lws,
                        "_download_resource",
                        return_value=lws.ResourceDownloadResult(
                            kind="retryable_error",
                            failure_reason="html_no_download_link",
                            failure_detail="final_url=https://example.com/mod/resource/view.php?id=cm-1",
                            final_url="https://example.com/mod/resource/view.php?id=cm-1",
                        ),
                    ) as diagnose_download,
                ):
                    diagnosed = lws.cmd_diagnose_resource_errors(
                        session=mock.Mock(),
                        limit=10,
                    )

            conn = sqlite3.connect(db_path)
            try:
                rows = conn.execute(
                    "SELECT cmid, status, retryable FROM resources ORDER BY cmid"
                ).fetchall()
            finally:
                conn.close()

        self.assertEqual(diagnose_download.call_count, 1)
        self.assertEqual(len(diagnosed), 1)
        self.assertIn("html_no_download_link: 1", stdout.getvalue())
        self.assertIn("cm-1", stdout.getvalue())
        self.assertEqual(rows, [("cm-1", "error", 1), ("cm-2", "error", 0)])

    def test_cmd_diagnose_resource_errors_excludes_deferred_rows(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "state.db"
            with mock.patch.object(lws, "DB_PATH", db_path):
                conn = lws.init_db()
                try:
                    conn.execute(
                        """
                        INSERT INTO resources
                            (cmid, course_id, course_name, course_shortname, modtype, name,
                             section, view_url, first_seen, last_seen, status, retryable)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            "cm-1",
                            "1",
                            "Course 1",
                            "Course_1",
                            "resource",
                            "Lecture 1",
                            "General",
                            "https://example.com/mod/resource/view.php?id=cm-1",
                            "2026-01-01T00:00:00+00:00",
                            "2026-01-01T00:00:00+00:00",
                            "error",
                            1,
                        ),
                    )
                    conn.execute(
                        """
                        INSERT INTO resources
                            (cmid, course_id, course_name, course_shortname, modtype, name,
                             section, view_url, first_seen, last_seen, status, retryable,
                             failure_reason)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            "cm-2",
                            "1",
                            "Course 1",
                            "Course_1",
                            "resource",
                            "Lecture 2",
                            "General",
                            None,
                            "2026-01-01T00:00:00+00:00",
                            "2026-01-01T00:00:00+00:00",
                            lws.RESOURCE_STATUS_DEFERRED,
                            1,
                            lws.DEFERRED_FAILURE_REASON,
                        ),
                    )
                    conn.commit()
                finally:
                    conn.close()

                with mock.patch.object(
                    lws,
                    "_download_resource",
                    return_value=lws.ResourceDownloadResult(
                        kind="retryable_error",
                        failure_reason="html_no_download_link",
                        failure_detail="final_url=https://example.com/mod/resource/view.php?id=cm-1",
                        final_url="https://example.com/mod/resource/view.php?id=cm-1",
                    ),
                ) as diagnose_download:
                    diagnosed = lws.cmd_diagnose_resource_errors(
                        session=mock.Mock(),
                        limit=10,
                        include_terminal=True,
                    )

        self.assertEqual(diagnose_download.call_count, 1)
        self.assertEqual(len(diagnosed), 1)
        self.assertEqual(diagnosed[0]["cmid"], "cm-1")

    def test_main_dispatches_diagnose_resource_errors_with_flags(self):
        with (
            mock.patch.object(lws, "cmd_diagnose_resource_errors") as diagnose_cmd,
            mock.patch("sys.argv", ["learnweb_sync.py", "diagnose-resource-errors", "--limit", "7", "--include-terminal"]),
        ):
            lws.main()

        diagnose_cmd.assert_called_once_with(limit=7, include_terminal=True)

    def test_cmd_run_exits_when_active_course_has_no_pushable_resources(self):
        tracked_only_courses = [
            {
                "course_id": "92533",
                "shortname": "AFW-2026_1",
                "total_activities": 22,
                "modtype_counts": {"forum": 7, "quiz": 5, "zoom": 10},
            }
        ]

        with (
            mock.patch.object(lws, "login", return_value=True),
            mock.patch.object(lws, "cmd_sync_courses", return_value={}),
            mock.patch.object(lws, "cmd_scan", return_value={"tracked_only_courses": tracked_only_courses}),
            mock.patch.object(lws, "cmd_push"),
        ):
            with self.assertRaises(SystemExit) as exc:
                lws.cmd_run()

        self.assertEqual(exc.exception.code, 2)


if __name__ == "__main__":
    unittest.main()
