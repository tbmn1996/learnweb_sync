#!/usr/bin/env python3
"""
learnweb_sync — Synchronizes LearnWeb (Moodle) content to Notion.

Usage:
    python learnweb_sync.py scan         # Scrape courses, record new activities
    python learnweb_sync.py push         # Download new resources + create Notion pages [Phase 2]
    python learnweb_sync.py run          # scan + push [Phase 2]
    python learnweb_sync.py export-zips  # Download all courses as ZIPs (backup mode)
"""

import argparse
import os
import re
import sqlite3
import sys
import logging
from datetime import datetime, timezone
from pathlib import Path

import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv


# ── Config ────────────────────────────────────────────────────────────────────

load_dotenv(Path(__file__).parent / ".env")

BASE_URL = os.environ["LEARNWEB_URL"].rstrip("/")
USERNAME = os.environ["LEARNWEB_USERNAME"]
PASSWORD = os.environ["LEARNWEB_PASSWORD"]
DOWNLOAD_DIR = Path(os.getenv("DOWNLOAD_DIR", "./downloads"))


# ── Logging ───────────────────────────────────────────────────────────────────

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


# ── Database (SQLite manifest) ────────────────────────────────────────────────

DB_PATH = Path(__file__).parent / "state.db"


def init_db() -> sqlite3.Connection:
    """Open (or create) the manifest database and return a connection."""
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS resources (
            cmid        TEXT PRIMARY KEY,   -- Moodle course module ID (stable key)
            course_id   TEXT NOT NULL,      -- e.g. "88671"
            course_name TEXT NOT NULL,      -- e.g. "Informatik I WiSe 2025/26"
            modtype     TEXT NOT NULL,      -- resource / forum / url / opencast / assign
            name        TEXT NOT NULL,      -- activity display name
            section     TEXT,               -- section name on course page
            view_url    TEXT,               -- /mod/resource/view.php?id={cmid}
            first_seen  TEXT NOT NULL,      -- ISO-8601 UTC
            last_seen   TEXT NOT NULL,      -- ISO-8601 UTC
            file_hash   TEXT,               -- MD5 of downloaded file (Phase 2)
            file_name   TEXT,               -- original filename from server (Phase 2)
            notion_id   TEXT,               -- Notion page ID after push (Phase 2)
            status      TEXT DEFAULT 'new'  -- new / synced / error / removed
        )
    """)
    conn.commit()
    return conn


def upsert_activity(conn: sqlite3.Connection, activity: dict) -> bool:
    """
    Insert a new activity or update last_seen for an existing one.
    Returns True if this activity is new (never seen before).
    """
    now = datetime.now(timezone.utc).isoformat()
    existing = conn.execute(
        "SELECT cmid FROM resources WHERE cmid = ?", (activity["cmid"],)
    ).fetchone()

    if existing:
        conn.execute(
            "UPDATE resources SET last_seen = ? WHERE cmid = ?",
            (now, activity["cmid"]),
        )
        return False
    else:
        conn.execute(
            """
            INSERT INTO resources
                (cmid, course_id, course_name, modtype, name, section, view_url,
                 first_seen, last_seen)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                activity["cmid"],
                activity["course_id"],
                activity["course_name"],
                activity["modtype"],
                activity["name"],
                activity["section"],
                activity["view_url"],
                now,
                now,
            ),
        )
        return True


# ── LearnWeb scraping ─────────────────────────────────────────────────────────


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
    """Return list of {course_id, name, url} for all enrolled courses."""
    resp = session.get(f"{BASE_URL}/my/index.php")
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")

    courses = []
    seen_ids = set()

    for a in soup.find_all("a", href=re.compile(r"/course/view\.php\?id=\d+")):
        href = a["href"]
        m = re.search(r"id=(\d+)", href)
        if not m:
            continue
        course_id = m.group(1)
        if course_id in seen_ids:
            continue
        seen_ids.add(course_id)

        if not href.startswith("http"):
            href = BASE_URL + href

        # Use title attribute (full name) if available, else the link text
        name = a.get("title") or a.get_text(strip=True) or f"Course {course_id}"

        courses.append({"course_id": course_id, "name": name, "url": href})

    log.info(f"Found {len(courses)} course(s) on dashboard")
    return courses


