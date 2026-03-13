#!/usr/bin/env python3
"""
learnweb_sync — Synchronizes LearnWeb (Moodle) content to Notion.

Usage:
    python learnweb_sync.py sync-courses # Kurse in KurseLearnWeb prüfen / anlegen
    python learnweb_sync.py scan         # Kurse scrapen, neue Aktivitäten im Manifest erfassen
    python learnweb_sync.py push         # Neue Ressourcen herunterladen + Notion-Seiten anlegen [Phase 2]
    python learnweb_sync.py run          # scan + push [Phase 2]
    python learnweb_sync.py export-zips  # Alle Kurse als ZIP-Backup herunterladen
"""

import argparse
import hashlib
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

# Notion API
NOTION_TOKEN = os.getenv("NOTION_TOKEN", "")
NOTION_COURSES_DB_ID = os.getenv("NOTION_COURSES_DB_ID", "")  # KurseLearnWeb-DB
NOTION_LW_DB_ID = os.getenv("NOTION_LW_DB_ID", "")           # Learnweb Inhalte-DB
NOTION_API = "https://api.notion.com/v1"
CURRENT_SEMESTER = os.getenv("CURRENT_SEMESTER", "")          # z.B. "SoSe 26"


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
            status           TEXT DEFAULT 'new'  -- new / synced / error / removed
        )
    """)
    # Migration: course_shortname-Spalte zu bestehenden DBs hinzufügen (einmalig)
    existing = {row[1] for row in conn.execute("PRAGMA table_info(resources)")}
    if "course_shortname" not in existing:
        conn.execute("ALTER TABLE resources ADD COLUMN course_shortname TEXT")
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
    return re.sub(r"[^\w\-]", "_", shortname).strip("_")


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


def get_course_activities(
    session: requests.Session, course: dict
) -> tuple[str, list[dict]]:
    """
    Scrape a course page and return (shortname, activities).

    shortname: Moodle-Kürzel aus dem Breadcrumb (z.B. "Inf1-2025_2")
    activities: Liste aller Aktivitäten auf der Seite

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
    resp = session.get(course["url"])
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")
    shortname = _extract_shortname(soup)

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

    return shortname, activities


# ── Notion API ────────────────────────────────────────────────────────────────


def _notion_headers() -> dict:
    """Standard-Header für alle Notion-API-Aufrufe."""
    return {
        "Authorization": f"Bearer {NOTION_TOKEN}",
        "Notion-Version": "2022-06-28",
        "Content-Type": "application/json",
    }


def notion_query_courses_db() -> dict[str, dict]:
    """
    Liest alle Einträge aus KurseLearnWeb (TESTING).
    Gibt {lw_id: {"page_id": ..., "sync_content": bool, "url": ...}} zurück.
    Paginierung wird automatisch behandelt (bis zu 100 Einträge pro Request).
    """
    all_pages = []
    next_cursor = None

    while True:
        body: dict = {"page_size": 100}
        if next_cursor:
            body["start_cursor"] = next_cursor
        resp = requests.post(
            f"{NOTION_API}/databases/{NOTION_COURSES_DB_ID}/query",
            headers=_notion_headers(),
            json=body,
        )
        resp.raise_for_status()
        data = resp.json()
        all_pages.extend(data.get("results", []))
        if not data.get("has_more"):
            break
        next_cursor = data.get("next_cursor")

    result = {}
    for page in all_pages:
        props = page["properties"]
        # LW-ID ist ein title-Feld — normieren wie _extract_shortname(), damit
        # manuell angelegte Einträge (z.B. mit Leerzeichen) korrekt erkannt werden.
        lw_id_raw = "".join(t["plain_text"] for t in props["LW-ID"]["title"])
        lw_id = re.sub(r"[^\w\-]", "_", lw_id_raw).strip("_")
        result[lw_id] = {
            "page_id": page["id"],
            "sync_content": props["SyncContent"]["checkbox"],
            "url": (props["URL"]["url"] or "") if props.get("URL") else "",
        }
    return result


