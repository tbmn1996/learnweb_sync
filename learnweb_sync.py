#!/usr/bin/env python3
"""
learnweb_sync — Synchronizes LearnWeb (Moodle) content to Notion.

Usage:
    python learnweb_sync.py sync-courses # Kurse in KurseLearnWeb prüfen / anlegen
    python learnweb_sync.py scan         # Kurse scrapen, neue Aktivitäten im Manifest erfassen
    python learnweb_sync.py push         # Pushbare Inhalte nach Notion schreiben/aktualisieren
    python learnweb_sync.py run          # scan + push [Phase 2]
    python learnweb_sync.py diagnose-resource-errors  # Offene Resource-Fehler klassifizieren
    python learnweb_sync.py export-zips  # Alle Kurse als ZIP-Backup herunterladen
"""

import argparse
from collections import Counter
from dataclasses import dataclass
import hashlib
import json
import os
import re
import sqlite3
import sys
import logging
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import unquote, urljoin, urlparse
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv


# ── Config ────────────────────────────────────────────────────────────────────

load_dotenv(Path(__file__).parent / ".env")

BASE_URL = os.environ["LEARNWEB_URL"].rstrip("/")
USERNAME = os.environ["LEARNWEB_USERNAME"]
PASSWORD = os.environ["LEARNWEB_PASSWORD"]
DOWNLOAD_DIR = Path(os.getenv("DOWNLOAD_DIR", "./downloads"))

# Notion API
NOTION_TOKEN = os.getenv("NOTION_TOKEN", "")
NOTION_COURSES_DB_ID = os.getenv("NOTION_COURSES_DB_ID", "")  # KurseLearnWeb-DB
NOTION_LW_DB_ID = os.getenv("NOTION_LW_DB_ID", "")           # Learnweb Inhalte-DB
NOTION_API = "https://api.notion.com/v1"
NOTION_VERSION = "2022-06-28"
LW_TARGET_URL_PROPERTY = "Ziel-URL"
SEMESTER_TIMEZONE = os.getenv("SEMESTER_TIMEZONE", "Europe/Berlin")
CURRENT_SEMESTER_OVERRIDE = os.getenv("CURRENT_SEMESTER_OVERRIDE", "").strip()

# Mapping: Moodle-Shortname → Notion-Kurs-Select-Wert
# Beispiel in .env: COURSE_MAP={"OR-2025_1": "OR", "FOF-2025_2": "FoF"}
try:
    COURSE_MAP: dict[str, str] = json.loads(os.getenv("COURSE_MAP", "{}"))
except json.JSONDecodeError as e:
    print(f"FEHLER: COURSE_MAP ist kein gültiges JSON: {e}", file=sys.stderr)
    COURSE_MAP = {}


# ── Logging ───────────────────────────────────────────────────────────────────

# Standardmäßig nur stdout – kein File-Handler, damit der Import keine
# Log-Dateien erzeugt wenn das Skript als Subprozess (z.B. von server.py)
# oder in einer Umgebung ohne schreibbares Dateisystem läuft.
# LOG_DIR setzen um zusätzlich in eine Datei zu schreiben (lokal nützlich).
_log_handlers: list[logging.Handler] = [logging.StreamHandler(sys.stdout)]
_log_dir_env = os.getenv("LOG_DIR")
if _log_dir_env:
    _log_dir = Path(_log_dir_env)
    _log_dir.mkdir(parents=True, exist_ok=True)
    _log_file = _log_dir / f"{datetime.now():%Y%m%d_%H%M%S}.log"
    _log_handlers.append(logging.FileHandler(_log_file))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)s  %(message)s",
    handlers=_log_handlers,
)
log = logging.getLogger(__name__)


# ── Database (SQLite manifest) ────────────────────────────────────────────────

# STATE_DB_PATH aus Env lesen; lokal bleibt der Default neben dem Skript,
# auf Railway zeigt die Variable auf /data/state.db (persistentes Volume).
DB_PATH = Path(os.getenv("STATE_DB_PATH", str(Path(__file__).parent / "state.db")))
PUSHABLE_MODTYPES = {"resource", "folder", "url", "page"}
MAX_NOTION_SINGLE_PART_BYTES = 20 * 1024 * 1024
MAX_NOTION_BLOCKS_PER_REQUEST = 100
MAX_NOTION_RICH_TEXT_CHARS = 2000
DOWNLOAD_MAX_RETRIES = 3
DOWNLOAD_RETRY_BACKOFF_SECONDS = (2, 4, 8)
RESOURCE_STATUS_DEFERRED = "deferred"
DEFERRED_FAILURE_REASON = "not_yet_available"
MISSING_VIEW_URL_FAILURE_REASON = "missing_view_url"
RESOURCE_PRIMARY_DOWNLOAD_PATTERNS = (r"pluginfile\.php", r"forcedownload")
RESOURCE_FALLBACK_DOWNLOAD_HINTS = (
    "pluginfile.php",
    "forcedownload",
    "draftfile.php",
    "tokenpluginfile.php",
)
RESOURCE_FAILURE_DETAIL_LIMIT = 500
RESTRICTED_ACTIVITY_DATA_REGION = "availabilityinfo"
RESTRICTED_ACTIVITY_CLASSES = (
    "activity-availability",
    "availabilityinfo",
    "isrestricted",
)


@dataclass
class ResourceDownloadResult:
    """Beschreibt das Ergebnis eines Resource-Probe-/Download-Versuchs."""

    kind: str
    file_bytes: bytes | None = None
    file_name: str | None = None
    failure_reason: str | None = None
    failure_detail: str | None = None
    final_url: str | None = None


def _is_pushable_modtype(modtype: str) -> bool:
    """Gibt True zurück, wenn dieser Modtype aktuell nach Notion gepusht wird."""
    return modtype in PUSHABLE_MODTYPES


def _now_utc_iso() -> str:
    """Liefert den aktuellen UTC-Zeitpunkt als ISO-8601-String."""
    return datetime.now(timezone.utc).isoformat()


def _format_modtype_counts(counts: dict[str, int]) -> str:
    """Formatiert gruppierte Modtypes für Logs/Summaries."""
    return ", ".join(f"{modtype}={count}" for modtype, count in sorted(counts.items()))


def _semester_timezone() -> ZoneInfo:
    """Lädt die Zeitzone für die Semesterlogik."""
    return ZoneInfo(SEMESTER_TIMEZONE)


def _semester_label_for_datetime(reference_dt: datetime | None = None) -> str:
    """
    Leitet das aktuelle Semesterlabel automatisch aus den Semesterzeiten in NRW/Münster ab.
    Sommersemester: 01.04.–30.09.
    Wintersemester: 01.10.–31.03.
    """
    if CURRENT_SEMESTER_OVERRIDE:
        return CURRENT_SEMESTER_OVERRIDE

    tz = _semester_timezone()
    if reference_dt is None:
        reference_dt = datetime.now(tz)
    elif reference_dt.tzinfo is None:
        reference_dt = reference_dt.replace(tzinfo=tz)
    else:
        reference_dt = reference_dt.astimezone(tz)

    year = reference_dt.year
    month = reference_dt.month

    if 4 <= month <= 9:
        return f"SoSe {year % 100:02d}"
    if month >= 10:
        return f"WS {year % 100:02d}/{(year + 1) % 100:02d}"
    return f"WS {(year - 1) % 100:02d}/{year % 100:02d}"


