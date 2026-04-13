"""
server.py — Railway-Orchestrator für learnweb_sync.

Stellt zwei HTTP-Endpunkte bereit:
  GET  /health         — Deploy-Gating + Betriebsdiagnose
  POST /webhook/sync   — Manueller Trigger (z.B. GitHub Actions Fallback)

Sync-Läufe werden als Subprozess gestartet (learnweb_sync.py run).
server.py schreibt selbst nie in state.db.

Parallelisierungsschutz:
  - threading.Lock  → primärer in-process Guard (Webhook ↔ Scheduler)
  - fcntl.flock     → sekundärer Guard gegen versehentliche Mehrfach-Prozesse

Gunicorn muss mit genau einem Worker gestartet werden, damit der
BackgroundScheduler nur einmal läuft:
  gunicorn server:app --workers 1 --threads 4 --bind 0.0.0.0:$PORT
"""

import fcntl
import logging
import os
import subprocess
import sys
import threading
from pathlib import Path

from apscheduler.schedulers.background import BackgroundScheduler
from flask import Flask, jsonify, request


# ── Konfiguration aus Env ──────────────────────────────────────────────────────

# Pfad zur SQLite-Datenbank; auf Railway via Railway-Volume auf /data gemountet.
STATE_DB_PATH = os.getenv("STATE_DB_PATH", "/data/state.db")

# Separate Lock-Datei für prozessübergreifenden flock-Schutz.
STATE_LOCK_PATH = os.getenv("STATE_LOCK_PATH", "/data/state.lock")

# Bearer-Token für den Webhook-Endpunkt. Wenn leer, wird keine Auth geprüft
# (nur für lokale Tests akzeptabel – in Produktion immer setzen).
WEBHOOK_SECRET = os.getenv("SYNC_WEBHOOK_SECRET", "")

# Cron-Ausdruck im Standard-Format "min stunde tag monat wochentag".
# Default: Mo–Fr 06:00 UTC (entspricht dem bisherigen GitHub-Actions-Zeitplan).
CRON_SCHEDULE = os.getenv("CRON_SCHEDULE", "0 6 * * 1-5")

# Zeitzone für den APScheduler-Cron.
TIMEZONE = os.getenv("SYNC_SCHEDULE_TIMEZONE", "UTC")

# Maximale Laufzeit eines Sync-Subprozesses in Sekunden.
TIMEOUT = int(os.getenv("SYNC_RUN_TIMEOUT_SECONDS", "1800"))  # 30 Minuten

# Ab dieser Anzahl aufeinanderfolgender Fehler beendet sich der Prozess,
# damit Railway über die Restart-Policy neu startet.
FAILURE_THRESH = int(os.getenv("SYNC_FAILURE_EXIT_THRESHOLD", "3"))


# ── In-process State ───────────────────────────────────────────────────────────

# Primärer Guard: verhindert parallele Sync-Starts innerhalb desselben Prozesses.
# Lock.acquire(blocking=False) gibt False zurück wenn bereits gehalten → 409.
_sync_lock = threading.Lock()

# Zähler für aufeinanderfolgende Fehler; wird bei Erfolg auf 0 zurückgesetzt.
_consecutive_failures = 0


# ── Flask-App + Logging ────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)s  %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger(__name__)

app = Flask(__name__)


# ── Startup-Validierung ────────────────────────────────────────────────────────

# Pflicht-Env-Variablen, die der Sync-Subprozess benötigt.
_REQUIRED_ENV = ["LEARNWEB_URL", "LEARNWEB_USERNAME", "LEARNWEB_PASSWORD"]


def _check_config() -> list[str]:
    """Gibt eine Liste von Konfigurationsproblemen zurück (leer = alles ok)."""
    issues = []
    for var in _REQUIRED_ENV:
        if not os.getenv(var):
            issues.append(f"Env '{var}' fehlt")
    if not Path(STATE_DB_PATH).parent.exists():
        issues.append(
            f"Volume-Pfad '{Path(STATE_DB_PATH).parent}' nicht vorhanden — "
            "Railway-Volume gemountet?"
        )
    return issues


# ── Sync-Ausführung ────────────────────────────────────────────────────────────

def _run_sync() -> bool:
    """
    Führt learnweb_sync.py run als Subprozess aus.

    Hält dabei einen fcntl-Lock auf STATE_LOCK_PATH, um versehentliche
    Mehrfach-Prozesse (z.B. lokaler Test parallel zu Railway) zu blockieren.

    Gibt True bei Erfolg zurück, False bei Fehler oder Timeout.
    Bei Erreichen von FAILURE_THRESH aufeinanderfolgenden Fehlern
    beendet sich der Prozess mit exit(1) für Railway-Restart.
    """
    global _consecutive_failures

    # Sekundärer Guard: prozessübergreifende Lock-Datei.
    lock_path = Path(STATE_LOCK_PATH)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_fd = lock_path.open("w")

    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        log.warning("fcntl-Lock bereits belegt — externer Prozess aktiv?")
        lock_fd.close()
        return False

    try:
        log.info("Starte Sync-Subprozess …")
        result = subprocess.run(
            [sys.executable, "-u", str(Path(__file__).parent / "learnweb_sync.py"), "run"],
            timeout=TIMEOUT,
            # STATE_DB_PATH explizit weitergeben, damit der Subprozess
            # denselben Datenbankpfad wie der Server verwendet.
            env={**os.environ, "STATE_DB_PATH": STATE_DB_PATH},
        )

        if result.returncode == 0:
            _consecutive_failures = 0
            log.info("Sync erfolgreich abgeschlossen.")
            return True
        else:
            _consecutive_failures += 1
            log.error(
                f"Sync fehlgeschlagen (returncode={result.returncode}). "
                f"Aufeinanderfolgende Fehler: {_consecutive_failures}/{FAILURE_THRESH}"
            )

    except subprocess.TimeoutExpired:
        _consecutive_failures += 1
        log.error(
            f"Sync-Timeout nach {TIMEOUT}s. "
            f"Aufeinanderfolgende Fehler: {_consecutive_failures}/{FAILURE_THRESH}"
        )

    finally:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        lock_fd.close()

    # Schwellenwert erreicht → Prozess beenden, Railway startet neu.
    if _consecutive_failures >= FAILURE_THRESH:
        log.critical(
            f"Schwellenwert von {FAILURE_THRESH} aufeinanderfolgenden Fehlern "
            "erreicht — Prozess beendet sich für Railway-Restart."
        )
        sys.exit(1)

    return False


