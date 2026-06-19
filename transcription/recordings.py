r"""Findet Opencast-Aufzeichnungen (eLectures) zu einer LearnWeb-Aktivität.

Portierung von `tbmn-learnweb-connector` (TypeScript, Branch `codex/transkribierer`,
Datei `src/learnweb/parsers/recordings.ts`) nach Python. Verbindlicher Schnittstellen-
Vertrag siehe `transcription.types.Recording`.

Discovery-Befunde (Uni Münster LearnWeb, Stand Juni 2026, siehe TS-Original):
  - Aktivitäts-Typ "opencast" (mod_opencast, "eLectures Videos"): Die Listenseite
    `/mod/opencast/view.php?id=<cmid>` kann zwei grundverschiedene HTML-Formen liefern:
      1) NEUES Format: Eine Moodle-Aktivität entspricht direkt einer einzelnen Opencast-
         Episode. Die Player-Metadaten (Episode-UUID, Titel, Streams) stehen als
         JavaScript-Objektliteral `window.episode = {...};` direkt im HTML.
      2) ALTES Format: Eine Aktivität listet mehrere Episoden über Links der Form
         `/mod/opencast/view.php?id=<cmid>&e=<uuid>` auf (Episode-Listenseite). Die
         eigentlichen Stream-URLs müssten dafür von der jeweiligen Episode-Detailseite
         nachgeladen werden (`amd.init({...})`-Aufruf, JSON-escaped im <script>).
  - Die mp4-Stream-URLs liegen in beiden Formaten JSON-escaped vor (`https:\/\/...`)
    und werden öffentlich (ohne Moodle-Auth) von `ele-cdn.*` ausgeliefert.

Datums-Extraktion (`recorded_at`):
  Opencast-Episode-Metadaten kennen offiziell Felder wie `created` oder `start`
  (ISO-8601-Zeitstempel des Aufzeichnungsbeginns). In der Praxis liefern weder das
  alte Listenformat noch das neue `window.episode`-JSON der Uni-Münster-Instanz
  (Stand Juni 2026, siehe Fixtures `opencast-list.html` / `opencast-direct-episode.html`
  im TS-Connector) eines dieser Felder mit aus — `window.episode.metadata` enthält nur
  `id`, `title`, `duration`. Wir suchen daher defensiv nach `"created"` bzw. `"start"`
  als JSON-Schlüssel irgendwo im episode-Objekt (für den Fall, dass eine andere Instanz
  oder ein künftiger LearnWeb-Release das Feld doch mitliefert) und normalisieren den
  Wert nach ISO-8601. Wird kein solches Feld gefunden, ist `recorded_at = None` —
  es wird NIEMALS aus dem Titel oder sonstigen Heuristiken geraten (siehe Plan §6 /
  Interface-Vertrag in `types.py`).

Architektur-Prinzip (wie im TS-Original): reine Parse-Funktionen (HTML/JSON-String →
Daten) sind von der HTTP-Orchestrierung getrennt, damit sie offline gegen Fixtures
testbar sind. `discover_recordings()` ist der einzige Funktionsaufruf mit Netz-I/O.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from .types import Recording

logger = logging.getLogger(__name__)

# Medien-Endungen, die wir als Aufzeichnung/Transkriptionsquelle akzeptieren
# (Video oder Audio). Analog zu `isMediaUrl()` im TS-Original.
_MEDIA_EXT_RE = re.compile(
    r"\.(mp4|m4v|m4a|mp3|webm|mov|mkv|aac|wav|ogg|opus)(\?|#|$)",
    re.IGNORECASE,
)

# Findet das `window.episode = {...};`-Objektliteral im neuen Aktivitäts-Format.
# Non-greedy bis zum ersten `};` am Zeilenende des Statements — funktioniert,
# weil das JSON selbst kein literales "};" enthält (Opencast-Metadaten sind flach).
_WINDOW_EPISODE_RE = re.compile(r"window\.episode\s*=\s*(\{.*?\})\s*;", re.DOTALL)

# Findet JSON-escaped mp4-URLs (https:\/\/...concat.mp4) irgendwo im HTML/Script,
# unabhängig davon ob sie in window.episode oder einem amd.init(...)-Call stehen.
_ESCAPED_MP4_RE = re.compile(r'https?:\\?/\\?/[^\s"\'<>]+?\.mp4', re.IGNORECASE)

# Episode-UUID in Opencast-Links: /mod/opencast/view.php?id=<cmid>&e=<uuid>
_EPISODE_LINK_E_PARAM_RE = re.compile(r"[?&]e=([0-9a-fA-F-]{36})")

# Sprachumschalt-Links ("de"/"en") verweisen auf dieselbe Episode und müssen
# beim Parsen der alten Listenansicht übersprungen werden (Linktext ist nur "de"/"en").
_LANG_SWITCH_TEXT_RE = re.compile(r"^(de|en)$", re.IGNORECASE)


def is_media_url(url: str) -> bool:
    """Prüft, ob `url` auf eine von uns unterstützte Medien-Datei zeigt.

    Whitelist-Ansatz (Endung vor optionalem Query-String/Fragment), analog zur
    TS-Funktion `isMediaUrl()`. Bewusst keine Content-Type-Prüfung hier, da diese
    Funktion rein auf der URL-Struktur arbeitet (kein Netz-Zugriff).
    """
    if not url:
        return False
    return bool(_MEDIA_EXT_RE.search(url))


def _unescape_json_url(raw: str) -> str:
    """Entfernt JSON-Backslash-Escapes aus einer URL (`https:\\/\\/` → `https://`)."""
    return raw.replace("\\/", "/")


def _extract_mp4_urls(text: str) -> list[str]:
    """Sammelt alle (deduplizierten) mp4-URLs aus einem HTML-/Script-Fragment.

    Die Streams liegen je nach Opencast-Player-Variante JSON-escaped vor; wir
    matchen daher robust auf das escaped ODER unescaped Muster und normalisieren
    danach. Reihenfolge im Dokument bleibt erhalten (erster Treffer = bevorzugter
    Track, identisch zum TS-Original: "ein Track genügt, der Audiotrack ist in
    allen Renditions identisch").
    """
    seen: set[str] = set()
    urls: list[str] = []
    for match in _ESCAPED_MP4_RE.finditer(text):
        url = _unescape_json_url(match.group(0))
        if url not in seen:
            seen.add(url)
            urls.append(url)
    return urls


def _normalize_recorded_at(value: Any) -> str | None:
    """Normalisiert einen rohen Opencast-Zeitstempel nach ISO-8601 (Datum/Zeit).

    Akzeptiert ISO-8601-Strings (ggf. mit "Z"-Suffix) sowie Unix-Timestamps
    (int/float, Sekunden seit Epoche) – beide Formen kommen in Opencast-APIs vor.
    Gibt bei jedem Parse-Problem `None` zurück (niemals raten/crashen).
    """
    if value is None:
        return None
    try:
        if isinstance(value, (int, float)):
            return datetime.fromtimestamp(float(value), tz=timezone.utc).isoformat()
        if isinstance(value, str):
            text = value.strip()
            if not text:
                return None
            # Python <3.11-kompatibel: "Z"-Suffix manuell auf UTC-Offset abbilden.
            iso_text = text[:-1] + "+00:00" if text.endswith("Z") else text
            parsed = datetime.fromisoformat(iso_text)
            return parsed.isoformat()
    except (ValueError, OverflowError, OSError) as exc:
        logger.debug("recorded_at konnte nicht geparst werden (%r): %s", value, exc)
    return None


def _find_recorded_at(episode_obj: dict[str, Any] | None, raw_text: str) -> str | None:
    """Sucht ein Aufzeichnungsdatum in Opencast-Metadaten.

    Reihenfolge (erste vorhandene Quelle gewinnt):
      1. `episode_obj["metadata"]["created"]` bzw. `["start"]` (verschachtelt,
         wie im "metadata"-Knoten des neuen window.episode-Formats).
      2. `episode_obj["created"]` bzw. `episode_obj["start"]` (flach, falls die
         API das Feld direkt auf dem Episode-Objekt mitliefert statt verschachtelt).
      3. Regex-Fallback über den rohen Text, falls kein valides JSON geparst werden
         konnte (z. B. altes Listenformat ohne window.episode-Objekt).
    Liefert None, wenn nichts gefunden wird – wird NICHT aus dem Titel geraten.
    """
    candidates: list[Any] = []
    if episode_obj:
        metadata = episode_obj.get("metadata")
        if isinstance(metadata, dict):
            candidates.append(metadata.get("created"))
            candidates.append(metadata.get("start"))
        candidates.append(episode_obj.get("created"))
        candidates.append(episode_obj.get("start"))

    for candidate in candidates:
        normalized = _normalize_recorded_at(candidate)
        if normalized:
            return normalized

    # Fallback: rohes Regex-Matching auf "created"/"start" als JSON-Schlüssel,
    # falls das window.episode-Objekt nicht vollständig parsebar war.
    match = re.search(r'"(?:created|start)"\s*:\s*"([^"]+)"', raw_text)
    if match:
        normalized = _normalize_recorded_at(match.group(1))
        if normalized:
            return normalized
    return None


def _parse_window_episode(html: str, *, base_url: str) -> list[dict[str, Any]]:
    """Parst das NEUE Format: eine Aktivität == eine Episode, Daten in `window.episode`.

    Liefert eine Liste mit höchstens einem dict (oder leer, falls kein
    `window.episode`-Objekt gefunden wird oder kein mp4-Stream ableitbar ist).
    """
    match = _WINDOW_EPISODE_RE.search(html)
    if not match:
        return []

    raw_json = match.group(1)
    episode_obj: dict[str, Any] | None = None
    try:
        episode_obj = json.loads(raw_json)
    except json.JSONDecodeError as exc:
        # Defensiv: JSON kann z. B. durch Trunkierung in der Fixture/Seite kaputt sein.
        # Wir fahren trotzdem fort und versuchen Stream-URLs per Regex zu finden.
        logger.debug("window.episode-JSON nicht parsebar: %s", exc)

    mp4_urls = _extract_mp4_urls(raw_json) or _extract_mp4_urls(html)
    if not mp4_urls:
        return []

    episode_id: str | None = None
    title: str | None = None
    if episode_obj:
        metadata = episode_obj.get("metadata")
        if isinstance(metadata, dict):
            episode_id = metadata.get("id")
            title = metadata.get("title")
        episode_id = episode_id or episode_obj.get("id")
        title = title or episode_obj.get("title")

    if isinstance(episode_id, str):
        episode_id = episode_id.lower()

    return [
        {
            "episode_id": episode_id,
            "title": title or None,
            "media_url": mp4_urls[0],
            "recorded_at": _find_recorded_at(episode_obj, raw_json),
            "source_url": base_url or None,
        }
    ]


def _parse_legacy_episode_list(html: str, *, base_url: str) -> list[dict[str, Any]]:
    """Parst das ALTE Format: Tabellenliste mit `&e=<uuid>`-Links zu Episoden.

    Liefert pro gefundener Episode ein dict OHNE `media_url` (die echte Stream-URL
    liegt erst auf der jeweiligen Episode-Detailseite – siehe `discover_recordings`,
    welches diese Funktion mit dem Listen-HTML aufruft und danach pro Episode
    optional die Detailseite nachlädt). `source_url` zeigt auf den jeweiligen
    Episode-Detail-Link (inkl. `&e=<uuid>`), analog zu `detailUrl` im TS-Original.
    """
    soup = BeautifulSoup(html, "html.parser")
    episodes: list[dict[str, Any]] = []
    seen_ids: set[str] = set()

    for anchor in soup.find_all("a", href=True):
        href = anchor["href"]
        if "/mod/opencast/view.php" not in href:
            continue
        # HTML-Entities (&amp;) können bereits von bs4 dekodiert sein; trotzdem
        # defensiv normalisieren, falls roh durchgereichter Text vorliegt.
        normalized_href = href.replace("&amp;", "&")
        id_match = _EPISODE_LINK_E_PARAM_RE.search(normalized_href)
        if not id_match:
            continue
        episode_id = id_match.group(1).lower()
        if episode_id in seen_ids:
            continue

        link_text = " ".join(anchor.get_text().split())
        if _LANG_SWITCH_TEXT_RE.match(link_text):
            # de/en-Sprachumschalter auf dieselbe Episode – kein eigener Eintrag.
            continue

        seen_ids.add(episode_id)
        detail_url = urljoin(base_url, normalized_href) if base_url else normalized_href
        episodes.append(
            {
                "episode_id": episode_id,
                "title": link_text or f"Episode {episode_id[:8]}",
                "media_url": None,
                "recorded_at": None,
                "source_url": detail_url,
            }
        )

    return episodes


def parse_opencast_episodes(html: str, *, base_url: str = "") -> list[dict]:
    """Parst eine Opencast-View-Seite und liefert gefundene Episoden als dicts.

    Unterstützt beide bekannten LearnWeb-HTML-Formate (siehe Modul-Docstring):
      - NEU: `window.episode = {...};` (eine Aktivität == eine Episode).
      - ALT: Tabellenliste mehrerer `&e=<uuid>`-Links (eine Aktivität == mehrere
        Episoden, Stream-URL muss separat von der Detailseite geladen werden).

    Jedes zurückgegebene dict enthält mindestens die Schlüssel:
      `episode_id`, `title`, `media_url` (None, falls nicht direkt ableitbar –
      z. B. im alten Format vor dem Nachladen der Detailseite), `recorded_at`
      (ISO-8601 oder None, siehe `_find_recorded_at`), `source_url`.

    Reihenfolge der Versuche: zuerst das neue Format (spezifischer, da es ohne
    HTML-Tabellenstruktur funktioniert), erst danach Fallback auf das alte
    Listenformat – identisch zur Priorisierung in `extractOpencast()` im
    TS-Original.
    """
    if not html:
        return []

    try:
        direct = _parse_window_episode(html, base_url=base_url)
        if direct:
            return direct
    except Exception as exc:  # defensiv: Parse-Fehler dürfen nie crashen
        logger.warning("Parsen von window.episode fehlgeschlagen: %s", exc)

    try:
        return _parse_legacy_episode_list(html, base_url=base_url)
    except Exception as exc:  # defensiv: Parse-Fehler dürfen nie crashen
        logger.warning("Parsen der Opencast-Episodenliste fehlgeschlagen: %s", exc)
        return []


def _build_recording(
    episode: dict[str, Any],
    *,
    cmid: str,
    course_id: str,
    view_url: str,
    course_shortname: str | None,
    course_name: str | None,
) -> Recording:
    """Baut ein `Recording`-Objekt aus einem von `parse_opencast_episodes` gelieferten dict."""
    title = episode.get("title") or f"Aufzeichnung {cmid}"
    return Recording(
        cmid=cmid,
        title=title,
        source_url=episode.get("source_url") or view_url,
        course_id=course_id,
        episode_id=episode.get("episode_id"),
        media_url=episode.get("media_url"),
        recorded_at=episode.get("recorded_at"),
        course_shortname=course_shortname,
        course_name=course_name,
    )


def discover_recordings(
    session: "requests.Session",  # noqa: F821 - nur als Typ-Hinweis, kein Hard-Import nötig
    view_url: str,
    *,
    cmid: str,
    course_id: str,
    course_shortname: str | None = None,
    course_name: str | None = None,
) -> list[Recording]:
    """Lädt die Opencast-View-Seite und löst sie in `Recording`-Objekte auf.

    Netzgebundener Wrapper um `parse_opencast_episodes()`. Für das ALTE Listen-
    format (mehrere Episoden ohne direkte Stream-URL) wird zusätzlich pro
    gefundener Episode die jeweilige Detailseite (`view_url` + `&e=<uuid>`)
    nachgeladen, um die mp4-URL aufzulösen – analog zu `extractOpencast()` im
    TS-Original. Schlägt das Nachladen einzelner Episoden fehl, wird die
    betroffene Episode übersprungen statt die gesamte Funktion abzubrechen.

    Defensiv: Bei Netzwerk-Fehlern (Timeout, Connection-Error, Non-2xx-Status)
    oder Parse-Fehlern wird eine leere Liste zurückgegeben – niemals eine
    Exception nach außen geworfen, damit ein einzelner defekter Kurs den
    gesamten Scan-Lauf nicht abbricht.
    """
    try:
        response = session.get(view_url, timeout=30)
    except Exception as exc:  # requests.RequestException u. a. – defensiv breit gefasst
        logger.warning("Opencast-View-Seite nicht erreichbar (%s): %s", view_url, exc)
        return []

    if response.status_code < 200 or response.status_code >= 300:
        logger.warning(
            "Opencast-View-Seite lieferte Status %s (%s)", response.status_code, view_url
        )
        return []

    try:
        episodes = parse_opencast_episodes(response.text, base_url=view_url)
    except Exception as exc:  # defensiv: Parser darf den Scan nie crashen
        logger.warning("Opencast-Episoden konnten nicht geparst werden (%s): %s", view_url, exc)
        return []

    recordings: list[Recording] = []
    for episode in episodes:
        # Im neuen Format ist media_url bereits gesetzt → direkt übernehmen.
        if episode.get("media_url"):
            recordings.append(
                _build_recording(
                    episode,
                    cmid=cmid,
                    course_id=course_id,
                    view_url=view_url,
                    course_shortname=course_shortname,
                    course_name=course_name,
                )
            )
            continue

        # Altes Format: Detailseite der einzelnen Episode nachladen, um die
        # mp4-Stream-URL aufzulösen. Fehler bei einer einzelnen Episode führen
        # nur zum Überspringen dieser Episode, nicht zum Abbruch des Laufs.
        detail_url = episode.get("source_url") or view_url
        try:
            detail_response = session.get(detail_url, timeout=30)
        except Exception as exc:
            logger.warning("Episode-Detailseite nicht erreichbar (%s): %s", detail_url, exc)
            continue

        if detail_response.status_code < 200 or detail_response.status_code >= 300:
            logger.warning(
                "Episode-Detailseite lieferte Status %s (%s)",
                detail_response.status_code,
                detail_url,
            )
            continue

        try:
            detail_episodes = _parse_window_episode(detail_response.text, base_url=detail_url)
            if not detail_episodes:
                # Die Detailseite einer einzelnen alten Episode nutzt typischerweise
                # ein amd.init({...})-Script statt window.episode – wir extrahieren
                # daher zusätzlich direkt die mp4-URLs aus dem Roh-HTML als Fallback.
                mp4_urls = _extract_mp4_urls(detail_response.text)
                if not mp4_urls:
                    continue
                episode["media_url"] = mp4_urls[0]
                episode["recorded_at"] = episode.get("recorded_at") or _find_recorded_at(
                    None, detail_response.text
                )
            else:
                detail = detail_episodes[0]
                episode["media_url"] = detail.get("media_url")
                episode["recorded_at"] = episode.get("recorded_at") or detail.get("recorded_at")
                episode["title"] = episode.get("title") or detail.get("title")
        except Exception as exc:
            logger.warning("Episode-Detailseite konnte nicht geparst werden (%s): %s", detail_url, exc)
            continue

        if not episode.get("media_url"):
            continue

        recordings.append(
            _build_recording(
                episode,
                cmid=cmid,
                course_id=course_id,
                view_url=view_url,
                course_shortname=course_shortname,
                course_name=course_name,
            )
        )

    return recordings
