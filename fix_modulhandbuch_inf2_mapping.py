#!/usr/bin/env python3
"""
fix_modulhandbuch_inf2_mapping.py — Post-Migration Cleanup.

Setzt die LearnWeb-Relation für Inf2/BWL2/WI2/QM2 im MODULHANDBUCH auf die
NEW KurseLearnWeb-DB. Ändert learnweb_sync.py nicht.

Usage:
    python fix_modulhandbuch_inf2_mapping.py          # Dry-Run (kein Schreibzugriff)
    python fix_modulhandbuch_inf2_mapping.py --apply  # Schreibt Änderungen nach Notion
    python fix_modulhandbuch_inf2_mapping.py --force  # Überspringt Pre-Flight Lock-Check
"""

import argparse
import logging
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import requests
from dotenv import load_dotenv

# ── Konfiguration ─────────────────────────────────────────────────────────────

load_dotenv()

NOTION_TOKEN: str = os.getenv("NOTION_TOKEN", "")
NOTION_API = "https://api.notion.com/v1"
NOTION_VERSION = "2022-06-28"

# Notion Data-Source IDs (privater Workspace)
MODULHANDBUCH_DS_ID = "320bf244-cadc-80e3-b714-ece5096f83d7"
KURSELEARNWEB_DS_ID = "a44bf244-cadc-8266-9c11-816719a8ec06"

# Pre-Flight Lock gegen parallel laufenden Sync-Job
LOCKFILE_PATH = Path("~/.cache/learnweb_sync.lock").expanduser()

# Suchbegriffe für MODULHANDBUCH-Suche via ModNum (title-Property).
# Jeder Eintrag ist ein exakter oder eindeutiger Substring des ModNum-Codes.
MODULE_DB_ALIASES: dict[str, list[str]] = {
    "Inf2": ["Inf2"],
    "BWL2": ["BWL2"],
    "WI2":  ["WI2"],
    "QM2":  ["QM2"],
}

# Suchbegriffe für KurseLearnWeb-Kurs-Suche (title-Property der Kurs-Pages).
# Kurs-Namen im Format "Kürzel-Semester", z.B. "Informatik_II-2026_1".
MODULE_LW_ALIASES: dict[str, list[str]] = {
    "Inf2": ["Informatik_II", "Informatik II"],
    "BWL2": ["BWL2"],
    "WI2":  ["DaMa", "Datenmanagement"],
    "QM2":  ["OR-"],
}

# Werden zur Laufzeit per Schema-Inspektion gesetzt.
# ModNum (title) ist der eindeutige Modul-Code wie "Inf2", "BWL2" etc.
MATCH_PROP: str = "ModNum"        # Property-Name für Modul-Suche in MODULHANDBUCH
MATCH_PROP_TYPE: str = "title"    # Notion-Filtertyp passend zu MATCH_PROP
RELATION_PROP: str = "LearnWeb"   # Property-Name der LearnWeb-Relation

# Verify-Schwellwert: nach --apply müssen mindestens so viele Module eine
# LearnWeb-Relation haben. (Stand 2026-05-20: 6 bereits gemappt.)
VERIFY_MIN_MODULES = 6

# ── Logging ───────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

# ── Notion HTTP-Helpers ────────────────────────────────────────────────────────

_session = requests.Session()


def _headers() -> dict:
    """Standard-Header für alle Notion-API-Aufrufe."""
    return {
        "Authorization": f"Bearer {NOTION_TOKEN}",
        "Notion-Version": NOTION_VERSION,
        "Content-Type": "application/json",
    }


def _notion_request(method: str, url: str, **kwargs) -> requests.Response:
    """
    HTTP-Wrapper für Notion-API.
    - Timeout: 30 s
    - Rate-Limiting: 0,35 s Pause nach jedem Request (< 3/s)
    - Retry bei 429 mit Retry-After-Header (max. 3 Versuche)
    """
    kwargs.setdefault("timeout", 30)
    for attempt in range(3):
        resp = _session.request(method, url, **kwargs)
        if resp.status_code == 429:
            wait = int(resp.headers.get("Retry-After", 2 ** (attempt + 1)))
            log.warning("Rate-Limit 429 – warte %ds (Versuch %d/3)", wait, attempt + 1)
            time.sleep(wait)
            continue
        resp.raise_for_status()
        time.sleep(0.35)
        return resp
    resp.raise_for_status()
    return resp


