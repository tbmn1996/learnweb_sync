"""YouTube-Metadaten- und Subtitle-Parser für Learnweb-Transkriptions-Pipeline.

Reine Funktionen ohne Netzwerk oder System-Dependencies. BeautifulSoup + JSON/Regex
für robuste HTML/JSON/VTT-Analyse.
"""

from __future__ import annotations

import json
import re
from bs4 import BeautifulSoup
from .types import Segment


def parse_youtube_id(url: str) -> str | None:
    """Extrahiert die 11-stellige YouTube-Video-ID aus einer URL.

    Erkannte Formate:
    - https://youtu.be/<ID> (auch mit Query-Parametern)
    - https://www.youtube.com/watch?v=<ID>
    - https://www.youtube.com/embed/<ID>
    - https://www.youtube-nocookie.com/embed/<ID>
    - https://www.youtube.com/shorts/<ID>

    Eine gültige ID hat die Form [A-Za-z0-9_-]{11}.

    Args:
        url: Die zu analysierende YouTube-URL (oder beliebiger String).

    Returns:
        Die 11-stellige Video-ID als String, oder None wenn keine gültige ID gefunden.
    """
    # Eingabe-Sanitization: None oder leere Strings → None
    if not url or not isinstance(url, str):
        return None

    url = url.strip()
    if not url:
        return None

    # youtu.be/ID (mit oder ohne Query-Parametern wie ?t=...)
    # Video-IDs sind genau 11 Zeichen: [A-Za-z0-9_-]{11}
    # Negative Lookahead nach der ID stellt sicher, dass nicht mehr Video-ID-Zeichen folgen
    match = re.search(r"youtu\.be/([A-Za-z0-9_-]{11})(?![A-Za-z0-9_-])", url)
    if match:
        return match.group(1)

    # youtube.com/watch?v=ID (v-Parameter, weitere Parameter ignoriert)
    match = re.search(r"youtube\.com/watch\?.*?v=([A-Za-z0-9_-]{11})(?![A-Za-z0-9_-])", url)
    if match:
        return match.group(1)

    # youtube.com/embed/ID
    match = re.search(r"youtube\.com/embed/([A-Za-z0-9_-]{11})(?![A-Za-z0-9_-])", url)
    if match:
        return match.group(1)

    # youtube-nocookie.com/embed/ID
    match = re.search(r"youtube-nocookie\.com/embed/([A-Za-z0-9_-]{11})(?![A-Za-z0-9_-])", url)
    if match:
        return match.group(1)

    # youtube.com/shorts/ID
    match = re.search(r"youtube\.com/shorts/([A-Za-z0-9_-]{11})(?![A-Za-z0-9_-])", url)
    if match:
        return match.group(1)

    return None


def extract_youtube_links(html: str, *, base_url: str = "") -> list[dict]:
    """Extrahiert alle YouTube-Links aus HTML und dedupliziert sie.

    Sucht nach:
    - <a href="..."> mit YouTube-Links
    - <iframe src="..."> mit YouTube-Links

    Jeder Treffer wird mit parse_youtube_id validiert. Duplikate (nach video_id)
    werden entfernt; das erste Vorkommen behält seinen Titel.

    Args:
        html: Der HTML-String zum Parsen (oder None/leer → leere Liste).
        base_url: Aktuell ungenutzt, aber Teil der API-Signatur.

    Returns:
        Liste von dicts mit {
            "video_id": str (11-stellig),
            "url": str (kanonisch https://www.youtube.com/watch?v=<ID>),
            "title": str | None (Linktext oder iframe-title-Attribut, bereinigte Whitespace)
        }.
        Reihenfolge = erstes Auftreten im HTML. Dedupliziert per video_id.
    """
    # Eingabe-Sanitization
    if not html or not isinstance(html, str):
        return []

    html = html.strip()
    if not html:
        return []

    # Parser-Fehler robust abfangen (kaputtes HTML → leer vs. Exception)
    try:
        soup = BeautifulSoup(html, "html.parser")
    except Exception:
        return []

    # Deduplizierungs-Tracker: video_id → Position der ersten Nennung
    seen_ids = {}
    result = []

    # <a href="..."> durchsuchen
    for a_tag in soup.find_all("a"):
        href = a_tag.get("href")
        if not href:
            continue

        video_id = parse_youtube_id(href)
        if not video_id:
            continue

        # Duplikat-Check: nur wenn noch nicht gesehen
        if video_id in seen_ids:
            continue

        # Linktext bereinigen: mehrfache Whitespace→ein Leerzeichen, trim
        link_text = a_tag.get_text(" ", strip=True)
        link_text = " ".join(link_text.split())
        title = link_text if link_text else None

        seen_ids[video_id] = True
        result.append({
            "video_id": video_id,
            "url": f"https://www.youtube.com/watch?v={video_id}",
            "title": title,
        })

    # <iframe src="..."> durchsuchen
    for iframe in soup.find_all("iframe"):
        src = iframe.get("src")
        if not src:
            continue

        video_id = parse_youtube_id(src)
        if not video_id:
            continue

        # Duplikat-Check
        if video_id in seen_ids:
            continue

        # title-Attribut oder None
        title_attr = iframe.get("title")
        if title_attr:
            title_attr = " ".join(title_attr.split())
            title = title_attr if title_attr else None
        else:
            title = None

        seen_ids[video_id] = True
        result.append({
            "video_id": video_id,
            "url": f"https://www.youtube.com/watch?v={video_id}",
            "title": title,
        })

    return result


