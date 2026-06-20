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
    """Eine in LearnWeb gefundene Opencast- oder YouTube-Aufzeichnung.

    `episode_id`/`media_url` dienen als stabiler Diskriminator für den Recording-Key
    (siehe ``manifest.recording_key``). `recorded_at` ist ein ISO-8601-Datum oder
    ``None`` – niemals aus dem Titel geraten (Plan §6).
    """

    cmid: str                          # Moodle course module ID der Quellaktivität
    title: str                         # Anzeigename der Aufzeichnung
    source_url: str                    # Opencast view-URL bzw. Seite, von der geladen wird
    course_id: str                     # LearnWeb course_id (numerisch, als String)
    episode_id: str | None = None      # Opencast-Episode-UUID bzw. YouTube-video_id, falls vorhanden
    media_url: str | None = None       # direkter Medien-/Stream-Link (HLS/MP4) bzw. YouTube-watch-URL
    recorded_at: str | None = None     # ISO-8601-Datum der Aufzeichnung oder None
    course_shortname: str | None = None
    course_name: str | None = None
    # Quelltyp der Aufzeichnung. Steuert die Acquisition-Phase in `_process_recording`:
    #   "opencast" → Download (yt-dlp + Cookies) → Audio → Whisper (Default, unverändert)
    #   "youtube"  → Untertitel zuerst (cookie-frei), sonst Audio + Whisper
    source_kind: str = "opencast"


@dataclass
class Segment:
    """Ein Transkript-Segment in Sekunden (vereinheitlicht über alle Whisper-Backends)."""

    start: float   # Startzeit in Sekunden
    end: float     # Endzeit in Sekunden
    text: str
