"""Lädt Learnweb/Opencast-Aufzeichnungen über yt-dlp herunter und extrahiert Audio.

Authentifizierung über Moodle-Session-Cookies (requests.Session). yt-dlp deckt
direkte MP4 (pluginfile.php), HLS, Opencast und externe Player ab und streamt
zu Disk (umgeht RAM-Cap). Audio wird mit ffmpeg zu 16 kHz mono WAV extrahiert
— das von Whisper erwartete Format.
"""

import os
import subprocess
import tempfile
from pathlib import Path
from typing import Optional

from .types import Recording


# Konstanten für Kommandos (über env oder Defaults).
YT_DLP = os.getenv("YT_DLP_BIN", "yt-dlp")
FFMPEG = os.getenv("FFMPEG_BIN", "ffmpeg")
FFPROBE = os.getenv("FFPROBE_BIN", "ffprobe")

# Timeouts (großzügig für Netzwerk).
YT_DLP_TIMEOUT_S = 2 * 3600  # 2 Stunden für große Aufzeichnungen
FFMPEG_TIMEOUT_S = 30 * 60   # 30 Minuten für Audio-Extraktion
FFPROBE_TIMEOUT_S = 60        # 1 Minute für Duration-Abfrage

# User-Agent (muss der Session entsprechen).
USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"


def _session_cookies_to_netscape(session, path: Path) -> None:
    """Exportiert requests.Session-Cookies im Netscape-Cookie-Jar-Format.

    Der generierte Cookie-Jar kompatibel mit yt-dlp und curl. Jede Zeile:
    domain, flag, path, secure (0|1), expiry, name, value
    """
    lines = ["# Netscape HTTP Cookie File", ""]
    for cookie in session.cookies:
        # Standard-Netscape-Format: domain, flag, path, secure, expiry, name, value
        domain = cookie.domain or "example.com"
        path_field = cookie.path or "/"
        secure = "1" if cookie.secure else "0"
        expiry = str(int(cookie.expires)) if cookie.expires else "0"
        lines.append(f"{domain}\tTRUE\t{path_field}\t{secure}\t{expiry}\t{cookie.name}\t{cookie.value}")

    path.write_text("\n".join(lines))


def download_media(session, recording: Recording, dest_dir: Path) -> Path:
    """Lädt eine Aufzeichnung mit yt-dlp herunter (authentifiziert via Session-Cookies).

    Args:
        session: requests.Session mit Moodle-Session-Cookies
        recording: Recording-Objekt mit media_url oder source_url
        dest_dir: Zielverzeichnis für Download

    Returns:
        Path zur heruntergeladenen Mediendatei (größte Datei mit Präfix)

    Raises:
        RuntimeError: Bei yt-dlp-Fehler oder wenn keine Datei gefunden wird
    """
    dest_dir = Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)

    # Cookie-Datei erstellen (temporär, wird am Ende gelöscht).
    cookie_fd, cookie_path = tempfile.mkstemp(suffix=".cookies.txt")
    try:
        os.close(cookie_fd)  # Dateideskriptor schließen, Path für Schreiben öffnen.
        os.chmod(cookie_path, 0o600)  # Nur Owner lesbar (sensible Daten).

        cookie_path_obj = Path(cookie_path)
        _session_cookies_to_netscape(session, cookie_path_obj)

        # Basisname für Ausgabedatei (z.B. <cmid>).
        base_name = f"recording_{recording.cmid}"
        out_template = str(dest_dir / f"{base_name}.%(ext)s")

        # URL: Bevorzuge media_url (direkter Stream), fallback auf source_url (Seite).
        url = recording.media_url or recording.source_url
        if not url:
            raise RuntimeError("Recording hat keine media_url und keine source_url")

        # yt-dlp aufrufen.
        cmd = [
            YT_DLP,
            "--no-playlist",
            "--no-part",
            "--cookies", cookie_path,
            "--user-agent", USER_AGENT,
            "-f", "best",
            "-o", out_template,
            url,
        ]

        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=YT_DLP_TIMEOUT_S,
            )
        except subprocess.TimeoutExpired as e:
            raise RuntimeError(f"yt-dlp timeout nach {YT_DLP_TIMEOUT_S}s für {recording.title}")

        if result.returncode != 0:
            stderr_snippet = result.stderr[:500]  # Erste 500 Zeichen des Fehlers.
            raise RuntimeError(f"yt-dlp Fehler: {stderr_snippet}")

        # Finde die heruntergeladene Datei (größte Datei mit dem Präfix, ohne .part).
        candidates = [
            f for f in dest_dir.iterdir()
            if f.is_file()
            and f.name.startswith(f"{base_name}.")
            and not f.name.endswith(".part")
            and not f.name.endswith(".cookies.txt")
        ]

        if not candidates:
            raise RuntimeError(f"yt-dlp lieferte keine Datei für {recording.title} in {dest_dir}")

        # Größte Datei wählen.
        best_file = max(candidates, key=lambda f: f.stat().st_size)
        return best_file

    finally:
        # Cookie-Datei IMMER löschen (enthält Session-Token).
        try:
            Path(cookie_path).unlink()
        except FileNotFoundError:
            pass


