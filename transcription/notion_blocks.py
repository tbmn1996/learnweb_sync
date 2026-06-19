"""Wandelt Transkript-Segmente in Notion-paragraph-Blöcke um.

Notion-Limits: max 2000 Zeichen pro rich_text-Objekt. Absätze werden alle ~30 s
gebildet, jeder Absatz beginnt mit einem fetten Timestamp [HH:MM:SS]. Der Whisper-
Text bleibt SEMANTISCH UNVERÄNDERT (nur Absatzbildung + Timestamp, keine Glättung).
"""

from __future__ import annotations

from .types import Segment


def format_timestamp(seconds: float) -> str:
    """Formatiert Sekunden als 'HH:MM:SS' (Stunden ohne Nullpad-Begrenzung).

    Args:
        seconds: Zeit in Sekunden (kann 0.0 oder Bruch sein)

    Returns:
        String im Format "HH:MM:SS" oder "MM:SS" (wenn < 1 Stunde)
    """
    total = max(0, int(seconds))
    hours = total // 3600
    minutes = (total % 3600) // 60
    secs = total % 60

    if hours > 0:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes:02d}:{secs:02d}"


def group_paragraphs(
    segments: list[Segment],
    paragraph_seconds: float = 30.0,
) -> list[tuple[float, str]]:
    """Gruppiert aufeinanderfolgende Segmente zu Absätzen.

    Segmente werden gesammelt, bis seit dem Absatzbeginn `paragraph_seconds`
    überschritten sind. Leere/Whitespace-Segmente werden übersprungen.

    Args:
        segments: Liste von Segment-Objekten (mit start, end, text)
        paragraph_seconds: Sekundenschwelle pro Absatz (Default 30.0)

    Returns:
        Liste von (start_seconds, paragraph_text) Tupeln
    """
    paragraphs: list[tuple[float, str]] = []
    current_start: float | None = None
    current_parts: list[str] = []

    for seg in segments:
        # Whitespace-Segmente überspringen
        if not seg.text or not seg.text.strip():
            continue

        # Neuer Absatz initialisieren
        if current_start is None:
            current_start = seg.start
            current_parts = [seg.text]
            continue

        # Prüfen, ob wir die Schwelle überschreiten würden
        would_exceed = seg.end - current_start >= paragraph_seconds
        current_parts.append(seg.text)

        if would_exceed:
            # Absatz abschließen
            paragraph_text = " ".join(current_parts)
            paragraphs.append((current_start, paragraph_text))
            current_start = None
            current_parts = []

    # Letzter Absatz (falls vorhanden)
    if current_start is not None:
        paragraph_text = " ".join(current_parts)
        paragraphs.append((current_start, paragraph_text))

    return paragraphs