# ── Datenbank-Paginator ────────────────────────────────────────────────────────

def query_data_source(
    ds_id: str,
    filter_body: dict | None = None,
    filter_properties: list[str] | None = None,
) -> list[dict]:
    """
    Paginiert über alle Einträge einer Notion-Datenbank.
    filter_body   – optionaler Notion-Filter (wird in den POST-Body eingefügt)
    filter_properties – Liste von Property-IDs/-Namen, die zurückgegeben werden
    """
    url = f"{NOTION_API}/databases/{ds_id}/query"
    params: dict = {}
    if filter_properties:
        params["filter_properties"] = filter_properties

    body: dict = {"page_size": 100}
    if filter_body:
        body["filter"] = filter_body

    pages: list[dict] = []
    cursor: str | None = None

    while True:
        if cursor:
            body["start_cursor"] = cursor
        resp = _notion_request("POST", url, headers=_headers(), json=body, params=params)
        data = resp.json()
        pages.extend(data.get("results", []))
        if not data.get("has_more"):
            break
        cursor = data.get("next_cursor")

    return pages


# ── Schema-Inspektion ──────────────────────────────────────────────────────────

def _detect_match_prop(ds_id: str) -> None:
    """
    Liest das MODULHANDBUCH-Schema und verifiziert/setzt MATCH_PROP und RELATION_PROP.
    Bevorzugte Match-Property: "ModNum" (title, eindeutiger Modul-Code).
    RELATION_PROP: erste Relation auf KURSELEARNWEB_DS_ID.
    """
    global MATCH_PROP, MATCH_PROP_TYPE, RELATION_PROP

    resp = _notion_request("GET", f"{NOTION_API}/databases/{ds_id}", headers=_headers())
    schema: dict = resp.json().get("properties", {})

    if not schema:
        log.error(
            "MODULHANDBUCH-Schema ist leer. NOTION_TOKEN prüfen oder DB-ID falsch. "
            "Response-Keys: %s",
            list(resp.json().keys()),
        )
        sys.exit(1)

    # Beste Match-Property finden.
    # ModNum (title) zuerst – eindeutiger Code, keine Substring-Kollisionen.
    candidates = [
        ("ModNum", "title"),
        ("Modultitel (DE)", "rich_text"),
        ("Modulname", "rich_text"),
        ("Modul", "rich_text"),
        ("Name", "rich_text"),
    ]
    for candidate, prop_type in candidates:
        if candidate in schema:
            MATCH_PROP = candidate
            MATCH_PROP_TYPE = prop_type
            break
    else:
        MATCH_PROP = "title"
        MATCH_PROP_TYPE = "title"

    log.info("MATCH_PROP: %r (type=%s)", MATCH_PROP, MATCH_PROP_TYPE)

    # Relation-Property finden: DB-ID-Match hat Vorrang, dann Name-Substring
    target_id_norm = KURSELEARNWEB_DS_ID.replace("-", "")
    for prop_name, prop_def in schema.items():
        if prop_def.get("type") != "relation":
            continue
        related_db = prop_def.get("relation", {}).get("database_id", "").replace("-", "")
        if related_db == target_id_norm:
            RELATION_PROP = prop_name
            break

    if not RELATION_PROP:
        for prop_name, prop_def in schema.items():
            if (
                prop_def.get("type") == "relation"
                and "learnweb" in prop_name.lower()
                and "old" not in prop_name.lower()
                and "archiv" not in prop_name.lower()
            ):
                RELATION_PROP = prop_name
                break

    if RELATION_PROP:
        log.info("RELATION_PROP: %r", RELATION_PROP)
    else:
        log.error(
            "Keine Relation-Property gefunden, die auf KurseLearnWeb zeigt. "
            "Schema-Properties: %s",
            list(schema.keys()),
        )
        sys.exit(1)