def extract_audio(media_path: Path, dest_dir: Path) -> Path:
    """Extrahiert 16-kHz-Mono-PCM-WAV aus beliebiger Mediaquelle.

    Verwendet ffmpeg mit -vn (kein Video), -ac 1 (mono), -ar 16000 (16 kHz),
    -c:a pcm_s16le (16-Bit PCM) — das von Whisper erwartete Format.

    Args:
        media_path: Pfad zur Eingabedatei (Video oder Audio)
        dest_dir: Zielverzeichnis für WAV

    Returns:
        Path zur generierten WAV-Datei

    Raises:
        RuntimeError: Bei ffmpeg-Fehler
    """
    media_path = Path(media_path)
    dest_dir = Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)

    # WAV-Datei: selber Stamm wie Eingabedatei.
    wav_name = media_path.stem + ".wav"
    wav_path = dest_dir / wav_name

    cmd = [
        FFMPEG,
        "-y",  # Überschreibe vorhandene Datei.
        "-i", str(media_path),
        "-vn",         # Kein Video.
        "-ac", "1",    # Mono.
        "-ar", "16000", # 16 kHz.
        "-c:a", "pcm_s16le",  # 16-Bit PCM.
        str(wav_path),
    ]

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=FFMPEG_TIMEOUT_S,
        )
    except subprocess.TimeoutExpired as e:
        raise RuntimeError(f"ffmpeg timeout nach {FFMPEG_TIMEOUT_S}s für {media_path.name}")

    if result.returncode != 0:
        stderr_snippet = result.stderr[:500]
        raise RuntimeError(f"ffmpeg Fehler: {stderr_snippet}")

    if not wav_path.exists():
        raise RuntimeError(f"ffmpeg lieferte keine WAV-Datei: {wav_path}")

    return wav_path


def probe_duration(media_path: Path) -> Optional[float]:
    """Ermittelt die Dauer einer Mediendatei in Sekunden.

    Nutzt ffprobe mit `-show_entries format=duration` und parst den
    numerischen Wert. Gibt None bei Fehler zurück (z.B. unbekanntes Format).

    Args:
        media_path: Pfad zur Mediendatei

    Returns:
        Dauer in Sekunden (float), oder None bei Fehler
    """
    media_path = Path(media_path)

    cmd = [
        FFPROBE,
        "-v", "error",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        str(media_path),
    ]

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=FFPROBE_TIMEOUT_S,
        )
    except subprocess.TimeoutExpired:
        return None

    if result.returncode != 0:
        return None

    try:
        duration = float(result.stdout.strip())
        return duration if duration > 0 else None
    except ValueError:
        return None
