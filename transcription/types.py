"""Gemeinsame Datenstrukturen des Transkriptions-Workers (Schnittstellen-Vertrag).

Diese Datei ist der verbindliche Vertrag zwischen den ansonsten unabhängigen
Modulen des `transcription`-Pakets (recordings, downloader, transcriber,
notion_blocks, manifest) und der Pipeline in `learnweb_sync.py`. Wer ein Modul
ändert, hält sich an die hier definierten Felder/Typen.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class Recording:
    """Eine in LearnWeb gefundene Aufzeichnung (eine Opencast-Episode bzw. Mediendatei).

    `episode_id`/`media_url` dienen als stabiler Diskriminator für den Recording-Key
    (siehe ``manifest.recording_key``). `recorded_at` ist ein ISO-8601-Datum oder
    ``None`` – niemals aus dem Titel geraten (Plan §6).
    """

    cmid: str                          # Moodle course module ID der opencast-Aktivität
    title: str                         # Anzeigename der Aufzeichnung
    source_url: str                    # Opencast view-URL bzw. Seite, von der geladen wird
    course_id: str                     # LearnWeb course_id (numerisch, als String)
    episode_id: str | None = None      # Opencast-Episode-UUID, falls vorhanden
    media_url: str | None = None       # direkter Medien-/Stream-Link (HLS/MP4), falls bekannt
    recorded_at: str | None = None     # ISO-8601-Datum der Aufzeichnung oder None
    course_shortname: str | None = None
    course_name: str | None = None


@dataclass
class Segment:
    """Ein Transkript-Segment in Sekunden (vereinheitlicht über alle Whisper-Backends)."""

    start: float   # Startzeit in Sekunden
    end: float     # Endzeit in Sekunden
    text: str