def get_course_activities(session: requests.Session, course: dict) -> list[dict]:
    """
    Scrape a course page and return all activities as a list of dicts.

    The HTML structure is:
        <li class="section course-section" data-sectionname="Vorlesungsunterlagen">
          <ul data-for="cmlist">
            <li data-for="cmitem" data-id="3857603" class="activity resource modtype_resource">
              <div data-activityname="Vorlesung 1">
                <a href=".../mod/resource/view.php?id=3857603">...</a>
              </div>
            </li>
          </ul>
        </li>

    Labels (modtype_label) are skipped – they are pure text/layout elements with no content.
    """
    resp = session.get(course["url"])
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")

    activities = []

    for section_li in soup.find_all("li", class_="course-section"):
        section_name = section_li.get("data-sectionname", "")

        cmlist = section_li.find("ul", attrs={"data-for": "cmlist"})
        if not cmlist:
            continue

        for li in cmlist.find_all("li", attrs={"data-for": "cmitem"}):
            cmid = li.get("data-id")
            if not cmid:
                continue

            # Determine activity type from CSS classes
            modtype = None
            for cls in li.get("class", []):
                if cls.startswith("modtype_"):
                    modtype = cls[len("modtype_"):]
                    break

            if modtype is None or modtype == "label":
                continue  # skip pure text labels

            # Activity display name
            activity_div = li.find(attrs={"data-activityname": True})
            name = (
                activity_div.get("data-activityname", f"Activity {cmid}")
                if activity_div
                else f"Activity {cmid}"
            )
            # Truncate very long names (some labels leak full text here)
            if len(name) > 200:
                name = name[:197] + "..."

            # Link to the activity page
            link = li.find("a", class_=re.compile(r"aalink|stretched-link"))
            if link and link.get("href"):
                view_url = link["href"]
                if not view_url.startswith("http"):
                    view_url = BASE_URL + view_url
            else:
                view_url = f"{BASE_URL}/mod/{modtype}/view.php?id={cmid}"

            activities.append(
                {
                    "cmid": cmid,
                    "course_id": course["course_id"],
                    "course_name": course["name"],
                    "modtype": modtype,
                    "name": name,
                    "section": section_name,
                    "view_url": view_url,
                }
            )

    return activities


# ── Commands ──────────────────────────────────────────────────────────────────


def cmd_scan():
    """Scrape all courses and record new activities in the manifest."""
    conn = init_db()
    session = requests.Session()
    session.headers["User-Agent"] = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"

    if not login(session):
        sys.exit(1)

    courses = get_courses(session)
    if not courses:
        log.warning("No courses found – are you logged in correctly?")
        sys.exit(1)

    total_new = 0
    new_by_course: dict[str, list] = {}

    for course in courses:
        log.info(f"Scanning: {course['name']}")
        try:
            activities = get_course_activities(session, course)
        except Exception as e:
            log.error(f"Error scraping {course['name']}: {e}")
            continue

        new_in_course = []
        for activity in activities:
            is_new = upsert_activity(conn, activity)
            if is_new:
                new_in_course.append(activity)
                total_new += 1

        conn.commit()
        log.info(
            f"  {len(activities)} activities found, {len(new_in_course)} new"
        )
        if new_in_course:
            new_by_course[course["name"]] = new_in_course

    conn.close()

    # ── Summary report ────────────────────────────────────────────────────────
    print("\n" + "=" * 60)
    if total_new == 0:
        print("✓ Keine neuen Aktivitäten gefunden.")
    else:
        print(f"✓ {total_new} neue Aktivität(en) gefunden:\n")
        for course_name, items in new_by_course.items():
            print(f"  [{course_name}]")
            for item in items:
                tag = f"[{item['modtype']:10s}]"
                section = f"  ({item['section']})" if item["section"] else ""
                print(f"    + {tag}  {item['name']}{section}")
    print("=" * 60 + "\n")


