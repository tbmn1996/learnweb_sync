"""Dedupe- und Zustandsautomat-Helfer für den Transkriptions-Worker.

Arbeitet auf einer SQLite-Tabelle `transcripts` mit atomarem Claiming,
Crash-/Resume-Sicherheit (alle Zwischenstände sofort persistiert) und
Doppelverarbeitungs-Schutz durch pessimistic locking (claimed_at).

Gültige status-Werte (in Fortschrittsreihenfolge):
  pending → claimed → downloaded → transcribed → inhalt_created →
  meeting_created → appending → done
  (oder failed als Endzustand bei Fehlern)
"""

from __future__ import annotations

import hashlib
import sqlite3
from datetime import datetime, timezone
from typing import Any

from .types import Recording


# ── Konstanten ────────────────────────────────────────────────────────────────

# Gültige Spalten (Whitelist gegen SQL-Injection + UnknownColumnError).
# Alle Spalten aus init_transcribe_schema, minus Primary Key (key) und die
# Zeitfelder (first_seen, updated_at, claimed_at), die vom Modul verwaltet werden.
UPDATABLE_COLUMNS = {
    "cmid",
    "course_id",
    "course_shortname",
    "episode_id",
    "title",
    "source_url",
    "recorded_at",
    "status",
    "failure_stage",
    "failure_reason",
    "media_path",
    "audio_path",
    "duration_seconds",
    "model",
    "content_page_id",
    "meeting_page_id",
    "body_sha256",
    "total_block_count",
    "appended_block_count",
    "attempts",
    "course_name",  # optional, nicht im Schema aber für Metadaten nützlich
}

VALID_STATUSES = {
    "pending",
    "claimed",
    "downloaded",
    "transcribed",
    "inhalt_created",
    "meeting_created",
    "appending",
    "done",
    "failed",
}


# ── Helper-Funktionen ─────────────────────────────────────────────────────────

def _now_utc_iso() -> str:
    """Liefert den aktuellen UTC-Zeitpunkt als ISO-8601-String (Z-Suffix)."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def recording_key(cmid: str, discriminator: str) -> str:
    """Bildet einen stabilen, eindeutigen Schlüssel für ein Recording.

    Args:
        cmid: Moodle course module ID.
        discriminator: episode_id oder media_url oder source_url (erste nicht-None).

    Returns:
        String in Form "{cmid}-{sha1[:12]}" für DB-Primary-Key.
    """
    sha = hashlib.sha1(discriminator.encode("utf-8")).hexdigest()[:12]
    return f"{cmid}-{sha}"


def discriminator_for(recording: Recording) -> str:
    """Wählt den stabilen Diskriminator aus einem Recording für recording_key().

    Reihenfolge: episode_id → media_url → source_url.
    """
    return recording.episode_id or recording.media_url or recording.source_url


# ── Manifest-Operationen (atomare Transaktionen) ──────────────────────────────

def upsert_pending(conn: sqlite3.Connection, recording: Recording, key: str) -> None:
    """INSERT OR IGNORE: Neue Zeile mit status='pending', oder existierende Zeile ignorieren.

    Dient als Dedupe: existiert bereits ein Record mit diesem key, wird NICHT überschrieben
    (das würde Fortschritt verlieren). Nur wenn der key völlig neu ist, wird eine Zeile
    mit status='pending' und Metadaten angelegt.

    Args:
        conn: SQLite-Verbindung (mit autocommit=False).
        recording: Recording-Objekt mit allen Eingabe-Feldern.
        key: Ausgabe von recording_key(cmid, discriminator).
    """
    now = _now_utc_iso()
    with conn:
        conn.execute(
            """
            INSERT OR IGNORE INTO transcripts (
                key, cmid, course_id, course_shortname, episode_id, title,
                source_url, recorded_at, status, first_seen, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                key,
                recording.cmid,
                recording.course_id,
                recording.course_shortname,
                recording.episode_id,
                recording.title,
                recording.source_url,
                recording.recorded_at,
                "pending",
                now,
                now,
            ),
        )


def claim(conn: sqlite3.Connection, key: str) -> bool:
    """ATOMARER CLAIM: Markiert einen Record als claimed, falls noch nicht claimed/done.

    Setzt status='claimed', claimed_at=now, updated_at=now, attempts=attempts+1.
    Dieser Status signalisiert: „dieser Record wird gerade vom Worker verarbeitet".

    Args:
        conn: SQLite-Verbindung (autocommit=False).
        key: Record-PK.

    Returns:
        True, wenn genau eine Zeile aktualisiert wurde (erfolgreich geclaimed).
        False, wenn der Record bereits claimed oder fertig ist (oder nicht existiert).
    """
    now = _now_utc_iso()
    with conn:
        cursor = conn.execute(
            """
            UPDATE transcripts
            SET status = ?, claimed_at = ?, updated_at = ?, attempts = attempts + 1
            WHERE key = ? AND status IN ('pending', 'failed')
            """,
            ("claimed", now, now, key),
        )
        return cursor.rowcount == 1