def init_db() -> sqlite3.Connection:
    """Open (or create) the manifest database and return a connection."""
    # Elternverzeichnis anlegen falls nötig (z.B. /data auf Railway beim ersten Start).
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS resources (
            cmid             TEXT PRIMARY KEY,   -- Moodle course module ID (stable key)
            course_id        TEXT NOT NULL,      -- e.g. "88671"
            course_name      TEXT NOT NULL,      -- e.g. "Informatik I WiSe 2025/26"
            course_shortname TEXT,               -- Moodle-Kürzel, e.g. "Inf1-2025_2"
            modtype          TEXT NOT NULL,      -- resource / forum / url / opencast / assign
            name             TEXT NOT NULL,      -- activity display name
            section          TEXT,               -- section name on course page
            view_url         TEXT,               -- /mod/resource/view.php?id={cmid}
            first_seen       TEXT NOT NULL,      -- ISO-8601 UTC
            last_seen        TEXT NOT NULL,      -- ISO-8601 UTC
            file_hash        TEXT,               -- MD5 of downloaded file (Phase 2)
            file_name        TEXT,               -- original filename from server (Phase 2)
            notion_id        TEXT,               -- Notion page ID after push (Phase 2)
            status           TEXT DEFAULT 'new', -- new / synced / error / removed / deferred
            failure_reason   TEXT,
            failure_detail   TEXT,
            last_attempt_at  TEXT,
            retryable        INTEGER NOT NULL DEFAULT 1
        )
    """)
    # Migration: course_shortname-Spalte zu bestehenden DBs hinzufügen (einmalig)
    existing = {row[1] for row in conn.execute("PRAGMA table_info(resources)")}
    if "course_shortname" not in existing:
        conn.execute("ALTER TABLE resources ADD COLUMN course_shortname TEXT")
    if "failure_reason" not in existing:
        conn.execute("ALTER TABLE resources ADD COLUMN failure_reason TEXT")
    if "failure_detail" not in existing:
        conn.execute("ALTER TABLE resources ADD COLUMN failure_detail TEXT")
    if "last_attempt_at" not in existing:
        conn.execute("ALTER TABLE resources ADD COLUMN last_attempt_at TEXT")
    if "retryable" not in existing:
        conn.execute("ALTER TABLE resources ADD COLUMN retryable INTEGER NOT NULL DEFAULT 1")
    conn.commit()
    return conn


def upsert_activity(conn: sqlite3.Connection, activity: dict) -> bool:
    """
    Insert a new activity or update last_seen for an existing one.
    Returns True if this activity is new (never seen before).
    """
    now = _now_utc_iso()
    existing = conn.execute(
        "SELECT status, notion_id, view_url FROM resources WHERE cmid = ?",
        (activity["cmid"],),
    ).fetchone()
    is_restricted = bool(activity.get("restricted"))
    failure_detail = _build_availability_failure_detail(activity.get("availability_text"))

    if existing:
        status, notion_id, _existing_view_url = existing
        if notion_id and is_restricted:
            conn.execute(
                """
                UPDATE resources
                SET course_id = ?,
                    course_name = ?,
                    course_shortname = ?,
                    modtype = ?,
                    name = ?,
                    section = ?,
                    view_url = CASE WHEN ? IS NULL THEN view_url ELSE ? END,
                    last_seen = ?
                WHERE cmid = ?
                """,
                (
                    activity["course_id"],
                    activity["course_name"],
                    activity.get("course_shortname"),
                    activity["modtype"],
                    activity["name"],
                    activity["section"],
                    activity.get("view_url"),
                    activity.get("view_url"),
                    now,
                    activity["cmid"],
                ),
            )
            return False

        if is_restricted and status in {"new", "error", RESOURCE_STATUS_DEFERRED, "removed"}:
            conn.execute(
                """
                UPDATE resources
                SET course_id = ?,
                    course_name = ?,
                    course_shortname = ?,
                    modtype = ?,
                    name = ?,
                    section = ?,
                    view_url = NULL,
                    last_seen = ?,
                    status = ?,
                    retryable = 1,
                    failure_reason = ?,
                    failure_detail = ?
                WHERE cmid = ?
                """,
                (
                    activity["course_id"],
                    activity["course_name"],
                    activity.get("course_shortname"),
                    activity["modtype"],
                    activity["name"],
                    activity["section"],
                    now,
                    RESOURCE_STATUS_DEFERRED,
                    DEFERRED_FAILURE_REASON,
                    failure_detail,
                    activity["cmid"],
                ),
            )
            return False

        conn.execute(
            """
            UPDATE resources
            SET course_id = ?,
                course_name = ?,
                course_shortname = ?,
                modtype = ?,
                name = ?,
                section = ?,
                view_url = ?,
                last_seen = ?,
                status = CASE WHEN status IN ('removed', ?) THEN 'new' ELSE status END,
                retryable = CASE WHEN status IN ('removed', ?) THEN 1 ELSE retryable END,
                failure_reason = CASE WHEN status IN ('removed', ?) THEN NULL ELSE failure_reason END,
                failure_detail = CASE WHEN status IN ('removed', ?) THEN NULL ELSE failure_detail END
            WHERE cmid = ?
            """,
            (
                activity["course_id"],
                activity["course_name"],
                activity.get("course_shortname"),
                activity["modtype"],
                activity["name"],
                activity["section"],
                activity["view_url"],
                now,
                RESOURCE_STATUS_DEFERRED,
                RESOURCE_STATUS_DEFERRED,
                RESOURCE_STATUS_DEFERRED,
                RESOURCE_STATUS_DEFERRED,
                activity["cmid"],
            ),
        )
        return False
    else:
        if is_restricted:
            conn.execute(
                """
                INSERT INTO resources
                    (cmid, course_id, course_name, course_shortname, modtype, name,
                     section, view_url, first_seen, last_seen, status, failure_reason,
                     failure_detail, retryable)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    activity["cmid"],
                    activity["course_id"],
                    activity["course_name"],
                    activity.get("course_shortname"),
                    activity["modtype"],
                    activity["name"],
                    activity["section"],
                    None,
                    now,
                    now,
                    RESOURCE_STATUS_DEFERRED,
                    DEFERRED_FAILURE_REASON,
                    failure_detail,
                    1,
                ),
            )
            return True

        conn.execute(
            """
            INSERT INTO resources
                (cmid, course_id, course_name, course_shortname, modtype, name,
                 section, view_url, first_seen, last_seen)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                activity["cmid"],
                activity["course_id"],
                activity["course_name"],
                activity.get("course_shortname"),
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


def _extract_shortname(soup: BeautifulSoup) -> str:
    """
    Extrahiert das Moodle-Kürzel aus dem Breadcrumb der Kursseite.
    Beispiel: "Informatik I WiSe 2025/26" → "Informatik_I_WiSe_2025_26"
    """
    breadcrumb = soup.select("ol.breadcrumb li, nav[aria-label='Navigation bar'] li")
    if breadcrumb:
        shortname = breadcrumb[-1].get_text(strip=True)
    else:
        title = soup.find("title")
        shortname = title.get_text(strip=True).split(":")[0].strip() if title else "UNKNOWN"
    return _normalize_lw_id(shortname)


def _normalize_lw_id(value: str) -> str:
    """Normalisiert Kurskennungen wie Notion-LW-ID und Breadcrumb-Shortnames identisch."""
    return re.sub(r"[^\w\-]", "_", value).strip("_")


def _html_text(node) -> str:
    """Normalisiert sichtbaren HTML-Text für robuste Strukturprüfungen."""
    return " ".join(node.get_text(" ", strip=True).split()) if node else ""


def parse_course_id_from_url(url: str) -> str | None:
    """Extrahiert die Moodle-Kurs-ID aus einer LearnWeb-URL."""
    if not url:
        return None
    match = re.search(r"[?&]id=(\d+)", url)
    return match.group(1) if match else None


def _has_html_class(node, class_name: str) -> bool:
    """Prüft BeautifulSoup-Klassen defensiv auch bei String-/Listenvarianten."""
    classes = node.get("class", []) if node else []
    if isinstance(classes, str):
        classes = classes.split()
    return class_name in classes


def _is_enrolled_course_link(a) -> bool:
    """
    Erkennt echte Kurslinks aus der LearnWeb-Kursnavigation.

    Auf /my/index.php kommen zusätzlich Course-URLs in Townsquare/Postletter-
    Nachrichtentexten vor. Diese sind keine belegten Kurse und dürfen nicht
    als Kursseiten in Notion angelegt werden.
    """
    return a.find_parent(
        lambda tag: getattr(tag, "name", None) == "li"
        and _has_html_class(tag, "sub-sub-menu-item")
    ) is not None


def _is_enrolment_page(soup: BeautifulSoup, final_url: str) -> bool:
    """Erkennt Moodle-Einschreibeseiten, die keine zugänglichen Kursseiten sind."""
    if "/enrol/index.php" in urlparse(final_url).path:
        return True

    breadcrumbs = [
        _html_text(li)
        for li in soup.select("ol.breadcrumb li, nav[aria-label='Navigation bar'] li")
    ]
    headings = {_html_text(node) for node in soup.find_all(["h1", "h2"])}
    return (
        bool(breadcrumbs)
        and breadcrumbs[-1] == "Enrolment options"
        and "Enrolment options" in headings
    )


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
        if not _is_enrolled_course_link(a):
            continue
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

    if not courses:
        log.warning(
            "Keine Kurse in der LearnWeb-Navigation gefunden — Theme-Selektor "
            "'sub-sub-menu-item' evtl. obsolet"
        )

    return courses


def _load_course_page(session: requests.Session, course_url: str) -> BeautifulSoup:
    """Lädt eine Kursseite und gibt den geparsten HTML-Baum zurück."""
    resp = session.get(course_url)
    resp.raise_for_status()
    final_url = resp.url or course_url
    soup = BeautifulSoup(resp.text, "html.parser")
    if _is_enrolment_page(soup, final_url):
        raise RuntimeError(f"Keine belegte Kursseite: {course_url} -> {final_url}")
    return soup


def _extract_restriction_info(li) -> tuple[bool, str | None]:
    """Erkennt explizite Restriction-Hinweise in einer Activity-Card."""
    restriction_node = li.find(attrs={"data-region": RESTRICTED_ACTIVITY_DATA_REGION})
    if restriction_node is None:
        for node in li.find_all(["div", "span"], class_=True):
            classes = set(node.get("class", []))
            if any(marker in classes for marker in RESTRICTED_ACTIVITY_CLASSES):
                restriction_node = node
                break

    if restriction_node is None:
        return False, None

    availability_text = " ".join(restriction_node.get_text(" ", strip=True).split())
    return True, availability_text or None


def _extract_activities(soup: BeautifulSoup, course: dict) -> list[dict]:
    """Extrahiert alle Aktivitäten aus einem bereits geladenen Kurs-HTML."""
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
            is_restricted, availability_text = _extract_restriction_info(li)
            link = li.find("a", class_=re.compile(r"aalink|stretched-link"))
            if link and link.get("href"):
                view_url = link["href"]
                if not view_url.startswith("http"):
                    view_url = BASE_URL + view_url
                is_restricted = False
                availability_text = None
            elif is_restricted:
                view_url = None
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
                    "restricted": is_restricted,
                    "availability_text": availability_text,
                }
            )

    return activities


def get_course_activities(
    session: requests.Session, course: dict
) -> tuple[str | None, list[dict]]:
    """
    Lädt eine Kursseite und gibt (shortname, activities) zurück.

    shortname: Moodle-Kürzel aus dem Breadcrumb (z.B. "Inf1-2025_2") oder None
               falls nicht eingeschrieben (Enrolment-Redirect)
    activities: Liste aller Aktivitäten auf der Seite (leer bei Fehler)

    HTML-Struktur:
        <li class="section course-section" data-sectionname="Vorlesungsunterlagen">
          <ul data-for="cmlist">
            <li data-for="cmitem" data-id="3857603" class="activity resource modtype_resource">
              <div data-activityname="Vorlesung 1">
                <a href=".../mod/resource/view.php?id=3857603">...</a>
              </div>
            </li>
          </ul>
        </li>

    Labels (modtype_label) werden übersprungen – reine Layout-Elemente ohne Inhalt.
    """
    try:
        soup = _load_course_page(session, course["url"])
    except RuntimeError as e:
        log.warning(f"  Kurs übersprungen (nicht eingeschrieben): {e}")
        return None, []
    shortname = _extract_shortname(soup)
    return shortname, _extract_activities(soup, course)


def _normalize_absolute_url(url: str) -> str:
    """Macht relative LearnWeb-URLs absolut."""
    return urljoin(f"{BASE_URL}/", url)


def _extract_filename_from_url(url: str) -> str:
    """Leitet einen Dateinamen aus einer URL ab."""
    parsed = urlparse(url)
    filename = Path(unquote(parsed.path.rstrip("/"))).name
    return filename or "download.bin"


def _response_is_html(resp: requests.Response) -> bool:
    """Prüft den Content-Type grob auf HTML."""
    return "text/html" in resp.headers.get("Content-Type", "")


def _extract_html_title(soup: BeautifulSoup) -> str | None:
    """Liest den Seitentitel aus einem HTML-Dokument."""
    title = soup.find("title")
    if title is None:
        return None
    title_text = " ".join(title.get_text(" ", strip=True).split())
    return title_text or None


def _truncate_failure_detail(text: str) -> str:
    """Kürzt Failure-Details auf eine kompakte, logfreundliche Länge."""
    if len(text) <= RESOURCE_FAILURE_DETAIL_LIMIT:
        return text
    return text[:RESOURCE_FAILURE_DETAIL_LIMIT - 3] + "..."


def _build_availability_failure_detail(availability_text: str | None) -> str:
    """Formatiert Restriction-Details für deferred Rows."""
    return _truncate_failure_detail(f"availability={availability_text or 'restricted'}")


def _build_failure_detail(
    *,
    final_url: str | None = None,
    title: str | None = None,
    exception: Exception | None = None,
) -> str | None:
    """Formatiert optionale Failure-Metadaten kompakt für das Manifest."""
    parts: list[str] = []
    if final_url:
        parts.append(f"final_url={final_url}")
    if title:
        parts.append(f"title={title}")
    if exception is not None:
        parts.append(f"exception={type(exception).__name__}: {exception}")
    if not parts:
        return None
    return _truncate_failure_detail("; ".join(parts))


def _resource_file_result(
    file_bytes: bytes,
    file_name: str,
    *,
    final_url: str | None = None,
) -> ResourceDownloadResult:
    """Erzeugt ein erfolgreiches Resource-Download-Ergebnis."""
    return ResourceDownloadResult(
        kind="file",
        file_bytes=file_bytes,
        file_name=file_name,
        final_url=final_url,
    )


def _resource_error_result(
    kind: str,
    *,
    failure_reason: str,
    final_url: str | None = None,
    title: str | None = None,
    exception: Exception | None = None,
) -> ResourceDownloadResult:
    """Erzeugt ein klassifiziertes Fehlerergebnis für Resource-Downloads."""
    return ResourceDownloadResult(
        kind=kind,
        failure_reason=failure_reason,
        failure_detail=_build_failure_detail(
            final_url=final_url,
            title=title,
            exception=exception,
        ),
        final_url=final_url,
    )


def _extract_meta_refresh_url(soup: BeautifulSoup) -> str | None:
    """Extrahiert ein Ziel aus einem HTML Meta-Refresh."""
    for meta in soup.find_all("meta"):
        http_equiv = (meta.get("http-equiv") or "").strip().lower()
        if http_equiv != "refresh":
            continue
        content = (meta.get("content") or "").strip()
        match = re.search(r"url\s*=\s*([^;]+)$", content, re.IGNORECASE)
        if not match:
            continue
        return _normalize_absolute_url(match.group(1).strip(" '\""))
    return None


def _is_candidate_download_url(url: str) -> bool:
    """Prüft, ob eine URL wie ein LearnWeb-Dateidownload aussieht."""
    candidate = _normalize_absolute_url(url)
    parsed = urlparse(candidate)
    if parsed.scheme not in {"http", "https"}:
        return False
    if not _is_same_learnweb_host(candidate):
        return False
    haystack = f"{parsed.path}?{parsed.query}".lower()
    return any(hint in haystack for hint in RESOURCE_FALLBACK_DOWNLOAD_HINTS)


def _is_invalid_module_page(soup: BeautifulSoup) -> bool:
    """Erkennt die LearnWeb-Fehlerseite für tote Course-Module."""
    title = _extract_html_title(soup)
    text = " ".join(soup.get_text(" ", strip=True).split())
    return title == "Error | Learnweb" and "Invalid course module ID" in text


def _extract_first_download_link(soup: BeautifulSoup) -> str | None:
    """Sucht den ersten Moodle-typischen Download-Link in bevorzugter Reihenfolge."""
    for pattern in RESOURCE_PRIMARY_DOWNLOAD_PATTERNS:
        link = soup.find("a", href=re.compile(pattern))
        if link and link.get("href"):
            return _normalize_absolute_url(link["href"])

    meta_refresh_url = _extract_meta_refresh_url(soup)
    if meta_refresh_url and _is_candidate_download_url(meta_refresh_url):
        return meta_refresh_url

    for selector in (".resourceworkaround a[href]", "#region-main a[href]", "a[href]"):
        for link in soup.select(selector):
            href = (link.get("href") or "").strip()
            if not href:
                continue
            candidate = _normalize_absolute_url(href)
            if _is_candidate_download_url(candidate):
                return candidate
    return None


def _extract_folder_files(soup: BeautifulSoup) -> list[tuple[str, str]]:
    """
    Extrahiert alle Dateilinks einer Folder-Aktivität als stabile Liste
    von (display_name, download_url)-Tupeln.
    """
    file_links: set[tuple[str, str]] = set()

    for link in soup.find_all("a", href=True):
        href = (link.get("href") or "").strip()
        if "pluginfile.php" not in href and "forcedownload" not in href:
            continue

        download_url = _normalize_absolute_url(href)
        display_name = (
            link.get("download")
            or " ".join(link.stripped_strings)
            or _extract_filename_from_url(download_url)
        ).strip()
        if not display_name:
            display_name = _extract_filename_from_url(download_url)

        file_links.add((display_name, download_url))

    return sorted(file_links, key=lambda item: (item[0].casefold(), item[1]))


def _folder_fingerprint(files: list[tuple[str, str]]) -> str:
    """Bildet einen stabilen Fingerprint für Folder-Inhalte."""
    serialized = "\n".join(f"{name}\t{url}" for name, url in files)
    return hashlib.md5(serialized.encode("utf-8")).hexdigest()


def _fetch_activity_soup(session: requests.Session, view_url: str) -> BeautifulSoup:
    """Lädt eine Aktivitätsseite und gibt den HTML-Baum zurück."""
    resp = session.get(view_url, allow_redirects=True, timeout=60)
    resp.raise_for_status()
    return BeautifulSoup(resp.text, "html.parser")


def _is_same_learnweb_host(url: str) -> bool:
    """Prüft, ob eine URL auf denselben LearnWeb-Host zeigt."""
    parsed = urlparse(url)
    return parsed.netloc == urlparse(BASE_URL).netloc


def _extract_url_target(session: requests.Session, view_url: str) -> str | None:
    """
    Bestimmt die Ziel-URL einer Moodle-URL-Aktivität.
    Bevorzugt echte Redirect-Ziele, fällt sonst auf Links im HTML zurück.
    """
    resp = session.get(view_url, allow_redirects=True, timeout=60)
    resp.raise_for_status()

    final_url = resp.url
    if (
        final_url
        and final_url != view_url
        and "/mod/url/view.php" not in urlparse(final_url).path
    ):
        return final_url

    if not _response_is_html(resp):
        return final_url if final_url != view_url else None

    soup = BeautifulSoup(resp.text, "html.parser")
    selectors = (
        ".urlworkaround a[href]",
        ".activity-description a[href]",
        "#region-main a[href]",
        "a[href]",
    )

    for selector in selectors:
        for link in soup.select(selector):
            href = (link.get("href") or "").strip()
            if not href or href.startswith("#") or href.startswith("javascript:"):
                continue

            candidate = _normalize_absolute_url(href)
            parsed = urlparse(candidate)
            if parsed.scheme not in {"http", "https"}:
                continue
            if candidate == view_url:
                continue
            if _is_same_learnweb_host(candidate) and "/mod/url/view.php" in parsed.path:
                continue

            return candidate

    return None


def _extract_page_content(session: requests.Session, view_url: str) -> str | None:
    """
    Extrahiert den bereinigten Klartext einer Moodle-Page-Aktivität.
    """
    soup = _fetch_activity_soup(session, view_url)
    selectors = (
        ".box.generalbox",
        ".activity-description",
        "#region-main .box.py-3.generalbox",
        "#region-main",
        "body",
    )

    container = None
    for selector in selectors:
        node = soup.select_one(selector)
        if node and node.get_text(strip=True):
            container = node
            break
    if container is None:
        return None

    working = BeautifulSoup(str(container), "html.parser")
    for selector in (
        "script",
        "style",
        "noscript",
        "nav",
        "form",
        ".activity-header",
        ".modified",
        ".completion-info",
        ".navtop",
        ".navbottom",
    ):
        for node in working.select(selector):
            node.decompose()

    lines: list[str] = []
    for raw_line in working.get_text("\n", strip=True).splitlines():
        line = " ".join(raw_line.split())
        if not line:
            if lines and lines[-1] != "":
                lines.append("")
            continue
        lines.append(line)

    text = "\n".join(lines).strip()
    return text or None


# ── Notion API ────────────────────────────────────────────────────────────────

# Persistente Session für alle Notion-Calls (HTTP Keep-Alive, Verbindungspool)
_notion_session = requests.Session()


def _notion_headers() -> dict:
    """Standard-Header für alle Notion-API-Aufrufe."""
    return {
        "Authorization": f"Bearer {NOTION_TOKEN}",
        "Notion-Version": NOTION_VERSION,
        "Content-Type": "application/json",
    }


def _notion_request(method: str, url: str, **kwargs) -> requests.Response:
    """
    HTTP-Wrapper für alle Notion-API-Aufrufe.
    - Timeout: 30s
    - Ratenlimit: 0.35s Pause nach jedem Request (bleibt unter 3/s)
    - Retry bei 429 mit Retry-After-Header (max. 3 Versuche)
    """
    kwargs.setdefault("timeout", 30)
    for attempt in range(3):
        resp = _notion_session.request(method, url, **kwargs)
        if resp.status_code == 429:
            wait = int(resp.headers.get("Retry-After", 2 ** (attempt + 1)))
            log.warning(f"Notion Rate-Limit (429) – warte {wait}s (Versuch {attempt + 1}/3)")
            time.sleep(wait)
            continue
        resp.raise_for_status()
        time.sleep(0.35)  # max. ~3 Requests/s
        return resp
    # Letzter Versuch – wirft Exception bei erneutem 429
    resp.raise_for_status()
    return resp


def notion_query_courses_db() -> dict[str, dict]:
    """
    Liest alle Einträge aus KurseLearnWeb (TESTING).
    Gibt eindeutige Indizes und Konfliktmengen für course_id und LW-ID zurück.
    Paginierung wird automatisch behandelt (bis zu 100 Einträge pro Request).
    """
    all_pages = []
    next_cursor = None

    while True:
        body: dict = {"page_size": 100}
        if next_cursor:
            body["start_cursor"] = next_cursor
        resp = _notion_request(
            "POST",
            f"{NOTION_API}/databases/{NOTION_COURSES_DB_ID}/query",
            headers=_notion_headers(),
            json=body,
        )
        data = resp.json()
        all_pages.extend(data.get("results", []))
        if not data.get("has_more"):
            break
        next_cursor = data.get("next_cursor")

    rows = []
    for page in all_pages:
        props = page["properties"]
        # LW-ID ist ein title-Feld — normieren wie _extract_shortname(), damit
        # manuell angelegte Einträge (z.B. mit Leerzeichen) korrekt erkannt werden.
        lw_id_raw = "".join(t["plain_text"] for t in props["LW-ID"]["title"])
        lw_id = _normalize_lw_id(lw_id_raw)
        url = (props["URL"]["url"] or "") if props.get("URL") else ""
        rows.append(
            {
            "page_id": page["id"],
            "lw_id": lw_id,
            "sync_content": props["SyncContent"]["checkbox"],
            "url": url,
            "course_id": parse_course_id_from_url(url),
            }
        )

    by_course_id: dict[str, dict] = {}
    duplicate_course_ids: set[str] = set()
    by_lw_id: dict[str, dict] = {}
    duplicate_lw_ids: set[str] = set()

    for row in rows:
        course_id = row["course_id"]
        if course_id:
            if course_id in duplicate_course_ids:
                pass
            elif course_id in by_course_id:
                by_course_id.pop(course_id, None)
                duplicate_course_ids.add(course_id)
            else:
                by_course_id[course_id] = row

        lw_id = row["lw_id"]
        if lw_id in duplicate_lw_ids:
            continue
        if lw_id in by_lw_id:
            by_lw_id.pop(lw_id, None)
            duplicate_lw_ids.add(lw_id)
        else:
            by_lw_id[lw_id] = row

    return {
        "by_course_id": by_course_id,
        "duplicate_course_ids": duplicate_course_ids,
        "by_lw_id": by_lw_id,
        "duplicate_lw_ids": duplicate_lw_ids,
    }


def notion_create_course(lw_id: str, course_url: str) -> str:
    """
    Legt einen neuen Kurs in KurseLearnWeb an.
    SyncContent wird auf false gesetzt — Thomas aktiviert manuell.
    Gibt die Notion Page-ID zurück.
    """
    resp = _notion_request(
        "POST",
        f"{NOTION_API}/pages",
        headers=_notion_headers(),
        json={
            "parent": {"database_id": NOTION_COURSES_DB_ID},
            "properties": {
                "LW-ID": {"title": [{"text": {"content": lw_id}}]},
                "URL": {"url": course_url},
                "SyncContent": {"checkbox": False},
            },
        },
    )
    return resp.json()["id"]


def notion_update_course(page_id: str, *, course_url: str | None = None) -> None:
    """Aktualisiert nur die URL-Property einer bestehenden Notion-Kursseite."""
    if course_url is None:
        return
    _notion_request(
        "PATCH",
        f"{NOTION_API}/pages/{page_id}",
        headers=_notion_headers(),
        json={"properties": {"URL": {"url": course_url}}},
    )


def notion_lw_db_has_target_url_property() -> bool:
    """Prüft, ob die Learnweb-Inhalte-DB die Ziel-URL-Property als URL-Feld enthält."""
    resp = _notion_request(
        "GET",
        f"{NOTION_API}/databases/{NOTION_LW_DB_ID}",
        headers=_notion_headers(),
    )
    properties = resp.json().get("properties", {})

    property_obj = properties.get(LW_TARGET_URL_PROPERTY)
    if isinstance(property_obj, dict):
        return property_obj.get("type") == "url"

    return any(
        isinstance(prop, dict)
        and prop.get("name") == LW_TARGET_URL_PROPERTY
        and prop.get("type") == "url"
        for prop in properties.values()
    )


def _normalize_file_upload_ids(file_upload_ids: str | list[str] | None) -> list[str]:
    """Normalisiert einen einzelnen Upload oder eine Upload-Liste auf eine Liste."""
    if file_upload_ids is None:
        return []
    if isinstance(file_upload_ids, str):
        return [file_upload_ids]
    return [upload_id for upload_id in file_upload_ids if upload_id]


def _guess_uniform_format(filenames: list[str]) -> str | None:
    """Liefert genau dann ein Format, wenn alle Dateinamen dasselbe erkennbare Format haben."""
    if not filenames:
        return None

    detected_formats = [_guess_format(filename) for filename in filenames]
    if any(fmt is None for fmt in detected_formats):
        return None
    if len(set(detected_formats)) != 1:
        return None
    return detected_formats[0]


def _resolve_course_shortname(
    course_map: dict | None, course_id: str, db_course_shortname: str | None
) -> str:
    """Bevorzugt den frisch gescannten Shortname und fällt auf den DB-Wert zurück."""
    map_shortname = (course_map or {}).get(course_id, {}).get("shortname", "")
    return map_shortname or (db_course_shortname or "")


def _build_lw_page_properties(
    resource: dict,
    course_notion_page_id: str | None,
    *,
    file_upload_ids: str | list[str] | None = None,
    target_url: str | None = None,
) -> dict:
    """
    Baut die Notion-Properties für einen LearnWeb-Inhalt.
    Die Children-Blöcke werden separat angehängt.
    """
    display_name = resource.get("display_name") or resource["name"]
    source_name = resource.get("source_name") or resource["name"]
    normalized_upload_ids = _normalize_file_upload_ids(file_upload_ids)

    properties: dict = {
        "Name": {"title": [{"text": {"content": display_name}}]},
        "Nr": {"rich_text": [{"text": {"content": resource["cmid"]}}]},
        "Kurs-ID": {"rich_text": [{"text": {"content": resource["course_id"]}}]},
        "Variante": {"select": {"name": _guess_variante(source_name)}},
    }

    course_shortname = resource.get("course_shortname", "")
    if course_shortname and COURSE_MAP:
        kurs_val = COURSE_MAP.get(course_shortname)
        if kurs_val:
            properties["Kurs"] = {"select": {"name": kurs_val}}

    kategorie = _guess_kategorie(source_name)
    if kategorie:
        properties["Kategorie"] = {"select": {"name": kategorie}}

    file_names = resource.get("file_names") or (
        [resource["file_name"]] if resource.get("file_name") else []
    )
    if len(file_names) > 1:
        fmt = _guess_uniform_format(file_names)
    elif file_names:
        fmt = _guess_format(file_names[0])
    else:
        fmt = None
    if fmt:
        properties["Format"] = {"select": {"name": fmt}}

    properties["Quell-Semester"] = {"select": {"name": _semester_label_for_datetime()}}

    if target_url:
        properties[LW_TARGET_URL_PROPERTY] = {"url": target_url}

    if normalized_upload_ids:
        properties["LW Download"] = {
            "files": [
                {"type": "file_upload", "file_upload": {"id": upload_id}}
                for upload_id in normalized_upload_ids
            ]
        }

    if course_notion_page_id:
        properties["KurseLearnWeb (TESTING)"] = {
            "relation": [{"id": course_notion_page_id}]
        }

    return properties


def notion_create_lw_page(
    resource: dict,
    file_upload_ids: str | list[str] | None,
    course_notion_page_id: str | None,
    *,
    target_url: str | None = None,
) -> str:
    """
    Legt eine neue Seite in Learnweb Inhalte (TESTING) an.
    Gibt die Notion page_id zurück.
    """
    resp = _notion_request(
        "POST",
        f"{NOTION_API}/pages",
        headers=_notion_headers(),
        json={
            "parent": {"database_id": NOTION_LW_DB_ID},
            "properties": _build_lw_page_properties(
                resource,
                course_notion_page_id,
                file_upload_ids=file_upload_ids,
                target_url=target_url,
            ),
        },
    )
    return resp.json()["id"]


def notion_update_lw_page(
    page_id: str,
    resource: dict,
    file_upload_ids: str | list[str] | None,
    course_notion_page_id: str | None,
    *,
    target_url: str | None = None,
) -> None:
    """Aktualisiert die Properties einer bestehenden LearnWeb-Inhaltsseite."""
    _notion_request(
        "PATCH",
        f"{NOTION_API}/pages/{page_id}",
        headers=_notion_headers(),
        json={
            "properties": _build_lw_page_properties(
                resource,
                course_notion_page_id,
                file_upload_ids=file_upload_ids,
                target_url=target_url,
            )
        },
    )


def notion_append_page_children(page_id: str, children: list[dict]) -> None:
    """Hängt Children-Blöcke in Notion in request-konformen Batches an."""
    for start in range(0, len(children), MAX_NOTION_BLOCKS_PER_REQUEST):
        batch = children[start:start + MAX_NOTION_BLOCKS_PER_REQUEST]
        _notion_request(
            "PATCH",
            f"{NOTION_API}/blocks/{page_id}/children",
            headers=_notion_headers(),
            json={"children": batch},
        )


def notion_archive_page(page_id: str) -> None:
    """Archiviert eine Notion-Seite auf Best-Effort-Basis."""
    _notion_request(
        "PATCH",
        f"{NOTION_API}/pages/{page_id}",
        headers=_notion_headers(),
        json={"archived": True},
    )


def _chunk_text(text: str, limit: int = MAX_NOTION_RICH_TEXT_CHARS) -> list[str]:
    """Teilt Text an Wortgrenzen in Notion-kompatible Rich-Text-Stücke."""
    chunks: list[str] = []
    remaining = text.strip()

    while remaining:
        if len(remaining) <= limit:
            chunks.append(remaining)
            break

        split_at = remaining.rfind(" ", 0, limit + 1)
        if split_at <= 0:
            split_at = limit

        chunks.append(remaining[:split_at].rstrip())
        remaining = remaining[split_at:].lstrip()

    return chunks


def _build_paragraph_blocks(text: str) -> list[dict]:
    """Wandelt Klartext in Paragraph-Blöcke um und beachtet Notion-Grenzen."""
    blocks: list[dict] = []
    paragraphs = [paragraph.strip() for paragraph in re.split(r"\n{2,}", text) if paragraph.strip()]

    for paragraph in paragraphs:
        for chunk in _chunk_text(paragraph):
            blocks.append(
                {
                    "type": "paragraph",
                    "paragraph": {
                        "rich_text": [{"type": "text", "text": {"content": chunk}}]
                    },
                }
            )

    return blocks


# ── Commands ──────────────────────────────────────────────────────────────────


def cmd_sync_courses(session: requests.Session | None = None) -> dict[str, dict]:
    """
    Gleicht alle belegten LearnWeb-Kurse mit der KurseLearnWeb-DB in Notion ab.
    Fehlende Kurse werden neu angelegt (SyncContent=false).

    Lädt Kursseiten nur für bisher unbekannte Kurse und gibt zurück:
        {course_id: {shortname, notion_page_id, sync_content, url, conflict, ...}}

    Der Rückgabewert wird von cmd_scan() und cmd_push() weiterverwendet.
    """
    if not NOTION_TOKEN:
        log.error("NOTION_TOKEN nicht gesetzt – bitte in .env eintragen")
        sys.exit(1)
    if not NOTION_COURSES_DB_ID:
        log.error("NOTION_COURSES_DB_ID nicht gesetzt – bitte in .env eintragen")
        sys.exit(1)

    # Wenn kein Session übergeben: eigenen Login durchführen (Standalone-Aufruf)
    own_session = session is None
    if own_session:
        session = requests.Session()
        session.headers["User-Agent"] = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"
        if not login(session):
            sys.exit(1)

    courses = get_courses(session)
    if not courses:
        log.warning("Keine Kurse gefunden – korrekt eingeloggt?")
        sys.exit(1)

    # Bestehende Kurs-Einträge aus Notion holen
    log.info("Lese KurseLearnWeb aus Notion...")
    try:
        notion_courses = notion_query_courses_db()
    except Exception as e:
        log.error(f"Notion-Abfrage fehlgeschlagen: {e}")
        sys.exit(1)
    known_unique = len(notion_courses["by_lw_id"]) + len(notion_courses["duplicate_lw_ids"])
    log.info(f"  {known_unique} Kurs/Kurse bereits in Notion erfasst")

    course_map: dict[str, dict] = {}
    new_courses: list[str] = []
    conflict_courses: list[str] = []
    known_courses = 0

    for course in courses:
        course_id = course["course_id"]
        known_row = notion_courses["by_course_id"].get(course_id)
        if known_row:
            known_courses += 1
            course_map[course_id] = {
                "name": course["name"],
                "shortname": known_row["lw_id"],
                "notion_page_id": known_row["page_id"],
                "sync_content": known_row["sync_content"],
                "url": course["url"],
                "conflict": False,
            }
            continue

        if course_id in notion_courses["duplicate_course_ids"]:
            blocked = f"{course['name']} [{course_id}]"
            log.warning(
                f"Konflikt in Notion – mehrere Kursseiten für course_id={course_id}; "
                f"überspringe {course['name']}"
            )
            conflict_courses.append(blocked)
            course_map[course_id] = {
                "name": course["name"],
                "shortname": None,
                "notion_page_id": None,
                "sync_content": False,
                "url": course["url"],
                "conflict": True,
                "blocked_reason": "duplicate_course_id",
            }
            continue

        log.info(f"Lade Kursseite für unbekannten Kurs: {course['name']}")
        try:
            soup = _load_course_page(session, course["url"])
            shortname = _extract_shortname(soup)
        except Exception as e:
            log.error(f"  Fehler beim Laden von {course['name']}: {e}")
            continue

        if shortname in notion_courses["duplicate_lw_ids"]:
            blocked = f"{shortname} [{course_id}]"
            log.warning(
                f"Konflikt in Notion – mehrere Kursseiten für LW-ID={shortname}; "
                f"überspringe {course['name']}"
            )
            conflict_courses.append(blocked)
            course_map[course_id] = {
                "name": course["name"],
                "shortname": shortname,
                "notion_page_id": None,
                "sync_content": False,
                "url": course["url"],
                "conflict": True,
                "blocked_reason": "duplicate_lw_id",
            }
            continue

        notion_row = notion_courses["by_lw_id"].get(shortname)
        if notion_row:
            page_id = notion_row["page_id"]
            sync_content = notion_row["sync_content"]
            if notion_row["url"] != course["url"]:
                notion_update_course(page_id, course_url=course["url"])
                old_course_id = notion_row.get("course_id")
                if old_course_id and notion_courses["by_course_id"].get(old_course_id) == notion_row:
                    notion_courses["by_course_id"].pop(old_course_id, None)
                notion_row = {
                    **notion_row,
                    "url": course["url"],
                    "course_id": course_id,
                }
                notion_courses["by_lw_id"][shortname] = notion_row
            notion_courses["by_course_id"][course_id] = notion_row
        else:
            log.info(f"  → Neuer Kurs: {shortname} – wird in Notion angelegt")
            try:
                page_id = notion_create_course(shortname, course["url"])
                sync_content = False
                notion_row = {
                    "page_id": page_id,
                    "lw_id": shortname,
                    "sync_content": sync_content,
                    "url": course["url"],
                    "course_id": course_id,
                }
                notion_courses["by_lw_id"][shortname] = notion_row
                notion_courses["by_course_id"][course_id] = notion_row
                new_courses.append(shortname)
            except Exception as e:
                log.error(f"  Fehler beim Anlegen von {shortname}: {e}")
                page_id = None
                sync_content = False

        course_map[course_id] = {
            "name": course["name"],
            "shortname": shortname,
            "notion_page_id": page_id,
            "sync_content": sync_content,
            "url": course["url"],
            "conflict": False,
        }

    # ── Summary ───────────────────────────────────────────────────────────────
    print("\n" + "=" * 60)
    print(f"✓ {known_courses} bekannter Kurs/Kurse eindeutig erkannt.")
    print(f"✓ {len(new_courses)} neuer Kurs/Kurse in Notion angelegt.")
    print(f"✓ {len(conflict_courses)} Konflikt(e) blockiert.")
    sync_count = sum(
        1 for v in course_map.values() if v["sync_content"] and not v.get("conflict", False)
    )
    print(f"  {sync_count} von {len(course_map)} Kurs/Kursen haben SyncContent aktiviert.")
    if conflict_courses:
        print("  Blockierte Konflikte:")
        for name in conflict_courses:
            print(f"    ! {name}")
    print("=" * 60 + "\n")

    return course_map


def cmd_scan(session: requests.Session | None = None, course_map: dict | None = None):
    """
    Scrapt nur Kurse mit SyncContent=true und erfasst neue Aktivitäten im Manifest.
    Führt zuerst sync-courses aus (prüft/legt Kurse in Notion an).

    session und course_map können von cmd_run() übergeben werden, um
    doppelten Login zu vermeiden.
    """
    # Eigener Login nur wenn kein Session von außen übergeben wurde
    if session is None:
        session = requests.Session()
        session.headers["User-Agent"] = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"
        if not login(session):
            sys.exit(1)

    # Schritt 1: Kurse in Notion synchronisieren (oder gecachte Map verwenden)
    if course_map is None:
        course_map = cmd_sync_courses(session)

    active_courses = [
        (course_id, info)
        for course_id, info in course_map.items()
        if info["sync_content"] and not info.get("conflict", False)
    ]
    scan_result = {
        "total_new": 0,
        "new_by_course": {},
        "tracked_only_courses": [],
    }
    if not active_courses:
        log.warning("Keine konfliktfreien Kurse mit SyncContent=true – nichts zu scannen.")
        return scan_result

    # Schritt 2: Nur konfliktfreie Kurse mit SyncContent=true scannen
    conn = init_db()
    total_new = 0
    new_by_course: dict[str, list] = {}
    tracked_only_courses: list[dict] = []

    try:
        for course_id, info in active_courses:
            course = {
                "course_id": course_id,
                "name": info.get("name") or info.get("shortname") or course_id,
                "url": info["url"],
            }
            try:
                soup = _load_course_page(session, info["url"])
                shortname = _extract_shortname(soup)
                activities = _extract_activities(soup, course)
            except Exception as e:
                log.error(f"Fehler beim Scrapen von {info.get('shortname') or course_id}: {e}")
                continue

            info["shortname"] = shortname
            log.info(f"Scanne: {shortname} ({len(activities)} Aktivitäten)")
            new_in_course = []
            modtype_counts = Counter(activity["modtype"] for activity in activities)
            seen_cmids: set[str] = set()

            if activities and not any(_is_pushable_modtype(activity["modtype"]) for activity in activities):
                tracked_only_courses.append(
                    {
                        "course_id": course_id,
                        "shortname": shortname,
                        "total_activities": len(activities),
                        "modtype_counts": dict(sorted(modtype_counts.items())),
                    }
                )
                log.warning(
                    "Aktiver Kurs ohne pushbare Inhalte erkannt: "
                    f"{shortname} [{course_id}] ({_format_modtype_counts(modtype_counts)})"
                )

            for activity in activities:
                activity["course_shortname"] = shortname
                seen_cmids.add(activity["cmid"])
                is_new = upsert_activity(conn, activity)
                if is_new:
                    new_in_course.append(activity)
                    total_new += 1

            removed_count = _mark_missing_activities_removed(conn, course_id, seen_cmids)

            # Bestehende Einträge ohne Shortname nachfüllen (Migration alter Daten)
            conn.execute(
                "UPDATE resources SET course_shortname = ? WHERE course_id = ? AND course_shortname IS NULL",
                (shortname, course_id),
            )
            conn.commit()
            log.info(f"  {len(new_in_course)} neue Aktivität(en)")
            if removed_count:
                log.info(f"  {removed_count} verschwundene Aktivität(en) als removed markiert")
            if new_in_course:
                new_by_course[shortname] = new_in_course
    finally:
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
    if tracked_only_courses:
        print("\n! Aktive Kurse ohne pushbare Inhalte erkannt:")
        for course in tracked_only_courses:
            counts = _format_modtype_counts(course["modtype_counts"])
            print(
                f"  ! {course['shortname']} [{course['course_id']}] "
                f"→ {course['total_activities']} Aktivität(en), {counts}"
            )
    print("=" * 60 + "\n")

    scan_result["total_new"] = total_new
    scan_result["new_by_course"] = new_by_course
    scan_result["tracked_only_courses"] = tracked_only_courses
    return scan_result


def _guess_kategorie(name: str) -> str:
    """
    Heuristik: Kategorie aus dem Aktivitätsnamen ableiten.
    Gibt einen der erlaubten Notion-Select-Werte zurück.

    Reihenfolge: spezifische Keywords zuerst, Nummernpräfix als Fallback vor R Resource,
    damit "01 Aufgabenblatt" korrekt als Aufgabensammlung erkannt wird.
    """
    n = name.lower()
    # Vorlesung / Lecture (explizite Keywords)
    if any(k in n for k in ("vorlesung", "lecture", " vl ", "vl_", "vl.", "folie", "slides")):
        return "L Lecture"
    # Tutorial / Übung
    if any(k in n for k in ("tutorial", "tutorium", "übung", "uebung", " ue ", "ue_", "problem set", "exercise sheet")):
        return "T Tutorial"
    # Klausur / Exam
    if any(k in n for k in ("klausur", "exam", "prüfung", "pruefung", "mock", "altklausur")):
        return "E Exam"
    # Python / Notebook
    if any(k in n for k in ("python", ".ipynb", ".py", "notebook", "jupyter")):
        return "P Python"
    # Aufgaben
    if any(k in n for k in ("aufgabe", "blatt", "sheet", "exercise", "hausaufgabe")):
        return "A Aufgabensammlung"
    # Skript / Mitschrift / Zusammenfassung
    if any(k in n for k in ("skript", "script", "mitschrift", "zusammenfassung", "cheatsheet", "formula")):
        return "S Script"
    # Nummernpräfix à la "01 Einleitung", "02a Optimierung" → OR-Vorlesungsstil
    # (nach spezifischen Keywords, damit "01 Aufgabenblatt" korrekt als Aufgabe erkannt wird)
    if re.match(r"^\d{2}[a-z]?\s", n):
        return "L Lecture"
    return "R Resource"


def _guess_variante(name: str) -> str:
    """
    Heuristik: Variante aus dem Aktivitätsnamen ableiten.
    Gibt einen der erlaubten Notion-Select-Werte zurück.
    """
    n = name.lower()
    # Reihenfolge wichtig: Partial-Solution vor Solution prüfen
    if any(k in n for k in ("partial-solution", "partial solution", "partly solution", "partial sol")):
        return "Partial-Solution"
    if any(k in n for k in ("solution", "lösung", "loesung", "musterlösung", "musterloesung", " sol ", "answer")):
        return "Solution"
    if any(k in n for k in ("template", "vorlage")):
        return "Template"
    if any(k in n for k in ("annotated", "kommentiert", "mit anmerkungen")):
        return "Annotated"
    return "Original"


def _guess_format(filename: str) -> str | None:
    """Dateiendung → Notion-Format-Select-Wert (oder None wenn unbekannt)."""
    ext = Path(filename).suffix.lower().lstrip(".")
    return ext if ext in {"pdf", "ipynb", "py", "pkl", "zip"} else None


def _read_download_response(resp: requests.Response) -> tuple[bytes, str] | None:
    """Liest einen Download-Response in RAM und beachtet das Notion-Upload-Limit."""
    cd = resp.headers.get("Content-Disposition", "")
    match = re.search(r"filename\*?=(?:UTF-8'')?[\"']?([^\"';\r\n]+)", cd, re.IGNORECASE)
    if match:
        filename = unquote(match.group(1).strip().strip("\"'"))
    else:
        filename = _extract_filename_from_url(resp.url)

    content_length = int(resp.headers.get("Content-Length", 0))
    if content_length > MAX_NOTION_SINGLE_PART_BYTES:
        log.warning(
            f"Datei zu groß laut Content-Length ({content_length / 1024 / 1024:.1f} MB) – überspringe: {filename}"
        )
        return None

    file_bytes = b"".join(resp.iter_content(chunk_size=256 * 1024))
    if content_length and len(file_bytes) < content_length:
        raise requests.ConnectionError(
            f"Unvollständiger Download: {len(file_bytes)}/{content_length} Bytes – {filename}"
        )
    if len(file_bytes) > MAX_NOTION_SINGLE_PART_BYTES:
        log.warning(
            f"Datei zu groß nach Download ({len(file_bytes) / 1024 / 1024:.1f} MB) – überspringe: {filename}"
        )
        return None

    return file_bytes, filename


def _retry_download_operation(operation, *, request_url: str):
    """Führt eine Download-Operation mit begrenzten Retries für transiente Fehler aus."""
    retryable_exceptions = (
        requests.Timeout,
        requests.exceptions.ChunkedEncodingError,
        requests.ConnectionError,
    )

    for attempt in range(DOWNLOAD_MAX_RETRIES):
        try:
            return operation()
        except retryable_exceptions as exc:
            if attempt >= DOWNLOAD_MAX_RETRIES - 1:
                raise
            wait_seconds = DOWNLOAD_RETRY_BACKOFF_SECONDS[attempt]
            log.warning(
                "Retrybarer Download-Fehler für %s (%s: %s) – Versuch %s/%s, warte %ss",
                request_url,
                type(exc).__name__,
                exc,
                attempt + 1,
                DOWNLOAD_MAX_RETRIES,
                wait_seconds,
            )
            time.sleep(wait_seconds)


def _download_file_url(
    session: requests.Session, file_url: str
) -> tuple[bytes, str] | None:
    """Lädt eine direkte Datei-URL herunter."""
    def _download_once() -> tuple[bytes, str] | None:
        resp = session.get(file_url, allow_redirects=True, stream=True, timeout=60)
        try:
            resp.raise_for_status()
            if _response_is_html(resp):
                log.warning(f"Direkte Datei-URL liefert HTML statt Datei: {file_url}")
                return None
            return _read_download_response(resp)
        finally:
            resp.close()

    return _retry_download_operation(_download_once, request_url=file_url)


def _download_resource(
    session: requests.Session,
    view_url: str,
    *,
    _visited_urls: set[str] | None = None,
) -> ResourceDownloadResult:
    """
    Lädt eine Moodle-Ressource herunter.
    Gibt ein strukturiertes Ergebnis mit Datei oder klassifiziertem Fehler zurück.
    """
    normalized_view_url = _normalize_absolute_url(view_url)
    visited_urls = set(_visited_urls or ())
    if normalized_view_url in visited_urls:
        return _resource_error_result(
            "retryable_error",
            failure_reason="html_no_download_link",
            final_url=normalized_view_url,
        )
    visited_urls.add(normalized_view_url)

    def _download_once() -> ResourceDownloadResult:
        resp = session.get(normalized_view_url, allow_redirects=True, stream=True, timeout=60)
        try:
            resp.raise_for_status()
            final_url = resp.url or normalized_view_url
            if "/course/view.php" in urlparse(final_url).path:
                return _resource_error_result(
                    "terminal_error",
                    failure_reason="redirected_to_course",
                    final_url=final_url,
                )

            if not _response_is_html(resp):
                result = _read_download_response(resp)
                if result is None:
                    return _resource_error_result(
                        "retryable_error",
                        failure_reason="download_unavailable",
                        final_url=final_url,
                    )
                file_bytes, file_name = result
                return _resource_file_result(file_bytes, file_name, final_url=final_url)

            soup = BeautifulSoup(resp.text, "html.parser")
            title = _extract_html_title(soup)
            if _is_invalid_module_page(soup):
                return _resource_error_result(
                    "terminal_error",
                    failure_reason="invalid_module",
                    final_url=final_url,
                    title=title,
                )

            file_url = _extract_first_download_link(soup)
            if file_url is None:
                return _resource_error_result(
                    "retryable_error",
                    failure_reason="html_no_download_link",
                    final_url=final_url,
                    title=title,
                )

            return _download_resource(session, file_url, _visited_urls=visited_urls)
        finally:
            resp.close()

    try:
        return _retry_download_operation(_download_once, request_url=normalized_view_url)
    except (requests.Timeout, requests.exceptions.ChunkedEncodingError, requests.ConnectionError) as exc:
        return _resource_error_result(
            "retryable_error",
            failure_reason="download_exception",
            final_url=normalized_view_url,
            exception=exc,
        )


def notion_upload_file(file_bytes: bytes, filename: str) -> str | None:
    """
    Lädt eine Datei über die Notion File Upload API hoch (single_part, max 20 MB).
    Gibt die file_upload_id zurück, oder None bei Überschreitung des Größenlimits.
    """
    if len(file_bytes) > MAX_NOTION_SINGLE_PART_BYTES:
        log.warning(
            f"Datei zu groß ({len(file_bytes) / 1024 / 1024:.1f} MB) – kein Upload: {filename}"
        )
        return None

    # Datei-Typ ermitteln
    ext = Path(filename).suffix.lower()
    content_type = "application/pdf" if ext == ".pdf" else "application/octet-stream"

    # Schritt 1: Upload-Objekt erstellen
    resp = _notion_request(
        "POST",
        f"{NOTION_API}/file_uploads",
        headers={"Authorization": f"Bearer {NOTION_TOKEN}", "Notion-Version": NOTION_VERSION},
        json={"mode": "single_part", "content_type": content_type},
    )
    upload_id = resp.json()["id"]
    upload_url = resp.json()["upload_url"]

    # Schritt 2: Datei hochladen — POST (nicht PUT!) + Notion-Version Header erforderlich
    # Content-Type darf hier NICHT im Header stehen (multipart/form-data wird automatisch gesetzt)
    _notion_request(
        "POST",
        upload_url,
        headers={"Authorization": f"Bearer {NOTION_TOKEN}", "Notion-Version": NOTION_VERSION},
        files={"file": (filename, file_bytes, content_type)},
    )

    return upload_id


def _mark_activity_error(
    conn: sqlite3.Connection,
    cmid: str,
    *,
    file_name: str | None = None,
    failure_reason: str | None = None,
    failure_detail: str | None = None,
    retryable: bool = True,
) -> None:
    """Markiert eine Aktivität als Fehler und aktualisiert optional den Dateinamen."""
    now = _now_utc_iso()
    if file_name is None:
        conn.execute(
            """
            UPDATE resources
            SET status = 'error',
                failure_reason = ?,
                failure_detail = ?,
                last_attempt_at = ?,
                retryable = ?
            WHERE cmid = ?
            """,
            (failure_reason, failure_detail, now, int(retryable), cmid),
        )
    else:
        conn.execute(
            """
            UPDATE resources
            SET status = 'error',
                file_name = ?,
                failure_reason = ?,
                failure_detail = ?,
                last_attempt_at = ?,
                retryable = ?
            WHERE cmid = ?
            """,
            (file_name, failure_reason, failure_detail, now, int(retryable), cmid),
        )
    conn.commit()


def _mark_activity_synced(
    conn: sqlite3.Connection,
    cmid: str,
    notion_id: str,
    *,
    file_hash: str | None = None,
    file_name: str | None = None,
) -> None:
    """Persistiert den erfolgreichen Sync-Zustand einer Aktivität."""
    now = _now_utc_iso()
    conn.execute(
        """
        UPDATE resources
        SET notion_id = ?,
            file_name = ?,
            file_hash = ?,
            status = 'synced',
            failure_reason = NULL,
            failure_detail = NULL,
            last_attempt_at = ?,
            retryable = 1
        WHERE cmid = ?
        """,
        (notion_id, file_name, file_hash, now, cmid),
    )
    conn.commit()


def _mark_missing_activities_removed(
    conn: sqlite3.Connection, course_id: str, seen_cmids: set[str]
) -> int:
    """Markiert nicht mehr gesichtete Activities eines Kurses als entfernt."""
    params: list[str] = [course_id]
    query = """
        UPDATE resources
        SET status = 'removed',
            failure_reason = NULL,
            failure_detail = NULL,
            retryable = 1
        WHERE course_id = ?
          AND status != 'removed'
    """
    if seen_cmids:
        placeholders = ",".join("?" * len(seen_cmids))
        query += f" AND cmid NOT IN ({placeholders})"
        params.extend(sorted(seen_cmids))

    cursor = conn.execute(query, params)
    conn.commit()
    return cursor.rowcount


def _archive_page_after_failure(page_id: str) -> None:
    """Archiviert eine bereits angelegte Seite nach einem Folgefehler best effort."""
    try:
        notion_archive_page(page_id)
    except Exception as archive_error:
        log.warning(f"    Konnte Notion-Seite nach Fehler nicht archivieren: {archive_error}")


def _guard_missing_view_url(conn: sqlite3.Connection, row: dict) -> dict | None:
    """Fängt manifestseitig fehlende view_url defensiv vor Handler-Logik ab."""
    if row.get("view_url"):
        return None

    if row.get("status") == RESOURCE_STATUS_DEFERRED:
        log.info("    Überspringe deferred-Aktivität ohne view_url")
        return {"status": "skipped", "modtype": row["modtype"], "reason": "no_view_url"}

    log.error("    Ungültiger Manifest-Zustand: fehlende view_url")
    _mark_activity_error(
        conn,
        row["cmid"],
        failure_reason=MISSING_VIEW_URL_FAILURE_REASON,
        failure_detail="view_url is NULL",
        retryable=False,
    )
    return {
        "status": "error",
        "modtype": row["modtype"],
        "failure_reason": MISSING_VIEW_URL_FAILURE_REASON,
    }


def _push_resource_activity(
    conn: sqlite3.Connection,
    session: requests.Session,
    row: dict,
    course_map: dict,
    course_notion_page_id: str | None,
) -> dict:
    """Pusht eine einzelne Resource-Aktivität nach Notion."""
    guard_result = _guard_missing_view_url(conn, row)
    if guard_result is not None:
        return guard_result

    if row["notion_id"]:
        return {"status": "skipped", "modtype": "resource"}

    try:
        result = _download_resource(session, row["view_url"])
    except Exception as e:
        log.error(f"    Download-Fehler: {e}")
        _mark_activity_error(
            conn,
            row["cmid"],
            failure_reason="download_exception",
            failure_detail=_build_failure_detail(exception=e),
        )
        return {"status": "retryable_error", "modtype": "resource"}

    if result.kind != "file":
        detail = f" [{result.failure_reason}]" if result.failure_reason else ""
        if result.failure_detail:
            log.error(f"    Resource-Fehler{detail}: {result.failure_detail}")
        else:
            log.error(f"    Resource-Fehler{detail}: {row['name']}")
        retryable = result.kind != "terminal_error"
        _mark_activity_error(
            conn,
            row["cmid"],
            failure_reason=result.failure_reason,
            failure_detail=result.failure_detail,
            retryable=retryable,
        )
        return {
            "status": "retryable_error" if retryable else "terminal_error",
            "modtype": "resource",
            "failure_reason": result.failure_reason,
        }

    assert result.file_bytes is not None
    assert result.file_name is not None
    file_bytes, file_name = result.file_bytes, result.file_name
    file_hash = hashlib.md5(file_bytes).hexdigest()

    upload_id = None
    try:
        upload_id = notion_upload_file(file_bytes, file_name)
    except Exception as e:
        log.warning(f"    Upload-Fehler: {e} – Seite wird ohne Datei angelegt")

    resource = {
        "cmid": row["cmid"],
        "course_id": row["course_id"],
        "name": row["name"],
        "file_name": file_name,
        "course_shortname": _resolve_course_shortname(
            course_map, row["course_id"], row["course_shortname"]
        ),
    }

    try:
        notion_id = notion_create_lw_page(resource, upload_id, course_notion_page_id)
    except Exception as e:
        log.error(f"    Notion-Fehler: {e}")
        _mark_activity_error(conn, row["cmid"], file_name=file_name)
        return {"status": "error", "modtype": "resource"}

    _mark_activity_synced(
        conn,
        row["cmid"],
        notion_id,
        file_hash=file_hash,
        file_name=file_name,
    )
    if upload_id:
        log.info("    ✓ Mit Datei angelegt")
    else:
        log.info("    ✓ Ohne Datei angelegt (>20 MB oder Upload-Fehler)")
    return {"status": "created", "modtype": "resource", "with_file": bool(upload_id)}


def _push_folder_activity(
    conn: sqlite3.Connection,
    session: requests.Session,
    row: dict,
    course_map: dict,
    course_notion_page_id: str | None,
) -> dict:
    """Pusht oder aktualisiert eine Folder-Aktivität atomar."""
    guard_result = _guard_missing_view_url(conn, row)
    if guard_result is not None:
        return guard_result

    try:
        soup = _fetch_activity_soup(session, row["view_url"])
        folder_files = _extract_folder_files(soup)
    except Exception as e:
        log.error(f"    Folder-Extraktionsfehler: {e}")
        _mark_activity_error(conn, row["cmid"])
        return {"status": "error", "modtype": "folder"}

    if not folder_files:
        log.error("    Keine Dateien im Folder gefunden")
        _mark_activity_error(conn, row["cmid"])
        return {"status": "error", "modtype": "folder"}

    fingerprint = _folder_fingerprint(folder_files)
    if row["notion_id"] and row["file_hash"] == fingerprint:
        log.info("    ✓ Folder unverändert")
        return {"status": "skipped", "modtype": "folder"}

    upload_ids: list[str] = []
    file_names: list[str] = []
    for _, download_url in folder_files:
        try:
            result = _download_file_url(session, download_url)
        except Exception as e:
            log.error(f"    Folder-Download-Fehler: {e}")
            _mark_activity_error(conn, row["cmid"])
            return {"status": "error", "modtype": "folder"}

        if result is None:
            log.error("    Folder enthält eine nicht uploadbare Datei – kompletter Folder übersprungen")
            _mark_activity_error(conn, row["cmid"])
            return {"status": "error", "modtype": "folder"}

        file_bytes, file_name = result
        file_names.append(file_name)

        try:
            upload_id = notion_upload_file(file_bytes, file_name)
        except Exception as e:
            log.error(f"    Folder-Upload-Fehler: {e}")
            _mark_activity_error(conn, row["cmid"])
            return {"status": "error", "modtype": "folder"}

        if upload_id is None:
            log.error("    Folder enthält eine zu große Datei – kompletter Folder übersprungen")
            _mark_activity_error(conn, row["cmid"])
            return {"status": "error", "modtype": "folder"}
        upload_ids.append(upload_id)

    display_name = (
        f"{row['section']} — {row['name']}" if row.get("section") else row["name"]
    )
    resource = {
        "cmid": row["cmid"],
        "course_id": row["course_id"],
        "name": row["name"],
        "display_name": display_name,
        "source_name": row["name"],
        "file_names": file_names,
        "course_shortname": _resolve_course_shortname(
            course_map, row["course_id"], row["course_shortname"]
        ),
    }

    try:
        if row["notion_id"]:
            notion_update_lw_page(
                row["notion_id"],
                resource,
                upload_ids,
                course_notion_page_id,
            )
            notion_id = row["notion_id"]
            status = "updated"
            log.info(f"    ✓ Folder aktualisiert ({len(upload_ids)} Datei(en))")
        else:
            notion_id = notion_create_lw_page(resource, upload_ids, course_notion_page_id)
            status = "created"
            log.info(f"    ✓ Folder angelegt ({len(upload_ids)} Datei(en))")
    except Exception as e:
        log.error(f"    Notion-Fehler: {e}")
        _mark_activity_error(conn, row["cmid"])
        return {"status": "error", "modtype": "folder"}

    _mark_activity_synced(
        conn,
        row["cmid"],
        notion_id,
        file_hash=fingerprint,
        file_name=json.dumps(file_names, ensure_ascii=True),
    )
    return {"status": status, "modtype": "folder", "attachment_count": len(upload_ids)}


def _push_url_activity(
    conn: sqlite3.Connection,
    session: requests.Session,
    row: dict,
    course_map: dict,
    course_notion_page_id: str | None,
) -> dict:
    """Pusht eine URL-Aktivität als Notion-Seite mit Ziel-URL-Property."""
    guard_result = _guard_missing_view_url(conn, row)
    if guard_result is not None:
        return guard_result

    if row["notion_id"]:
        return {"status": "skipped", "modtype": "url"}

    try:
        target_url = _extract_url_target(session, row["view_url"])
    except Exception as e:
        log.error(f"    URL-Extraktionsfehler: {e}")
        _mark_activity_error(conn, row["cmid"])
        return {"status": "error", "modtype": "url"}

    if not target_url:
        log.error("    Keine Ziel-URL gefunden")
        _mark_activity_error(conn, row["cmid"])
        return {"status": "error", "modtype": "url"}

    resource = {
        "cmid": row["cmid"],
        "course_id": row["course_id"],
        "name": row["name"],
        "course_shortname": _resolve_course_shortname(
            course_map, row["course_id"], row["course_shortname"]
        ),
    }

    try:
        notion_id = notion_create_lw_page(
            resource,
            None,
            course_notion_page_id,
            target_url=target_url,
        )
    except Exception as e:
        log.error(f"    Notion-Fehler: {e}")
        _mark_activity_error(conn, row["cmid"])
        return {"status": "error", "modtype": "url"}

    _mark_activity_synced(
        conn,
        row["cmid"],
        notion_id,
        file_hash=hashlib.md5(target_url.encode("utf-8")).hexdigest(),
    )
    log.info("    ✓ URL-Seite angelegt (Ziel-URL gesetzt)")
    return {"status": "created", "modtype": "url"}


def _push_page_activity(
    conn: sqlite3.Connection,
    session: requests.Session,
    row: dict,
    course_map: dict,
    course_notion_page_id: str | None,
) -> dict:
    """Pusht eine Page-Aktivität als Notion-Seite mit Paragraph-Blöcken."""
    guard_result = _guard_missing_view_url(conn, row)
    if guard_result is not None:
        return guard_result

    if row["notion_id"]:
        return {"status": "skipped", "modtype": "page"}

    try:
        page_text = _extract_page_content(session, row["view_url"])
    except Exception as e:
        log.error(f"    Page-Extraktionsfehler: {e}")
        _mark_activity_error(conn, row["cmid"])
        return {"status": "error", "modtype": "page"}

    if not page_text:
        log.error("    Page enthält keinen nutzbaren Text")
        _mark_activity_error(conn, row["cmid"])
        return {"status": "error", "modtype": "page"}

    blocks = _build_paragraph_blocks(page_text)
    if not blocks:
        log.error("    Page erzeugt keine Notion-Blöcke")
        _mark_activity_error(conn, row["cmid"])
        return {"status": "error", "modtype": "page"}

    resource = {
        "cmid": row["cmid"],
        "course_id": row["course_id"],
        "name": row["name"],
        "course_shortname": _resolve_course_shortname(
            course_map, row["course_id"], row["course_shortname"]
        ),
    }

    try:
        notion_id = notion_create_lw_page(resource, None, course_notion_page_id)
        notion_append_page_children(notion_id, blocks)
    except Exception as e:
        log.error(f"    Notion-Fehler: {e}")
        if "notion_id" in locals():
            _archive_page_after_failure(notion_id)
        _mark_activity_error(conn, row["cmid"])
        return {"status": "error", "modtype": "page"}

    _mark_activity_synced(
        conn,
        row["cmid"],
        notion_id,
        file_hash=hashlib.md5(page_text.encode("utf-8")).hexdigest(),
    )
    log.info(f"    ✓ Page mit {len(blocks)} Paragraph-Blöcken angelegt")
    return {"status": "created", "modtype": "page"}


def cmd_push(session: requests.Session | None = None, course_map: dict | None = None):
    """
    Pusht die aktuell unterstützten LearnWeb-Inhalte nach Notion.
    `resource`, `url` und `page` werden nur initial angelegt.
    `folder` wird zusätzlich auf Inhaltsänderungen geprüft und bei Bedarf gepatcht.

    session und course_map können von cmd_run() übergeben werden, um
    doppelten Login und doppeltes Laden der Kursseiten zu vermeiden.
    """
    if not NOTION_TOKEN:
        log.error("NOTION_TOKEN nicht gesetzt")
        sys.exit(1)
    if not NOTION_LW_DB_ID:
        log.error("NOTION_LW_DB_ID nicht gesetzt")
        sys.exit(1)

    # Eigener Login nur wenn kein Session von außen übergeben wurde
    if session is None:
        session = requests.Session()
        session.headers["User-Agent"] = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"
        if not login(session):
            sys.exit(1)

    # Kursliste + KurseLearnWeb-Abgleich (oder gecachte Map verwenden)
    if course_map is None:
        course_map = cmd_sync_courses(session)

    # Nur aktive, konfliktfreie Kurse (SyncContent=true) → {course_id: notion_page_id}
    active = {
        cid: info["notion_page_id"]
        for cid, info in course_map.items()
        if info["sync_content"] and not info.get("conflict", False)
    }
    if not active:
        log.warning("Keine konfliktfreien Kurse mit SyncContent=true – nichts zu pushen.")
        return

    conn = init_db()
    counters: Counter[str] = Counter()

    try:
        modtype_placeholders = ",".join("?" * len(PUSHABLE_MODTYPES))
        course_placeholders = ",".join("?" * len(active))
        rows = conn.execute(
            f"""
            SELECT cmid, course_id, name, view_url, course_shortname,
                   modtype, notion_id, file_hash, file_name, section,
                   status, retryable, failure_reason, failure_detail
            FROM resources
            WHERE modtype IN ({modtype_placeholders})
              AND course_id IN ({course_placeholders})
              AND status NOT IN ('removed', 'deferred')
              AND (modtype != 'resource' OR notion_id IS NOT NULL OR retryable = 1)
            ORDER BY course_id, first_seen
            """,
            [*sorted(PUSHABLE_MODTYPES), *list(active.keys())],
        ).fetchall()

        if not rows:
            print("\n✓ Keine pushbaren Inhalte im Manifest gefunden.\n")
            return

        log.info(f"{len(rows)} pushbare Aktivität(en) im Manifest.")
        url_schema_error: tuple[str, str] | None = None
        has_unsynced_url_rows = any(
            raw_row[5] == "url" and raw_row[6] is None for raw_row in rows
        )

        if has_unsynced_url_rows:
            try:
                if not notion_lw_db_has_target_url_property():
                    detail = (
                        f"Pflicht-Property '{LW_TARGET_URL_PROPERTY}' (Typ URL) fehlt in "
                        f"NOTION_LW_DB_ID={NOTION_LW_DB_ID}. Vor dem Deploy in Notion anlegen."
                    )
                    log.error(detail)
                    url_schema_error = ("missing_target_url_property", detail)
            except Exception as e:
                detail = _build_failure_detail(exception=e)
                log.error(
                    f"Schema-Check für '{LW_TARGET_URL_PROPERTY}' fehlgeschlagen: {e}"
                )
                url_schema_error = ("target_url_schema_check_failed", detail)

        handlers = {
            "resource": _push_resource_activity,
            "folder": _push_folder_activity,
            "url": _push_url_activity,
            "page": _push_page_activity,
        }
        columns = (
            "cmid",
            "course_id",
            "name",
            "view_url",
            "course_shortname",
            "modtype",
            "notion_id",
            "file_hash",
            "file_name",
            "section",
            "status",
            "retryable",
            "failure_reason",
            "failure_detail",
        )

        for raw_row in rows:
            row = dict(zip(columns, raw_row))
            log.info(
                f"  Push: {row['name']} (cmid={row['cmid']}, modtype={row['modtype']})"
            )

            if (
                row["modtype"] == "url"
                and row["notion_id"] is None
                and url_schema_error is not None
            ):
                failure_reason, failure_detail = url_schema_error
                _mark_activity_error(
                    conn,
                    row["cmid"],
                    failure_reason=failure_reason,
                    failure_detail=failure_detail,
                )
                counters["error"] += 1
                counters["url_error"] += 1
                continue

            handler = handlers[row["modtype"]]
            result = handler(
                conn,
                session,
                row,
                course_map,
                active.get(row["course_id"]),
            )
            counters[result["status"]] += 1
            counters[f"{row['modtype']}_{result['status']}"] += 1
    finally:
        conn.close()

    print("\n" + "=" * 60)
    total_errors = (
        counters["error"] + counters["retryable_error"] + counters["terminal_error"]
    )
    print(
        "✓ Push: "
        f"{counters['created']} erstellt, "
        f"{counters['updated']} aktualisiert, "
        f"{counters['skipped']} unverändert, "
        f"{total_errors} Fehler"
    )
    if counters["resource_retryable_error"] or counters["resource_terminal_error"]:
        print(
            "  Resource-Fehler: "
            f"{counters['resource_retryable_error']} retryable, "
            f"{counters['resource_terminal_error']} terminal"
        )
    print("=" * 60 + "\n")


def cmd_run():
    """
    scan + push in einem Schritt (Standard für automatische Läufe).
    Teilt Login und Kursseiten-Laden zwischen scan und push – nur 1× Login, 1× Kursseiten.
    """
    session = requests.Session()
    session.headers["User-Agent"] = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"
    if not login(session):
        sys.exit(1)
    course_map = cmd_sync_courses(session)
    scan_result = cmd_scan(session=session, course_map=course_map)
    cmd_push(session=session, course_map=course_map)
    tracked_only_courses = scan_result["tracked_only_courses"]
    if tracked_only_courses:
        summaries = ", ".join(
            f"{course['shortname']} [{course['course_id']}]"
            for course in tracked_only_courses
        )
        log.error(
            "Sync unvollständig: aktive Kurse ohne pushbare Inhalte erkannt: "
            f"{summaries}"
        )
        sys.exit(2)


def cmd_diagnose_resource_errors(
    session: requests.Session | None = None,
    *,
    limit: int = 50,
    include_terminal: bool = False,
) -> list[dict]:
    """
    Klassifiziert offene Resource-Fälle read-only mit derselben Logik wie cmd_push().
    Gibt eine Liste diagnostizierter Einträge zurück und schreibt keine DB-Änderungen.
    """
    if limit <= 0:
        log.error("limit muss > 0 sein")
        sys.exit(1)

    if session is None:
        session = requests.Session()
        session.headers["User-Agent"] = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"
        if not login(session):
            sys.exit(1)

    conn = init_db()
    try:
        terminal_filter = "" if include_terminal else "AND retryable = 1"
        rows = conn.execute(
            f"""
            SELECT cmid, course_id, course_shortname, name, view_url, status, retryable
            FROM resources
            WHERE modtype = 'resource'
              AND notion_id IS NULL
              AND status = 'error'
              {terminal_filter}
            ORDER BY last_seen DESC, first_seen DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
    finally:
        conn.close()

    if not rows:
        print("\n✓ Keine offenen Resource-Fälle zur Diagnose gefunden.\n")
        return []

    columns = ("cmid", "course_id", "course_shortname", "name", "view_url", "status", "retryable")
    diagnosed: list[dict] = []
    counts: Counter[str] = Counter()

    for raw_row in rows:
        row = dict(zip(columns, raw_row))
        result = _download_resource(session, row["view_url"])
        reason = "ok" if result.kind == "file" else (result.failure_reason or "unknown")
        counts[reason] += 1
        diagnosed.append(
            {
                **row,
                "kind": result.kind,
                "failure_reason": reason,
                "failure_detail": result.failure_detail,
                "final_url": result.final_url,
            }
        )

    print("\n" + "=" * 60)
    print(f"✓ Diagnose: {len(diagnosed)} Resource-Fall/Fälle geprüft")
    for reason, count in sorted(counts.items()):
        print(f"  {reason}: {count}")
    print("-" * 60)
    for item in diagnosed:
        course_shortname = item["course_shortname"] or item["course_id"]
        print(
            f"  [{item['failure_reason']}] {item['cmid']} "
            f"{course_shortname} — {item['name']}"
        )
        if item["final_url"]:
            print(f"    final_url: {item['final_url']}")
        if item["failure_detail"] and item["kind"] != "file":
            print(f"    detail: {item['failure_detail']}")
    print("=" * 60 + "\n")

    return diagnosed


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

    shortname = _extract_shortname(soup)

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
  sync-courses  Sync enrolled courses to KurseLearnWeb in Notion
  scan          Scrape courses with SyncContent=true, record new activities in manifest
  push          Push supported LearnWeb content to Notion pages
  run           sync-courses + scan + push in one step (for automated runs)
  diagnose-resource-errors
                Probe open resource rows read-only and classify failures
  export-zips   Download all courses as ZIP files (backup mode)
""",
    )
    parser.add_argument(
        "command",
        choices=[
            "sync-courses",
            "scan",
            "push",
            "run",
            "diagnose-resource-errors",
            "export-zips",
        ],
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=50,
        help="Maximalzahl für diagnose-resource-errors (Default: 50)",
    )
    parser.add_argument(
        "--include-terminal",
        action="store_true",
        help="diagnose-resource-errors: auch terminal markierte Fälle erneut prüfen",
    )
    args = parser.parse_args()

    if not USERNAME or not PASSWORD:
        log.error("LEARNWEB_USERNAME oder LEARNWEB_PASSWORD nicht gesetzt in .env")
        sys.exit(1)

    if args.command == "sync-courses":
        cmd_sync_courses()
    elif args.command == "scan":
        cmd_scan()
    elif args.command == "push":
        cmd_push()
    elif args.command == "run":
        cmd_run()
    elif args.command == "diagnose-resource-errors":
        cmd_diagnose_resource_errors(
            limit=args.limit,
            include_terminal=args.include_terminal,
        )
    elif args.command == "export-zips":
        cmd_export_zips()


if __name__ == "__main__":
    main()
