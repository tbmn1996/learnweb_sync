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
import io
import mimetypes
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import BinaryIO
import unicodedata
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
NOTION_MEETING_DB_ID = os.getenv("NOTION_MEETING_DB_ID", "") # Meeting-Notizen-DB (Transkripte)
NOTION_API = "https://api.notion.com/v1"

# ── Transkriptions-Worker (lokal, On-Device-Whisper) ──────────────────────────
# Diese Konfiguration nutzt ausschließlich der lokale `transcribe`-Command (Mac).
# Railway lädt diesen Pfad nie (kein mlx/ffmpeg dort).
WHISPER_MODEL = os.getenv("WHISPER_MODEL", "large-v3-turbo")
WHISPER_LANGUAGE = os.getenv("WHISPER_LANGUAGE", "de")
TRANSCRIBE_MAX_ATTEMPTS = int(os.getenv("TRANSCRIBE_MAX_ATTEMPTS", "3"))
TRANSCRIBE_LOCK_PATH = Path(
    os.getenv("TRANSCRIBE_LOCK_PATH", str(Path(__file__).parent / "transcribe.lock"))
)
TRANSCRIBE_WORK_DIR = Path(
    os.getenv("TRANSCRIBE_WORK_DIR", str(Path(__file__).parent / ".transcribe_tmp"))
)
# rich_text-Property in BEIDEN Ziel-DBs, die den eindeutigen Recording-Key trägt
# (Basis für remote create-or-find / Idempotenz, siehe Plan §3). Wird per additiver
# Schema-Erweiterung angelegt (Task #5, freigabepflichtig).
RECORDING_QUELLE_PROPERTY = "Quelle"
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
# Modtypes, die der LOKALE Transkriptions-Worker verarbeitet. Bewusst getrennt von
# PUSHABLE_MODTYPES: opencast darf NICHT in den globalen Railway-push-Pfad geraten
# (cmd_push besitzt keinen opencast-Handler) und wird ausschließlich vom transcribe-
# Command auf dem Mac genutzt.
TRANSCRIBABLE_MODTYPES = {"opencast"}
MAX_NOTION_SINGLE_PART_BYTES = 20 * 1024 * 1024
NOTION_UPLOAD_PART_BYTES = 10 * 1024 * 1024
MAX_NOTION_FILE_BYTES = 5 * 1024 * 1024 * 1024
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
    file_bytes: bytes | BinaryIO | None = None
    file_name: str | None = None
    failure_reason: str | None = None
    failure_detail: str | None = None
    final_url: str | None = None
    page_text: str | None = None


class CourseSyncResult(dict):
    """Kursindex mit separaten Fehlern; bestehende dict-Aufrufer bleiben gültig."""

    def __init__(self):
        super().__init__()
        self.errors: list[dict] = []


class UploadBlockedError(RuntimeError):
    """Eine Providergrenze verhindert den unveränderten Originalupload."""


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
    if "attachment_state" not in existing:
        conn.execute("ALTER TABLE resources ADD COLUMN attachment_state TEXT")
    conn.commit()
    return conn


# Gültige Zustände des Transkriptions-Automaten (siehe Plan §3). Reihenfolge =
# Fortschritt; "failed" ist ein Seitenzweig, der mit failure_stage/-reason erklärt wird.
TRANSCRIBE_STATES = (
    "pending",          # entdeckt, noch nicht beansprucht
    "claimed",          # atomar beansprucht (genau ein Worker bearbeitet)
    "downloaded",       # Medien + Audio lokal vorhanden
    "transcribed",      # Whisper-Segmente erzeugt
    "inhalt_created",   # schlanker Learnweb-Inhalte-Eintrag steht (content_page_id)
    "meeting_created",  # Meeting-Notiz-Seite steht (meeting_page_id)
    "appending",        # Body wird in Batches angehängt (appended_block_count wächst)
    "done",             # vollständig
    "failed",           # abgebrochen, mit failure_stage/failure_reason
)


