"""Independent contracts from live LearnWeb 500/connection-abort failures."""
import unittest
from unittest import mock

import requests
import learnweb_sync as lws


def response(url, *, status=200, body=b"<html><body>Source</body></html>", headers=None):
    result = requests.Response()
    result.status_code = status
    result.url = url
    result._content = body
    result._content_consumed = True
    result.headers.update(headers or {"Content-Type": "text/html"})
    return result


class SourceReadRetryAcceptance(unittest.TestCase):
    def test_source_html_reads_recover_one_connection_abort(self):
        cases = [
            (lws.get_courses, (), lws.BASE_URL + "/my/index.php"),
            (lws._load_course_page, (lws.BASE_URL + "/course/view.php?id=1",), lws.BASE_URL + "/course/view.php?id=1"),
            (lws._fetch_activity_soup, (lws.BASE_URL + "/mod/page/view.php?id=1",), lws.BASE_URL + "/mod/page/view.php?id=1"),
        ]
        for function, args, url in cases:
            with self.subTest(function=function.__name__):
                session = mock.Mock()
                session.get.side_effect = [requests.ConnectionError("remote closed connection"), response(url)]
                with mock.patch.object(lws.time, "sleep"):
                    function(session, *args)
                self.assertEqual(session.get.call_count, 2)

    def test_resource_read_recovers_transient_http_500(self):
        url = lws.BASE_URL + "/mod/resource/view.php?id=1"
        bad = response(url, status=500)
        good = response(lws.BASE_URL + "/pluginfile.php/1/mod_resource/content/0/file.pdf",
                        body=b"%PDF-original", headers={"Content-Type": "application/pdf", "Content-Disposition": 'attachment; filename="file.pdf"'})
        session = mock.Mock()
        session.get.side_effect = [bad, good]
        with mock.patch.object(lws.time, "sleep"), mock.patch.object(bad, "close", wraps=bad.close) as close:
            result = lws._download_resource(session, url)
        self.assertEqual(session.get.call_count, 2)
        self.assertGreaterEqual(close.call_count, 1)
        self.assertEqual(result.kind, "file")
        self.assertEqual(result.file_bytes, b"%PDF-original")

    def test_permanent_server_error_is_bounded_to_three_reads(self):
        url = lws.BASE_URL + "/course/view.php?id=1"
        session = mock.Mock()
        session.get.side_effect = lambda *args, **kwargs: response(url, status=503)
        with mock.patch.object(lws.time, "sleep"), self.assertRaises(requests.HTTPError):
            lws._load_course_page(session, url)
        self.assertEqual(session.get.call_count, 3)

    def test_permission_errors_are_not_retried(self):
        for status in (401, 403, 404):
            with self.subTest(status=status):
                url = lws.BASE_URL + "/course/view.php?id=1"
                session = mock.Mock()
                session.get.return_value = response(url, status=status)
                with mock.patch.object(lws.time, "sleep"), self.assertRaises(requests.HTTPError):
                    lws._load_course_page(session, url)
                self.assertEqual(session.get.call_count, 1)

    def test_linked_resource_does_not_multiply_retry_loops(self):
        view = lws.BASE_URL + "/mod/resource/view.php?id=1"
        download = lws.BASE_URL + "/pluginfile.php/1/mod_resource/content/0/file.pdf"
        session = mock.Mock()
        def get(url, **kwargs):
            if url == view:
                return response(view, body=('<html><body><a href="' + download + '">Original</a></body></html>').encode())
            self.assertEqual(url, download)
            return response(download, status=503)
        session.get.side_effect = get
        with mock.patch.object(lws.time, "sleep"):
            try:
                result = lws._download_resource(session, view)
            except requests.HTTPError:
                pass
            else:
                self.assertIn(result.kind, ("retryable_error", "terminal_error"))
        self.assertEqual(session.get.call_count, 4, "One HTML read plus three file attempts, never three nested batches")