def parse_youtube_subtitles(raw: str, fmt: str) -> list[Segment]:
    """Parst YouTube-Untertitel in das Segment-Format.

    Unterstützte Formate:
    - "json3": YouTube JSON3-Format mit `events[].tStartMs`, `dDurationMs`, `segs[].utf8`
    - "vtt": WebVTT-Format mit Cue-Timings und Textblöcken

    Bei leerem/kaputtem Input oder unbekanntem Format → leere Liste (nie Exception).

    Args:
        raw: Der rohe Untertitel-String (JSON oder VTT).
        fmt: Das Format ("json3" oder "vtt").

    Returns:
        Liste von Segment(start: float, end: float, text: str) in Sekunden.
    """
    # Eingabe-Sanitization
    if not raw or not isinstance(raw, str):
        return []

    raw = raw.strip()
    if not raw:
        return []

    # Format-Dispatch
    if fmt == "json3":
        return _parse_json3_subtitles(raw)
    elif fmt == "vtt":
        return _parse_vtt_subtitles(raw)
    else:
        # Unbekanntes Format → leer (nicht Exception)
        return []


def _parse_json3_subtitles(raw: str) -> list[Segment]:
    """Parser für YouTube JSON3-Untertitel-Format.

    Erwartet:
    {
        "events": [
            {
                "tStartMs": 0,
                "dDurationMs": 5000,
                "segs": [{"utf8": "Text"}, ...]
            },
            ...
        ]
    }
    """
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        # Kaputtes JSON → leer
        return []

    if not isinstance(data, dict):
        return []

    events = data.get("events", [])
    if not isinstance(events, list):
        return []

    result = []

    for event in events:
        if not isinstance(event, dict):
            continue
        # Pflichtfelder prüfen
        if "tStartMs" not in event or "segs" not in event:
            continue

        try:
            start_ms = int(event["tStartMs"])
        except (TypeError, ValueError):
            continue

        # dDurationMs optional, Default 0
        duration_ms = 0
        if "dDurationMs" in event:
            try:
                duration_ms = int(event["dDurationMs"])
            except (TypeError, ValueError):
                pass

        # Text aus segs[] konkatenieren
        segs = event.get("segs", [])
        if not isinstance(segs, list):
            continue

        text_parts = []
        for seg in segs:
            if isinstance(seg, dict):
                utf8_text = seg.get("utf8", "")
                if utf8_text:
                    text_parts.append(utf8_text)

        text = "".join(text_parts).strip()
        if not text:
            continue  # Leere Segmente überspringen

        # Zeiten in Sekunden konvertieren
        start = start_ms / 1000.0
        end = (start_ms + duration_ms) / 1000.0

        result.append(Segment(start=start, end=end, text=text))

    return result


def _parse_vtt_subtitles(raw: str) -> list[Segment]:
    """Parser für WebVTT-Untertitel-Format.

    Erwartet:
    WEBVTT

    00:00:00.000 --> 00:00:05.000
    Textzeile 1
    Textzeile 2

    00:00:05.000 --> 00:00:10.000
    Nächstes Cue
    """
    lines = raw.split("\n")
    result = []

    # Timing-Regex: HH:MM:SS[.,]mmm --> HH:MM:SS[.,]mmm
    # Erlaubt Punkt oder Komma als Dezimal-Trennzeichen
    timing_regex = (
        r"(\d{2}):(\d{2}):(\d{2})[.,](\d{3})\s*-->\s*"
        r"(\d{2}):(\d{2}):(\d{2})[.,](\d{3})"
    )

    i = 0
    while i < len(lines):
        line = lines[i].strip()

        # Nach Timing-Zeile suchen
        match = re.match(timing_regex, line)
        if not match:
            i += 1
            continue

        # Timing aus Regex extrahieren
        h_start, m_start, s_start, ms_start = map(int, match.groups()[:4])
        h_end, m_end, s_end, ms_end = map(int, match.groups()[4:])

        start = h_start * 3600 + m_start * 60 + s_start + ms_start / 1000.0
        end = h_end * 3600 + m_end * 60 + s_end + ms_end / 1000.0

        # Textzeilen sammeln bis zur nächsten Leerzeile
        text_lines = []
        i += 1
        while i < len(lines):
            text_line = lines[i]
            # Leerzeile markiert Ende des Cues
            if not text_line.strip():
                break
            # Inline-Tags entfernen: alles zwischen < und >
            text_line = re.sub(r"<[^>]+>", "", text_line)
            # Whitespace normalisieren
            text_line = " ".join(text_line.split())
            if text_line:  # Leere Zeilen nicht hinzufügen
                text_lines.append(text_line)
            i += 1

        text = " ".join(text_lines).strip()
        if text:  # Nur nicht-leere Segmente
            result.append(Segment(start=start, end=end, text=text))

    return result