def init_transcribe_schema(conn: sqlite3.Connection) -> sqlite3.Connection:
    """Legt die `transcripts`-Tabelle idempotent an (lokaler Transkriptions-Worker).

    Bewusst NICHT in init_db(): so bleibt der Railway-Pfad (der nur init_db ruft)
    bit-identisch. Diese Funktion ruft ausschließlich der lokale transcribe-Command.
    Crash-/Resume-sicher: alle Zwischenstände (page_ids, Body-Hash, Append-Fortschritt)
    werden hier persistiert, damit ein abgebrochener Lauf wieder aufsetzen kann (Plan §3).
    """
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS transcripts (
            key                  TEXT PRIMARY KEY,   -- {cmid}-{sha1(episode_id|media_url)[:12]}
            cmid                 TEXT NOT NULL,
            course_id            TEXT,
            course_shortname     TEXT,
            episode_id           TEXT,               -- Opencast-Episode-ID / Media-URL-Diskriminator
            title                TEXT,
            source_url           TEXT,               -- Opencast view-URL bzw. Media-URL
            recorded_at          TEXT,               -- ISO-Datum der Aufzeichnung (darf NULL sein)
            status               TEXT NOT NULL DEFAULT 'pending',
            failure_stage        TEXT,               -- Zustand, in dem es scheiterte
            failure_reason       TEXT,
            media_path           TEXT,               -- temporärer lokaler Medien-Pfad
            audio_path           TEXT,               -- temporärer WAV-Pfad
            duration_seconds     REAL,
            model                TEXT,               -- genutztes Whisper-Backend/Modell
            content_page_id      TEXT,               -- Notion Learnweb-Inhalte page id
            meeting_page_id      TEXT,               -- Notion Meeting-Notizen page id
            body_sha256          TEXT,               -- Hash des Transkript-Quelltexts (Resume-Konsistenz)
            total_block_count    INTEGER,            -- Gesamtzahl Body-Blöcke
            appended_block_count INTEGER NOT NULL DEFAULT 0,
            attempts             INTEGER NOT NULL DEFAULT 0,
            first_seen           TEXT NOT NULL,      -- ISO-8601 UTC
            updated_at           TEXT NOT NULL,      -- ISO-8601 UTC
            claimed_at           TEXT                -- ISO-8601 UTC des letzten Claims
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_transcripts_status ON transcripts(status)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_transcripts_cmid ON transcripts(cmid)")
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
    if _is_enrolment_page(soup, final_url) or _is_login_or_error_page(soup, final_url):
        raise RuntimeError(f"Keine belegte Kursseite: {course_url} -> {final_url}")
    if parse_course_id_from_url(final_url) != parse_course_id_from_url(course_url):
        raise RuntimeError(f"Falsche Kursidentität: {course_url} -> {final_url}")
    if not urlparse(final_url).path.endswith("/course/view.php"):
        raise RuntimeError(f"Keine Kursansicht: {final_url}")
    body = soup.find("body")
    body_course_ids = [cls.removeprefix("course-") for cls in (body.get("class", []) if body else [])
                       if re.fullmatch(r"course-\d+", cls)]
    if body_course_ids and parse_course_id_from_url(course_url) not in body_course_ids:
        raise RuntimeError("HTML-Kursidentität passt nicht zur angeforderten Kursseite")
    return soup


def _is_login_or_error_page(soup: BeautifulSoup, final_url: str) -> bool:
    """Erkennt Authentifizierungs- und Moodle-Fehlerseiten vor einer Übernahme."""
    path = urlparse(final_url).path.lower()
    return bool(
        "/login/" in path or "/auth/" in path
        or soup.select_one('input[name="password"], #page-login-index, form[action*="/login/"] input[name="logintoken"], #loginerrormessage, .errorbox, [data-region="error"]')
        or _is_invalid_module_page(soup)
        or (_extract_html_title(soup) or "").lower().startswith("error |")
    )


def _validate_course_inventory(soup: BeautifulSoup, activities: list[dict]) -> None:
    """Ein Leerbestand braucht erkennbare Kursstruktur; Parsefehler sind kein Entfernen."""
    if not soup.select("li.course-section"):
        raise RuntimeError("Kursstruktur fehlt; Manifest bleibt erhalten")
    for item in soup.select('li[data-for="cmitem"]'):
        classes = item.get("class", [])
        if "modtype_label" in classes:
            continue
        if not item.get("data-id") or not any(c.startswith("modtype_") for c in classes):
            raise RuntimeError("Unvollständige Activity-Struktur; Manifest bleibt erhalten")
    parsed = {activity["cmid"] for activity in activities}
    expected = {
        item.get("data-id") for item in soup.select('li[data-for="cmitem"]')
        if "modtype_label" not in item.get("class", [])
    }
    if expected != parsed:
        raise RuntimeError("Unvollständiges Activity-Inventar; Manifest bleibt erhalten")


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
    file_bytes: bytes | BinaryIO,
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

    for node in soup.select("iframe[src], embed[src], object[data]"):
        candidate = _normalize_absolute_url(node.get("src") or node.get("data") or "")
        if _is_candidate_download_url(candidate):
            return candidate

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
    soup = BeautifulSoup(resp.text, "html.parser")
    if _is_login_or_error_page(soup, resp.url or view_url):
        raise RuntimeError("Aktivität liefert eine Login-/Fehlerseite")
    if "/course/view.php" in urlparse(resp.url or view_url).path:
        raise RuntimeError("Aktivität wurde auf die Kursseite umgeleitet")
    return soup


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
    if _is_same_learnweb_host(final_url or view_url) and (
        "/login/" in urlparse(final_url or view_url).path
        or "/course/view.php" in urlparse(final_url or view_url).path
    ):
        raise RuntimeError("URL-Aktivität wurde auf eine Login-/Kursseite umgeleitet")
    if _response_is_html(resp) and _is_same_learnweb_host(final_url or view_url):
        if _is_login_or_error_page(BeautifulSoup(resp.text, "html.parser"), final_url or view_url):
            raise RuntimeError("URL-Aktivität liefert eine Login-/Fehlerseite")
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


def _extract_iframe_target(soup: BeautifulSoup) -> str | None:
    """
    Sucht in einem bereits geparsten HTML-Baum nach dem ersten externen
    iframe-Embed und gibt dessen src-URL zurück (z. B. Opencast/Electures).
    Interne LearnWeb-iframes und ungültige Schemata werden übersprungen.
    """
    for iframe in soup.select("iframe[src]"):
        src = (iframe.get("src") or "").strip()
        # Leere oder nicht navigierbare Quellen ignorieren
        if not src or src == "about:blank" or src.startswith("javascript:"):
            continue
        candidate = _normalize_absolute_url(src)
        parsed = urlparse(candidate)
        # Nur http/https zulassen
        if parsed.scheme not in {"http", "https"}:
            continue
        # Iframes, die auf denselben LearnWeb-Host zeigen, überspringen
        if _is_same_learnweb_host(candidate):
            continue
        return candidate
    return None


def _extract_page_text_from_soup(soup: BeautifulSoup) -> str | None:
    """
    Extrahiert den bereinigten Klartext einer Moodle-Page-Aktivität
    aus einem bereits geparsten HTML-Baum. Wird von _extract_page_content
    als dünner Wrapper aufgerufen, kann aber auch direkt mit einem
    vorhandenen soup-Objekt genutzt werden (kein zusätzlicher HTTP-Call).
    """
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


def _extract_page_content(session: requests.Session, view_url: str) -> str | None:
    """
    Dünner Wrapper: lädt die Seite und delegiert die Textextraktion an
    _extract_page_text_from_soup. Bestehende Aufrufer bleiben kompatibel.
    """
    soup = _fetch_activity_soup(session, view_url)
    return _extract_page_text_from_soup(soup)


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
    Liest alle Einträge aus KurseLearnWeb.
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
        "duplicate_active_course_ids": {
            row["course_id"] for row in rows
            if row["sync_content"] and row["course_id"] in duplicate_course_ids
        },
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
        upload_names = resource.get("upload_names") or file_names
        properties["LW Download"] = {
            "files": [
                {
                    "type": "file_upload", "file_upload": {"id": upload_id},
                    **({"name": upload_names[index]} if index < len(upload_names) else {}),
                }
                for index, upload_id in enumerate(normalized_upload_ids)
            ]
        }

    if course_notion_page_id:
        properties["KurseLearnWeb"] = {
            "relation": [{"id": course_notion_page_id}]
        }

    return properties


def _notion_find_lw_page(resource: dict) -> dict | None:
    """Findet genau die Quellidentität in der aktuellen DB; Fehler bleiben Fehler."""
    matches = []
    cursor = None
    while True:
        body = {"page_size": 100, "filter": {"and": [
            {"property": "Nr", "rich_text": {"equals": str(resource["cmid"])}},
            {"property": "Kurs-ID", "rich_text": {"equals": str(resource["course_id"])}},
        ]}}
        if cursor:
            body["start_cursor"] = cursor
        data = _notion_request(
            "POST", f"{NOTION_API}/databases/{NOTION_LW_DB_ID}/query",
            headers=_notion_headers(), json=body,
        ).json()
        if not isinstance(data.get("results"), list) or "has_more" not in data:
            raise RuntimeError("Unklarer Notion-Lookup; keine Neuanlage")
        matches.extend(data["results"])
        if not data["has_more"]:
            break
        next_cursor = data.get("next_cursor")
        if not next_cursor or next_cursor == cursor:
            raise RuntimeError("Unvollständiger Notion-Lookup; keine Neuanlage")
        cursor = next_cursor
    if len(matches) > 1:
        raise RuntimeError(f"Mehrere Inhaltsseiten für cmid={resource['cmid']}; keine Neuanlage")
    return matches[0] if matches else None


def _notion_get_lw_page(page_id: str, resource: dict) -> dict:
    page = _notion_request(
        "GET", f"{NOTION_API}/pages/{page_id}", headers=_notion_headers(),
    ).json()
    parent = page.get("parent", {})
    target = parent.get("database_id")
    if not target or target.replace("-", "") != NOTION_LW_DB_ID.replace("-", ""):
        raise RuntimeError("Manifest-Ziel liegt nicht in der aktuellen Inhaltsdatenbank")
    if page.get("archived") or page.get("in_trash"):
        raise RuntimeError("Manifest-Ziel ist archiviert; keine Neuanlage")
    props = page.get("properties", {})
    for prop, expected in (("Nr", resource["cmid"]), ("Kurs-ID", resource["course_id"])):
        value = "".join(
            text.get("plain_text", text.get("text", {}).get("content", ""))
            for text in props.get(prop, {}).get("rich_text", [])
        )
        if value != str(expected):
            raise RuntimeError(f"Manifest-Ziel hat andere {prop}; keine Änderung")
    return page


def _checkpoint_notion_page(conn: sqlite3.Connection, row: dict, page_id: str) -> None:
    """ID vor Folgeoperationen sichern; Remote-Create-Lücke deckt der Quelllookup ab."""
    conn.execute(
        "UPDATE resources SET notion_id = ? WHERE cmid = ?",
        (page_id, row["cmid"]),
    )
    conn.commit()
    row["notion_id"] = page_id


def _missing_lw_metadata(page: dict, resource: dict, course_page_id: str | None) -> dict:
    properties = page["properties"]
    updates = {}
    expected = COURSE_MAP.get(resource.get("course_shortname", ""))
    selected = (properties.get("Kurs", {}).get("select") or {}).get("name")
    if expected and not selected:
        updates["Kurs"] = {"select": {"name": expected}}
    elif expected and selected != expected:
        log.warning("Kurs-Select für cmid=%s beibehalten: %s (Mapping: %s)", resource["cmid"], selected, expected)
    relation = properties.get("KurseLearnWeb", {}).get("relation", [])
    if course_page_id and not relation:
        updates["KurseLearnWeb"] = {"relation": [{"id": course_page_id}]}
    return updates


def _repair_lw_metadata(page_id: str, resource: dict, course_page_id: str | None) -> dict:
    page = _notion_get_lw_page(page_id, resource)
    updates = _missing_lw_metadata(page, resource, course_page_id)
    if updates:
        _notion_request("PATCH", f"{NOTION_API}/pages/{page_id}",
                        headers=_notion_headers(), json={"properties": updates})
    return page


def _remote_attachment_hash(file: dict) -> str:
    """Nur bei fehlendem Checkpoint vorhandene Anlage byteweise zur Quelle abgleichen."""
    kind = file.get("type")
    url = file.get(kind, {}).get("url") if kind in {"file", "external"} else None
    if not url:
        raise RuntimeError("Vorhandene Anlage lässt sich nicht eindeutig zurücklesen")
    try:
        # Eigenes Session-Objekt: keine LearnWeb-Cookies und keine Notion-API-Header.
        with requests.Session() as session:
            with session.get(url, stream=True, timeout=60) as response:
                response.raise_for_status()
                digest = hashlib.md5()
                size = 0
                for chunk in response.iter_content(chunk_size=256 * 1024):
                    size += len(chunk)
                    if size > MAX_NOTION_FILE_BYTES:
                        raise RuntimeError("Vorhandene Anlage überschreitet die Dateigrenze")
                    digest.update(chunk)
                return digest.hexdigest()
    except requests.RequestException:
        # Presigned-URLs enthalten Credentials; sie gehören nicht in Manifest/Logs.
        raise RuntimeError("Vorhandene Anlage konnte nicht byteweise abgeglichen werden") from None


def _normalize_attachment_name(name: str | None) -> str:
    if not name:
        return ""
    normalized = unicodedata.normalize("NFKD", name)
    without_marks = "".join(c for c in normalized if unicodedata.category(c) != "Mn")
    return without_marks.replace(" ", "_").lower()


def _is_name_match(name1: str | None, name2: str | None) -> bool:
    if not name1 or not name2:
        return False
    if name1 == name2:
        return True
    return _normalize_attachment_name(name1) == _normalize_attachment_name(name2)


def _find_remote_file_matches(filename: str | None, remote_files: list[dict], target_hash: str | None = None) -> list[dict]:
    if not remote_files:
        return []
    if filename:
        exact = [f for f in remote_files if f.get("name") == filename]
        if exact:
            return exact
        norm_candidates = [f for f in remote_files if _is_name_match(f.get("name"), filename)]
        if norm_candidates:
            if target_hash:
                hash_matches = [f for f in norm_candidates if _remote_attachment_hash(f) == target_hash]
                if len(hash_matches) == 1:
                    return hash_matches
            return norm_candidates
    if target_hash:
        hash_matches = [f for f in remote_files if _remote_attachment_hash(f) == target_hash]
        if hash_matches:
            return hash_matches
    return []


def notion_create_lw_page(
    resource: dict,
    file_upload_ids: str | list[str] | None,
    course_notion_page_id: str | None,
    *,
    target_url: str | None = None,
) -> str:
    """
    Legt eine neue Seite in Learnweb Inhalte an.
    Gibt die Notion page_id zurück.
    """
    existing = _notion_find_lw_page(resource)
    if existing:
        raw_filenames = resource.get("upload_names") or resource.get("file_names") or [resource.get("file_name")]
        filenames = [f for f in raw_filenames if f is not None]
        remote_files = existing.get("properties", {}).get("LW Download", {}).get("files", [])
        ids = _normalize_file_upload_ids(file_upload_ids)
        if ids and len(filenames) != len(ids):
            raise RuntimeError("Upload-Dateinamen nicht eindeutig; bestehende Anlagen bleiben erhalten")
        matched_filenames = set()
        for filename in filenames:
            matches = _find_remote_file_matches(filename, remote_files, resource.get("file_hash"))
            if matches:
                if len(matches) != 1 or not resource.get("file_hash"):
                    raise RuntimeError("Bestehende Anlage nicht eindeutig zur Quelle zuordenbar")
                if _remote_attachment_hash(matches[0]) != resource["file_hash"]:
                    raise UploadBlockedError(f"Vorhandene Anlage {filename} weicht von der Quelle ab; bleibt erhalten")
                matched_filenames.add(filename)
        pending = [(upload_id, filename) for upload_id, filename in zip(ids, filenames) if filename not in matched_filenames]
        resource = {**resource, "upload_names": [filename for _, filename in pending]}
        file_upload_ids = [upload_id for upload_id, _ in pending]
        notion_update_lw_page(existing["id"], resource, file_upload_ids,
                              course_notion_page_id, target_url=target_url)
        return existing["id"]
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
    replace_file_hashes: dict[str, str] | None = None,
) -> None:
    """Ergänzt Anlagen/Ziel und leere Zuordnung, erhält manuelle Metadaten."""
    page = _notion_get_lw_page(page_id, resource)
    properties = _missing_lw_metadata(page, resource, course_notion_page_id)
    additions = _build_lw_page_properties(
        resource, None, file_upload_ids=file_upload_ids, target_url=target_url,
    )
    if "LW Download" in additions:
        existing_files = page["properties"].get("LW Download", {}).get("files", [])
        new_files = additions["LW Download"]["files"]
        replaced_files = []
        for new_file in new_files:
            filename = new_file["name"]
            matches = [file for file in existing_files if _is_name_match(file.get("name"), filename)]
            if matches:
                expected_hash = (replace_file_hashes or {}).get(filename)
                if (len(matches) != 1 or not expected_hash
                        or _remote_attachment_hash(matches[0]) != expected_hash):
                    raise UploadBlockedError(f"Vorhandene Anlage {filename} nicht als eigener Quellstand belegt; bleibt erhalten")
                replaced_files.extend(matches)
        preserved = []
        for file in existing_files:
            if file in replaced_files:
                continue
            kind = file.get("type")
            if kind not in {"file", "external", "file_upload"}:
                raise RuntimeError("Unbekannter vorhandener Anlagentyp; kein Ersetzen")
            field = "id" if kind == "file_upload" else "url"
            preserved.append({"name": file["name"], "type": kind, kind: {field: file[kind][field]}})
        properties["LW Download"] = {"files": preserved + new_files}
    if target_url:
        properties[LW_TARGET_URL_PROPERTY] = {"url": target_url}
    if not properties:
        return
    _notion_request(
        "PATCH",
        f"{NOTION_API}/pages/{page_id}",
        headers=_notion_headers(),
        json={"properties": properties},
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


def _append_lw_page_body(page_id: str, blocks: list[dict]) -> None:
    """Nach Create-/Append-Abbruch vorhandenes Textpräfix prüfen und nur Rest anhängen."""
    existing = []
    cursor = None
    while True:
        params = {"page_size": 100}
        if cursor:
            params["start_cursor"] = cursor
        data = _notion_request("GET", f"{NOTION_API}/blocks/{page_id}/children",
                               headers=_notion_headers(), params=params).json()
        if not isinstance(data.get("results"), list) or "has_more" not in data:
            raise RuntimeError("Unklarer Remote-Body; kein blindes Append")
        existing.extend(data["results"])
        if not data["has_more"]:
            break
        next_cursor = data.get("next_cursor")
        if not next_cursor or next_cursor == cursor:
            raise RuntimeError("Unvollständiger Remote-Body; kein blindes Append")
        cursor = next_cursor

    def paragraph_text(block):
        if block.get("type") != "paragraph":
            return None
        return "".join(text.get("plain_text", text.get("text", {}).get("content", ""))
                       for text in block.get("paragraph", {}).get("rich_text", []))

    matched = 0
    for old, desired in zip(existing, blocks):
        if paragraph_text(old) != paragraph_text(desired):
            raise RuntimeError("Remote-Body weicht vom Quellpräfix ab; manuelle Inhalte bleiben erhalten")
        matched += 1
    notion_append_page_children(page_id, blocks[matched:])


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

    course_map = CourseSyncResult()
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
            if course_id in notion_courses.get("duplicate_active_course_ids", set()):
                course_map.errors.append({"course_id": course_id, "stage": "courses", "reason": "duplicate_active_course_id"})
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
            course_map.errors.append({"course_id": course_id, "stage": "course_load", "reason": str(e)})
            continue

        if not shortname or shortname == "UNKNOWN":
            course_map.errors.append({"course_id": course_id, "stage": "course_load", "reason": "missing_shortname"})
            continue

        if shortname in notion_courses["duplicate_lw_ids"]:
            blocked = f"{shortname} [{course_id}]"
            log.warning(
                f"Konflikt in Notion – mehrere Kursseiten für LW-ID={shortname}; "
                f"überspringe {course['name']}"
            )
            conflict_courses.append(blocked)
            course_map.errors.append({"course_id": course_id, "stage": "courses", "reason": "duplicate_lw_id"})
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
                try:
                    notion_update_course(page_id, course_url=course["url"])
                except Exception as e:
                    course_map.errors.append({"course_id": course_id, "stage": "course_update", "reason": str(e)})
                    log.error("Kurs-URL konnte nicht aktualisiert werden: %s", e)
                    continue
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
                course_map.errors.append({"course_id": course_id, "stage": "course_create", "reason": str(e)})
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

    if course_map.errors:
        log.error("Kursabgleich unvollständig: %s Fehler", len(course_map.errors))
        if own_session:
            sys.exit(1)
    return course_map


def cmd_scan(session: requests.Session | None = None, course_map: dict | None = None):
    """
    Scrapt nur Kurse mit SyncContent=true und erfasst neue Aktivitäten im Manifest.
    Führt zuerst sync-courses aus (prüft/legt Kurse in Notion an).

    session und course_map können von cmd_run() übergeben werden, um
    doppelten Login zu vermeiden.
    """
    own_session = session is None
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
        "errors": list(getattr(course_map, "errors", [])),
    }
    if not active_courses:
        log.warning("Keine konfliktfreien Kurse mit SyncContent=true – nichts zu scannen.")
        if own_session and scan_result["errors"]:
            sys.exit(1)
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
                _validate_course_inventory(soup, activities)
            except Exception as e:
                log.error(f"Fehler beim Scrapen von {info.get('shortname') or course_id}: {e}")
                scan_result["errors"].append({"course_id": course_id, "stage": "scan", "reason": str(e)})
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
    if own_session and scan_result["errors"]:
        sys.exit(1)
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


