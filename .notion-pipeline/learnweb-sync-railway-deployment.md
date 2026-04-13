# learnweb-sync: Railway-Deployment
> Quelle: Notion Coding Pipeline – 2026-04-13
> Repo: https://github.com/tbmn1996/learnweb_sync

## Kontext
- Projekt: learnweb_sync
- Repo: https://github.com/tbmn1996/learnweb_sync
- Relevante Dateien:
  - `learnweb_sync.py` — Haupt-Script (CLI, argparse, Funktionen: `cmd_run()`, `cmd_scan()`, `cmd_push()`, `cmd_sync_courses()`)
  - `.github/workflows/sync.yml` — GitHub Actions Workflow (Cron Mo–Fr 06:00 UTC, state.db via orphan-Branch `state`)
  - `requirements.txt` — Abhängigkeiten: requests, beautifulsoup4, python-dotenv
  - `.env.example` — Env-Variablen-Vorlage (LearnWeb-Credentials, Notion-Token, DB-IDs, COURSE_MAP)
  - `docs/PLAN.md` — Architektur- und Entwicklungsplan (Phasen 1–5)
- Abhängigkeiten (bestehend): requests, beautifulsoup4, python-dotenv
- Abhängigkeiten (neu): flask>=3.0,<4, gunicorn>=22,<23, apscheduler>=3.10,<4
- Architektur-Entscheidungen:
  - Flask als HTTP-Framework (leichtgewichtig, kein async-Bedarf)
  - APScheduler `BackgroundScheduler` + `CronTrigger` für internes Cron (Railway native Cron inkompatibel mit Always-on)
  - Neues `server.py` als Entrypoint → `learnweb_sync.py` bleibt **unverändert** (Zero-Touch)
  - `SystemExit`-Wrapper: `cmd_run()` nutzt `sys.exit(1)` → `server.py` fängt ab, gibt HTTP-Status zurück
  - WEBHOOK_SECRET: Bearer-Token für Authentifizierung (bereitet Phase 4 Notion-Button vor)
  - **state.db via Railway Volume (harte Voraussetzung):** state.db ist zentral für Deduplizierung (cmid-basiert). Railway Volume überlebt Redeploys. `server.py` überschreibt `learnweb_sync.DB_PATH` via Env-Var `STATE_DB_PATH` → Zero-Touch. Kein stiller Fallback auf ephemeral FS.
  - Logging zu stdout vor Import von learnweb_sync (Railway ephemeral FS)
  - Non-Daemon Threads + `atexit`-Handler für Graceful Shutdown (Gunicorn #2510)
  - **Health Endpoint (Deploy-Gating + Diagnose, NICHT Runtime-Monitoring):** Railway prüft `/health` nur beim Deploy, nicht kontinuierlich. Für Runtime-Recovery: Self-Termination via `os._exit(1)` nach `MAX_CONSECUTIVE_FAILURES` → Gunicorn Master respawnt Worker automatisch.
  - RAILPACK_START_CMD statt Procfile (deprecated)
  - **409 Conflict bei laufendem Sync:** Atomarer Lock-Acquire im Request-Handler (TOCTOU-Fix). Lock wird via `_lock_pre_acquired` an Background-Thread übergeben.
  - Stale Sync Detection: `/health` trackt Startzeit, meldet "stale" bei Überschreitung von `MAX_SYNC_DURATION` (Diagnose, kein Auto-Restart)
  - Graceful Import-Failure: try/except bei Import → Server startet trotzdem → /health 503
  - **Scheduler-Guard:** `init_scheduler()` prüft `cmd_run is None`, startet keinen Scheduler bei Import-Fehler
  - **Thread-Cleanup:** `_active_threads` wird vor Append um beendete Threads bereinigt (is_alive-Filter)
  - **Volume-Mount-Prüfung:** `STATE_DB_PATH` gesetzt → `Path.parent.exists()` beim Start. Fehlt Volume → `_volume_error` → /health 503
  - **STATE_DB_PATH als Pflicht-Variable:** Wenn nicht gesetzt → `_volume_error` Flag → kein stiller Fallback
  - **Railway Restart Policy "On Failure":** Defense-in-Depth. `os._exit(1)` terminiert nur Gunicorn Worker (Fork), nicht Master. Primärer Recovery = Gunicorn Worker Respawn.
  - **Repo-Docs aktualisieren:** README.md + PLAN.md beschreiben Railway als primäres Ausführungsmodell

## Implementierungsschritte

### Schritt 1: Neue Abhängigkeiten hinzufügen
- Datei: `requirements.txt`
- Änderung: Gesamte Datei ersetzen durch:
```
requests
beautifulsoup4
python-dotenv
flask>=3.0,<4
gunicorn>=22,<23
apscheduler>=3.10,<4
```

### Schritt 2: HTTP-Service erstellen (Hauptänderung)
- Datei: `server.py` (neu, im Repo-Root)
- Änderung: Neue Datei anlegen
- Code:
```python
#!/usr/bin/env python3
"""Railway HTTP service for learnweb_sync.

Endpoints:
    GET  /health         → Healthcheck (deploy-gating + diagnosis, NOT runtime-monitored by Railway)
    POST /webhook/sync   → Trigger sync run (requires WEBHOOK_SECRET, 409 if busy)

Cron:
    Configurable via CRON_SCHEDULE env var (default: Mo-Fr 06:00 UTC).

Runtime Recovery:
    Self-terminates worker after MAX_CONSECUTIVE_FAILURES → Gunicorn master respawns worker.
    Railway restart policy ("On Failure") as defense-in-depth for master crashes.
"""

import atexit
import logging
import os
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path

# Configure logging to stdout BEFORE importing learnweb_sync
# (learnweb_sync creates file handlers on import → ephemeral FS on Railway)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)]
)

from flask import Flask, jsonify, request
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

# Import sync logic from existing script (graceful degradation on missing env vars)
_import_error = None
_volume_error = None
try:
    import learnweb_sync
    from learnweb_sync import cmd_run
    # Override state.db path for Railway Volume (Zero-Touch: no change to learnweb_sync.py)
    _state_db_path = os.getenv("STATE_DB_PATH")
    if _state_db_path:
        if not Path(_state_db_path).parent.exists():
            _volume_error = f"Volume mount missing: {Path(_state_db_path).parent} does not exist"
            logging.getLogger(__name__).error(_volume_error)
        else:
            learnweb_sync.DB_PATH = Path(_state_db_path)
            logging.getLogger(__name__).info(f"state.db path overridden: {_state_db_path}")
    else:
        _volume_error = "STATE_DB_PATH not configured – state.db would use ephemeral filesystem"
        logging.getLogger(__name__).error(_volume_error)
except (KeyError, ImportError) as e:
    cmd_run = None
    _import_error = str(e)
    logging.getLogger(__name__).error(f"Failed to import learnweb_sync: {e}")

app = Flask(__name__)
log = logging.getLogger(__name__)

# Config
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "")
CRON_SCHEDULE = os.getenv("CRON_SCHEDULE", "0 6 * * 1-5")  # Default: Mo-Fr 06:00 UTC
PORT = int(os.getenv("PORT", 8080))
MAX_CONSECUTIVE_FAILURES = int(os.getenv("MAX_CONSECUTIVE_FAILURES", 3))
MAX_SYNC_DURATION = int(os.getenv("MAX_SYNC_DURATION", 600))  # seconds

# Track sync state
_sync_lock = threading.Lock()
_last_sync = {"status": None, "timestamp": None, "error": None}
_consecutive_failures = 0
_sync_start_time = None
_active_threads = []


def run_sync_safe(_lock_pre_acquired=False) -> dict:
    """Run cmd_run() safely, catching SystemExit from learnweb_sync."""
    global _consecutive_failures, _sync_start_time
    if cmd_run is None:
        if _lock_pre_acquired:
            _sync_lock.release()
        return {"status": "error", "error": f"Import failed: {_import_error}"}
    if not _lock_pre_acquired:
        if not _sync_lock.acquire(blocking=False):
            return {"status": "skipped", "reason": "Sync already running"}
    try:
        _sync_start_time = datetime.now(timezone.utc)
        log.info("Starting sync run...")
        cmd_run()
        _consecutive_failures = 0
        result = {"status": "success", "timestamp": datetime.now(timezone.utc).isoformat()}
        _last_sync.update(result)
        log.info("Sync run completed successfully.")
        return result
    except SystemExit as e:
        _consecutive_failures += 1
        result = {"status": "error", "error": f"Exit code: {e.code}",
                  "timestamp": datetime.now(timezone.utc).isoformat()}
        _last_sync.update(result)
        log.error(f"Sync run failed with exit code {e.code}")
        _check_fatal_failures()
        return result
    except Exception as e:
        _consecutive_failures += 1
        result = {"status": "error", "error": str(e),
                  "timestamp": datetime.now(timezone.utc).isoformat()}
        _last_sync.update(result)
        log.error(f"Sync run failed: {e}")
        _check_fatal_failures()
        return result
    finally:
        _sync_start_time = None
        _sync_lock.release()


def _check_fatal_failures():
    """Self-terminate worker after too many consecutive failures.

    os._exit(1) terminates the Gunicorn worker process (fork).
    Primary recovery: Gunicorn master automatically respawns the worker.
    Defense-in-depth: Railway Restart Policy covers master crashes.
    """
    if _consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
        log.critical(
            f"Max consecutive failures ({MAX_CONSECUTIVE_FAILURES}) reached "
            f"– terminating for Railway restart policy"
        )
        os._exit(1)  # Hard exit → Gunicorn master respawns worker


@app.route("/health", methods=["GET"])
def health():
    """Health endpoint for deploy-gating and manual diagnosis.

    NOTE: Railway only checks this during deployment (zero-downtime deploy),
    NOT continuously at runtime. Runtime recovery is handled by
    self-termination in _check_fatal_failures().
    """
    healthy = (_consecutive_failures < MAX_CONSECUTIVE_FAILURES
               and _import_error is None and _volume_error is None)
    stale = False
    if _sync_start_time:
        elapsed = (datetime.now(timezone.utc) - _sync_start_time).total_seconds()
        stale = elapsed > MAX_SYNC_DURATION
    status_code = 200 if healthy and not stale else 503
    return jsonify({
        "status": "degraded" if not healthy else ("stale" if stale else "ok"),
        "service": "learnweb_sync",
        "last_sync": _last_sync,
        "consecutive_failures": _consecutive_failures,
        "sync_running_since": _sync_start_time.isoformat() if _sync_start_time else None,
        "import_error": _import_error,
        "volume_error": _volume_error,
        "cron_schedule": CRON_SCHEDULE,
    }), status_code


@app.route("/webhook/sync", methods=["POST"])
def webhook_sync():
    # Authenticate
    if WEBHOOK_SECRET:
        auth = request.headers.get("Authorization", "")
        if auth != f"Bearer {WEBHOOK_SECRET}":
            return jsonify({"error": "Unauthorized"}), 401

    if cmd_run is None:
        return jsonify({"error": "Service degraded", "import_error": _import_error}), 503

    # Acquire lock atomically (prevents TOCTOU race vs. locked() check)
    if not _sync_lock.acquire(blocking=False):
        return jsonify({"status": "already_running"}), 409

    # Run sync in non-daemon thread (lock pre-acquired, released by run_sync_safe)
    _active_threads[:] = [t for t in _active_threads if t.is_alive()]
    thread = threading.Thread(target=run_sync_safe,
                              kwargs={"_lock_pre_acquired": True}, daemon=False)
    thread.start()
    _active_threads.append(thread)
    return jsonify({"status": "accepted", "message": "Sync triggered"}), 202


def _cleanup_threads():
    """Wait for active sync threads on shutdown."""
    for t in _active_threads:
        t.join(timeout=30)

atexit.register(_cleanup_threads)


def init_scheduler():
    """Start APScheduler with cron trigger from CRON_SCHEDULE env var."""
    if cmd_run is None:
        log.warning("cmd_run unavailable – scheduler not started")
        return
    if not CRON_SCHEDULE:
        log.info("CRON_SCHEDULE is empty – no scheduled sync.")
        return
    scheduler = BackgroundScheduler()
    try:
        trigger = CronTrigger.from_crontab(CRON_SCHEDULE)
    except ValueError as e:
        log.error(f"Invalid CRON_SCHEDULE '{CRON_SCHEDULE}': {e}")
        return
    scheduler.add_job(run_sync_safe, trigger, id="learnweb_sync_cron",
                      max_instances=1, replace_existing=True)
    scheduler.start()
    log.info(f"Scheduler started: '{CRON_SCHEDULE}'")


# Initialize scheduler on import (gunicorn will call this)
init_scheduler()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT, debug=False)
```

### Schritt 3: Railway Start-Command konfigurieren
- Konfiguration: Railway Dashboard (manuell)
- Änderung: Environment Variable `RAILPACK_START_CMD` setzen
- Wert: `gunicorn server:app --bind 0.0.0.0:$PORT --workers 1 --timeout 300`
- Hinweis: `--workers 1` bewusst (APScheduler im Hauptprozess, mehrere Worker → doppelte Cron-Runs). `--timeout 300` für lang laufende Sync-Requests.

### Schritt 4: Environment-Variablen dokumentieren
- Datei: `.env.example`
- Änderung: Am Ende der Datei anhängen:
```
# === Railway Deployment ===
# Port for HTTP service (Railway sets this automatically)
# PORT=8080

# Cron schedule for automatic sync (UTC, standard 5-field crontab)
# Default: Mo-Fr 06:00 UTC = 07:00 CET / 08:00 CEST
CRON_SCHEDULE=0 6 * * 1-5

# Secret for webhook authentication (Bearer token)
# Generate: python -c "import secrets; print(secrets.token_urlsafe(32))"
WEBHOOK_SECRET=

# Max consecutive sync failures before /health returns 503
MAX_CONSECUTIVE_FAILURES=3

# Max sync duration in seconds before /health reports stale (default: 600 = 10 min)
MAX_SYNC_DURATION=600

# Path to state.db on Railway Volume (overrides learnweb_sync.DB_PATH)
# Mount a Railway Volume at /data and set this to /data/state.db
STATE_DB_PATH=/data/state.db
```

### Schritt 5: Phase 3 Dokumentation aktualisieren
- Datei: `docs/PLAN.md`
- Änderung: Im Abschnitt `### Phase 3` ergänzen:
```
#### Railway-Deployment (primär)
- `server.py`: Flask + APScheduler als Always-on HTTP-Service
- Endpoint: POST /webhook/sync (mit WEBHOOK_SECRET)
- Cron: Konfigurierbar via CRON_SCHEDULE env var
- Start: `gunicorn server:app --bind 0.0.0.0:$PORT --workers 1`
- GitHub Actions bleibt als Fallback erhalten
```

### Schritt 6: Railway-Konfiguration (manuell im Dashboard)
- Im Railway-Projekt (TBMN Cloud Tools) neuen Service hinzufügen
- GitHub-Repo `tbmn1996/learnweb_sync` verbinden
- Environment Variables setzen: alle aus `.env.example` + `WEBHOOK_SECRET`
- Railway erkennt Python automatisch via `requirements.txt` (Railpack)
- `RAILPACK_START_CMD` setzen (siehe Schritt 3)
- Domain generieren (für Webhook-URL)
- **Restart Policy** auf "On Failure" setzen (Defense-in-Depth für Master-Crashes)
- **Volume** hinzufügen: Mount Path `/data` → Env-Var `STATE_DB_PATH=/data/state.db`

### Schritt 7: README.md aktualisieren
- Datei: `README.md`
- Änderung: Neuen Abschnitt `## Railway Deployment` nach `## Befehle` einfügen
- Inhalt: Beschreibung des HTTP-Service, Webhook-Endpoint, Cron-Schedule, Verweis auf `server.py`
- Entwicklungsphasen aktualisieren: Phase 3 als "✅ Railway-Deployment (primär) + GitHub Actions (Fallback)"

### Schritt 8: PLAN.md umschreiben
- Datei: `docs/PLAN.md`
- Änderung: `### Phase 3` Titel und Inhalt aktualisieren
- Bestehenden GitHub-Actions-Teil als "Fallback" kennzeichnen
- Railway-Deployment als "Primär" hinzufügen
- Repo-Struktur-Diagramm um `server.py` ergänzen

## Testkriterien
- [ ] `python learnweb_sync.py run` funktioniert weiterhin lokal (CLI unverändert)
- [ ] `.github/workflows/sync.yml` ist unverändert und funktionsfähig (GitHub Actions Fallback)
- [ ] `server.py` startet lokal mit `python server.py` ohne Fehler
- [ ] `GET /health` gibt JSON mit Status `ok` und Cron-Schedule zurück
- [ ] `POST /webhook/sync` ohne Token gibt 401 zurück (wenn WEBHOOK_SECRET gesetzt)
- [ ] `POST /webhook/sync` mit korrektem Bearer-Token gibt 202 zurück und startet Sync
- [ ] APScheduler loggt geplanten nächsten Run beim Start
- [ ] Sync-Ergebnisse (Notion-Seiten) sind identisch zum GitHub-Actions-Betrieb
- [ ] Gunicorn startet via `RAILPACK_START_CMD` und bedient HTTP-Requests
- [ ] `GET /health` gibt HTTP 503 nach `MAX_CONSECUTIVE_FAILURES` konsekutiven Fehlern
- [ ] `POST /webhook/sync` gibt 409 zurück wenn Sync bereits läuft
- [ ] `GET /health` meldet `"stale"` bei Sync > `MAX_SYNC_DURATION`
- [ ] Server startet auch mit fehlenden LearnWeb-Env-Vars → `/health` gibt 503 mit `import_error`
- [ ] Railway Volume gemountet → state.db überlebt Redeploy (Deduplizierung funktioniert)
- [ ] `STATE_DB_PATH` korrekt gelesen → `learnweb_sync.DB_PATH` überschrieben
- [ ] Self-Termination: Nach `MAX_CONSECUTIVE_FAILURES` Fehlern terminiert Prozess mit Exit-Code 1
- [ ] Gunicorn respawnt Worker nach `os._exit(1)` (primärer Recovery-Mechanismus)
- [ ] Railway Restart Policy "On Failure" als Defense-in-Depth für Master-Crashes
- [ ] Server mit fehlendem `STATE_DB_PATH` → `/health` gibt 503 mit `volume_error`
- [ ] `README.md` und `docs/PLAN.md` dokumentieren Railway als primäres Ausführungsmodell
- [ ] `_active_threads` enthält keine beendeten Threads nach mehreren Webhook-Calls
- [ ] Zwei gleichzeitige `POST /webhook/sync` → nur einer 202, der andere 409 (kein TOCTOU)
- [ ] Scheduler startet nicht bei fehlgeschlagenem Import (`cmd_run = None`)
- [ ] `/health` meldet `volume_error` wenn `STATE_DB_PATH` gesetzt aber Volume nicht gemountet

## Abbruchbedingungen
- **STOP** wenn: `learnweb_sync.py` modifiziert werden muss (Ziel: Zero-Touch). Falls doch nötig: Abweichung dokumentieren, nicht eigenmächtig ändern.
- **STOP** wenn: Import von `cmd_run` aus `learnweb_sync.py` fehlschlägt (z.B. wegen Modul-Level `os.environ[]`-Aufrufen ohne gesetzte Env-Vars). Fehlermeldung dokumentieren, Workaround vorschlagen.
- **STOP** wenn: Railway Volume nicht verfügbar oder Mount fehlschlägt → state.db-Persistenz ist harte Voraussetzung, nicht ohne Volume deployen.
- Bei jeder Abweichung vom Plan → **STOP** → Abweichung dokumentieren → Nicht eigenmächtig weitermachen.