# ── Titel-Extraktion ───────────────────────────────────────────────────────────

def _extract_title(page: dict, prop_name: str = "title") -> str:
    """Gibt den Plain-Text-Wert einer Property zurück (title oder rich_text)."""
    props = page.get("properties", {})
    prop = props.get(prop_name, {})
    rich_text = prop.get("title") or prop.get("rich_text") or []
    text = "".join(t.get("plain_text", "") for t in rich_text)
    if text:
        return text
    # Fallback: erste title-type Property
    for p in props.values():
        if p.get("type") == "title":
            rich_text = p.get("title", [])
            text = "".join(t.get("plain_text", "") for t in rich_text)
            if text:
                return text
    return ""


# ── Modul-Suche im MODULHANDBUCH ──────────────────────────────────────────────

def _dump_all_titles() -> None:
    """Loggt alle MODULHANDBUCH-Einträge für manuelle Verifikation."""
    log.info("=== Pre-Step: MODULHANDBUCH-Dump (%s) ===", MATCH_PROP)
    pages = query_data_source(MODULHANDBUCH_DS_ID)
    for page in pages:
        value = _extract_title(page, MATCH_PROP)
        title = _extract_title(page, "Modultitel (DE)") or ""
        log.info("  %r → %r  (id=%s)", value, title, page["id"])
    log.info("=== Dump fertig (%d Seiten) ===", len(pages))


def find_module_pages(modules: dict[str, list[str]]) -> dict[str, list[str]]:
    """
    Sucht für jedes Modul passende Pages in MODULHANDBUCH via MATCH_PROP.
    Bei ModNum ist der Treffer eindeutig (Substring des exakten Code-Felds).
    Gibt {modulname: [page_id, ...]} zurück.
    CRITICAL: bricht ab wenn ein Modul > 2 Treffer hat.
    """
    result: dict[str, list[str]] = {}

    for module, aliases in modules.items():
        found_ids: list[str] = []

        for alias in aliases:
            filter_body = {
                "property": MATCH_PROP,
                MATCH_PROP_TYPE: {"contains": alias},
            }
            pages = query_data_source(MODULHANDBUCH_DS_ID, filter_body=filter_body)
            for page in pages:
                if page["id"] not in found_ids:
                    found_ids.append(page["id"])

        log.info("Modul %r: %d Treffer in MODULHANDBUCH", module, len(found_ids))

        if len(found_ids) > 2:
            log.error(
                "CRITICAL: Modul %r hat %d Treffer (> 2) → Disambiguierung nötig. "
                "Aliases: %s",
                module, len(found_ids), aliases,
            )
            sys.exit(1)

        if len(found_ids) == 0:
            log.error(
                "CRITICAL: Modul %r hat 0 Treffer. "
                "MODULHANDBUCH-Naming weicht von MODULE_DB_ALIASES ab. "
                "Dump oben prüfen und MODULE_DB_ALIASES erweitern.",
                module,
            )
            sys.exit(1)

        result[module] = found_ids

    return result


# ── Kurs-Suche in KurseLearnWeb ────────────────────────────────────────────────

def find_lw_courses(modules: dict[str, list[str]]) -> dict[str, list[str]]:
    """
    Sucht für jedes Modul passende Kurs-Pages in NEW KurseLearnWeb.
    Nutzt MODULE_LW_ALIASES (Kurs-Kürzel statt ModNum).
    Gibt {modulname: [kurs_page_id, ...]} zurück.
    """
    result: dict[str, list[str]] = {}

    for module, aliases in modules.items():
        found_ids: list[str] = []

        for alias in aliases:
            filter_body = {
                "property": "title",
                "title": {"contains": alias},
            }
            pages = query_data_source(KURSELEARNWEB_DS_ID, filter_body=filter_body)
            for page in pages:
                if page["id"] not in found_ids:
                    found_ids.append(page["id"])

        if found_ids:
            log.info("Modul %r: %d Kurs-Treffer in KurseLearnWeb", module, len(found_ids))
        else:
            log.warning("Modul %r: kein Kurs in KurseLearnWeb gefunden – wird übersprungen", module)

        result[module] = found_ids

    return result


