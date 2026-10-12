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
  gunicorn 'server:create_app()' --workers 1 --threads 4 --bind 0.0.0.0:$PORT
"""

import fcntl
import hmac
import json
import logging
import os
import re
import subprocess
import sys
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from flask import Flask, jsonify, request


# ── Konfiguration aus Env ──────────────────────────────────────────────────────

# Pfad zur SQLite-Datenbank; auf Railway via Railway-Volume auf /data gemountet.
STATE_DB_PATH = os.getenv("STATE_DB_PATH", "/data/state.db")

# Separate Lock-Datei für prozessübergreifenden flock-Schutz.
STATE_LOCK_PATH = os.getenv("STATE_LOCK_PATH", "/data/state.lock")

# Ohne Bearer-Token bleibt der schreibende Webhook gesperrt.
WEBHOOK_SECRET = os.getenv("SYNC_WEBHOOK_SECRET", "")

# Cron-Ausdruck im Standard-Format "min stunde tag monat wochentag".
# Default: Mo–Fr 06:00 UTC (entspricht dem bisherigen GitHub-Actions-Zeitplan).
CRON_SCHEDULE = os.getenv("CRON_SCHEDULE", "0 6 * * mon-fri")

# Zeitzone für den APScheduler-Cron.
TIMEZONE = os.getenv("SYNC_SCHEDULE_TIMEZONE", "UTC")

# Maximale Laufzeit eines Sync-Subprozesses in Sekunden.
def _positive_int(name: str, default: str) -> int | None:
    try:
        value = int(os.getenv(name, default))
        return value if value > 0 else None
    except ValueError:
        return None


TIMEOUT = _positive_int("SYNC_RUN_TIMEOUT_SECONDS", "1800")

# Ab dieser Anzahl aufeinanderfolgender Fehler beendet sich der Prozess,
# Gunicorn ersetzt den beendeten Worker; der Master bleibt bestehen.
FAILURE_THRESH = _positive_int("SYNC_FAILURE_EXIT_THRESHOLD", "3")


# ── In-process State ───────────────────────────────────────────────────────────

# Primärer Guard: verhindert parallele Sync-Starts innerhalb desselben Prozesses.
# Lock.acquire(blocking=False) gibt False zurück wenn bereits gehalten → 409.
_sync_lock = threading.Lock()

# Zähler für technische Fehler; erfolgreiche und inhaltlich blockierte Läufe setzen ihn zurück.
_consecutive_failures = 0
_last_run = None
_state_lock = threading.Lock()
_status_issue = None
_status_loaded = False
_scheduler = None
_restart_pending = False


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
_REQUIRED_ENV = [
    "LEARNWEB_URL", "LEARNWEB_USERNAME", "LEARNWEB_PASSWORD", "NOTION_TOKEN",
    "NOTION_COURSES_DB_ID", "NOTION_LW_DB_ID",
]


def _status_path() -> Path:
    return Path(f"{STATE_DB_PATH}.server-status.json")


def _check_path(path: Path, label: str) -> list[str]:
    if not path.parent.is_dir():
        return [f"{label}: Elternverzeichnis fehlt"]
    if not os.access(path.parent, os.W_OK | os.X_OK):
        return [f"{label}: Elternverzeichnis nicht beschreibbar"]
    if path.exists() and (
        not path.is_file() or not os.access(path, os.R_OK | os.W_OK)
    ):
        return [f"{label}: keine lesbare/beschreibbare Datei"]
    return []


def _check_config() -> list[str]:
    """Gibt eine Liste von Konfigurationsproblemen zurück (leer = alles ok)."""
    issues = []
    for var in _REQUIRED_ENV:
        if not os.getenv(var, "").strip():
            issues.append(f"Env '{var}' fehlt")
    if not WEBHOOK_SECRET.strip():
        issues.append("Env 'SYNC_WEBHOOK_SECRET' fehlt")
    for var in ("NOTION_COURSES_DB_ID", "NOTION_LW_DB_ID"):
        if os.getenv(var):
            try:
                UUID(os.environ[var])
            except ValueError:
                issues.append(f"Env '{var}' ist keine Datenbank-ID")
    if TIMEOUT is None:
        issues.append("SYNC_RUN_TIMEOUT_SECONDS muss positiv und ganzzahlig sein")
    if FAILURE_THRESH is None:
        issues.append("SYNC_FAILURE_EXIT_THRESHOLD muss positiv und ganzzahlig sein")
    try:
        CronTrigger.from_crontab(CRON_SCHEDULE, timezone=TIMEZONE)
    except (ValueError, KeyError, TypeError):
        issues.append("CRON_SCHEDULE oder SYNC_SCHEDULE_TIMEZONE ungültig")
    for path, label in (
        (Path(STATE_DB_PATH), "STATE_DB_PATH"),
        (Path(STATE_LOCK_PATH), "STATE_LOCK_PATH"),
        (_status_path(), "Server-Laufstatus"),
    ):
        issues.extend(_check_path(path, label))
    if Path(STATE_DB_PATH).resolve() == Path(STATE_LOCK_PATH).resolve():
        issues.append("STATE_DB_PATH und STATE_LOCK_PATH müssen verschieden sein")
    if _status_issue:
        issues.append(_status_issue)
    if _restart_pending:
        issues.append("Gunicorn-Worker wird neu gestartet")
    return issues


def _save_status() -> bool:
    """Aufrufer hält _state_lock; atomarer kleiner Checkpoint neben state.db."""
    global _status_issue
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=_status_path().parent,
            prefix=".sync-status-", delete=False,
        ) as stream:
            temporary = Path(stream.name)
            json.dump({"last_run": _last_run, "consecutive_failures": _consecutive_failures}, stream)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(_status_path())
        _status_issue = None
        return True
    except OSError:
        _status_issue = "Server-Laufstatus nicht speicherbar"
        log.error(_status_issue)
        return False
    finally:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                log.error("Temporärer Server-Laufstatus konnte nicht bereinigt werden.")


def _load_status():
    global _last_run, _consecutive_failures, _status_issue, _status_loaded
    with _state_lock:
        if _status_loaded:
            return
        _status_loaded = True
        try:
            stored = json.loads(_status_path().read_text(encoding="utf-8"))
            count = stored["consecutive_failures"]
            run = stored["last_run"]
            if type(count) is not int or count < 0 or not isinstance(run, dict):
                raise ValueError("Invalid status")
            if run["outcome"] not in ("running", "success", "incomplete", "failed", "timeout", "interrupted"):
                raise ValueError("Invalid outcome")
            for name in ("started_at", "finished_at"):
                if run[name] is not None:
                    datetime.fromisoformat(run[name])
            for name in ("exit_code", "error_count"):
                if run[name] is not None and type(run[name]) is not int:
                    raise ValueError("Invalid count")
            if run["error_count_source"] not in ("log_records", "push_summary", "process_failure", "process_exit", "unknown"):
                raise ValueError("Invalid source")
            if run["trigger"] not in ("scheduler", "webhook", "manual"):
                raise ValueError("Invalid trigger")
            if run["error_kind"] not in (None, "content_blocked", "exit_code", "timeout", "os_error", "interrupted", "status_write"):
                raise ValueError("Invalid error kind")
            _last_run = {key: run[key] for key in (
                "started_at", "finished_at", "outcome", "exit_code", "error_count",
                "error_count_source", "trigger", "error_kind",
            )}
            _consecutive_failures = count
            if _last_run["outcome"] == "running":
                with Path(STATE_LOCK_PATH).open("a") as lock_fd:
                    try:
                        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    except BlockingIOError:
                        # Ein anderer Prozess hält noch den Lauf, kein Fehler.
                        return
                    # Der Abbruchzeitpunkt ist unbekannt; keinen Endzeitpunkt erfinden.
                    _last_run.update(outcome="interrupted", error_kind="interrupted",
                                     error_count=None, error_count_source="unknown")
                    _consecutive_failures += 1
                    _save_status()
        except FileNotFoundError:
            pass
        except (OSError, ValueError, KeyError, TypeError):
            _status_issue = "Server-Laufstatus nicht lesbar oder ungültig"
            log.error(_status_issue)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ── Sync-Ausführung ────────────────────────────────────────────────────────────

def _run_sync(trigger: str = "manual") -> bool | None:
    """
    Führt learnweb_sync.py run als Subprozess aus.

    Hält dabei einen fcntl-Lock auf STATE_LOCK_PATH, um versehentliche
    Mehrfach-Prozesse (z.B. lokaler Test parallel zu Railway) zu blockieren.

    True = Exit 0, False = unvollständig/Fehler/Timeout, None = externer Lock belegt.
    Bei wiederholten Fehlern endet der Worker nach dem Checkpoint mit os._exit(1).
    """
    global _consecutive_failures, _last_run
    _load_status()
    started_at = _now()
    outcome, error_kind, exit_code = "failed", "os_error", None
    error_count = 0
    push_errors = 0
    lock_fd = None
    process = None
    reader = None

    def _read_output():
        nonlocal error_count, push_errors
        secrets = [os.getenv(name, "") for name in
                   ("LEARNWEB_PASSWORD", "NOTION_TOKEN", "SYNC_WEBHOOK_SECRET")]
        for line in process.stdout:
            if re.match(r"^\d{4}-\d\d-\d\d .*?\s(?:ERROR|CRITICAL)\s", line):
                error_count += 1
            summary = re.match(r"^[✓!] Push(?: unvollständig)?: .*? (\d+) Fehler\s*$", line)
            if summary:
                push_errors = int(summary[1])
            for secret in secrets:
                if secret:
                    line = line.replace(secret, "[redacted]")
            sys.stdout.write(line)
            sys.stdout.flush()

    try:
        lock_fd = Path(STATE_LOCK_PATH).open("a")
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            log.info("fcntl-Lock belegt — Lauf übersprungen.")
            return None
        with _state_lock:
            _last_run = dict(
                started_at=started_at, finished_at=None, outcome="running",
                exit_code=None, error_count=None, error_count_source="unknown",
                trigger=trigger, error_kind=None,
            )
            if not _save_status():
                error_kind = "status_write"
                raise OSError("Status checkpoint failed")
        log.info("Starte Sync-Subprozess …")
        process = subprocess.Popen(
            [sys.executable, "-u", str(Path(__file__).parent / "learnweb_sync.py"), "run"],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace",
            env={**os.environ, "STATE_DB_PATH": STATE_DB_PATH},
        )
        reader = threading.Thread(target=_read_output, daemon=True)
        reader.start()
        exit_code = process.wait(timeout=TIMEOUT)
        if exit_code == 2:
            outcome, error_kind = "incomplete", "content_blocked"
        else:
            outcome = "success" if exit_code == 0 else "failed"
            error_kind = None if exit_code == 0 else "exit_code"
    except subprocess.TimeoutExpired:
        outcome, error_kind = "timeout", "timeout"
        log.error("Sync-Timeout nach %ss.", TIMEOUT)
    except (OSError, RuntimeError):
        outcome = "failed"
        log.error("Sync konnte nicht gestartet oder ausgeführt werden.")
    finally:
        try:
            if process is not None:
                if process.poll() is None:
                    process.kill()
                process.wait()
                if reader is not None and reader.ident is not None:
                    reader.join()
                process.stdout.close()
        except OSError:
            outcome, error_kind = "failed", "os_error"
            log.error("Sync-Subprozess konnte nicht vollständig bereinigt werden.")
        finally:
            if lock_fd is not None:
                # close() gibt flock auch bei Fehlern und übersprungenen Läufen frei.
                lock_fd.close()

    success = outcome == "success"
    with _state_lock:
        _consecutive_failures = 0 if outcome in {"success", "incomplete"} else _consecutive_failures + 1
        # shortcut: Beobachtete Fehler, keine eindeutige Inhaltszählung; bei CLI-JSON-Resultat umstellen.
        _last_run = dict(
            started_at=started_at, finished_at=_now(), outcome=outcome,
            exit_code=exit_code, error_count=max(error_count, push_errors, 0 if success else 1),
            error_count_source=("push_summary" if push_errors > 0 and push_errors >= error_count else
                                "log_records" if error_count else
                                "process_exit" if success else "process_failure"),
            trigger=trigger, error_kind=error_kind,
        )
        _save_status()
    log.info("Sync beendet: outcome=%s, exit_code=%s, Fehlerfolgeläufe=%s/%s.",
             outcome, exit_code, _consecutive_failures, FAILURE_THRESH)
    return success


def _finish_sync(trigger: str):
    global _restart_pending
    try:
        result = _run_sync(trigger)
        restart = result is False and FAILURE_THRESH is not None and _consecutive_failures >= FAILURE_THRESH
        _restart_pending = restart
    finally:
        _sync_lock.release()
    if restart:
        log.critical("Fehlerschwelle erreicht — Gunicorn-Worker wird ersetzt.")
        # SystemExit beendet im Scheduler/Webhook nur den Thread, nicht den Worker.
        logging.shutdown()
        os._exit(1)


def _scheduled_sync():
    """
    Wird vom APScheduler-Background-Thread aufgerufen.

    threading.Lock verhindert gleichzeitige Starts aus Scheduler und Webhook.
    """
    if _check_config():
        log.error("Scheduler: Konfiguration nicht startbereit.")
        return
    if not _sync_lock.acquire(blocking=False):
        log.info("Scheduler: Sync läuft bereits — überspringe diesen Lauf.")
        return
    _finish_sync("scheduler")


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
    with _state_lock:
        last_run = dict(_last_run) if _last_run is not None else None
        failures = _consecutive_failures
    return jsonify({
        "status": "error" if issues else "ready",
        "ready": not issues,
        "issues": issues,
        "health_scope": "configuration_and_local_storage",
        "content_verified": False,
        "sync_running": _sync_lock.locked(),
        "consecutive_failures": failures,
        "last_run": last_run,
        "scheduler_running": bool(_scheduler and _scheduler.running),
        "cron_schedule": CRON_SCHEDULE if not issues else None,
        "timezone": TIMEZONE if not issues else None,
    }), 503 if issues else 200


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
    if not WEBHOOK_SECRET.strip():
        return jsonify({"status": "error", "issues": ["Env 'SYNC_WEBHOOK_SECRET' fehlt"]}), 503
    auth_header = request.headers.get("Authorization", "")
    if not hmac.compare_digest(auth_header.encode(), f"Bearer {WEBHOOK_SECRET}".encode()):
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
    try:
        threading.Thread(target=_finish_sync, args=("webhook",), daemon=True).start()
    except RuntimeError:
        _sync_lock.release()
        return jsonify({"status": "error", "message": "Sync-Thread konnte nicht starten."}), 503
    return jsonify({"status": "accepted", "message": "Sync angefordert; Ergebnis unter /health."}), 202


# ── APScheduler ────────────────────────────────────────────────────────────────

def create_app():
    """Gunicorn-Factory: Import bleibt inert, ein Worker startet einen Scheduler."""
    global _scheduler
    _load_status()
    issues = _check_config()
    if issues:
        log.error("Server nicht startbereit: %s", "; ".join(issues))
    elif _scheduler is None:
        _scheduler = BackgroundScheduler(timezone=TIMEZONE)
        _scheduler.add_job(_scheduled_sync, CronTrigger.from_crontab(CRON_SCHEDULE, timezone=TIMEZONE))
        _scheduler.start()
        log.info("APScheduler gestartet — Zeitplan: %s (%s)", CRON_SCHEDULE, TIMEZONE)
    return app
