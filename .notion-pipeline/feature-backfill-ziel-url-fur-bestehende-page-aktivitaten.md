# [FEATURE] Backfill Ziel-URL für bestehende Page-Aktivitäten
> Quelle: Notion Coding Pipeline – 2026-05-20
> Repo: https://github.com/tbmn1996/learnweb_sync
> Notion-Seite: https://www.notion.so/page (siehe Pipeline)

## Kontext
- Projekt: `learnweb_sync` (Python 3.12, `requests` + `beautifulsoup4` + `sqlite3`, Railway-Hosting). Monolithisches CLI, Subcommands per `cmd_<name>`, Dispatch via `if/elif command == "...":`. Kein `argparse`.
- Bestehende Subcommands: `sync-courses`, `scan`, `push`, `run`, `diagnose-resource-errors`, `export-zips`.
- Relevante Dateien:
  - `learnweb_sync.py` — Extractoren, Notion-Helfer (`_notion_request`, `notion_create_lw_page`, `notion_lw_db_has_target_url_property`), `init_db`, CLI-Dispatch.
  - `tests/test_learnweb_sync.py` — Unittest, `mock.patch.object(lws, …)`-Pattern.
  - `docs/PLAN.md` — Architektur + Feldmapping (Ziel-URL-Notiz ergänzen).
  - `README.md` — CLI-Befehlstabelle.
- Abhängigkeiten: `requirements.txt` unverändert (`requests`, `beautifulsoup4`, `python-dotenv`). Env: `LEARNWEB_URL`/`LEARNWEB_USERNAME`/`LEARNWEB_PASSWORD`, `NOTION_TOKEN`, `NOTION_LW_DB_ID`, `STATE_DB_PATH`.
- Wiederverwendete Helfer: `_extract_url_target(session, view_url)`, `_extract_iframe_target(soup)`, `_fetch_activity_soup(session, view_url)`, `_notion_request`, `notion_lw_db_has_target_url_property`, `init_db`, `LW_TARGET_URL_PROPERTY = "Ziel-URL"`, `NOTION_API`, `NOTION_LW_DB_ID`.
- `state.db`-Schema: `resources(cmid PK, course_id, course_name, course_shortname, modtype, name, section, view_url, first_seen, last_seen, file_hash, file_name, notion_id, status)`.
- Production-`state.db` zwingend: `STATE_DB_PATH=/data/state.db` auf Railway. Lokal-Default `./state.db` → leer/veraltet → alle Treffer stumm in `skipped_unknown_cmid`. Pre-Flight 2c fängt das ab.

## Architektur-Entscheidungen
- Eigener Subcommand `backfill-ziel-url` (kein Flag in `cmd_push`). Folgt `cmd_<name>`-Pattern. Zero-Touch auf Scheduler.
- Notion = Wahrheit für „leere Ziel-URL" → Filter `{"property": LW_TARGET_URL_PROPERTY, "url": {"is_empty": true}}`. Idempotenz aus Filter.
- `state.db` = Wahrheit für `modtype` + `view_url`. Lookup per cmid (Notion-Property `Nr`). Kein Re-Scrape Kursseiten.
- Skip statt Crash: cmid fehlt → `skipped_unknown_cmid`; `modtype ∉ {url, page}` → `skipped_modtype`; Extractor leer → `no_target_found`. Kein Notion-Write.
- PATCH nur bei truthy `target_url`. Verhindert Datenverlust bei transienten Fehlern (Login-Drop, 5xx, geblockter iframe).
- Pre-Flight-Schema-Check via `notion_lw_db_has_target_url_property()`. Early-Exit.
- `--dry-run`: keine PATCHes, Fetch+Extractor normal. Prefix `[DRY-RUN]`.
- `--limit N` für Probeläufe.
- Rate-Limiting: `time.sleep(0.34)` nach jeder LearnWeb-Iteration. Notion-Retry via `_notion_request`.
- Session einmalig (lazy `if session is None`).
- `state.db` read-only.
- Dedizierte Exception `BackfillAbort(Exception)`: Generator-Pre-Flight raised; Haupt­schleife fängt + returnt `{"aborted": 1}`.
- Modul-Level Session `_lw_session` (analog `_notion_session`). Wrapper `_session_get_with_relogin` re-bindet Modul-Variable bei Drop → alle Caller sehen neue Session automatisch. Verhindert Reference-Mutation-Falle (N× redundante Logins).
- Kein neuer Notion-Wrapper. PATCH direkt via `_notion_request("PATCH", f"{NOTION_API}/pages/{page_id}", json={"properties": {LW_TARGET_URL_PROPERTY: {"url": target}}})`. PIN: NIE `notion_update_lw_page` / `_build_lw_page_properties` (8+ Felder → würde Operator-Edits stumm zerstören).