# ── Relations lesen, mergen, schreiben ────────────────────────────────────────

def current_relation(page_id: str, prop_id: str) -> list[str]:
    """
    Liest bestehende Relation-IDs einer Page paginiert.
    GET /v1/pages/{page_id}/properties/{prop_id} umgeht den 25-Item-Cap.
    """
    url = f"{NOTION_API}/pages/{page_id}/properties/{prop_id}"
    ids: list[str] = []
    cursor: str | None = None

    while True:
        params: dict = {"page_size": 100}
        if cursor:
            params["start_cursor"] = cursor
        resp = _notion_request("GET", url, headers=_headers(), params=params)
        data = resp.json()

        for item in data.get("results", []):
            rel = item.get("relation", {})
            if rel.get("id"):
                ids.append(rel["id"])

        if not data.get("has_more"):
            break
        cursor = data.get("next_cursor")

    return ids


def _get_relation_prop_id(page_id: str) -> str:
    """Gibt die interne Property-ID für RELATION_PROP zurück."""
    resp = _notion_request("GET", f"{NOTION_API}/pages/{page_id}", headers=_headers())
    props = resp.json().get("properties", {})
    prop = props.get(RELATION_PROP, {})
    return prop.get("id", RELATION_PROP)


def merge_relation(existing: list[str], new: list[str]) -> list[str]:
    """Set-Merge: keine Duplikate, bestehende Relations bleiben erhalten."""
    return list(set(existing) | set(new))


def update_module_relation(
    page_id: str,
    prop_id: str,
    existing_ids: list[str],
    new_ids: list[str],
    dry_run: bool,
) -> bool:
    """
    Merged existing + new Relation-IDs und schreibt sie zurück.
    Gibt True zurück wenn eine Änderung nötig war.
    """
    merged = merge_relation(existing_ids, new_ids)

    if set(existing_ids) == set(merged):
        log.info("  Page %s bereits korrekt gesetzt – überspringe", page_id)
        return False

    relation_payload = [{"id": rid} for rid in merged]

    if dry_run:
        log.info(
            "  [DRY-RUN] PATCH page %s → %s: %d IDs (%d neu)",
            page_id, RELATION_PROP, len(merged), len(merged) - len(existing_ids),
        )
    else:
        _notion_request(
            "PATCH",
            f"{NOTION_API}/pages/{page_id}",
            headers=_headers(),
            json={"properties": {RELATION_PROP: {"relation": relation_payload}}},
        )
        log.info(
            "  PATCH page %s → %s: %d IDs gesetzt (%d neu)",
            page_id, RELATION_PROP, len(merged), len(merged) - len(existing_ids),
        )

    return True


# ── Verify ─────────────────────────────────────────────────────────────────────

def verify_modulhandbuch() -> int:
    """Zählt Pages in MODULHANDBUCH mit nicht-leerer LearnWeb-Relation."""
    pages = query_data_source(MODULHANDBUCH_DS_ID)
    count = 0
    for page in pages:
        prop = page.get("properties", {}).get(RELATION_PROP, {})
        relation = prop.get("relation", [])
        has_more = prop.get("has_more", False)
        if relation or has_more:
            count += 1
    return count


# ── Pre-Flight Lock ────────────────────────────────────────────────────────────

def _preflight_lock(force: bool) -> None:
    """Verhindert Race Condition mit parallelem learnweb_sync.py."""
    try:
        out = subprocess.run(
            ["pgrep", "-f", "learnweb_sync.py"],
            capture_output=True, text=True
        )
        if out.returncode == 0 and out.stdout.strip():
            msg = f"learnweb_sync.py läuft bereits (PIDs: {out.stdout.strip()})"
            if force:
                log.warning("Pre-Flight Lock übersprungen (--force): %s", msg)
            else:
                log.error("CRITICAL: %s – Abbruch. Mit --force überspringen.", msg)
                sys.exit(2)
    except FileNotFoundError:
        pass

    if LOCKFILE_PATH.exists():
        msg = f"Lockfile existiert: {LOCKFILE_PATH}"
        if force:
            log.warning("Pre-Flight Lock übersprungen (--force): %s", msg)
        else:
            log.error("CRITICAL: %s – Abbruch. Mit --force überspringen.", msg)
            sys.exit(2)

    LOCKFILE_PATH.parent.mkdir(parents=True, exist_ok=True)
    LOCKFILE_PATH.write_text(str(os.getpid()))


