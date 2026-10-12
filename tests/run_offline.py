"""Run the repository suite without loading credentials or contacting the network."""
import os
from pathlib import Path
import sys
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

if __name__ == "__main__":
    environment = {
        "LEARNWEB_URL": "https://learnweb.invalid/LearnWeb/learnweb2",
        "LEARNWEB_USERNAME": "offline-user",
        "LEARNWEB_PASSWORD": "offline-placeholder",
        "NOTION_TOKEN": "offline-placeholder",
        "NOTION_COURSES_DB_ID": "11111111-1111-1111-1111-111111111111",
        "NOTION_LW_DB_ID": "22222222-2222-2222-2222-222222222222",
        "NOTION_MEETING_DB_ID": "33333333-3333-3333-3333-333333333333",
        "LOG_DIR": "",
    }
    with (
        mock.patch.dict(os.environ, environment),
        mock.patch("dotenv.load_dotenv", return_value=False),
        mock.patch("requests.sessions.Session.request", side_effect=AssertionError("Unmocked network request in offline test")),
    ):
        suite = unittest.defaultTestLoader.discover(str(ROOT / "tests"), pattern="test_*.py")
        result = unittest.TextTestRunner(verbosity=2).run(suite)
    sys.exit(not result.wasSuccessful())