def _read_download_response(resp: requests.Response) -> tuple[bytes | BinaryIO, str]:
    """Kleine Dateien als Bytes, große als temporäre Datei mit begrenztem RAM."""
    cd = resp.headers.get("Content-Disposition", "")
    match = re.search(r"filename\*?=(?:UTF-8'')?[\"']?([^\"';\r\n]+)", cd, re.IGNORECASE)
    if match:
        filename = unquote(match.group(1).strip().strip("\"'"))
    else:
        filename = _extract_filename_from_url(resp.url)

    content_length = int(resp.headers.get("Content-Length", 0))
    if content_length > MAX_NOTION_FILE_BYTES:
        raise UploadBlockedError(f"Datei überschreitet Notions 5-GiB-Grenze: {filename}")
    downloaded = tempfile.SpooledTemporaryFile(max_size=MAX_NOTION_SINGLE_PART_BYTES)
    try:
        size = 0
        for chunk in resp.iter_content(chunk_size=256 * 1024):
            size += len(chunk)
            if size > MAX_NOTION_FILE_BYTES:
                raise UploadBlockedError(f"Datei überschreitet Notions 5-GiB-Grenze: {filename}")
            downloaded.write(chunk)
        if content_length and size < content_length:
            raise requests.ConnectionError(f"Unvollständiger Download: {size}/{content_length} Bytes – {filename}")
        downloaded.seek(0)
        if size <= MAX_NOTION_SINGLE_PART_BYTES:
            data = downloaded.read()
            downloaded.close()
            return data, filename
        return downloaded, filename
    except BaseException:
        downloaded.close()
        raise


def _file_size(data: bytes | BinaryIO) -> int:
    if isinstance(data, bytes):
        return len(data)
    data.seek(0, os.SEEK_END)
    size = data.tell()
    data.seek(0)
    return size


def _file_hash(data: bytes | BinaryIO) -> str:
    if isinstance(data, bytes):
        return hashlib.md5(data).hexdigest()
    digest = hashlib.md5()
    data.seek(0)
    for chunk in iter(lambda: data.read(256 * 1024), b""):
        digest.update(chunk)
    data.seek(0)
    return digest.hexdigest()


def _close_download(data: bytes | BinaryIO | None) -> None:
    if data is not None and not isinstance(data, bytes):
        data.close()


def _validate_html_file(data: bytes | BinaryIO, final_url: str) -> None:
    if isinstance(data, bytes):
        sample = data[:256 * 1024]
    else:
        data.seek(0)
        sample = data.read(256 * 1024)
        data.seek(0)
    if _is_login_or_error_page(BeautifulSoup(sample, "html.parser"), final_url):
        raise RuntimeError("HTML-Anlage liefert eine Login-/Fehlerseite")


def _validate_downloaded_file(data: bytes | BinaryIO, final_url: str) -> None:
    """Falsch deklarierte Login-HTML ebenfalls ablehnen, ohne Binärdateien zu parsen."""
    path = urlparse(final_url).path
    if "/login/" in path or "/auth/" in path:
        raise RuntimeError("Dateidownload wurde zur Anmeldung umgeleitet")
    if isinstance(data, bytes):
        sample = data[:1024]
    else:
        data.seek(0)
        sample = data.read(1024)
        data.seek(0)
    if re.match(br"\s*(?:<!doctype\s+html|<html|<head|<body)", sample, re.IGNORECASE):
        _validate_html_file(data, final_url)


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
) -> tuple[bytes | BinaryIO, str] | None:
    """Lädt eine direkte Datei-URL herunter."""
    def _download_once() -> tuple[bytes | BinaryIO, str] | None:
        resp = session.get(file_url, allow_redirects=True, stream=True, timeout=60)
        try:
            resp.raise_for_status()
            if _response_is_html(resp) and not (
                _is_candidate_download_url(resp.url or file_url)
                or "attachment" in resp.headers.get("Content-Disposition", "").lower()
            ):
                return None
            result = _read_download_response(resp)
            try:
                _validate_downloaded_file(result[0], resp.url or file_url)
                if _response_is_html(resp):
                    _validate_html_file(result[0], resp.url or file_url)
            except BaseException:
                _close_download(result[0])
                raise
            return result
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

            if not _response_is_html(resp) or (
                _is_candidate_download_url(final_url)
                or "attachment" in resp.headers.get("Content-Disposition", "").lower()
            ):
                result = _read_download_response(resp)
                if result is None:
                    return _resource_error_result(
                        "retryable_error",
                        failure_reason="download_unavailable",
                        final_url=final_url,
                    )
                file_bytes, file_name = result
                try:
                    _validate_downloaded_file(file_bytes, final_url)
                    if _response_is_html(resp):
                        _validate_html_file(file_bytes, final_url)
                except BaseException:
                    _close_download(file_bytes)
                    raise
                return _resource_file_result(file_bytes, file_name, final_url=final_url)

            soup = BeautifulSoup(resp.text, "html.parser")
            title = _extract_html_title(soup)
            if _is_login_or_error_page(soup, final_url):
                return _resource_error_result(
                    "terminal_error" if _is_invalid_module_page(soup) else "retryable_error",
                    failure_reason="invalid_module" if _is_invalid_module_page(soup) else "login_or_error_page",
                    final_url=final_url, title=title,
                )
            if _is_invalid_module_page(soup):
                return _resource_error_result(
                    "terminal_error",
                    failure_reason="invalid_module",
                    final_url=final_url,
                    title=title,
                )

            file_url = _extract_first_download_link(soup)
            if file_url is None:
                container = soup.select_one(".resourcecontent, .resource-content, #resource-content, #region-main .box.generalbox")
                if container is not None:
                    text = _extract_page_text_from_soup(BeautifulSoup(str(container), "html.parser"))
                    if text:
                        return ResourceDownloadResult(kind="page", page_text=text, final_url=final_url)
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
    except UploadBlockedError as exc:
        return _resource_error_result("terminal_error", failure_reason="file_size_limit",
                                      final_url=normalized_view_url, exception=exc)