def cmd_export_zips():
    """Download all courses as ZIP files (legacy backup mode)."""
    if not DOWNLOAD_DIR.exists():
        log.error(f"DOWNLOAD_DIR does not exist: {DOWNLOAD_DIR}")
        sys.exit(1)

    log.info("=== LearnWeb ZIP Export ===")
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
        info = _get_zip_info(session, course["url"])
        if info is None:
            skipped += 1
            continue
        try:
            result = _download_zip(session, info)
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


def _get_zip_info(session: requests.Session, course_url: str) -> dict | None:
    """Extract contextid + sesskey needed for the ZIP download POST."""
    resp = session.get(course_url)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")

    # Shortname from breadcrumb or title
    shortname = None
    breadcrumb = soup.select(
        "ol.breadcrumb li, nav[aria-label='Navigation bar'] li"
    )
    if breadcrumb:
        last = breadcrumb[-1].get_text(strip=True)
        if last:
            shortname = last
    if not shortname:
        title = soup.find("title")
        shortname = (
            title.get_text(strip=True).split(":")[0].strip() if title else "UNKNOWN"
        )
    shortname = re.sub(r"[^\w\-]", "_", shortname).strip("_")

    dl_link = soup.find("a", href=re.compile(r"downloadcontent\.php\?contextid="))
    if not dl_link:
        log.warning(f"No download link for: {shortname} ({course_url})")
        return None

    dl_url = dl_link["href"]
    if not dl_url.startswith("http"):
        dl_url = BASE_URL + dl_url

    m = re.search(r"contextid=(\d+)", dl_url)
    contextid = m.group(1) if m else "0"

    dl_page = session.get(dl_url)
    dl_page.raise_for_status()
    dl_soup = BeautifulSoup(dl_page.text, "html.parser")

    sesskey_input = dl_soup.find("input", {"name": "sesskey"})
    if not sesskey_input:
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


def _download_zip(session: requests.Session, info: dict) -> Path | None:
    """Stream a course ZIP to DOWNLOAD_DIR."""
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
            log.warning(
                f"Unexpected content type '{content_type}' for {info['shortname']} – skipping"
            )
            return None

        dest.parent.mkdir(parents=True, exist_ok=True)
        with open(dest, "wb") as f:
            for chunk in resp.iter_content(chunk_size=1024 * 256):
                f.write(chunk)

    size_mb = dest.stat().st_size / 1024 / 1024
    log.info(f"Saved {filename} ({size_mb:.1f} MB)")
    return dest


# ── Entry point ───────────────────────────────────────────────────────────────


def main():
    parser = argparse.ArgumentParser(
        description="learnweb_sync — sync LearnWeb content to Notion",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
commands:
  scan         Scrape all courses and record new activities in the manifest
  push         Download new resources and create Notion pages (Phase 2, not yet built)
  run          scan + push (Phase 2, not yet built)
  export-zips  Download all courses as ZIP files (backup mode)
""",
    )
    parser.add_argument(
        "command", choices=["scan", "push", "run", "export-zips"]
    )
    args = parser.parse_args()

    if not USERNAME or not PASSWORD:
        log.error("LEARNWEB_USERNAME or LEARNWEB_PASSWORD not set in .env")
        sys.exit(1)

    if args.command == "scan":
        cmd_scan()
    elif args.command == "export-zips":
        cmd_export_zips()
    elif args.command in ("push", "run"):
        print(f"'{args.command}' is not yet implemented — coming in Phase 2.")
        sys.exit(0)


if __name__ == "__main__":
    main()