## Implementierungsschritte

### Schritt 1: Funktion `cmd_backfill_ziel_url` anlegen
- Datei: `learnweb_sync.py`
- Position: nach `cmd_diagnose_resource_errors`.
- Signatur: `cmd_backfill_ziel_url(session: requests.Session | None = None, *, dry_run: bool = False, limit: int | None = None) -> dict[str, int]`.
- Bei `session is None`: `login()` aufrufen.

### Schritt 2: Pre-Flight-Checks
- Datei: `learnweb_sync.py` (Funktionsanfang)
- Änderung:
```python
if not notion_lw_db_has_target_url_property():
    print("[backfill] Ziel-URL-Property fehlt in NOTION_LW_DB_ID — abort")
    return {"aborted": 1}
```
- Pre-Flight 2c: `init_db()` + `SELECT COUNT(*) FROM resources`. `count == 0` → STOP + Hinweis „state.db leer — STATE_DB_PATH falsch gesetzt? Default `./state.db`; Railway: `STATE_DB_PATH=/data/state.db`".
- Pre-Flight 2a + 2b konsolidiert in Generator (Schritt 3): url-Filter-Probe (HTTP 400 → STOP + Fallback Client-Side-Filter) + cmid-Sanity-Dump (erste 5 Treffer cmid+Title+has_cmid_in_db) im ersten `page_size=100`-Call.

### Schritt 3: Helper `_iter_notion_lw_pages_without_target_url`
- Datei: `learnweb_sync.py`
- Signatur: `_iter_notion_lw_pages_without_target_url(conn: sqlite3.Connection) -> Iterator[dict]`
- POST `{NOTION_API}/databases/{NOTION_LW_DB_ID}/query` mit Body:
```python
body = {
    "filter": {"property": LW_TARGET_URL_PROPERTY, "url": {"is_empty": True}},
    "page_size": 100,
}
```
- Paginierung via `next_cursor`/`has_more` analog bestehender `NOTION_COURSES_DB_ID`-Stelle.
- Yield: `{"page_id": result["id"], "cmid": <Nr-Property>, "title": <Title-Property>, "page_url": result["url"]}`.
- `cmid` aus Notion-Property `Nr` lesen (rich_text → plain_text).
- Im ersten Call vor erstem Yield: HTTP 400 → `raise BackfillAbort("url-Filter unsupported in API 2022-06-28 — Fallback Client-Side-Filter implementieren")`. Bei 200: erste 5 Treffer mit cmid+Title+has_cmid_in_db loggen.

### Schritt 4: Helper `_resolve_backfill_target`
- Datei: `learnweb_sync.py`
- Signatur: `_resolve_backfill_target(conn: sqlite3.Connection, cmid: str) -> tuple[str | None, str | None]`
- Implementierung: `conn.execute("SELECT modtype, view_url FROM resources WHERE cmid = ?", (cmid,)).fetchone()`. Returnt `(modtype, view_url)` oder `(None, None)`.

### Schritt 5: Hauptschleife
- Datei: `learnweb_sync.py` (in `cmd_backfill_ziel_url`)
- Änderung:
```python
global _lw_session
if session is not None:
    _lw_session = session
elif _lw_session is None:
    _lw_session = login()
conn = init_db()
counters = {"filled": 0, "would_fill": 0, "no_target_found": 0,
            "skipped_modtype": 0, "skipped_unknown_cmid": 0, "error": 0,
            "aborted": 0}
processed = 0
try:
    try:
        for hit in _iter_notion_lw_pages_without_target_url(conn):
            if limit is not None and processed >= limit:
                break
            processed += 1
            cmid = hit["cmid"]
            modtype, view_url = _resolve_backfill_target(conn, cmid)
            # … dispatch nach modtype, siehe Schritt 6
            time.sleep(0.34)
    except BackfillAbort as exc:
        print(f"[backfill] aborted: {exc}")
        counters["aborted"] = 1
        return counters
finally:
    conn.close()
```