def notion_upload_file(file_bytes: bytes | BinaryIO, filename: str) -> str | None:
    """Originaldatei mit MIME/Name; über 20 MiB in begrenzten Multipart-Stücken."""
    size = _file_size(file_bytes)
    if not size:
        raise UploadBlockedError(f"Leere Datei ist nicht uploadbar: {filename}")
    if len(filename.encode("utf-8")) > 900:
        raise UploadBlockedError(f"Originaldateiname überschreitet Notions 900-Byte-Grenze: {filename}")
    ext = Path(filename).suffix.lower()
    if ext in {".pkl", ".pickle"}:
        raise UploadBlockedError(f"Notion unterstützt kein Pickle-Original: {filename}")
    content_type = {
        ".ipynb": "application/json", ".py": "text/x-python",
        ".mov": "video/quicktime", ".html": "text/html", ".htm": "text/html",
    }.get(ext) or mimetypes.guess_type(filename)[0] or "application/octet-stream"
    headers = {"Authorization": f"Bearer {NOTION_TOKEN}", "Notion-Version": NOTION_VERSION}
    # Workspace-Grenze vor der Upload-Anlage lesen; Lookupfehler sind kein Freibrief.
    bot = _notion_request("GET", f"{NOTION_API}/users/me", headers=_notion_headers()).json()
    limit = bot.get("bot", {}).get("workspace_limits", {}).get("max_file_upload_size_in_bytes")
    if not isinstance(limit, int) or limit <= 0:
        raise RuntimeError("Notion-Workspace-Uploadgrenze nicht eindeutig verfügbar")
    if size > min(limit, MAX_NOTION_FILE_BYTES):
        raise UploadBlockedError(f"Original überschreitet Workspace-Grenze ({limit} Bytes): {filename}")
    multipart = size > MAX_NOTION_SINGLE_PART_BYTES
    body = {"mode": "multi_part" if multipart else "single_part",
            "filename": filename, "content_type": content_type}
    parts = (size + NOTION_UPLOAD_PART_BYTES - 1) // NOTION_UPLOAD_PART_BYTES
    if multipart:
        body["number_of_parts"] = parts
    source = io.BytesIO(file_bytes) if isinstance(file_bytes, bytes) else file_bytes
    try:
        response = _notion_request("POST", f"{NOTION_API}/file_uploads", headers=headers, json=body).json()
        upload_id, upload_url = response["id"], response["upload_url"]
        source.seek(0)
        for number in range(1, (parts if multipart else 1) + 1):
            # requests kodiert nur einen begrenzten Teil, nie die gesamte große Datei.
            chunk = source.read(NOTION_UPLOAD_PART_BYTES if multipart else MAX_NOTION_SINGLE_PART_BYTES)
            kwargs = {"data": {"part_number": str(number)}} if multipart else {}
            sent = _notion_request("POST", upload_url, headers=headers,
                                   files={"file": (filename, chunk, content_type)}, timeout=120, **kwargs).json()
            if not multipart and sent.get("status") != "uploaded":
                raise RuntimeError("Dateiupload ist nicht vollständig bestätigt")
        if multipart:
            completed = _notion_request("POST", f"{NOTION_API}/file_uploads/{upload_id}/complete",
                                        headers=headers, timeout=120).json()
            if completed.get("status") != "uploaded":
                raise RuntimeError("Multipart-Upload ist nicht vollständig bestätigt")
        return upload_id
    except requests.HTTPError as exc:
        if exc.response is not None and exc.response.status_code == 400:
            try:
                error = exc.response.json()
            except ValueError:
                error = {}
            detail = _truncate_failure_detail(str(error.get("message", "validation_error")))
            raise UploadBlockedError(f"Notion lehnt Original {filename} ab [{error.get('code', 'validation_error')}]: {detail}") from exc
        raise
    finally:
        if isinstance(file_bytes, bytes):
            source.close()
        else:
            source.seek(0)


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

    resource = {
        "cmid": row["cmid"], "course_id": row["course_id"], "name": row["name"],
        "course_shortname": _resolve_course_shortname(course_map, row["course_id"], row["course_shortname"]),
    }
    if row["notion_id"]:
        try:
            page = _repair_lw_metadata(row["notion_id"], resource, course_notion_page_id)
            files = page["properties"].get("LW Download", {}).get("files", [])
            matches = _find_remote_file_matches(row.get("file_name"), files, row.get("file_hash"))
            if matches:
                if len(matches) != 1:
                    raise RuntimeError("Pflichtanlage nicht eindeutig")
                if row.get("status") != "synced" or matches[0].get("name") != row.get("file_name"):
                    if not row.get("file_hash") or _remote_attachment_hash(matches[0]) != row["file_hash"]:
                        raise RuntimeError("Pflichtanlage weicht vom gespeicherten Quellhash ab")
                _mark_activity_synced(conn, row["cmid"], row["notion_id"],
                                      file_hash=row["file_hash"], file_name=row["file_name"])
                return {"status": "skipped", "modtype": "resource"}
        except Exception as exc:
            _mark_activity_error(conn, row["cmid"], failure_reason="remote_reconcile_failed",
                                  failure_detail=_build_failure_detail(exception=exc))
            return {"status": "error", "modtype": "resource"}

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

    if result.kind == "page" and result.page_text:
        try:
            existed = bool(row["notion_id"])
            notion_id = row["notion_id"] or notion_create_lw_page(resource, None, course_notion_page_id)
            _checkpoint_notion_page(conn, row, notion_id)
            _append_lw_page_body(notion_id, _build_paragraph_blocks(result.page_text))
            _mark_activity_synced(conn, row["cmid"], notion_id,
                                  file_hash=hashlib.md5(result.page_text.encode("utf-8")).hexdigest())
            return {"status": "updated" if existed else "created", "modtype": "resource", "via": "inline_html"}
        except Exception as exc:
            _mark_activity_error(conn, row["cmid"], failure_reason="inline_content_failed",
                                  failure_detail=_build_failure_detail(exception=exc))
            return {"status": "error", "modtype": "resource"}

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
    resource["file_name"] = file_name
    existed = bool(row["notion_id"])

    try:
        file_hash = _file_hash(file_bytes)
        resource["file_hash"] = file_hash

        if row["notion_id"]:
            matches = _find_remote_file_matches(file_name, files, file_hash)
            if matches:
                if len(matches) != 1 or _remote_attachment_hash(matches[0]) != file_hash:
                    raise UploadBlockedError(f"Vorhandene Anlage {file_name} weicht von der Quelle ab; bleibt erhalten")
                _mark_activity_synced(conn, row["cmid"], row["notion_id"],
                                      file_hash=file_hash, file_name=file_name)
                return {"status": "skipped", "modtype": "resource"}

        upload_id = notion_upload_file(file_bytes, file_name)
        if not upload_id:
            raise RuntimeError("Upload ohne bestätigte Datei-ID")
        if row["notion_id"]:
            notion_id = row["notion_id"]
            notion_update_lw_page(notion_id, resource, upload_id, course_notion_page_id)
        else:
            notion_id = notion_create_lw_page(resource, upload_id, course_notion_page_id)
        _checkpoint_notion_page(conn, row, notion_id)
        _mark_activity_synced(conn, row["cmid"], notion_id,
                              file_hash=file_hash, file_name=file_name)
    except Exception as e:
        log.error(f"    Notion-Fehler: {e}")
        terminal = isinstance(e, UploadBlockedError)
        _mark_activity_error(conn, row["cmid"], file_name=file_name,
                              failure_reason="upload_blocked" if terminal else "upload_or_attach_failed",
                              failure_detail=_build_failure_detail(exception=e), retryable=not terminal)
        return {"status": "terminal_error" if terminal else "retryable_error", "modtype": "resource"}
    finally:
        _close_download(file_bytes)
    log.info("    ✓ Pflichtdatei angehängt")
    return {"status": "updated" if existed else "created", "modtype": "resource", "with_file": True}


def _push_folder_activity(
    conn: sqlite3.Connection,
    session: requests.Session,
    row: dict,
    course_map: dict,
    course_notion_page_id: str | None,
) -> dict:
    """Erhält jede erfolgreiche Anlage; Gesamtstatus erst bei vollständigem Ordner."""
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
    originally_existing = bool(row["notion_id"])
    display_name = (
        f"{row['section']} — {row['name']}" if row.get("section") else row["name"]
    )
    resource = {
        "cmid": row["cmid"],
        "course_id": row["course_id"],
        "name": row["name"],
        "display_name": display_name,
        "source_name": row["name"],
        "course_shortname": _resolve_course_shortname(
            course_map, row["course_id"], row["course_shortname"]
        ),
    }

    try:
        state_row = conn.execute("SELECT attachment_state FROM resources WHERE cmid = ?", (row["cmid"],)).fetchone()
        state = json.loads(state_row[0] or "{}")
        remote_names = []
        remote_files = []
        if row["notion_id"]:
            page = _repair_lw_metadata(row["notion_id"], resource, course_notion_page_id)
            remote_files = page["properties"].get("LW Download", {}).get("files", [])
            remote_names = [file.get("name") for file in remote_files]
        if (row["notion_id"] and row["file_hash"] == fingerprint
                and row.get("status") == "synced"
                and all(url in state and not state[url].get("blocked")
                        and (remote_names.count(state[url]["name"]) == 1 or sum(_is_name_match(r, state[url]["name"]) for r in remote_names) == 1) for _, url in folder_files)):
            return {"status": "skipped", "modtype": "folder"}
    except Exception as exc:
        _mark_activity_error(conn, row["cmid"], failure_reason="remote_reconcile_failed",
                              failure_detail=_build_failure_detail(exception=exc))
        return {"status": "error", "modtype": "folder"}

    current_inventory_urls = {url for _, url in folder_files}
    errors = []
    file_names = []
    changed = False
    for display_name, download_url in folder_files:
        previous = state.get(download_url)
        if previous and previous.get("blocked"):
            errors.append({"file": display_name, "reason": previous["blocked"], "terminal": True})
            continue
        data = None
        try:
            result = _download_file_url(session, download_url)
            if result is None:
                raise RuntimeError("Datei-URL liefert keine gültige Originaldatei")
            data, filename = result
            file_names.append(filename)
            digest = _file_hash(data)
            old_url_to_clean = None
            if not previous:
                old_owners = [
                    url for url, item in state.items()
                    if item.get("name") == filename and url not in current_inventory_urls and not item.get("blocked")
                ]
                if len(old_owners) == 1:
                    previous = state[old_owners[0]]
                    old_url_to_clean = old_owners[0]

            other_owners = [url for url, item in state.items() if item.get("name") == filename and url != download_url and url in current_inventory_urls]
            if other_owners or remote_names.count(filename) > 1:
                raise UploadBlockedError(f"Anlagenname nicht eindeutig: {filename}")

            remote_matches = [file for file in remote_files if file.get("name") == filename]
            if not remote_matches:
                remote_matches = [file for file in remote_files if _is_name_match(file.get("name"), filename)]

            if remote_matches and (not previous or previous["hash"] == digest):
                if len(remote_matches) != 1:
                    raise UploadBlockedError(f"Anlagenname nicht eindeutig: {filename}")
                remote_file = remote_matches[0]
                # Create-/PATCH-Erfolg vor lokalem Commit: die eindeutige Anlage wiederfinden.
                if not previous:
                    if _remote_attachment_hash(remote_file) != digest:
                        raise UploadBlockedError(f"Vorhandene Anlage {filename} weicht von der Quelle ab; bleibt erhalten")
                if old_url_to_clean:
                    state.pop(old_url_to_clean, None)
                state[download_url] = {"name": filename, "hash": digest}
            else:
                if remote_matches:
                    if len(remote_matches) != 1:
                        raise UploadBlockedError(f"Anlagenname nicht eindeutig: {filename}")
                    remote_file = remote_matches[0]
                    if not previous or _remote_attachment_hash(remote_file) != previous["hash"]:
                        raise UploadBlockedError(f"Vorhandene Anlage {filename} hat einen anderen Quellstand; bleibt erhalten")
                upload_id = notion_upload_file(data, filename)
                if not upload_id:
                    raise RuntimeError("Upload ohne bestätigte Datei-ID")
                file_resource = {**resource, "file_names": [name for name, _ in folder_files],
                                 "upload_names": [filename], "file_hash": digest}
                if row["notion_id"]:
                    notion_update_lw_page(row["notion_id"], file_resource, [upload_id], course_notion_page_id,
                                          replace_file_hashes={filename: previous["hash"]} if remote_matches else None)
                else:
                    notion_id = notion_create_lw_page(file_resource, [upload_id], course_notion_page_id)
                    _checkpoint_notion_page(conn, row, notion_id)
                page = _notion_get_lw_page(row["notion_id"], resource)
                remote_files = page["properties"].get("LW Download", {}).get("files", [])
                remote_names = [file.get("name") for file in remote_files]
                if old_url_to_clean:
                    state.pop(old_url_to_clean, None)
                state[download_url] = {"name": filename, "hash": digest}
                changed = True
            conn.execute("UPDATE resources SET attachment_state = ? WHERE cmid = ?",
                         (json.dumps(state, ensure_ascii=False), row["cmid"]))
            conn.commit()
        except Exception as exc:
            terminal = isinstance(exc, UploadBlockedError)
            if terminal and data is not None:
                state[download_url] = {"name": filename, "hash": digest, "blocked": str(exc)}
                conn.execute("UPDATE resources SET attachment_state = ? WHERE cmid = ?",
                             (json.dumps(state, ensure_ascii=False), row["cmid"]))
                conn.commit()
            errors.append({"file": display_name, "reason": str(exc), "terminal": terminal})
            log.error("Folder-Datei %s bleibt offen: %s", display_name, exc)
        finally:
            _close_download(data)

    if errors:
        terminal = all(error["terminal"] for error in errors)
        _mark_activity_error(conn, row["cmid"], failure_reason="folder_files_blocked" if terminal else "folder_incomplete",
                              failure_detail=_truncate_failure_detail(json.dumps(errors, ensure_ascii=False)),
                              retryable=not terminal)
        return {"status": "terminal_error" if terminal else "retryable_error", "modtype": "folder",
                "errors": errors, "attachment_count": sum(not item.get("blocked") for item in state.values())}

    notion_id = row["notion_id"]
    if not notion_id:
        _mark_activity_error(conn, row["cmid"], failure_reason="folder_missing_target")
        return {"status": "error", "modtype": "folder"}

    _mark_activity_synced(
        conn,
        row["cmid"],
        notion_id,
        file_hash=fingerprint,
        file_name=json.dumps(file_names, ensure_ascii=True),
    )
    status = ("updated" if originally_existing else "created") if changed else "skipped"
    return {"status": status, "modtype": "folder", "attachment_count": len(folder_files)}


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

    existed = bool(row["notion_id"])

    if row["notion_id"] and row.get("status") == "synced":
        _repair_lw_metadata(row["notion_id"], {
            **row, "course_shortname": _resolve_course_shortname(course_map, row["course_id"], row["course_shortname"]),
        }, course_notion_page_id)
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
        if row["notion_id"]:
            notion_id = row["notion_id"]
            notion_update_lw_page(notion_id, resource, None, course_notion_page_id, target_url=target_url)
        else:
            notion_id = notion_create_lw_page(resource, None, course_notion_page_id, target_url=target_url)
    except Exception as e:
        log.error(f"    Notion-Fehler: {e}")
        _mark_activity_error(conn, row["cmid"])
        return {"status": "error", "modtype": "url"}

    _checkpoint_notion_page(conn, row, notion_id)
    _mark_activity_synced(
        conn,
        row["cmid"],
        notion_id,
        file_hash=hashlib.md5(target_url.encode("utf-8")).hexdigest(),
    )
    log.info("    ✓ URL-Seite angelegt (Ziel-URL gesetzt)")
    return {"status": "updated" if existed else "created", "modtype": "url"}


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

    existed = bool(row["notion_id"])

    if row["notion_id"]:
        _repair_lw_metadata(row["notion_id"], {
            **row, "course_shortname": _resolve_course_shortname(course_map, row["course_id"], row["course_shortname"]),
        }, course_notion_page_id)
        if row.get("status") == "synced":
            return {"status": "skipped", "modtype": "page"}

    # Seite einmal laden — soup wird für iframe- und Text-Pfad geteilt
    try:
        soup = _fetch_activity_soup(session, row["view_url"])
    except Exception as e:
        log.error(f"    Page-Extraktionsfehler: {e}")
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

    # ── iframe-Pfad: externe Embeds (z. B. Opencast / Electures) ──────────────
    iframe_target = _extract_iframe_target(soup)
    if iframe_target:
        try:
            if row["notion_id"]:
                notion_id = row["notion_id"]
                notion_update_lw_page(notion_id, resource, None, course_notion_page_id, target_url=iframe_target)
            else:
                notion_id = notion_create_lw_page(resource, None, course_notion_page_id, target_url=iframe_target)
            _checkpoint_notion_page(conn, row, notion_id)
        except Exception as e:
            log.error(f"    Notion-Fehler: {e}")
            _mark_activity_error(conn, row["cmid"])
            return {"status": "error", "modtype": "page"}

        _mark_activity_synced(
            conn,
            row["cmid"],
            notion_id,
            file_hash=hashlib.md5(iframe_target.encode("utf-8")).hexdigest(),
        )
        log.info("    ✓ Page mit iframe-Embed angelegt (Ziel-URL gesetzt)")
        return {"status": "updated" if existed else "created", "modtype": "page", "via": "iframe"}

    # ── Text-Pfad: normaler Seiteninhalt als Paragraph-Blöcke ─────────────────
    page_text = _extract_page_text_from_soup(soup)

    if not page_text:
        log.error("    Page enthält keinen nutzbaren Text")
        _mark_activity_error(conn, row["cmid"])
        return {"status": "error", "modtype": "page"}

    blocks = _build_paragraph_blocks(page_text)
    if not blocks:
        log.error("    Page erzeugt keine Notion-Blöcke")
        _mark_activity_error(conn, row["cmid"])
        return {"status": "error", "modtype": "page"}

    try:
        notion_id = row["notion_id"] or notion_create_lw_page(resource, None, course_notion_page_id)
        _checkpoint_notion_page(conn, row, notion_id)
        _append_lw_page_body(notion_id, blocks)
    except Exception as e:
        log.error(f"    Notion-Fehler: {e}")
        _mark_activity_error(conn, row["cmid"])
        return {"status": "error", "modtype": "page"}

    _mark_activity_synced(
        conn,
        row["cmid"],
        notion_id,
        file_hash=hashlib.md5(page_text.encode("utf-8")).hexdigest(),
    )
    log.info(f"    ✓ Page mit {len(blocks)} Paragraph-Blöcken angelegt")
    return {"status": "updated" if existed else "created", "modtype": "page"}


