import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from bs4 import BeautifulSoup

import learnweb_sync as lws


class FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload


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


class LearnwebSyncTests(unittest.TestCase):
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
                lws.cmd_scan(session=mock.Mock(), course_map=course_map)

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

        self.assertEqual(row, ("cm-1", "Fresh_Shortname"))

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
                    mock.patch.object(lws, "_download_resource", return_value=(b"pdf-bytes", "file.pdf")),
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


if __name__ == "__main__":
    unittest.main()