### Schritt 6: Dispatch pro Treffer
- Datei: `learnweb_sync.py` (in Schleife Schritt 5)
- Änderung:
```python
if modtype is None or view_url is None:
    counters["skipped_unknown_cmid"] += 1
    print(f"[backfill] cmid={cmid} skipped_unknown_cmid title={hit['title']!r} page_url={hit['page_url']}")
    continue
if modtype not in ("url", "page"):
    counters["skipped_modtype"] += 1
    print(f"[backfill] cmid={cmid} skipped_modtype={modtype} title={hit['title']!r} page_url={hit['page_url']}")
    continue
try:
    if modtype == "url":
        target = _extract_url_target(session, view_url)
    else:  # page
        soup = _fetch_activity_soup(session, view_url)
        target = _extract_iframe_target(soup)
except requests.RequestException as exc:
    counters["error"] += 1
    print(f"[backfill] cmid={cmid} error={exc!r}")
    continue
if not target:
    counters["no_target_found"] += 1
    print(f"[backfill] cmid={cmid} no_target_found modtype={modtype}")
    continue
if dry_run:
    print(f"[DRY-RUN] cmid={cmid} would set Ziel-URL={target}")
    counters["would_fill"] += 1
    continue
try:
    _notion_request(
        "PATCH",
        f"{NOTION_API}/pages/{hit['page_id']}",
        json={"properties": {LW_TARGET_URL_PROPERTY: {"url": target}}},
    )
    counters["filled"] += 1
    print(f"[backfill] cmid={cmid} filled Ziel-URL={target}")
except requests.HTTPError as exc:
    counters["error"] += 1
    print(f"[backfill] cmid={cmid} notion_patch_error={exc!r}")
```

### Schritt 7: Session-Wrapper `_session_get_with_relogin`
- Datei: `learnweb_sync.py`
- Modul-Level Variable `_lw_session = None` (analog `_notion_session`).
- Wrapper `_session_get_with_relogin(url) -> requests.Response`: liest Modul-Variable, macht `_lw_session.get(url)`, prüft Response-URL auf `/login/` ODER Title (`bs4`) auf `"log in"` → bei Detection `global _lw_session; _lw_session = login()` + 1× Retry.
- BEIDE Extractoren (`_extract_url_target` UND `_fetch_activity_soup`) nutzen Wrapper.
- Persistiert Login-Redirect nach Retry → `raise requests.RequestException("session_drop_persists")` → fällt in `except requests.RequestException`-Pfad → counter `error`.

### Schritt 8: Summary
- Datei: `learnweb_sync.py` (Funktionsende)
- Änderung:
```python
print("\n" + "=" * 60)
print(f"✓ Backfill done (mode={'DRY-RUN' if dry_run else 'LIVE'}):")
for key, val in counters.items():
    print(f"  {key}: {val}")
return counters
```

### Schritt 9: CLI-Dispatch
- Datei: `learnweb_sync.py` (bestehende `if/elif`-Kaskade)
- Änderung:
```python
elif command == "backfill-ziel-url":
    dry_run = "--dry-run" in sys.argv
    limit = None
    if "--limit" in sys.argv:
        idx = sys.argv.index("--limit")
        try:
            limit = int(sys.argv[idx + 1])
        except (IndexError, ValueError):
            print("Usage: backfill-ziel-url [--dry-run] [--limit N]")
            sys.exit(2)
    cmd_backfill_ziel_url(dry_run=dry_run, limit=limit)
```
- Kein `argparse` einführen.