def notion_create_course(lw_id: str, course_url: str) -> str:
    """
    Legt einen neuen Kurs in KurseLearnWeb an.
    SyncContent wird auf false gesetzt — Thomas aktiviert manuell.
    Gibt die Notion Page-ID zurück.
    """
    resp = requests.post(
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
    resp.raise_for_status()
    return resp.json()["id"]


# ── Commands ──────────────────────────────────────────────────────────────────


def cmd_sync_courses(session: requests.Session | None = None) -> dict[str, dict]:
    """
    Gleicht alle belegten LearnWeb-Kurse mit der KurseLearnWeb-DB in Notion ab.
    Fehlende Kurse werden neu angelegt (SyncContent=false).

    Lädt jede Kursseite einmal und gibt zurück:
        {course_id: {shortname, notion_page_id, sync_content, url, activities}}

    Der Rückgabewert wird von cmd_scan() weiterverwendet (activities bereits gecacht).
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
    log.info(f"  {len(notion_courses)} Kurs/Kurse bereits in Notion erfasst")

    course_map: dict[str, dict] = {}
    new_courses: list[str] = []

    for course in courses:
        log.info(f"Lade Kursseite: {course['name']}")
        try:
            shortname, activities = get_course_activities(session, course)
        except Exception as e:
            log.error(f"  Fehler beim Laden von {course['name']}: {e}")
            continue

        # Kurs in Notion anlegen falls noch nicht vorhanden
        if shortname not in notion_courses:
            log.info(f"  → Neuer Kurs: {shortname} – wird in Notion angelegt")
            try:
                page_id = notion_create_course(shortname, course["url"])
                notion_courses[shortname] = {
                    "page_id": page_id,
                    "sync_content": False,
                    "url": course["url"],
                }
                new_courses.append(shortname)
            except Exception as e:
                log.error(f"  Fehler beim Anlegen von {shortname}: {e}")
                page_id = None
        else:
            page_id = notion_courses[shortname]["page_id"]

        course_map[course["course_id"]] = {
            "shortname": shortname,
            "notion_page_id": page_id,
            "sync_content": notion_courses.get(shortname, {}).get("sync_content", False),
            "url": course["url"],
            "activities": activities,  # gecacht – wird in cmd_scan() weiterverwendet
        }

    # ── Summary ───────────────────────────────────────────────────────────────
    print("\n" + "=" * 60)
    if new_courses:
        print(f"✓ {len(new_courses)} neuer Kurs/Kurse in Notion angelegt:")
        for name in new_courses:
            print(f"    + {name}")
    else:
        print("✓ Alle Kurse bereits in Notion erfasst.")
    sync_count = sum(1 for v in course_map.values() if v["sync_content"])
    print(f"  {sync_count} von {len(course_map)} Kurs/Kursen haben SyncContent aktiviert.")
    print("=" * 60 + "\n")

    return course_map


def cmd_scan():
    """
    Kurse mit SyncContent=true scrapen und neue Aktivitäten im Manifest erfassen.
    Führt zuerst sync-courses aus (prüft/legt Kurse in Notion an).
    """
    conn = init_db()
    session = requests.Session()
    session.headers["User-Agent"] = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"

    if not login(session):
        sys.exit(1)

    # Schritt 1: Kurse in Notion synchronisieren, alle Seiten laden
    course_map = cmd_sync_courses(session)

    # Schritt 2: Nur Kurse mit SyncContent=true scannen
    total_new = 0
    new_by_course: dict[str, list] = {}

    for course_id, info in course_map.items():
        if not info["sync_content"]:
            log.info(f"Übersprungen (SyncContent=false): {info['shortname']}")
            continue

        log.info(f"Scanne: {info['shortname']} ({len(info['activities'])} Aktivitäten)")
        new_in_course = []

        for activity in info["activities"]:
            activity["course_shortname"] = info["shortname"]
            is_new = upsert_activity(conn, activity)
            if is_new:
                new_in_course.append(activity)
                total_new += 1

        # Bestehende Einträge ohne Shortname nachfüllen (Migration alter Daten)
        conn.execute(
            "UPDATE resources SET course_shortname = ? WHERE course_id = ? AND course_shortname IS NULL",
            (info["shortname"], course_id),
        )
        conn.commit()
        log.info(f"  {len(new_in_course)} neue Aktivität(en)")
        if new_in_course:
            new_by_course[info["shortname"]] = new_in_course

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


def _guess_kategorie(name: str) -> str | None:
    """
    Heuristik: Kategorie aus dem Aktivitätsnamen ableiten.
    Gibt einen der erlaubten Notion-Select-Werte zurück.
    """
    n = name.lower()
    if any(k in n for k in ("vorlesung", "lecture", " vl ", "vl_", "vl.")):
        return "L Lecture"
    if any(k in n for k in ("tutorial", "tutorium", "übung", "uebung", " ue ", "ue_")):
        return "T Tutorial"
    if any(k in n for k in ("klausur", "exam", "prüfung", "pruefung")):
        return "E Exam"
    if any(k in n for k in ("python", ".ipynb", ".py")):
        return "P Python"
    if any(k in n for k in ("aufgabe", "blatt", "sheet", "exercise", "hausaufgabe")):
        return "A Aufgabensammlung"
    if any(k in n for k in ("skript", "script")):
        return "S Script"
    return "R Resource"


def _guess_format(filename: str) -> str | None:
    """Dateiendung → Notion-Format-Select-Wert (oder None wenn unbekannt)."""
    ext = Path(filename).suffix.lower().lstrip(".")
    return ext if ext in {"pdf", "ipynb", "py", "pkl", "zip"} else None


def _download_resource(
    session: requests.Session, view_url: str
) -> tuple[bytes, str] | None:
    """
    Lädt eine Moodle-Ressource herunter.
    Gibt (file_bytes, filename) zurück, oder None bei Fehler.

    Ablauf:
      1. GET view_url → folgt Redirects
      2. Falls Antwort HTML: pluginfile.php-Link aus dem Seiteninhalt extrahieren
      3. Dateiname aus Content-Disposition oder URL-Pfad ableiten
    """
    resp = session.get(view_url, allow_redirects=True, stream=True, timeout=60)
    resp.raise_for_status()

    # Falls die Seite HTML zurückgibt, echten Download-Link suchen
    if "text/html" in resp.headers.get("Content-Type", ""):
        html = resp.text
        soup = BeautifulSoup(html, "html.parser")
        # Moodle-typischer Download-Link (pluginfile.php)
        dl_link = soup.find("a", href=re.compile(r"pluginfile\.php"))
        if not dl_link:
            dl_link = soup.find("a", href=re.compile(r"forcedownload"))
        if not dl_link:
            log.warning(f"Kein Download-Link in HTML-Seite: {view_url}")
            return None
        file_url = dl_link["href"]
        if not file_url.startswith("http"):
            file_url = BASE_URL + file_url
        resp = session.get(file_url, allow_redirects=True, stream=True, timeout=60)
        resp.raise_for_status()

    # Dateiname aus Content-Disposition extrahieren
    cd = resp.headers.get("Content-Disposition", "")
    m = re.search(r"filename\*?=(?:UTF-8'')?[\"']?([^\"';\r\n]+)", cd, re.IGNORECASE)
    if m:
        filename = m.group(1).strip().strip("\"'")
    else:
        # Fallback: letztes Segment der finalen URL
        url_path = resp.url.split("?")[0].rstrip("/")
        filename = url_path.split("/")[-1] or "download.bin"

    file_bytes = b"".join(resp.iter_content(chunk_size=256 * 1024))
    return file_bytes, filename


def notion_upload_file(file_bytes: bytes, filename: str) -> str | None:
    """
    Lädt eine Datei über die Notion File Upload API hoch (single_part, max 20 MB).
    Gibt die file_upload_id zurück, oder None bei Überschreitung des Größenlimits.
    """
    MAX_BYTES = 20 * 1024 * 1024  # 20 MB
    if len(file_bytes) > MAX_BYTES:
        log.warning(
            f"Datei zu groß ({len(file_bytes) / 1024 / 1024:.1f} MB) – kein Upload: {filename}"
        )
        return None

    # Datei-Typ ermitteln
    ext = Path(filename).suffix.lower()
    content_type = "application/pdf" if ext == ".pdf" else "application/octet-stream"

    # Schritt 1: Upload-Objekt erstellen
    resp = requests.post(
        f"{NOTION_API}/file_uploads",
        headers={"Authorization": f"Bearer {NOTION_TOKEN}", "Notion-Version": "2022-06-28"},
        json={"mode": "single_part", "content_type": content_type},
    )
    resp.raise_for_status()
    upload_id = resp.json()["id"]
    upload_url = resp.json()["upload_url"]

    # Schritt 2: Datei hochladen — POST (nicht PUT!) + Notion-Version Header erforderlich
    resp2 = requests.post(
        upload_url,
        headers={"Authorization": f"Bearer {NOTION_TOKEN}", "Notion-Version": "2022-06-28"},
        files={"file": (filename, file_bytes, content_type)},
    )
    resp2.raise_for_status()

    return upload_id


def notion_create_lw_page(
    resource: dict,
    file_upload_id: str | None,
    course_notion_page_id: str | None,
) -> str:
    """
    Legt eine neue Seite in Learnweb Inhalte (TESTING) an.
    Gibt die Notion page_id zurück.
    """
    properties: dict = {
        "Name": {"title": [{"text": {"content": resource["name"]}}]},
        "Nr": {"rich_text": [{"text": {"content": resource["cmid"]}}]},
        "Kurs-ID": {"rich_text": [{"text": {"content": resource["course_id"]}}]},
        "Variante": {"select": {"name": "Original"}},
    }

    # Kategorie per Heuristik
    kategorie = _guess_kategorie(resource["name"])
    if kategorie:
        properties["Kategorie"] = {"select": {"name": kategorie}}

    # Format aus Dateinamen
    if resource.get("file_name"):
        fmt = _guess_format(resource["file_name"])
        if fmt:
            properties["Format"] = {"select": {"name": fmt}}

    # Quell-Semester aus .env
    if CURRENT_SEMESTER:
        properties["Quell-Semester"] = {"select": {"name": CURRENT_SEMESTER}}

    # Datei-Anhang (falls hochgeladen)
    if file_upload_id:
        properties["LW Download"] = {
            "files": [{"type": "file_upload", "file_upload": {"id": file_upload_id}}]
        }

    # Relation zu KurseLearnWeb (TESTING)
    if course_notion_page_id:
        properties["KurseLearnWeb (TESTING)"] = {
            "relation": [{"id": course_notion_page_id}]
        }

    resp = requests.post(
        f"{NOTION_API}/pages",
        headers=_notion_headers(),
        json={"parent": {"database_id": NOTION_LW_DB_ID}, "properties": properties},
    )
    resp.raise_for_status()
    return resp.json()["id"]


def cmd_push():
    """
    Phase 2: Neue Ressourcen aus dem Manifest herunterladen und als Notion-Seiten anlegen.
    Verarbeitet nur Einträge mit notion_id IS NULL und modtype='resource'
    aus Kursen mit SyncContent=true.
    """
    if not NOTION_TOKEN:
        log.error("NOTION_TOKEN nicht gesetzt")
        sys.exit(1)
    if not NOTION_LW_DB_ID:
        log.error("NOTION_LW_DB_ID nicht gesetzt")
        sys.exit(1)

    session = requests.Session()
    session.headers["User-Agent"] = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"
    if not login(session):
        sys.exit(1)

    # Kursliste + KurseLearnWeb-Abgleich (enthält notion_page_id pro Kurs)
    course_map = cmd_sync_courses(session)

    # Nur aktive Kurse (SyncContent=true) → {course_id: notion_page_id}
    active = {
        cid: info["notion_page_id"]
        for cid, info in course_map.items()
        if info["sync_content"]
    }
    if not active:
        log.warning("Keine Kurse mit SyncContent=true – nichts zu pushen.")
        return

    conn = init_db()
    downloads_dir = Path(__file__).parent / "downloads"
    downloads_dir.mkdir(exist_ok=True)

    # Alle noch nicht gepushten Ressourcen der aktiven Kurse
    placeholders = ",".join("?" * len(active))
    rows = conn.execute(
        f"""
        SELECT cmid, course_id, name, view_url
        FROM resources
        WHERE modtype = 'resource'
          AND notion_id IS NULL
          AND course_id IN ({placeholders})
        ORDER BY course_id, first_seen
        """,
        list(active.keys()),
    ).fetchall()

    if not rows:
        print("\n✓ Nichts zu pushen – alle Ressourcen bereits in Notion.\n")
        conn.close()
        return

    log.info(f"{len(rows)} Ressource(n) zum Pushen.")
    pushed, no_file, errors = 0, 0, 0

    for cmid, course_id, name, view_url in rows:
        log.info(f"  Push: {name} (cmid={cmid})")

        # ── Datei herunterladen ────────────────────────────────────────────────
        try:
            result = _download_resource(session, view_url)
        except Exception as e:
            log.error(f"    Download-Fehler: {e}")
            conn.execute("UPDATE resources SET status = 'error' WHERE cmid = ?", (cmid,))
            conn.commit()
            errors += 1
            continue

        if result is None:
            log.error(f"    Kein Download möglich – übersprungen: {name}")
            conn.execute("UPDATE resources SET status = 'error' WHERE cmid = ?", (cmid,))
            conn.commit()
            errors += 1
            continue

        file_bytes, file_name = result
        file_hash = hashlib.md5(file_bytes).hexdigest()

        # ── Notion File Upload ─────────────────────────────────────────────────
        upload_id = None
        try:
            upload_id = notion_upload_file(file_bytes, file_name)
        except Exception as e:
            log.warning(f"    Upload-Fehler: {e} – Seite wird ohne Datei angelegt")

        # ── Notion-Seite anlegen ───────────────────────────────────────────────
        resource = {"cmid": cmid, "course_id": course_id, "name": name, "file_name": file_name}
        try:
            notion_id = notion_create_lw_page(resource, upload_id, active.get(course_id))
        except Exception as e:
            log.error(f"    Notion-Fehler: {e}")
            conn.execute(
                "UPDATE resources SET status = 'error', file_name = ? WHERE cmid = ?",
                (file_name, cmid),
            )
            conn.commit()
            errors += 1
            continue

        # ── Manifest aktualisieren ─────────────────────────────────────────────
        conn.execute(
            "UPDATE resources SET notion_id = ?, file_name = ?, file_hash = ?, status = 'synced' WHERE cmid = ?",
            (notion_id, file_name, file_hash, cmid),
        )
        conn.commit()

        if upload_id:
            pushed += 1
            log.info(f"    ✓ Mit Datei angelegt")
        else:
            no_file += 1
            log.info(f"    ✓ Ohne Datei angelegt (>20 MB oder Upload-Fehler)")

    conn.close()

    print("\n" + "=" * 60)
    print(f"✓ Push: {pushed} mit Datei, {no_file} ohne Datei, {errors} Fehler")
    print("=" * 60 + "\n")


def cmd_run():
    """scan + push in einem Schritt (Standard für automatische Läufe)."""
    cmd_scan()
    cmd_push()


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
  scan         Scrape all courses and record new activities in the manifest
  push         Download new resources and create Notion pages (Phase 2, not yet built)
  run          scan + push (Phase 2, not yet built)
  export-zips  Download all courses as ZIP files (backup mode)
""",
    )
    parser.add_argument(
        "command", choices=["sync-courses", "scan", "push", "run", "export-zips"]
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
    elif args.command == "export-zips":
        cmd_export_zips()


if __name__ == "__main__":
    main()