def set_status(
    conn: sqlite3.Connection,
    key: str,
    status: str,
    *,
    failure_stage: str | None = None,
    failure_reason: str | None = None,
    **fields: Any,
) -> None:
    """Setzt status + updated_at und beliebige weitere Spalten.

    Atomare Transaktion: status, failure_stage, failure_reason und alle Felder
    aus **fields werden sofort persistiert. Unbekannte Spalten werfen ValueError.

    Args:
        conn: SQLite-Verbindung.
        key: Record-PK.
        status: Einer aus VALID_STATUSES.
        failure_stage: Optional, wird nur bei status='failed' typischerweise gesetzt.
        failure_reason: Optional, wird nur bei status='failed' typischerweise gesetzt.
        **fields: Beliebige andere Spalten, z. B. content_page_id='abc123'.

    Raises:
        ValueError: Wenn status nicht gültig oder ein Feld unbekannt ist.
    """
    if status not in VALID_STATUSES:
        raise ValueError(f"status '{status}' is not in VALID_STATUSES: {VALID_STATUSES}")

    # Whitelist: bekannte Spalten erlauben, Rest ablehnen.
    unknown = set(fields.keys()) - UPDATABLE_COLUMNS
    if unknown:
        raise ValueError(f"Unknown columns: {unknown}")

    # Baue dynamisches UPDATE.
    now = _now_utc_iso()
    updates = ["status = ?", "updated_at = ?"]
    params: list[Any] = [status, now]

    if failure_stage is not None:
        updates.append("failure_stage = ?")
        params.append(failure_stage)
    if failure_reason is not None:
        updates.append("failure_reason = ?")
        params.append(failure_reason)

    for col in sorted(fields.keys()):  # sortiert für Determinismus in Tests
        updates.append(f"{col} = ?")
        params.append(fields[col])

    params.append(key)

    sql = f"UPDATE transcripts SET {', '.join(updates)} WHERE key = ?"
    with conn:
        conn.execute(sql, params)


def get(conn: sqlite3.Connection, key: str) -> dict[str, Any] | None:
    """Fetcht einen Record als dict (oder None falls nicht vorhanden).

    Args:
        conn: SQLite-Verbindung.
        key: Record-PK.

    Returns:
        dict mit allen Spalten, oder None.
    """
    # Lokal row_factory setzen um sqlite3.Row → dict zu casten.
    # Dies ist sicher weil wir nur lokal mit dem Ergebnis arbeiten.
    old_factory = conn.row_factory
    try:
        conn.row_factory = sqlite3.Row
        cursor = conn.execute("SELECT * FROM transcripts WHERE key = ?", (key,))
        row = cursor.fetchone()
        return dict(row) if row else None
    finally:
        conn.row_factory = old_factory


def is_done(conn: sqlite3.Connection, key: str) -> bool:
    """Prüft, ob ein Record im Endzustand ist (status IN ('done', 'failed')).

    Args:
        conn: SQLite-Verbindung.
        key: Record-PK.

    Returns:
        True, wenn status == 'done' oder 'failed'; False sonst.
    """
    row = conn.execute(
        "SELECT status FROM transcripts WHERE key = ?", (key,)
    ).fetchone()
    if not row:
        return False
    status = row[0]
    return status in ("done", "failed")


def reset_for_force(conn: sqlite3.Connection, key: str) -> None:
    """Setzt einen Record auf Anfang zurück für --force-Reruns.

    Dies ist für Fälle, wenn ein Record fehlgeschlagen ist und der Nutzer
    den Rerun erzwingen will: status → 'pending', Fehler gelöscht, Append-Fortschritt
    zurückgesetzt. Die Notion page_ids bleiben erhalten, damit der Aufrufer
    die Seite in-place ersetzen kann (keine Duplikate).

    Args:
        conn: SQLite-Verbindung.
        key: Record-PK.
    """
    now = _now_utc_iso()
    with conn:
        conn.execute(
            """
            UPDATE transcripts
            SET status = ?,
                failure_stage = NULL,
                failure_reason = NULL,
                appended_block_count = 0,
                body_sha256 = NULL,
                updated_at = ?
            WHERE key = ?
            """,
            ("pending", now, key),
        )
