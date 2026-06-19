"""On-Device-Whisper-Transkription von Vorlesungsaudio (16 kHz mono WAV).

Backend-Wahl (entschieden, siehe Modul-Docstring von ``transcription``):

- ``mlx-whisper`` ist das primäre Backend auf Apple Silicon (nutzt die
  GPU/Neural Engine über MLX). Modell-Default: ``large-v3-turbo``.
- ``faster-whisper`` (CTranslate2-basiert) ist der plattformneutrale Fallback,
  z. B. wenn dieses Modul auf Intel-Macs oder Linux läuft, wo ``mlx_whisper``
  nicht installierbar ist.

Beide Pakete werden NICHT in diesem Modul installiert — sie müssen bereits in
der aktiven Umgebung vorhanden sein. Imports erfolgen lazy (innerhalb der
Funktionen), damit dieses Modul auch ohne installierte Whisper-Pakete
importierbar bleibt (z. B. für Unit-Tests von ``detect_backend`` oder der
Normalisierungs-Helfer mit gemockten Daten).
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

from .types import Segment

# Default-Modellnamen je Backend. "large-v3-turbo" ist der MLX-Modellname;
# faster-whisper (CTranslate2) hat aktuell kein eigenes "-turbo"-Release,
# weshalb dort auf "large-v3" zurückgefallen wird, falls der Aufrufer den
# MLX-spezifischen Namen unverändert durchreicht (siehe _resolve_model_name).
DEFAULT_MODEL = "large-v3-turbo"
_MLX_HF_REPO_PREFIX = "mlx-community/whisper-"


def _is_importable(module_name: str) -> bool:
    """Prüft, ob ein Modul importierbar ist, ohne es tatsächlich zu importieren.

    ``importlib.util.find_spec`` löst den Modulpfad auf, führt aber keinen
    Modulcode aus — günstiger als ein try/except um einen echten Import, und
    löst keine Seiteneffekte (z. B. Modell-Downloads) aus.
    """

    try:
        return importlib.util.find_spec(module_name) is not None
    except (ImportError, ValueError):
        # ValueError: kann bei manchen kaputten/halbinstallierten Paketen auftreten,
        # wenn der Spec-Finder auf ungültige __spec__-Metadaten trifft.
        return False


def detect_backend() -> str:
    """Ermittelt das verfügbare Whisper-Backend.

    Rückgabe: ``"mlx"`` wenn ``mlx_whisper`` importierbar ist (Apple Silicon
    bevorzugt), sonst ``"faster"`` wenn ``faster_whisper`` importierbar ist.
    Ist keines der beiden Pakete installiert, wird ein ``RuntimeError`` mit
    Installationshinweis ausgelöst — die Pipeline soll diesen Fehler nicht
    stillschweigend abfangen, sondern dem Nutzer die fehlende Abhängigkeit
    melden.
    """

    if _is_importable("mlx_whisper"):
        return "mlx"
    if _is_importable("faster_whisper"):
        return "faster"
    raise RuntimeError(
        "Kein Whisper-Backend installiert. Installiere entweder "
        "'mlx-whisper' (Apple Silicon, empfohlen) oder 'faster-whisper' "
        "(plattformneutraler Fallback) in der aktiven Umgebung."
    )


def _resolve_model_name(model: str, backend: str) -> str:
    """Bildet einen generischen Modellnamen auf den Backend-spezifischen Bezeichner ab.

    MLX erwartet ein Hugging-Face-Repo (``mlx-community/whisper-<model>``);
    faster-whisper erwartet einen CTranslate2-Modellnamen wie ``large-v3``.
    Da faster-whisper kein offizielles "-turbo"-Modell führt, wird dieser
    Suffix für das faster-Backend entfernt (sinnvoller Default statt Crash).
    """

    if backend == "mlx":
        # Falls der Aufrufer schon ein vollständiges HF-Repo übergibt, unverändert lassen.
        if "/" in model:
            return model
        return f"{_MLX_HF_REPO_PREFIX}{model}"

    # faster-whisper: "-turbo"-Suffix gibt es dort nicht → auf Basismodell mappen.
    if model.endswith("-turbo"):
        return model.removesuffix("-turbo")
    return model


def _normalize_mlx_segments(raw_segments: list) -> list[Segment]:
    """Normalisiert die von ``mlx_whisper.transcribe`` zurückgegebenen Segmente.

    mlx-whisper (intern openai-whisper-kompatibel) liefert eine Liste von
    Dicts mit Schlüsseln ``start``/``end``/``text`` bereits in Sekunden.
    Leere/reine Whitespace-Texte werden verworfen (keine Phantom-Segmente).
    """

    segments: list[Segment] = []
    for raw in raw_segments:
        text = str(raw.get("text", "")).strip()
        if not text:
            continue
        start = float(raw.get("start", 0.0))
        end = float(raw.get("end", 0.0))
        segments.append(Segment(start=start, end=end, text=text))
    return segments


def _normalize_faster_segments(raw_segments) -> list[Segment]:
    """Normalisiert den Segment-Generator von ``faster_whisper.WhisperModel.transcribe``.

    faster-whisper liefert einen Generator von ``Segment``-Objekten (eigener
    Typ der Bibliothek, nicht zu verwechseln mit unserem ``types.Segment``)
    mit Attributen ``.start``/``.end``/``.text`` in Sekunden. Der Generator
    wird hier VOLLSTÄNDIG konsumiert, da faster-whisper erst beim Iterieren
    tatsächlich transkribiert (lazy evaluation) — ohne vollständigen Konsum
    würde die Transkription nie laufen bzw. nur teilweise.
    """

    segments: list[Segment] = []
    for raw in raw_segments:
        text = str(raw.text).strip()
        if not text:
            continue
        segments.append(Segment(start=float(raw.start), end=float(raw.end), text=text))
    return segments


def _transcribe_mlx(audio_path: str, *, language: str, model: str) -> list[Segment]:
    """Führt die Transkription mit ``mlx_whisper`` aus (Apple-Silicon-Pfad)."""

    try:
        import mlx_whisper
    except ImportError as exc:  # pragma: no cover - hängt von lokaler Installation ab
        raise RuntimeError(
            "Backend 'mlx' angefordert, aber 'mlx_whisper' ist nicht installiert. "
            "Installiere das Paket oder nutze backend='faster'."
        ) from exc

    hf_repo = _resolve_model_name(model, "mlx")
    result = mlx_whisper.transcribe(audio_path, path_or_hf_repo=hf_repo, language=language)
    raw_segments = result.get("segments", []) if isinstance(result, dict) else []
    return _normalize_mlx_segments(raw_segments)


def _transcribe_faster(audio_path: str, *, language: str, model: str) -> list[Segment]:
    """Führt die Transkription mit ``faster_whisper`` aus (plattformneutraler Fallback)."""

    try:
        from faster_whisper import WhisperModel
    except ImportError as exc:  # pragma: no cover - hängt von lokaler Installation ab
        raise RuntimeError(
            "Backend 'faster' angefordert, aber 'faster-whisper' ist nicht installiert. "
            "Installiere das Paket oder nutze backend='mlx' (nur Apple Silicon)."
        ) from exc

    ct2_model = _resolve_model_name(model, "faster")
    # device="auto"/compute_type="auto" lassen CTranslate2 selbst entscheiden
    # (z. B. GPU+float16 falls verfügbar, sonst CPU+int8) — sinnvoller Default
    # ohne Annahmen über die Zielmaschine zu treffen.
    whisper_model = WhisperModel(ct2_model, device="auto", compute_type="auto")
    raw_segments, _info = whisper_model.transcribe(audio_path, language=language)
    # raw_segments ist ein Generator – _normalize_faster_segments konsumiert ihn vollständig.
    return _normalize_faster_segments(raw_segments)


def transcribe(
    audio_path: Path | str,
    *,
    language: str = "de",
    model: str = DEFAULT_MODEL,
    backend: str | None = None,
) -> tuple[list[Segment], str]:
    """Transkribiert eine Audiodatei lokal und liefert (Segmente, Backend-Label).

    Args:
        audio_path: Pfad zur Audiodatei (i. d. R. 16 kHz mono WAV, siehe
            Modul-Docstring). Wird defensiv zu ``str`` gecastet, da beide
            Whisper-Bibliotheken Dateipfade als String erwarten.
        language: Sprachcode für Whisper (Default "de" für deutsche Vorlesungen).
        model: Generischer Modellname (Default ``"large-v3-turbo"``). Wird je
            Backend über ``_resolve_model_name`` auf den konkreten Bezeichner
            abgebildet.
        backend: Erzwingt ein Backend ("mlx"/"faster"). Bei ``None`` wird
            automatisch über ``detect_backend`` ermittelt.

    Returns:
        Tuple aus (Segmentliste, Backend-Label). Das Label hat das Format
        ``"<backend>-whisper:<konkretes-modell>"``, z. B.
        ``"mlx-whisper:large-v3-turbo"`` oder ``"faster-whisper:large-v3"``.

    Bei stiller/leerer Audiodatei wird eine LEERE Segmentliste zurückgegeben
    (kein Fake-Segment) — die aufrufende Pipeline interpretiert das als Fehler
    bzw. behandelt es entsprechend, dieses Modul täuscht keinen Erfolg vor.
    """

    resolved_backend = backend if backend is not None else detect_backend()
    path_str = str(audio_path)

    if resolved_backend == "mlx":
        segments = _transcribe_mlx(path_str, language=language, model=model)
        label = f"mlx-whisper:{_resolve_model_name(model, 'mlx').removeprefix(_MLX_HF_REPO_PREFIX)}"
    elif resolved_backend == "faster":
        segments = _transcribe_faster(path_str, language=language, model=model)
        label = f"faster-whisper:{_resolve_model_name(model, 'faster')}"
    else:
        raise RuntimeError(
            f"Unbekanntes Backend '{resolved_backend}'. Erwartet 'mlx' oder 'faster'."
        )

    return segments, label
