"""Lokaler Transkriptions-Worker für LearnWeb-Vorlesungsaufzeichnungen (Opencast).

On-Device-Whisper (MLX primär, faster-whisper als Fallback). Wird ausschließlich
lokal auf dem Mac vom `transcribe`-Command genutzt; die Railway-Instanz lädt dieses
Paket nicht. Schnittstellen-Vertrag: ``transcription.types``.
"""

from .types import Recording, Segment

__all__ = ["Recording", "Segment"]