def _cleanup_lock() -> None:
    """Entfernt das Lockfile."""
    try:
        LOCKFILE_PATH.unlink(missing_ok=True)
    except Exception:
        pass


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Setzt LearnWeb-Relation für Inf2/BWL2/WI2/QM2 im MODULHANDBUCH."
    )
    parser.add_argument("--apply", action="store_true",
                        help="Schreibt Änderungen nach Notion (Default: Dry-Run)")
    parser.add_argument("--force", action="store_true",
                        help="Überspringt Pre-Flight Lock-Check")
    args = parser.parse_args()
    dry_run = not args.apply

    if not NOTION_TOKEN:
        log.error("NOTION_TOKEN ist nicht gesetzt. .env prüfen.")
        sys.exit(1)

    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda s, f: (_cleanup_lock(), sys.exit(128 + s)))

    _preflight_lock(args.force)

    try:
        _run(dry_run)
    finally:
        _cleanup_lock()


def _run(dry_run: bool) -> None:
    mode = "DRY-RUN" if dry_run else "APPLY"
    log.info("=== fix_modulhandbuch_inf2_mapping — Modus: %s ===", mode)

    # 1. Schema-Inspektion (verifiziert MATCH_PROP und RELATION_PROP)
    _detect_match_prop(MODULHANDBUCH_DS_ID)

    # 2. Pre-Step: alle Einträge loggen (manuelle Verifikation der Aliases)
    _dump_all_titles()

    # 3. Modul-Pages finden (via ModNum = eindeutiger Code)
    log.info("=== Suche Modul-Pages in MODULHANDBUCH ===")
    module_pages = find_module_pages(MODULE_DB_ALIASES)

    # 4. Kurs-Pages finden (via Kurs-Kürzel in KurseLearnWeb)
    log.info("=== Suche Kurse in KurseLearnWeb ===")
    lw_courses = find_lw_courses(MODULE_LW_ALIASES)

    # 5. Relations setzen (Append, idempotent)
    log.info("=== Setze LearnWeb-Relations ===")
    pages_updated = 0
    pages_skipped = 0

    for module, page_ids in module_pages.items():
        kurs_ids = lw_courses.get(module, [])
        if not kurs_ids:
            log.warning("Modul %r: keine Kurs-IDs – überspringe", module)
            continue

        for page_id in page_ids:
            log.info("Modul %r → Page %s", module, page_id)
            prop_id = _get_relation_prop_id(page_id)
            existing = current_relation(page_id, prop_id)
            log.info("  Bestehende Relation-IDs: %d", len(existing))

            changed = update_module_relation(page_id, prop_id, existing, kurs_ids, dry_run)
            if changed:
                pages_updated += 1
            else:
                pages_skipped += 1

    log.info(
        "=== Ergebnis: %d aktualisiert, %d bereits korrekt ===",
        pages_updated, pages_skipped,
    )

    # 6. Verify
    log.info("=== Verify: MODULHANDBUCH-Abfrage ===")
    n = verify_modulhandbuch()
    log.info("Verify: %d Module mit LearnWeb-Relation (Soll: >= %d)", n, VERIFY_MIN_MODULES)

    if not dry_run and n < VERIFY_MIN_MODULES:
        log.error(
            "VERIFY FAILED: Nur %d Module gemappt (Soll >= %d).",
            n, VERIFY_MIN_MODULES,
        )
        sys.exit(1)

    if dry_run:
        log.info("Dry-Run abgeschlossen. Mit --apply zum Schreiben.")
    else:
        log.info("Erfolgreich abgeschlossen.")


if __name__ == "__main__":
    main()