def cmd_push(session: requests.Session | None = None, course_map: dict | None = None):
    """
    Pusht die aktuell unterstützten LearnWeb-Inhalte nach Notion.
    Fehlende Anlagen und leere Kurszuordnungen bestehender eigener Seiten nachziehen.
    Folder behalten je Datei einen Checkpoint; Seitenanlage/Append sind wiederholbar.

    session und course_map können von cmd_run() übergeben werden, um
    doppelten Login und doppeltes Laden der Kursseiten zu vermeiden.
    """
    if not NOTION_TOKEN:
        log.error("NOTION_TOKEN nicht gesetzt")
        sys.exit(1)
    if not NOTION_LW_DB_ID:
        log.error("NOTION_LW_DB_ID nicht gesetzt")
        sys.exit(1)

    own_session = session is None
    # Eigener Login nur wenn kein Session von außen übergeben wurde
    if session is None:
        session = requests.Session()
        session.headers["User-Agent"] = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"
        if not login(session):
            sys.exit(1)

    # Kursliste + KurseLearnWeb-Abgleich (oder gecachte Map verwenden)
    if course_map is None:
        course_map = cmd_sync_courses(session)

    push_result = {"errors": list(getattr(course_map, "errors", [])), "counters": {}}

    # Nur aktive, konfliktfreie Kurse (SyncContent=true) → {course_id: notion_page_id}
    active = {
        cid: info["notion_page_id"]
        for cid, info in course_map.items()
        if info["sync_content"] and not info.get("conflict", False)
    }
    if not active:
        log.warning("Keine konfliktfreien Kurse mit SyncContent=true – nichts zu pushen.")
        if own_session and push_result["errors"]:
            sys.exit(1)
        return push_result

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
            ORDER BY course_id, first_seen
            """,
            [*sorted(PUSHABLE_MODTYPES), *list(active.keys())],
        ).fetchall()

        if not rows:
            print("\n✓ Keine pushbaren Inhalte im Manifest gefunden.\n")
            if own_session and push_result["errors"]:
                sys.exit(1)
            return push_result

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

            if row["status"] == "error" and not row["retryable"] and row["modtype"] != "folder":
                counters["terminal_error"] += 1
                counters[f"{row['modtype']}_terminal_error"] += 1
                push_result["errors"].append({"cmid": row["cmid"], "reason": row["failure_reason"], "status": "terminal_error"})
                continue

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
                push_result["errors"].append({"cmid": row["cmid"], "reason": failure_reason, "status": "error"})
                continue

            handler = handlers[row["modtype"]]
            try:
                result = handler(conn, session, row, course_map, active.get(row["course_id"]))
            except Exception as exc:
                log.error("Push für cmid=%s fehlgeschlagen: %s", row["cmid"], exc)
                _mark_activity_error(conn, row["cmid"], failure_reason="push_exception",
                                      failure_detail=_build_failure_detail(exception=exc))
                result = {"status": "error", "modtype": row["modtype"]}
            counters[result["status"]] += 1
            counters[f"{row['modtype']}_{result['status']}"] += 1
            if result["status"] in {"error", "retryable_error", "terminal_error"}:
                push_result["errors"].append({"cmid": row["cmid"], **result})
    finally:
        conn.close()

    print("\n" + "=" * 60)
    total_errors = (
        counters["error"] + counters["retryable_error"] + counters["terminal_error"]
    )
    print(
        ("! Push: " if total_errors else "✓ Push: ")
        +
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
    push_result["counters"] = dict(counters)
    if own_session and push_result["errors"]:
        sys.exit(2 if all(error.get("status") == "terminal_error" for error in push_result["errors"]) else 1)
    return push_result


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
    push_result = cmd_push(session=session, course_map=course_map)
    tracked_only_courses = scan_result["tracked_only_courses"]
    if tracked_only_courses:
        summaries = ", ".join(
            f"{course['shortname']} [{course['course_id']}]"
            for course in tracked_only_courses
        )
        log.warning(
            "Aktive Kurse enthalten ausschließlich bewusst nicht unterstützte Inhaltstypen: "
            f"{summaries}"
        )
    push_errors = (push_result or {}).get("errors", [])
    if (getattr(course_map, "errors", []) or scan_result.get("errors")
            or any(error.get("status") != "terminal_error" for error in push_errors)):
        log.error("Sync unvollständig: Fehler beim Kursabgleich, Scan oder Push")
        sys.exit(1)
    if push_errors:
        log.warning("Sync unvollständig: %s Inhaltsblocker bleiben offen", len(push_errors))
        sys.exit(2)
    return {"scan": scan_result, "push": push_result, "status": "complete"}


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
        reason = "ok" if result.kind in {"file", "page"} else (result.failure_reason or "unknown")
        _close_download(result.file_bytes)
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
    try:
        DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryFile(dir=DOWNLOAD_DIR):
            pass
    except OSError as exc:
        log.error("DOWNLOAD_DIR ist nicht beschreibbar: %s (%s)", DOWNLOAD_DIR, exc)
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


def _acquire_transcribe_lock(lock_path: Path):
    """Exklusives, prozessübergreifendes Lock gegen parallele transcribe-Läufe.

    Nutzt fcntl.flock (non-blocking). Gibt das offene File-Objekt zurück
    (offen halten = Lock halten) oder None, falls bereits ein Lauf aktiv ist.
    Verhindert, dass launchd-Lauf und manueller Lauf gleichzeitig arbeiten.
    """
    import fcntl

    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fh = open(lock_path, "w")
    try:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        fh.close()
        return None
    return fh


def _notion_find_page_by_quelle(database_id: str, key: str) -> str | None:
    """create-or-find: sucht eine Seite mit Property `Quelle` == key.

    Deckt den Fall „verlorene Antwort" ab (Seite remote angelegt, Response verloren).
    Lookup-Fehler sind kein Negativtreffer; bei Mehrdeutigkeit keine Neuanlage.
    """
    if not database_id:
        raise RuntimeError("missing_recording_database")
    resp = _notion_request(
        "POST",
        f"{NOTION_API}/databases/{database_id}/query",
        headers=_notion_headers(),
        json={
            "filter": {
                "property": RECORDING_QUELLE_PROPERTY,
                "rich_text": {"equals": key},
            },
            "page_size": 2,
        },
    )
    data = resp.json()
    results = data["results"]
    if not isinstance(results, list):
        raise RuntimeError("invalid_recording_source_lookup")
    if len(results) > 1 or data.get("has_more"):
        raise RuntimeError(f"ambiguous_recording_source: {key}")
    return results[0]["id"] if results else None


def _build_recording_content_properties(recording, course_page_id: str | None, key: str) -> dict:
    """Properties für den schlanken Aufzeichnungs-Eintrag in `Learnweb Inhalte`.

    Bewusst eigene Builder-Funktion (statt _build_lw_page_properties), damit der
    Railway-push-Pfad unberührt bleibt und Kategorie/Format gezielt für Opencast
    gesetzt werden können.
    """
    props: dict = {
        "Name": {"title": [{"text": {"content": recording.title}}]},
        "Nr": {"rich_text": [{"text": {"content": recording.cmid}}]},
        "Kurs-ID": {"rich_text": [{"text": {"content": recording.course_id}}]},
        "Kategorie": {"select": {"name": "Recording"}},
        "Format": {"select": {"name": "mp4"}},
        "Quell-Semester": {"select": {"name": _semester_label_for_datetime()}},
        RECORDING_QUELLE_PROPERTY: {"rich_text": [{"text": {"content": key}}]},
    }
    if recording.source_url:
        props[LW_TARGET_URL_PROPERTY] = {"url": recording.source_url}
    if recording.course_shortname and COURSE_MAP:
        kurs_val = COURSE_MAP.get(recording.course_shortname)
        if kurs_val:
            props["Kurs"] = {"select": {"name": kurs_val}}
    if course_page_id:
        props["KurseLearnWeb"] = {"relation": [{"id": course_page_id}]}
    return props


def _build_meeting_properties(
    recording, content_page_id: str | None, course_page_id: str | None, key: str
) -> dict:
    """Properties für die Transkript-Seite in `Meeting-Notizen`."""
    props: dict = {
        "Titel des Meetings": {"title": [{"text": {"content": recording.title}}]},
        "Kategorie": {"select": {"name": "Uni/Vorlesung"}},
        "Status": {"status": {"name": "Erledigt"}},
        RECORDING_QUELLE_PROPERTY: {"rich_text": [{"text": {"content": key}}]},
    }
    if recording.recorded_at:
        # recorded_at ist ISO-8601 (oder None → Feld weglassen, niemals raten).
        props["Datum"] = {"date": {"start": recording.recorded_at}}
    if content_page_id:
        props["Learnweb Inhalte"] = {"relation": [{"id": content_page_id}]}
    if course_page_id:
        props["Kurs"] = {"relation": [{"id": course_page_id}]}
    return props


def _notion_create_page(database_id: str, properties: dict) -> str:
    """Legt eine Seite in der angegebenen DB an und gibt die page_id zurück."""
    resp = _notion_request(
        "POST",
        f"{NOTION_API}/pages",
        headers=_notion_headers(),
        json={"parent": {"database_id": database_id}, "properties": properties},
    )
    return resp.json()["id"]


def _notion_update_page_properties(page_id: str, properties: dict) -> None:
    """Aktualisiert Properties einer bestehenden Seite (für create-or-find-Reuse)."""
    _notion_request(
        "PATCH",
        f"{NOTION_API}/pages/{page_id}",
        headers=_notion_headers(),
        json={"properties": properties},
    )


def _notion_recording_children(page_id: str) -> list[dict]:
    """Liest den vollständigen Remote-Body vor einer Wiederaufnahme."""
    children = []
    cursor = None
    while True:
        endpoint = f"{NOTION_API}/blocks/{page_id}/children?page_size=100"
        if cursor:
            endpoint += f"&start_cursor={cursor}"
        resp = _notion_request(
            "GET", endpoint, headers=_notion_headers(),
        )
        data = resp.json()
        children.extend(data["results"])
        if not data.get("has_more"):
            return children
        next_cursor = data.get("next_cursor")
        if not next_cursor or next_cursor == cursor:
            raise RuntimeError("invalid_recording_children_cursor")
        cursor = next_cursor


def _recording_block_signature(block: dict) -> str:
    """Vergleicht Sendepayload und Notion-Response ohne Server-Metadaten."""
    kind = block.get("type")
    payload = block.get(kind, {})
    rich = []
    for item in payload.get("rich_text", []):
        annotations = item.get("annotations", {})
        rich.append({
            "type": item.get("type", "text"),
            "text": {"content": item.get("text", {}).get("content", ""),
                     "link": item.get("text", {}).get("link")},
            "annotations": {name: annotations.get(name, False) for name in
                            ("bold", "italic", "strikethrough", "underline", "code")},
            "color": annotations.get("color", "default"),
        })
    return json.dumps([kind, payload.get("color", "default"), rich], sort_keys=True)


def _match_recording_blocks(children, blocks, known_ids, *, replace=False):
    """Eigene IDs oder eindeutig identische Checkpoints; Fremdtext bleibt erhalten."""
    matched = []
    used = set()
    last_position = -1
    for index, expected in enumerate(blocks):
        known_id = known_ids[index] if index < len(known_ids) else None
        candidates = [pos for pos, child in enumerate(children)
                      if pos not in used and known_id and child["id"] == known_id]
        if known_id and not candidates:
            if replace:
                continue
            raise RuntimeError("recording_body_checkpoint_missing")
        if not candidates:
            signature = _recording_block_signature(expected)
            candidates = [pos for pos, child in enumerate(children)
                          if pos not in used and
                          _recording_block_signature(child) == signature]
            if len(candidates) > 1:
                raise RuntimeError("ambiguous_recording_body")
        if not candidates:
            if replace:
                continue  # Force darf nach einem Abbruch teilweise gelöscht sein.
            later = {_recording_block_signature(block) for block in blocks[index + 1:]}
            if any(_recording_block_signature(child) in later
                   for pos, child in enumerate(children) if pos not in used):
                raise RuntimeError("non_prefix_recording_body")
            break
        position = candidates[0]
        if not replace and position <= last_position:
            raise RuntimeError("reordered_recording_body")
        used.add(position)
        last_position = position
        matched.append((children[position], expected))
    return matched


def _reconcile_recording_body(conn, key, meeting_id, prior, blocks, body_hash, *, force, manifest):
    """Remote-Wirkung prüfen, bevor ein lokaler Zähler zum Append berechtigt."""
    children = _notion_recording_children(meeting_id)
    old_blocks = json.loads(prior.get("generated_blocks_json") or "null")
    known_ids = json.loads(prior.get("generated_block_ids_json") or "[]")
    replacing = force or prior.get("replacement_body_json") or (
        prior.get("body_sha256") and prior["body_sha256"] != body_hash
    )
    if old_blocks is None:
        # shortcut: Altbestand ohne Checkpoint ist nur bei exaktem Text belegbar;
        # geänderte Alttranskripte erfordern einen gesonderten Eigentumsnachweis.
        if children and prior.get("body_sha256") and prior["body_sha256"] != body_hash:
            raise RuntimeError("legacy_recording_body_unverified")
        old_blocks = blocks
    matched = _match_recording_blocks(children, old_blocks, known_ids, replace=bool(replacing))
    if (prior.get("appended_block_count") or 0) > len(matched) and not replacing:
        raise RuntimeError("recording_body_checkpoint_missing")
    if children and not prior.get("generated_blocks_json") and not matched:
        # Ein fremder Body darf nicht als leerer eigener Body interpretiert werden.
        if prior.get("body_sha256") or prior.get("appended_block_count"):
            raise RuntimeError("legacy_recording_body_unverified")
    if children and not matched and not known_ids and not prior.get("replacement_body_json"):
        # Auch ein bloßer Append-Intent beweist kein Eigentum an abweichendem Alttext.
        for child in children:
            rich = child.get("paragraph", {}).get("rich_text", [])
            if rich and re.match(r"\[(?:\d+:)?\d{2}:\d{2}\]", rich[0].get("text", {}).get("content", "")):
                raise RuntimeError("legacy_recording_body_unverified")
    if replacing:
        # Die Ersatzabsicht muss einen Abbruch während der einzelnen DELETEs überleben.
        manifest.set_status(
            conn, key, "appending",
            generated_blocks_json=json.dumps(old_blocks, ensure_ascii=False),
            replacement_body_json=json.dumps({"blocks": blocks, "body_hash": body_hash}, ensure_ascii=False),
        )
        for child, expected in matched:
            # Manuell bearbeitete eigene Absätze und angehängte Unterblöcke erhalten.
            if child.get("has_children") or _recording_block_signature(child) != _recording_block_signature(expected):
                continue
            _notion_request("DELETE", f"{NOTION_API}/blocks/{child['id']}", headers=_notion_headers())
        matched = []
    ids = [child["id"] for child, _ in matched]
    manifest.set_status(
        conn, key, "appending", body_sha256=body_hash,
        generated_blocks_json=json.dumps(blocks, ensure_ascii=False),
        generated_block_ids_json=json.dumps(ids),
        replacement_body_json=None,
        total_block_count=len(blocks), appended_block_count=len(ids),
    )
    return len(ids)


def _notify_transcribe_failures(failures: list[dict]) -> None:
    """Meldet fehlgeschlagene Transkriptionen über genau einen Kanal.

    Bis zur ausdrücklichen Freigabe (Plan §7) wird NICHT automatisch versendet —
    es bleibt bei Logfile + sichtbarer Konsolen-Ausgabe + Exit-Code. Der spätere
    himalaya-Mail-Versand wird hier ergänzt (Task #6) und ist freigabepflichtig.
    """
    if not failures:
        return
    log.error("Transkription: %d Aufzeichnung(en) fehlgeschlagen", len(failures))
    print("\n! Fehlgeschlagene Transkriptionen:")
    for item in failures:
        print(
            f"  ! {item.get('key')} [{item.get('stage')}]: {item.get('reason')}"
            f"  ({item.get('title')})"
        )


def _parse_section_course_id(soup: BeautifulSoup, html: str) -> str | None:
    """Leitet die Moodle-Kurs-ID aus einer geladenen Abschnitts-/Kursseite ab.

    Mehrere Fallback-Quellen (robust gegen Theme-Änderungen):
      1. <body class="… course-NNNN"> (zuverlässigste Quelle),
      2. courseId im Inline-JS (z. B. M.cfg).
    BEWUSST NICHT der erste `course/view.php?id=`-Link – das Navigationsmenü
    enthält viele fremde Kurse. Gibt None zurück, wenn nichts Eindeutiges da ist.
    """
    body = soup.find("body")
    if body:
        for cls in (body.get("class") or []):
            match = re.fullmatch(r"course-(\d+)", cls)
            if match:
                return match.group(1)
    match = re.search(r'courseId["\']?\s*[:=]\s*["\']?(\d+)', html)
    if match:
        return match.group(1)
    return None


def _extract_section_course_name(soup: BeautifulSoup) -> str | None:
    """Liest einen Kursnamen aus dem <title> einer Abschnittsseite (rein informativ).

    Moodle-Titel haben die Form "Section: … | <Kursname> | Learnweb". Wird in
    Notion NICHT als Property geschrieben (nur Logging/Recording-Metadaten).
    """
    if soup.title and soup.title.string:
        parts = [p.strip() for p in soup.title.string.split("|")]
        if len(parts) >= 3 and parts[1]:
            return parts[1]
    return None


def _youtube_subtitle_langs() -> str:
    """Bestimmt den yt-dlp `--sub-langs`-Wert (Plan, Designentscheidung 8).

    Expliziter Env-Wert `YT_SUBTITLE_LANGS`, sonst deduplizierte Folge
    [WHISPER_LANGUAGE, "en"] → normaler Default "de,en".
    """
    explicit = os.getenv("YT_SUBTITLE_LANGS", "").strip()
    if explicit:
        return explicit
    seen: list[str] = []
    for lang in (WHISPER_LANGUAGE, "en"):
        lang = (lang or "").strip()
        if lang and lang not in seen:
            seen.append(lang)
    return ",".join(seen) if seen else "en"


def _youtube_title_needs_probe(title: str | None) -> bool:
    """Erkennt fehlende oder generische HTML-Titel ohne Informationswert."""
    if not title:
        return True
    normalized = " ".join(title.lower().split())
    return normalized in {
        "video",
        "youtube",
        "youtube video",
        "youtube video player",
        "yt link",
    }


def discover_youtube_recordings(
    session: requests.Session,
    soup: BeautifulSoup,
    html: str,
    *,
    course_id: str,
    course_shortname: str | None = None,
    course_name: str | None = None,
    failures: list[dict] | None = None,
) -> list:
    """Findet YouTube-Videos auf einer LearnWeb-Abschnitts-/Seiten-HTML.

    Zwei Quellen, vereint und per video_id dedupliziert:
      1. Direkte YouTube-Links (<a>/<iframe>) im Seiten-HTML.
      2. `mod/url`-Aktivitäten, deren Ziel (via `_extract_url_target`) ein
         YouTube-Link ist. Der Aktivitätsname dient als (besserer) Titel.
    Gesperrte/linklose Aktivitäten (ohne view_url) werden übersprungen.
    """
    from transcription import downloader, youtube
    from transcription.types import Recording

    # video_id -> {"title": str|None, "url": kanonische watch-URL}
    found: dict[str, dict] = {}

    # Quelle 1: direkte Links im Seiten-HTML.
    for link in youtube.extract_youtube_links(html):
        found.setdefault(
            link["video_id"], {"title": link["title"], "url": link["url"]}
        )

    # Quelle 2: mod/url-Aktivitäten auflösen.
    activities = _extract_activities(
        soup, {"course_id": course_id, "name": course_name or course_id}
    )
    seen_cmids = set()
    for act in activities:
        if act.get("modtype") != "url":
            continue
        cmid = act.get("cmid")
        if cmid and cmid in seen_cmids:
            continue
        if cmid:
            seen_cmids.add(cmid)
        view_url = act.get("view_url")
        if not view_url:
            log.info("Überspringe gesperrte/linklose Aktivität: %s", act.get("name"))
            continue
        try:
            target = _extract_url_target(session, _normalize_absolute_url(view_url))
        except Exception as exc:
            log.warning("mod/url nicht auflösbar (%s): %s", act.get("name"), exc)
            if failures is not None:
                failures.append({"key": str(cmid), "stage": "discovery",
                                 "reason": str(exc), "title": act.get("name")})
            continue
        if not target:
            continue
        video_id = youtube.parse_youtube_id(target)
        if not video_id:
            continue  # Ziel ist kein YouTube-Link.
        entry = found.setdefault(
            video_id,
            {"title": None, "url": f"https://www.youtube.com/watch?v={video_id}"},
        )
        if act.get("name"):  # mod/url-Aktivitätsname ist der bessere Titel.
            entry["title"] = act["name"]

    recordings_out = []
    for video_id, info in found.items():
        watch_url = info["url"]
        title = info["title"]
        if _youtube_title_needs_probe(title):
            title = downloader.fetch_youtube_title(watch_url)
        title = title or f"Aufzeichnung {video_id}"
        recordings_out.append(
            Recording(
                cmid="yt",                 # konstanter Namespace → globaler Dedupe pro video_id
                title=title,
                source_url=watch_url,
                course_id=course_id,
                episode_id=video_id,       # stabiler Diskriminator (recording_key)
                media_url=watch_url,
                recorded_at=None,          # YouTube-Upload-Datum ≠ Aufzeichnungsdatum → nie raten
                course_shortname=course_shortname,
                course_name=course_name,
                source_kind="youtube",
            )
        )
    return recordings_out


def _open_transcribe_connection(*, dry_run: bool) -> sqlite3.Connection:
    """Öffnet den Transkriptions-State; Dry-Runs arbeiten ausschließlich im RAM."""
    conn = sqlite3.connect(":memory:") if dry_run else init_db()
    init_transcribe_schema(conn)
    from transcription import manifest

    if not dry_run:
        manifest.init_recovery_schema(conn)
        # Beide CLI-Pfade haben zu diesem Zeitpunkt bereits den exklusiven flock.
        recovered = manifest.recover_interrupted(conn)
        if recovered:
            log.info("Wiederaufnahme: %d unterbrochene Transkriptionen freigegeben.", recovered)
    return conn


def _lookup_transcribe_courses(session) -> dict[str, dict]:
    """Aktuell zugängliche Kurse mit eindeutigen Notion-Kurs-IDs rein lesend verbinden."""
    by_course = notion_query_courses_db()["by_course_id"]
    course_map = {}
    for course in get_courses(session):
        row = by_course.get(course["course_id"])
        if row:
            course_map[course["course_id"]] = {
                "name": course["name"], "shortname": row["lw_id"],
                "notion_page_id": row["page_id"], "sync_content": row["sync_content"],
                "url": course["url"],
            }
    return course_map


def _finish_transcribe(processed, failures, *, matched=None, targeted=False):
    status = "failed" if failures else "no_matching_active_recording" if targeted and not matched else "ok"
    print(json.dumps({"command": "transcribe", "status": status,
                      "processed": processed, "failures": len(failures), "matched": matched}))
    _notify_transcribe_failures(failures)
    if failures:
        sys.exit(2)
    if status == "no_matching_active_recording":
        sys.exit(3)


def cmd_transcribe_url(
    *,
    url: str,
    force: bool = False,
    limit: int | None = None,
    dry_run: bool = False,
):
    """Transkribiert YouTube-Videos, die unter einer LearnWeb-Abschnitts-/Seiten-URL liegen.

    Eigenständiger Pfad neben dem kursweiten Opencast-`cmd_transcribe`. Nutzt einen
    GARANTIERT schreibfreien Notion-Lookup (`notion_query_courses_db`) statt
    `cmd_sync_courses`, damit `--dry-run` nichts nach Notion schreibt. Untertitel
    zuerst (cookie-frei), sonst On-Device-Whisper. `--force` ist hier zulässig,
    weil die URL die Eingrenzung darstellt.
    """
    import shutil

    from transcription import downloader, manifest, notion_blocks, transcriber

    if not NOTION_TOKEN:
        log.error("NOTION_TOKEN nicht gesetzt – bitte in .env eintragen")
        sys.exit(1)
    if not dry_run and not NOTION_MEETING_DB_ID:
        log.error("NOTION_MEETING_DB_ID nicht gesetzt – bitte in .env eintragen")
        sys.exit(1)

    lock = _acquire_transcribe_lock(TRANSCRIBE_LOCK_PATH)
    if lock is None:
        log.warning("Ein transcribe-Lauf ist bereits aktiv – beende.")
        print(json.dumps({"command": "transcribe", "status": "busy"}))
        sys.exit(4)

    session = requests.Session()
    session.headers["User-Agent"] = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"

    failures: list[dict] = []
    processed = 0
    try:
        if not login(session):
            sys.exit(1)

        # Abschnitts-/Seiten-HTML laden.
        resp = session.get(url, timeout=60)
        resp.raise_for_status()
        html = resp.text
        soup = BeautifulSoup(html, "html.parser")

        # Kurs-ID ableiten – harter Fehler statt Abschnitts-ID zu missbrauchen.
        course_id = _parse_section_course_id(soup, html)
        if not course_id:
            log.error("Kurs-ID konnte aus der Seite nicht abgeleitet werden: %s", url)
            sys.exit(1)

        # Schreibfreier (read-only) Notion-Lookup für die Kurs-Verlinkung.
        by_course = notion_query_courses_db()["by_course_id"]
        row = by_course.get(course_id)
        course_page_id = row["page_id"] if row else None
        course_shortname = row["lw_id"] if row else None
        course_name = _extract_section_course_name(soup)

        recs = discover_youtube_recordings(
            session, soup, html,
            course_id=course_id,
            course_shortname=course_shortname,
            course_name=course_name,
            failures=failures,
        )
        if not recs:
            log.warning("Keine YouTube-Videos im Abschnitt gefunden: %s", url)
        log.info(
            "Abschnitt: %d YouTube-Video(s), Kurs-ID=%s%s",
            len(recs), course_id,
            "" if course_page_id else " (kein Notion-Kurs verknüpft)",
        )

        conn = _open_transcribe_connection(dry_run=dry_run)
        try:
            for rec in recs:
                if limit is not None and processed >= limit:
                    log.info("Limit %d erreicht – beende Lauf.", limit)
                    break
                result = _process_recording(
                    conn, session, rec,
                    course_page_id=course_page_id,
                    force=force, dry_run=dry_run,
                    modules=(downloader, manifest, notion_blocks, transcriber),
                    shutil_mod=shutil,
                )
                if result == "processed":
                    processed += 1
                elif isinstance(result, dict):
                    failures.append(result)
        finally:
            conn.close()
    except Exception as exc:
        failures.append({"key": url, "stage": "source", "reason": str(exc), "title": url})
    finally:
        lock.close()

    _finish_transcribe(processed, failures)


def cmd_transcribe(
    *,
    force: bool = False,
    cmid: str | None = None,
    course: str | None = None,
    limit: int | None = None,
    dry_run: bool = False,
    url: str | None = None,
):
    """Lokaler Transkriptions-Worker: Opencast-Aufzeichnungen → Whisper → Notion.

    Ablauf (Zustandsautomat, Plan §3/§4): Login → Sync-markierte Kurse → opencast-
    Aktivitäten → Episoden-Discovery → atomares Claiming → Download → Audio →
    On-Device-Whisper → create-or-find Inhalts-Eintrag + Meeting-Seite → Body in
    Batches anhängen (resume-fähig) → done. Jeder Übergang wird sofort persistiert.

    Args:
        force: re-transkribiert die per --cmid/--course/--url eingegrenzten Aufzeichnungen
            und ersetzt belegte eigene Blöcke; manuelle Ergänzungen bleiben erhalten. Ohne
            --cmid/--course/--url unzulässig (verhindert Massen-Reprocessing).
        cmid: nur Aufzeichnungen dieser Moodle-cmid verarbeiten.
        course: nur diesen Kurs (course_id oder Shortname) verarbeiten.
        limit: Höchstzahl tatsächlich transkribierter Aufzeichnungen pro Lauf.
        dry_run: Download + Transkription ausführen, aber NICHT nach Notion schreiben
            (reiner Kurslookup und In-Memory-Manifest).
        url: LearnWeb-Abschnitts-/Seiten-URL; transkribiert die dort verlinkten
            YouTube-Videos (delegiert an cmd_transcribe_url). Schließt --cmid/--course aus.
    """
    import shutil

    from transcription import downloader, manifest, notion_blocks, recordings, transcriber

    # YouTube-/Abschnitts-URL-Pfad ist eine eigenständige Funktion (schreibfreier
    # Notion-Lookup, cookie-freier Download). Vor dem Force-Guard, da --force --url gültig ist.
    if url:
        cmd_transcribe_url(url=url, force=force, limit=limit, dry_run=dry_run)
        return

    if force and not (cmid or course):
        log.error("--force erfordert --cmid, --course oder --url (kein Massen-Reprocessing).")
        sys.exit(1)
    if not NOTION_TOKEN:
        log.error("NOTION_TOKEN nicht gesetzt – bitte in .env eintragen")
        sys.exit(1)
    if not dry_run and not NOTION_MEETING_DB_ID:
        log.error("NOTION_MEETING_DB_ID nicht gesetzt – bitte in .env eintragen")
        sys.exit(1)

    # Lock gegen parallele Läufe (launchd + manuell).
    lock = _acquire_transcribe_lock(TRANSCRIBE_LOCK_PATH)
    if lock is None:
        log.warning("Ein transcribe-Lauf ist bereits aktiv – beende.")
        print(json.dumps({"command": "transcribe", "status": "busy"}))
        sys.exit(4)

    session = requests.Session()
    session.headers["User-Agent"] = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"

    failures: list[dict] = []
    processed = 0
    matched = 0
    try:
        if not login(session):
            sys.exit(1)
        course_map = _lookup_transcribe_courses(session) if dry_run else cmd_sync_courses(session)
        failures.extend(getattr(course_map, "errors", []))
        active_courses = [
            (course_id, info)
            for course_id, info in course_map.items()
            if info["sync_content"] and not info.get("conflict", False)
        ]
        if course:
            active_courses = [
                (cid, info)
                for cid, info in active_courses
                if cid == course or (info.get("shortname") == course)
            ]
        if not active_courses:
            log.warning("Keine passenden Kurse mit SyncContent=true gefunden.")

        conn = _open_transcribe_connection(dry_run=dry_run)
        try:
            for course_id, info in active_courses:
                if limit is not None and processed >= limit:
                    break
                course_obj = {
                    "course_id": course_id,
                    "name": info.get("name") or info.get("shortname") or course_id,
                    "url": info["url"],
                }
                course_page_id = info.get("notion_page_id")
                try:
                    soup = _load_course_page(session, info["url"])
                    shortname = _extract_shortname(soup)
                    activities = _extract_activities(soup, course_obj)
                except Exception as exc:
                    log.error("Kurs %s nicht ladbar: %s", info.get("shortname") or course_id, exc)
                    failures.append({"key": course_id, "stage": "course",
                                     "reason": str(exc), "title": course_obj["name"]})
                    continue

                opencast_acts = [
                    a for a in activities
                    if a["modtype"] in TRANSCRIBABLE_MODTYPES
                    and (cmid is None or a["cmid"] == cmid)
                ]
                if not opencast_acts:
                    continue
                log.info(
                    "Transkription: %s – %d opencast-Aktivität(en)",
                    shortname, len(opencast_acts),
                )

                for activity in opencast_acts:
                    if limit is not None and processed >= limit:
                        break
                    if not activity.get("view_url"):
                        failures.append({"key": activity["cmid"], "stage": "discovery",
                                         "reason": "missing_view_url", "title": activity.get("name")})
                        continue
                    view_url = _normalize_absolute_url(activity["view_url"])
                    try:
                        found = recordings.discover_recordings(
                            session, view_url, cmid=activity["cmid"], course_id=course_id,
                            course_shortname=shortname, course_name=course_obj["name"],
                            failures=failures,
                        )
                    except Exception as exc:
                        failures.append({"key": activity["cmid"], "stage": "discovery",
                                         "reason": str(exc), "title": activity.get("name")})
                        continue
                    if not found:
                        log.warning(
                            "Keine Aufzeichnung für cmid=%s (%s) gefunden.",
                            activity["cmid"], activity.get("name"),
                        )
                        continue

                    matched += len(found)

                    for rec in found:
                        if limit is not None and processed >= limit:
                            log.info("Limit %d erreicht – beende Lauf.", limit)
                            break
                        result = _process_recording(
                            conn, session, rec,
                            course_page_id=course_page_id,
                            force=force, dry_run=dry_run,
                            modules=(downloader, manifest, notion_blocks, transcriber),
                            shutil_mod=shutil,
                        )
                        if result == "processed":
                            processed += 1
                        elif isinstance(result, dict):
                            failures.append(result)
        finally:
            conn.close()
    except Exception as exc:
        failures.append({"key": cmid or course or "courses", "stage": "source",
                         "reason": str(exc), "title": course or cmid})
    finally:
        lock.close()

    _finish_transcribe(processed, failures, matched=matched, targeted=bool(cmid or course))


def _process_recording(conn, session, rec, *, course_page_id, force, dry_run, modules, shutil_mod):
    """Verarbeitet GENAU eine Aufzeichnung gemäß Zustandsautomat (Plan §3).

    Rückgabe: "processed" bei Erfolg, "skipped" wenn übersprungen, oder ein
    failure-dict {key, stage, reason, title} bei Fehler. Jeder Zustandsübergang
    wird sofort in `transcripts` persistiert, sodass ein abgebrochener Lauf
    wieder aufsetzen kann (create-or-find + Append-Resume).
    """
    import hashlib

    downloader, manifest, notion_blocks, transcriber = modules

    manifest.init_recovery_schema(conn)
    key = manifest.recording_key(rec.cmid, manifest.discriminator_for(rec))
    manifest.upsert_pending(conn, rec, key)
    prior = manifest.get(conn, key) or {}

    # Skip-/Force-Gate vor dem atomaren Claim.
    status = prior.get("status", "pending")
    if force:
        manifest.reset_for_force(conn, key)
    else:
        if status == "done":
            return "skipped"
        if status == "failed" and (prior.get("attempts") or 0) >= TRANSCRIBE_MAX_ATTEMPTS:
            log.warning("Übersprungen (max. Versuche erreicht): %s", key)
            return {"key": key, "stage": prior.get("failure_stage"),
                    "reason": "max_attempts_reached", "title": rec.title}

    if not manifest.claim(conn, key):
        # Bereits in Arbeit (claimed) oder von anderem Lauf erledigt.
        return "skipped"

    work_dir = TRANSCRIBE_WORK_DIR / key
    stage = "claimed"
    try:
        work_dir.mkdir(parents=True, exist_ok=True)

        replacement = json.loads(prior.get("replacement_body_json") or "null")
        cached_blocks = prior.get("generated_blocks_json")
        if replacement and not force and not dry_run:
            blocks = replacement["blocks"]
            body_hash = replacement["body_hash"]
            model_label = prior.get("model")
            duration = prior.get("duration_seconds")
        elif cached_blocks and prior.get("body_sha256") and not force and not dry_run:
            blocks = json.loads(cached_blocks)
            body_hash = prior["body_sha256"]
            model_label = prior.get("model")
            duration = prior.get("duration_seconds")
        else:
            # 1)+2) Acquisition: Transkript-Segmente beschaffen (quellspezifisch).
            if rec.source_kind == "youtube":
                # YouTube: Untertitel zuerst (cookie-frei), nur sonst Audio + Whisper.
                stage = "subtitles"
                segments = downloader.fetch_youtube_subtitles(
                    rec, work_dir, langs=_youtube_subtitle_langs()
                )
                if segments:
                    # Untertitel vorhanden → Download/Audio/Whisper komplett übersprungen.
                    model_label = "youtube-subs"
                    duration = segments[-1].end if segments else None
                    manifest.set_status(
                        conn, key, "transcribed",
                        model=model_label, duration_seconds=duration,
                    )
                else:
                    # Kein brauchbarer Untertitel → genau ein Audio-/Whisper-Fallback.
                    stage = "download"
                    media_path = downloader.download_youtube_audio(rec, work_dir)
                    audio_path = downloader.extract_audio(media_path, work_dir)
                    duration = downloader.probe_duration(media_path)
                    manifest.set_status(
                        conn, key, "downloaded",
                        media_path=str(media_path), audio_path=str(audio_path),
                        duration_seconds=duration,
                    )
                    stage = "transcribe"
                    segments, model_label = transcriber.transcribe(
                        audio_path, language=WHISPER_LANGUAGE, model=WHISPER_MODEL,
                    )
                    if not segments:
                        raise RuntimeError("empty_transcript")
                    manifest.set_status(conn, key, "transcribed", model=model_label)
            else:
                # 1) Download + Audio-Extraktion (Opencast, unverändert)
                stage = "download"
                media_path = downloader.download_media(session, rec, work_dir)
                audio_path = downloader.extract_audio(media_path, work_dir)
                duration = downloader.probe_duration(media_path)
                manifest.set_status(
                    conn, key, "downloaded",
                    media_path=str(media_path), audio_path=str(audio_path),
                    duration_seconds=duration,
                )

                # 2) Transkription (On-Device-Whisper)
                stage = "transcribe"
                segments, model_label = transcriber.transcribe(
                    audio_path, language=WHISPER_LANGUAGE, model=WHISPER_MODEL,
                )
                if not segments:
                    raise RuntimeError("empty_transcript")
                manifest.set_status(conn, key, "transcribed", model=model_label)

            # 3) Notion-Blöcke + Body-Hash
            blocks = notion_blocks.build_transcript_blocks(segments, with_timestamps=True)
            body_hash = hashlib.sha256(
                "\n".join(f"{s.start:.2f}|{s.text}" for s in segments).encode("utf-8")
            ).hexdigest()

        if dry_run:
            log.info(
                "[dry-run] %s: %d Segmente, %d Blöcke, Backend=%s, Dauer=%.0fs",
                key, len(segments), len(blocks), model_label, duration or 0.0,
            )
            # CLI-Dry-Runs nutzen eine In-Memory-DB. Reset hält die Funktion auch
            # bei direktem Aufruf mit einer externen Connection wiederholbar.
            manifest.reset_for_force(conn, key)
            return "processed"

        # Vor Remote-Anlage checkpointen; Force behält den bisherigen Eigentumsbeleg.
        if not prior.get("body_sha256") and not prior.get("meeting_page_id"):
            manifest.set_status(
                conn, key, "transcribed", body_sha256=body_hash,
                generated_blocks_json=json.dumps(blocks, ensure_ascii=False),
                total_block_count=len(blocks),
            )

        # 4) create-or-find: schlanker Inhalts-Eintrag in Learnweb Inhalte
        stage = "inhalt"
        content_props = _build_recording_content_properties(rec, course_page_id, key)
        content_id = prior.get("content_page_id") or _notion_find_page_by_quelle(NOTION_LW_DB_ID, key)
        if content_id:
            _notion_update_page_properties(content_id, content_props)
        else:
            content_id = _notion_create_page(NOTION_LW_DB_ID, content_props)
        manifest.set_status(conn, key, "inhalt_created", content_page_id=content_id)

        # 5) create-or-find: Meeting-Notiz-Seite (Transkript-Träger)
        stage = "meeting"
        meeting_props = _build_meeting_properties(rec, content_id, course_page_id, key)
        meeting_id = prior.get("meeting_page_id") or _notion_find_page_by_quelle(NOTION_MEETING_DB_ID, key)
        if meeting_id:
            _notion_update_page_properties(meeting_id, meeting_props)
        else:
            meeting_id = _notion_create_page(NOTION_MEETING_DB_ID, meeting_props)
        manifest.set_status(
            conn, key, "meeting_created",
            meeting_page_id=meeting_id, total_block_count=len(blocks),
        )

        # 6) Remote-Body statt bloßem lokalen Zähler als Append-Checkpoint nutzen.
        stage = "append"
        appended = _reconcile_recording_body(
            conn, key, meeting_id, prior, blocks, body_hash,
            force=force, manifest=manifest,
        )
        for start in range(appended, len(blocks), MAX_NOTION_BLOCKS_PER_REQUEST):
            batch = blocks[start:start + MAX_NOTION_BLOCKS_PER_REQUEST]
            notion_append_page_children(meeting_id, batch)
            checkpoint = manifest.get(conn, key)
            appended = _reconcile_recording_body(
                conn, key, meeting_id, checkpoint, blocks, body_hash,
                force=False, manifest=manifest,
            )
            if appended != min(start + len(batch), len(blocks)):
                raise RuntimeError("recording_append_not_confirmed")

        manifest.set_status(conn, key, "done")
        log.info("✓ Transkribiert: %s (%s)", rec.title, key)
        return "processed"

    except Exception as exc:
        reason = str(exc) or exc.__class__.__name__
        log.error("Transkription fehlgeschlagen [%s] %s: %s", stage, key, reason)
        try:
            manifest.set_status(conn, key, "failed", failure_stage=stage, failure_reason=reason[:500])
        except Exception:  # Persistenz-Fehler nicht über den Originalfehler legen
            log.exception("Konnte failed-Status nicht persistieren: %s", key)
        return {"key": key, "stage": stage, "reason": reason, "title": rec.title}
    finally:
        # Temporäre Medien-/Audiodateien immer aufräumen.
        try:
            if work_dir.exists():
                shutil_mod.rmtree(work_dir, ignore_errors=True)
        except Exception:
            pass


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
  transcribe    Lokaler Mac-Worker: Opencast/YouTube -> Untertitel/Whisper -> Notion
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
            "transcribe",
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
    # --- transcribe-spezifische Argumente (lokaler Mac-Worker) ---
    parser.add_argument(
        "--force",
        action="store_true",
        help="transcribe: bereits transkribierte Aufzeichnungen neu erstellen und "
             "Body ersetzen (erfordert --cmid, --course oder --url)",
    )
    # --cmid / --course / --url grenzen den transcribe-Lauf gegenseitig exklusiv ein.
    transcribe_scope = parser.add_mutually_exclusive_group()
    transcribe_scope.add_argument(
        "--cmid",
        type=str,
        default=None,
        help="transcribe: nur Aufzeichnungen dieser Moodle-cmid verarbeiten",
    )
    transcribe_scope.add_argument(
        "--course",
        type=str,
        default=None,
        help="transcribe: nur diesen Kurs (course_id oder Shortname) verarbeiten",
    )
    transcribe_scope.add_argument(
        "--url",
        type=str,
        default=None,
        help="transcribe: YouTube-Videos aus einer LearnWeb-Abschnitts-/Seiten-URL "
             "transkribieren (Untertitel zuerst, sonst Whisper)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="transcribe: Download + Transkription ausführen, aber NICHT nach Notion "
             "schreiben (Zustand wird hinterher zurückgesetzt)",
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
    elif args.command == "transcribe":
        cmd_transcribe(
            force=args.force,
            cmid=args.cmid,
            course=args.course,
            limit=args.limit,
            dry_run=args.dry_run,
            url=args.url,
        )


if __name__ == "__main__":
    main()