def _scheduled_sync():
    """
    Wird vom APScheduler-Background-Thread aufgerufen.

    threading.Lock verhindert gleichzeitige Starts aus Scheduler und Webhook.
    """
    if not _sync_lock.acquire(blocking=False):
        log.info("Scheduler: Sync läuft bereits — überspringe diesen Lauf.")
        return
    try:
        _run_sync()
    finally:
        _sync_lock.release()


# ── HTTP-Endpunkte ─────────────────────────────────────────────────────────────

@app.get("/health")
def health():
    """
    Gibt 200 zurück wenn die Konfiguration vollständig ist.
    Gibt 503 zurück bei fehlenden Env-Variablen oder nicht gemounttem Volume.

    Railway nutzt diesen Endpunkt als Deploy-Gate: ein Deploy gilt erst als
    erfolgreich wenn /health 200 antwortet (healthcheckTimeout=300s).
    """
    issues = _check_config()
    if issues:
        return jsonify({"status": "error", "issues": issues}), 503

    return jsonify({
        "status": "ok",
        "sync_running": _sync_lock.locked(),
        "consecutive_failures": _consecutive_failures,
        "cron_schedule": CRON_SCHEDULE,
        "timezone": TIMEZONE,
    })


@app.post("/webhook/sync")
def webhook_sync():
    """
    Startet einen Sync-Lauf manuell (z.B. via GitHub Actions Fallback oder curl).

    Auth: Authorization: Bearer <SYNC_WEBHOOK_SECRET>
    Gibt 202 Accepted zurück wenn der Lauf gestartet wurde.
    Gibt 409 Conflict zurück wenn bereits ein Lauf aktiv ist.
    Gibt 401 Unauthorized zurück bei fehlendem oder falschem Token.
    Gibt 503 zurück wenn die Konfiguration unvollständig ist.
    """
    # Auth-Check (nur wenn SYNC_WEBHOOK_SECRET gesetzt ist).
    if WEBHOOK_SECRET:
        auth_header = request.headers.get("Authorization", "")
        if auth_header != f"Bearer {WEBHOOK_SECRET}":
            return jsonify({"error": "Unauthorized"}), 401

    # Konfigurationscheck vor dem Start.
    issues = _check_config()
    if issues:
        return jsonify({"status": "error", "issues": issues}), 503

    # Primärer in-process Guard: 409 wenn Sync bereits läuft.
    if not _sync_lock.acquire(blocking=False):
        return jsonify({
            "status": "conflict",
            "message": "Ein Sync-Lauf ist bereits aktiv.",
        }), 409

    # Sync asynchron im Hintergrund starten; Webhook gibt sofort 202 zurück.
    def _background_sync():
        try:
            _run_sync()
        finally:
            _sync_lock.release()

    threading.Thread(target=_background_sync, daemon=True).start()
    return jsonify({"status": "accepted", "message": "Sync gestartet."}), 202


# ── APScheduler ────────────────────────────────────────────────────────────────

def _parse_cron(cron_str: str) -> dict:
    """
    Zerlegt einen 5-stelligen Cron-Ausdruck in APScheduler-CronTrigger-kwargs.

    Beispiel: '0 6 * * 1-5' → {minute: '0', hour: '6', day: '*', ...}
    """
    parts = cron_str.split()
    if len(parts) != 5:
        raise ValueError(
            f"CRON_SCHEDULE muss 5 Felder haben (min stunde tag monat wochentag), "
            f"erhalten: '{cron_str}'"
        )
    minute, hour, day, month, day_of_week = parts
    return dict(minute=minute, hour=hour, day=day, month=month, day_of_week=day_of_week)


# Scheduler beim Import des Moduls starten (wird von gunicorn beim Worker-Start
# ausgeführt; mit --workers 1 läuft genau eine Scheduler-Instanz).
_scheduler = BackgroundScheduler(timezone=TIMEZONE)
_scheduler.add_job(_scheduled_sync, "cron", **_parse_cron(CRON_SCHEDULE))
_scheduler.start()

log.info(f"APScheduler gestartet — Zeitplan: '{CRON_SCHEDULE}' ({TIMEZONE})")
log.info(f"STATE_DB_PATH={STATE_DB_PATH}")
log.info(f"Webhook-Auth: {'aktiv' if WEBHOOK_SECRET else 'DEAKTIVIERT (kein SYNC_WEBHOOK_SECRET gesetzt)'}")
