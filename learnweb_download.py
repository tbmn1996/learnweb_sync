#!/usr/bin/env python3
"""
LearnWeb Auto-Download
Downloads all enrolled course content ZIPs from Uni Münster LearnWeb
and saves them to Google Drive.
"""

import os
import re
import sys
import logging
from datetime import datetime
from pathlib import Path

import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv

# Load config
load_dotenv(Path(__file__).parent / ".env")

BASE_URL = os.environ["LEARNWEB_URL"].rstrip("/")
USERNAME = os.environ["LEARNWEB_USERNAME"]
PASSWORD = os.environ["LEARNWEB_PASSWORD"]
DOWNLOAD_DIR = Path(os.environ["DOWNLOAD_DIR"])

# Logging setup
log_dir = Path(__file__).parent / "logs"
log_dir.mkdir(exist_ok=True)
log_file = log_dir / f"{datetime.now():%Y%m%d_%H%M%S}.log"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)s  %(message)s",
    handlers=[
        logging.FileHandler(log_file),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger(__name__)


def login(session: requests.Session) -> bool:
    """Log in via the standard Moodle form login."""
    login_url = f"{BASE_URL}/login/index.php"
    resp = session.get(login_url)
    resp.raise_for_status()

    soup = BeautifulSoup(resp.text, "html.parser")
    token_input = soup.find("input", {"name": "logintoken"})
    logintoken = token_input["value"] if token_input else ""

    payload = {
        "username": USERNAME,
        "password": PASSWORD,
        "logintoken": logintoken,
        "anchor": "",
    }
    resp = session.post(login_url, data=payload)
    resp.raise_for_status()

    if "loginerrormessage" in resp.text or "login/index.php" in resp.url:
        log.error("Login failed – check username/password in .env")
        return False

    log.info("Login successful")
    return True


def get_courses(session: requests.Session) -> list[dict]:
    """Return list of {name, shortname, course_url} for all enrolled courses."""
    resp = session.get(f"{BASE_URL}/my/index.php")
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")

    courses = []
    seen = set()

    for a in soup.find_all("a", href=re.compile(r"/course/view\.php\?id=\d+")):
        href = a["href"]
        if href in seen:
            continue
        seen.add(href)

        # Ensure absolute URL
        if not href.startswith("http"):
            href = BASE_URL + href

        courses.append({"url": href})

    log.info(f"Found {len(courses)} course(s) on dashboard")
    return courses


def get_course_info(session: requests.Session, course_url: str) -> dict | None:
    """
    Fetch a course page and extract:
    - shortname from breadcrumb (e.g. EIDB-2025_2)
    - contextid + sesskey needed for the POST download request
    """
    resp = session.get(course_url)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")

    # Extract shortname from breadcrumb (last item)
    shortname = None
    breadcrumb = soup.select("ol.breadcrumb li, nav[aria-label='Navigation bar'] li")
    if breadcrumb:
        last = breadcrumb[-1].get_text(strip=True)
        if last:
            shortname = last
    # Fallback: page title
    if not shortname:
        title = soup.find("title")
        shortname = title.get_text(strip=True).split(":")[0].strip() if title else "UNKNOWN"

    # Sanitize for filename
    shortname = re.sub(r'[^\w\-]', '_', shortname).strip('_')

    # Find the download page link (GET leads to a confirmation page, then we POST)
    dl_link = soup.find("a", href=re.compile(r"downloadcontent\.php\?contextid="))
    if not dl_link:
        log.warning(f"No download link found for course: {shortname} ({course_url})")
        return None

    dl_url = dl_link["href"]
    if not dl_url.startswith("http"):
        dl_url = BASE_URL + dl_url

    m = re.search(r"contextid=(\d+)", dl_url)
    contextid = m.group(1) if m else "0"

    # Fetch the download confirmation page to get the sesskey
    dl_page = session.get(dl_url)
    dl_page.raise_for_status()
    dl_soup = BeautifulSoup(dl_page.text, "html.parser")

    sesskey_input = dl_soup.find("input", {"name": "sesskey"})
    if not sesskey_input:
        # Try extracting from JS config
        m2 = re.search(r'"sesskey":"([^"]+)"', dl_page.text)
        sesskey = m2.group(1) if m2 else ""
    else:
        sesskey = sesskey_input["value"]

    return {
        "shortname": shortname,
        "contextid": contextid,
        "sesskey": sesskey,
        "dl_post_url": f"{BASE_URL}/course/downloadcontent.php",
    }


def download_course(session: requests.Session, info: dict) -> Path | None:
    """Download the course ZIP and save it to DOWNLOAD_DIR."""
    timestamp = datetime.now().strftime("%y%m%d_%H%M%S")
    filename = f"{timestamp}_LW-{info['shortname']}.zip"
    dest = DOWNLOAD_DIR / filename

    log.info(f"Downloading {info['shortname']} → {filename}")

    payload = {
        "contextid": info["contextid"],
        "download": "1",
        "sesskey": info["sesskey"],
    }
    with session.post(info["dl_post_url"], data=payload, stream=True) as resp:
        resp.raise_for_status()
        content_type = resp.headers.get("Content-Type", "")
        if "zip" not in content_type and "octet-stream" not in content_type:
            log.warning(f"Unexpected content type '{content_type}' for {info['shortname']} – skipping")
            return None

        dest.parent.mkdir(parents=True, exist_ok=True)
        with open(dest, "wb") as f:
            for chunk in resp.iter_content(chunk_size=1024 * 256):
                f.write(chunk)

    size_mb = dest.stat().st_size / 1024 / 1024
    log.info(f"Saved {filename} ({size_mb:.1f} MB)")
    return dest


def main():
    if not USERNAME or not PASSWORD:
        log.error("LEARNWEB_USERNAME or LEARNWEB_PASSWORD not set in .env")
        sys.exit(1)

    if not DOWNLOAD_DIR.exists():
        log.error(f"DOWNLOAD_DIR does not exist: {DOWNLOAD_DIR}")
        sys.exit(1)

    log.info(f"=== LearnWeb Download started ===")
    log.info(f"Target folder: {DOWNLOAD_DIR}")

    session = requests.Session()
    session.headers["User-Agent"] = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"

    if not login(session):
        sys.exit(1)

    courses = get_courses(session)
    if not courses:
        log.warning("No courses found – are you logged in correctly?")
        sys.exit(1)

    success, skipped, failed = 0, 0, 0

    for course in courses:
        info = get_course_info(session, course["url"])
        if info is None:
            skipped += 1
            continue
        try:
            result = download_course(session, info)
            if result:
                success += 1
            else:
                skipped += 1
        except Exception as e:
            log.error(f"Error downloading {info.get('shortname', '?')}: {e}")
            failed += 1

    log.info(f"=== Done: {success} downloaded, {skipped} skipped, {failed} failed ===")

    if failed > 0:
        sys.exit(1)


if __name__ == "__main__":
    main()
