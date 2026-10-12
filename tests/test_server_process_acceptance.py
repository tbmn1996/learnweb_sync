"""Real Gunicorn/Flask worker checks with a synthetic child process, never LearnWeb."""
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
import urllib.error
import urllib.request


ROOT = Path(__file__).resolve().parents[1]


class GunicornProcessAcceptance(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.control = self.root / "child-result"
        self.control.write_text("fail")
        (self.root / "probe_app.py").write_text(textwrap.dedent('''
            import os, sys
            from pathlib import Path
            import server

            original_popen = server.subprocess.Popen
            def fixture_child(command, **kwargs):
                action = Path(os.environ['PROBE_CONTROL']).read_text()
                if action == 'sleep':
                    code = "import time; time.sleep(10)"
                elif action == 'success':
                    code = "print('✓ Push: 0 erstellt, 0 aktualisiert, 1 unverändert, 0 Fehler')"
                elif action == 'blocked':
                    code = "print('! Push: 0 erstellt, 0 aktualisiert, 1 unverändert, 1 Fehler'); raise SystemExit(2)"
                else:
                    code = "print('! Push: 0 erstellt, 0 aktualisiert, 0 unverändert, 6 Fehler'); raise SystemExit(1)"
                return original_popen([sys.executable, '-u', '-c', code], **kwargs)
            server.subprocess.Popen = fixture_child
            app = server.create_app()

            @app.get('/test/pid')
            def process_id():
                return {'pid': os.getpid()}
        '''))
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        self.url = f"http://127.0.0.1:{port}"
        environment = {
            **os.environ,
            "PYTHONPATH": str(ROOT), "PYTHON_DOTENV_DISABLED": "1",
            "LEARNWEB_URL": "https://learnweb.invalid", "LEARNWEB_USERNAME": "dummy", "LEARNWEB_PASSWORD": "dummy",
            "NOTION_TOKEN": "dummy", "NOTION_COURSES_DB_ID": "11111111-1111-1111-1111-111111111111",
            "NOTION_LW_DB_ID": "22222222-2222-2222-2222-222222222222", "SYNC_WEBHOOK_SECRET": "test-webhook",
            "STATE_DB_PATH": str(self.root / "state.db"), "STATE_LOCK_PATH": str(self.root / "state.lock"),
            "SYNC_RUN_TIMEOUT_SECONDS": "1", "SYNC_FAILURE_EXIT_THRESHOLD": "2", "CRON_SCHEDULE": "0 0 1 1 *",
            "SYNC_SCHEDULE_TIMEZONE": "UTC", "PROBE_CONTROL": str(self.control),
        }
        self.process = subprocess.Popen(
            [sys.executable, "-m", "gunicorn", "probe_app:app", "--workers", "1", "--threads", "2", "--bind", f"127.0.0.1:{port}", "--chdir", str(self.root)],
            env=environment, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, start_new_session=True,
        )
        self.addCleanup(self.stop_process)
        self.wait_until(lambda: self.get("/health")[0] == 200)

    def stop_process(self):
        if self.process.poll() is None:
            self.process.terminate()
        try:
            self.process.communicate(timeout=6)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.communicate(timeout=3)

    def get(self, path):
        return self.request("GET", path)

    def request(self, method, path, *, authenticated=False):
        headers = {"Authorization": "Bearer test-webhook"} if authenticated else {}
        request = urllib.request.Request(self.url + path, data=b"{}" if method == "POST" else None, headers=headers, method=method)
        try:
            response = urllib.request.urlopen(request, timeout=1)
        except urllib.error.HTTPError as error:
            response = error
        with response:
            return response.status, json.loads(response.read())

    def wait_until(self, predicate, timeout=12):
        deadline = time.monotonic() + timeout
        last = None
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                output = self.process.communicate(timeout=1)[0]
                self.fail(f"Gunicorn exited unexpectedly: {output[-1500:]}")
            try:
                value = predicate()
                if value:
                    return value
            except (OSError, ValueError) as exc:
                last = type(exc).__name__
            time.sleep(0.05)
        self.fail(f"Expected local server state was not reached ({last})")

    def trigger_and_wait(self, previous_start=None):
        self.assertEqual(self.request("POST", "/webhook/sync", authenticated=True)[0], 202)
        def finished():
            _, body = self.get("/health")
            run = body.get("last_run") or {}
            return body if not body.get("sync_running") and run.get("finished_at") and run.get("started_at") != previous_start else None
        return self.wait_until(finished)

    def test_actual_worker_is_replaced_and_status_survives(self):
        original_pid = self.get("/test/pid")[1]["pid"]
        self.assertEqual(self.request("POST", "/webhook/sync")[0], 401)
        first = self.trigger_and_wait()
        self.assertEqual(first["last_run"]["outcome"], "failed")
        self.assertEqual(first["last_run"]["error_count"], 6)
        self.assertEqual(first["last_run"]["error_count_source"], "push_summary")
        self.assertEqual(first["consecutive_failures"], 1)
        self.assertEqual(self.request("POST", "/webhook/sync", authenticated=True)[0], 202)
        self.wait_until(lambda: self.get("/test/pid")[1]["pid"] != original_pid)
        second = self.get("/health")[1]
        self.assertEqual(second["consecutive_failures"], 2)
        self.assertEqual(second["last_run"]["outcome"], "failed")
        self.assertIsNone(self.process.poll(), "Master must remain alive while replacing its worker")
        self.control.write_text("success")
        success = self.trigger_and_wait(second["last_run"]["started_at"])
        self.assertEqual(success["last_run"]["outcome"], "success")
        self.assertEqual(success["consecutive_failures"], 0)
        self.assertFalse(success["content_verified"])

    def test_known_content_blocker_remains_visible_without_worker_restart(self):
        original_pid = self.get("/test/pid")[1]["pid"]
        self.control.write_text("blocked")
        previous = None
        for _ in range(3):
            state = self.trigger_and_wait(previous)
            previous = state["last_run"]["started_at"]
            self.assertEqual(state["last_run"]["outcome"], "incomplete")
            self.assertEqual(state["last_run"]["error_kind"], "content_blocked")
            self.assertGreaterEqual(state["last_run"]["error_count"], 1)
            self.assertEqual(state["consecutive_failures"], 0)
            self.assertEqual(self.get("/test/pid")[1]["pid"], original_pid)
            self.assertFalse(state["content_verified"])

    def test_timeout_releases_guards_and_allows_next_success(self):
        self.control.write_text("sleep")
        timed_out = self.trigger_and_wait()
        self.assertEqual(timed_out["last_run"]["outcome"], "timeout")
        self.assertFalse(timed_out["sync_running"])
        self.control.write_text("success")
        success = self.trigger_and_wait(timed_out["last_run"]["started_at"])
        self.assertEqual(success["last_run"]["outcome"], "success")
        self.assertEqual(success["consecutive_failures"], 0)