def build_transcript_blocks(
    segments: list[Segment],
    *,
    paragraph_seconds: float = 30.0,
    with_timestamps: bool = True,
    max_chars: int = 1900,
) -> list[dict]:
    """Erstellt Notion-paragraph-Blöcke aus Transkript-Segmenten.

    Jeder Block entspricht einem Notion paragraph mit rich_text-Array. Wenn
    with_timestamps=True, beginnt jeder Block mit einem fetten Timestamp
    [HH:MM:SS] als erstes rich_text-Objekt, danach der Textinhalt.

    Lange Absätze werden an Wortgrenzen aufgeteilt (Sicherheitsmarge max_chars).
    Das erste Teilstück erhält den Timestamp-Prefix, Folgestücke nicht.

    Args:
        segments: Liste von Segment-Objekten
        paragraph_seconds: Gruppierungsschwelle (Default 30.0)
        with_timestamps: Timestamps voranstellen (Default True)
        max_chars: Max. Zeichen pro Textinhalt (Default 1900 für 2000er-Limit)

    Returns:
        Liste von Notion-Block-Dicts im Format:
        {"type": "paragraph", "paragraph": {"rich_text": [...]}}
    """
    blocks: list[dict] = []
    paragraphs = group_paragraphs(segments, paragraph_seconds)

    for start_seconds, paragraph_text in paragraphs:
        if with_timestamps:
            timestamp_str = format_timestamp(start_seconds)
            timestamp_prefix = f"[{timestamp_str}] "

            # Textinhalt mit Zeitstempel splitten
            chunks = _split_text_chunks(
                paragraph_text,
                max_chars,
                include_prefix_first=True,
                prefix=timestamp_prefix,
            )

            for i, chunk in enumerate(chunks):
                rich_text: list[dict] = []

                # Erstes Chunk: Timestamp bold + Text
                if i == 0:
                    rich_text.append({
                        "type": "text",
                        "text": {"content": timestamp_prefix},
                        "annotations": {"bold": True},
                    })
                    # Der Chunk ist bereits reiner Text (der Prefix wurde in
                    # _split_text_chunks nur für die Längenrechnung berücksichtigt,
                    # nicht in den Chunk eingefügt) → vollständig übernehmen.
                    text_part = chunk
                    if text_part:
                        rich_text.append({
                            "type": "text",
                            "text": {"content": text_part},
                        })
                # Folgende Chunks: nur Text
                else:
                    rich_text.append({
                        "type": "text",
                        "text": {"content": chunk},
                    })

                if rich_text:  # Nur wenn nicht leer
                    block: dict = {
                        "type": "paragraph",
                        "paragraph": {"rich_text": rich_text},
                    }
                    blocks.append(block)
        else:
            # Ohne Timestamps: einfach in Chunks aufteilen
            chunks = _split_text_chunks(paragraph_text, max_chars)

            for chunk in chunks:
                if chunk.strip():  # Nur nicht-leere Chunks
                    block: dict = {
                        "type": "paragraph",
                        "paragraph": {
                            "rich_text": [
                                {
                                    "type": "text",
                                    "text": {"content": chunk},
                                }
                            ]
                        },
                    }
                    blocks.append(block)

    return blocks


def _split_text_chunks(
    text: str,
    max_chars: int,
    include_prefix_first: bool = False,
    prefix: str = "",
) -> list[str]:
    """Teilt Text an Wortgrenzen auf, respektiert max_chars pro Chunk.

    Args:
        text: Zu teilender Text
        max_chars: Max. Zeichen pro Chunk
        include_prefix_first: Wenn True, ist prefix im ersten Chunk enthalten
        prefix: Präfix-String (nur relevant wenn include_prefix_first=True)

    Returns:
        Liste von Text-Chunks, jeder ≤ max_chars
    """
    if not text or not text.strip():
        return []

    chunks: list[str] = []
    words = text.split()

    current_chunk = ""
    for word in words:
        # Berechne tatsächliche Grenze für erstes Chunk (mit Präfix)
        if not chunks and include_prefix_first:
            test_str = prefix + current_chunk + (" " if current_chunk else "") + word
            would_exceed = len(test_str) > max_chars
        else:
            test_str = current_chunk + (" " if current_chunk else "") + word
            would_exceed = len(test_str) > max_chars

        if would_exceed and current_chunk:
            # Aktuelles Chunk speichern, neues beginnen
            chunks.append(current_chunk)
            current_chunk = word
        else:
            # Wort zum Chunk hinzufügen
            if current_chunk:
                current_chunk += " " + word
            else:
                current_chunk = word

    # Letztes Chunk (falls vorhanden)
    if current_chunk:
        chunks.append(current_chunk)

    # Fallback: Wenn ein Wort länger als max_chars ist, hart schneiden
    result: list[str] = []
    for chunk in chunks:
        if len(chunk) <= max_chars:
            result.append(chunk)
        else:
            # Hart schneiden (sollte selten vorkommen)
            pos = 0
            while pos < len(chunk):
                result.append(chunk[pos : pos + max_chars])
                pos += max_chars

    return result