### Schritt 10: Tests
- Datei: `tests/test_learnweb_sync.py`
- Neue Klasse `TestBackfillZielUrl` mit Tests:
  - `test_backfill_fills_url_modtype` — mock `_iter_notion_lw_pages_without_target_url` → 1 cmid; `_resolve_backfill_target` → `("url", "https://lw/view")`; `_extract_url_target` → `"https://target.example/x"`; assert PATCH-Body `{"properties": {"Ziel-URL": {"url": "https://target.example/x"}}}`.
  - `test_backfill_fills_page_modtype_iframe` — analog `modtype="page"`, mock `_fetch_activity_soup` + `_extract_iframe_target`.
  - `test_backfill_skips_non_url_page_modtypes` — `("resource", "...")`; assert kein PATCH, `skipped_modtype == 1`.
  - `test_backfill_dry_run_does_not_patch` — `dry_run=True`; assert kein PATCH, `would_fill == 1`.
  - `test_backfill_no_target_found_skips_patch` — Extractor → `None`; `no_target_found == 1`.
  - `test_backfill_missing_cmid_in_state_skips` — `(None, None)`; `skipped_unknown_cmid == 1`.
  - `test_backfill_aborts_when_schema_missing` — `notion_lw_db_has_target_url_property` → `False`; assert Notion-Query nicht aufgerufen, Return `{"aborted": 1}`.
  - `test_backfill_pagination_handles_multiple_pages` — 2-Page-Response (`has_more=true` → `has_more=false`); assert `start_cursor="X"` im 2. POST.
  - `test_session_wrapper_relogin_on_drop` — Unit auf `_session_get_with_relogin`: 1. Call Login-Redirect, 2. Call Content; assert `login()` 1×, Modul-Variable `_lw_session` zeigt auf neue Instanz.
  - `test_session_wrapper_used_by_both_extractors` — assert beide Extractoren rufen Wrapper auf.
  - `test_backfill_session_drop_triggers_relogin` — Integration: assert `login()` exakt 1×, `filled == 1`, kein `error`.
  - `test_backfill_url_filter_400_aborts` — Notion-Query HTTP 400; assert `BackfillAbort` gefangen, `counters["aborted"] == 1`.

### Schritt 11: Doku
- Datei: `docs/PLAN.md`
- Änderung: in „Bereits synchronisierte url-Seiten behalten ihren historischen Bookmark-Block" anhängen: „Backfill der `Ziel-URL`-Property ist über `python learnweb_sync.py backfill-ziel-url` möglich; Bookmark-Block bleibt unverändert."
- Datei: `README.md`
- Änderung: CLI-Tabelle um Zeile erweitern: `python learnweb_sync.py backfill-ziel-url [--dry-run] [--limit N]` | Einmaliger Nachzug-Lauf: füllt `Ziel-URL` für Alt-Pages mit modtype `url`/`page`. Idempotent.

### Schritt 12: Verifikation am Livestream-Testfall (cmid=4023900)
- a) Lokal `python learnweb_sync.py backfill-ziel-url --dry-run --limit 5` → erwartet `[DRY-RUN] cmid=4023900 would set Ziel-URL=https://...` ohne Notion-Write.
- b) Sichtkontrolle Target-URL im DRY-RUN-Log.
- c) Produktivlauf `python learnweb_sync.py backfill-ziel-url` → Livestream-Page in Notion-UI mit befüllter `Ziel-URL`.
- d) Idempotenz-Check: zweiter Produktivlauf → `filled: 0`.

## Testkriterien
- [ ] `./.venv/bin/python -m unittest tests.test_learnweb_sync` grün (bestehende + alle Tests aus Schritt 10).
- [ ] `python learnweb_sync.py backfill-ziel-url --dry-run --limit 5` druckt mind. eine `[DRY-RUN] cmid=4023900 would set Ziel-URL=...`-Zeile, keine Notion-PATCH-Logs.
- [ ] Produktivlauf setzt `Ziel-URL` auf Livestream-Page sichtbar in Notion-UI; counter-Summary `filled >= 1`.
- [ ] Zweiter Produktivlauf (Idempotenz): `filled: 0`.
- [ ] Regression: `cmd_scan`, `cmd_push`, `cmd_run`, Railway-Scheduler unverändert. `/health` grün.
- [ ] Schema-Check-Test: `notion_lw_db_has_target_url_property` gepatcht auf `False` → Early-Exit, kein Query-Call.

## Abbruchbedingungen
- Stoppe wenn: `notion_lw_db_has_target_url_property()` `False` → Operator legt Property manuell an, nicht per Code.
- Stoppe wenn: Notion-Filter `{"url": {"is_empty": true}}` in API `2022-06-28` nicht zuverlässig → Fallback: Query ohne Filter + Client-Side-Filterung, Issue dokumentieren.
- Stoppe wenn: LearnWeb-Login fehlschlägt → separates Auth-Ticket.
- Stoppe wenn: > 50 Notion-Treffer mit cmid, aber `state.db`-Lookup leer → inkonsistentes Manifest, eigenes Ticket.
- Stoppe wenn: PR #5 (`_extract_iframe_target`) nicht in `main` → vorher mergen.
- Stoppe wenn: CLI-Dispatch auf `argparse` umgestellt werden müsste → Out-of-Scope, `if/elif` beibehalten.
- Bei Unklarheit: Status auf „Blockiert" setzen, Seitenkommentar mit Detail, STOP.
