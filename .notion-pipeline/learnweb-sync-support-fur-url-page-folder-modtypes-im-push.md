# learnweb_sync: Support für url/page/folder Modtypes im Push
> Quelle: Notion Coding Pipeline – 2026-04-18
> Repo: https://github.com/tbmn1996/learnweb_sync
> Notion-Seite: https://www.notion.so/learnweb-sync-Support-fur-url-page-folder-Modtypes-im-Push

## Kontext
- Projekt: learnweb_sync
- Relevante Dateien:
  - `learnweb_sync.py` – Gesamte Sync-Logik (Scraper, Manifest, Push, Notion-API)
  - `tests/test_learnweb_sync.py` – Unit-Tests
  - `docs/PLAN.md` – Architektur-Dokumentation
- Abhängigkeiten: requests, beautifulsoup4, python-dotenv, Notion API (v2022-06-28)
- Betroffener Kurs: AFW-2026_1 (22 Aktivitäten: url=10, quiz=5, page=2, forum=2, folder=1, feedback=1, zoom=1)

### Architektur-Entscheidungen
- **Strategy-Pattern für Modtype-Handler**: Dispatcher-Dict `MODTYPE_HANDLERS` registriert pro Modtype eine Handler-Funktion. Künftige Typen (quiz, forum, assign) ohne Umbau ergänzbar.
- **`url`-Typ**: Kein Datei-Download. Ziel-URL aus Aktivitätsseite extrahieren, als Notion-Bookmark-Block speichern.
- **`page`-Typ**: Textextraktion statt Download. Inhalt als Notion-Paragraph-Block.
- **`folder`-Typ**: Multi-File-Download → eine Notion-Seite pro Datei. Synthetische cmids (`{folder_cmid}_file_{idx}`). Folder-Aktivität selbst als `synced` markiert.
- **Graceful Degradation**: Fehlende Extraktion → Aktivität als `error` markieren, Push läuft weiter.

## Implementierungsschritte

### Schritt 1: `_extract_url_target()` hinzufügen
- Datei: `learnweb_sync.py`
- Position: Nach `_download_resource()` (~Zeile 480)
- Änderung: Extrahiert Ziel-URL einer Moodle-URL-Aktivität. Fälle: (1) Redirect 301/302/303/307, (2) `<div class="urlworkaround">`, (2b) Embed `<iframe src>`, (2c) Popup `onclick="window.open(...)"`, (3) generischer `<a>`-Link in `region-main` als letzter Fallback. Login-Redirects filtern via `BASE_URL + "/login/"`.
- Code-Snippet:
```python
def _extract_url_target(
    session: requests.Session, view_url: str
) -> str | None:
    """
    Extrahiert die Ziel-URL aus einer Moodle-URL-Aktivität.
    Gibt die externe URL zurück, oder None bei Fehler.
    """
    resp = session.get(view_url, allow_redirects=False, timeout=30)
    # Fall 1: Redirect direkt zur Ziel-URL
    if resp.status_code in (301, 302, 303, 307):
        target = resp.headers.get("Location", "")
        if target and not (target.startswith(BASE_URL) and "/login/" in target):
            return target
    # Falls kein Redirect: HTML direkt aus erster Response parsen
    if resp.status_code == 200:
        soup = BeautifulSoup(resp.text, "html.parser")
        workaround = soup.find("div", class_="urlworkaround")
        if workaround:
            a = workaround.find("a", href=True)
            if a:
                return a["href"]
        main = soup.find("div", id="region-main")
        if main:
            iframe = main.find("iframe", src=True)
            if iframe and iframe["src"] != view_url:
                return iframe["src"]
            onclick_el = main.find(attrs={"onclick": re.compile(r"window\.open\(['\"]([^'\"]+)")})
            if onclick_el:
                m = re.search(r"window\.open\(['\"]([^'\"]+)", onclick_el["onclick"])
                if m:
                    return m.group(1)
            a = main.find("a", href=re.compile(r"^https?://"))
            if a and a["href"] != view_url:
                return a["href"]
    log.warning(f"Keine Ziel-URL extrahierbar: {view_url}")
    return None
```

### Schritt 2: `_extract_page_content()` hinzufügen
- Datei: `learnweb_sync.py`
- Position: Nach `_extract_url_target()`
- Änderung: Textextraktion aus Moodle-Page-Aktivität via `div.generalbox` oder `div#region-main`. Sicherheitsnetz 50.000 Zeichen (kein hartes 2000-Limit – `_notion_paragraph_block()` chunkt automatisch).
- Code-Snippet:
```python
def _extract_page_content(
    session: requests.Session, view_url: str
) -> str | None:
    resp = session.get(view_url, allow_redirects=True, timeout=30)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")
    content_div = (
        soup.find("div", class_="generalbox")
        or soup.find("div", id="region-main")
    )
    if not content_div:
        log.warning(f"Kein Page-Inhalt gefunden: {view_url}")
        return None
    text = content_div.get_text(separator="\n", strip=True)
    if len(text) > 50000:
        text = text[:49997] + "..."
    return text if text else None
```

### Schritt 3: `_extract_folder_files()` hinzufügen
- Datei: `learnweb_sync.py`
- Position: Nach `_extract_page_content()`
- Änderung: Extrahiert Dateiliste einer Moodle-Folder-Aktivität. Scope eingeschränkt auf `div.foldertree`/`div.filemanager` damit der optionale "Download folder"-ZIP-Button nicht mitzählt. Parst `pluginfile.php`-Links.
- Code-Snippet:
```python
def _extract_folder_files(
    session: requests.Session, view_url: str
) -> list[dict] | None:
    resp = session.get(view_url, allow_redirects=True, timeout=30)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")
    files = []
    scope = (
        soup.find("div", class_="foldertree")
        or soup.find("div", class_="filemanager")
        or soup.find("div", id="region-main")
        or soup
    )
    for a in scope.find_all("a", href=re.compile(r"pluginfile\.php")):
        href = a["href"]
        if not href.startswith("http"):
            href = BASE_URL + href
        name = a.get_text(strip=True) or href.split("/")[-1].split("?")[0]
        if name and href:
            files.append({"name": name, "url": href})
    if not files:
        log.warning(f"Keine Dateien in Folder: {view_url}")
        return None
    return files
```

### Schritt 4: `notion_create_lw_page()` erweitern
- Datei: `learnweb_sync.py`
- Änderung: Neuer optionaler Parameter `content_blocks: list[dict] | None`. Batching-Guard für Notion 100-Block-Limit: `content_blocks[:100]` beim Create, Overflow via `PATCH /v1/blocks/{page_id}/children` in 100er-Batches.
- Code-Snippet:
```python
def notion_create_lw_page(
    resource: dict,
    file_upload_id: str | None,
    course_notion_page_id: str | None,
    content_blocks: list[dict] | None = None,
) -> str:
    # ... properties wie bisher ...
    body: dict = {
        "parent": {"database_id": NOTION_LW_DB_ID},
        "properties": properties,
    }
    if content_blocks:
        body["children"] = content_blocks[:100]
    resp = _notion_request(
        "POST", f"{NOTION_API}/pages",
        headers=_notion_headers(), json=body,
    )
    page_id = resp.json()["id"]
    if content_blocks and len(content_blocks) > 100:
        for i in range(100, len(content_blocks), 100):
            _notion_request(
                "PATCH", f"{NOTION_API}/blocks/{page_id}/children",
                headers=_notion_headers(), json={"children": content_blocks[i:i+100]},
            )
    return page_id
```

### Schritt 5: Helfer für Notion-Content-Blocks
- Datei: `learnweb_sync.py`
- Position: Vor `notion_create_lw_page()`
- Änderung: Hilfsfunktionen für bookmark/paragraph/heading Blocks. `_notion_paragraph_block` chunkt rich_text automatisch auf 2000 Zeichen.
- Code-Snippet:
```python
def _notion_bookmark_block(url: str) -> dict:
    return {"type": "bookmark", "bookmark": {"url": url}}

def _notion_paragraph_block(text: str) -> dict:
    chunks = [text[i:i+2000] for i in range(0, len(text), 2000)]
    return {
        "type": "paragraph",
        "paragraph": {
            "rich_text": [{"type": "text", "text": {"content": chunk}} for chunk in chunks]
        },
    }

def _notion_heading_block(text: str, level: int = 3) -> dict:
    key = f"heading_{level}"
    return {
        "type": key,
        key: {"rich_text": [{"type": "text", "text": {"content": text}}]},
    }
```

### Schritt 6: `cmd_push()` refactoren – Modtype-Filter + Dispatcher
- Datei: `learnweb_sync.py`
- Änderung 1: SQL-Query `WHERE modtype IN ('resource', 'url', 'page', 'folder')` + `modtype` in SELECT.
- Änderung 2: Dispatcher-Dict `MODTYPE_HANDLERS` nach SELECT. Handler einheitlich `(pushed, no_file, errors)` zurückgeben.
- Code-Snippet:
```python
rows = conn.execute(
    f"""
    SELECT cmid, course_id, name, view_url, course_shortname, modtype
    FROM resources
    WHERE modtype IN ('resource', 'url', 'page', 'folder')
      AND notion_id IS NULL
      AND course_id IN ({placeholders})
    ORDER BY course_id, first_seen
    """,
    list(active.keys()),
).fetchall()

MODTYPE_HANDLERS = {
    "resource": _push_resource,
    "url": _push_url,
    "page": _push_page,
    "folder": _push_folder,
}

for cmid, course_id, name, view_url, db_course_shortname, modtype in rows:
    log.info(f"  Push [{modtype}]: {name} (cmid={cmid})")
    handler = MODTYPE_HANDLERS.get(modtype)
    if handler:
        p, n, e = handler(session, conn, cmid, course_id, name,
                          view_url, db_course_shortname, active, course_map)
        pushed += p; no_file += n; errors += e
    else:
        log.warning(f"  Kein Handler für modtype={modtype}, übersprungen")
```

### Schritt 7: Handler `_push_url()`
- Datei: `learnweb_sync.py`
- Änderung: Extrahiert Ziel-URL via `_extract_url_target()`. Notion-Seite mit Bookmark-Block, kein File-Upload. Kategorie `"R Resource"`. Manifest: `notion_id`, `status='synced'`. Bei Fehler: `status='error'`.
- Code-Snippet:
```python
def _push_url(session, conn, cmid, course_id, name, view_url,
              db_course_shortname, active, course_map):
    try:
        target_url = _extract_url_target(session, view_url)
    except Exception as e:
        log.error(f"    URL-Extraktion fehlgeschlagen: {e}")
        conn.execute("UPDATE resources SET status = 'error' WHERE cmid = ?", (cmid,))
        conn.commit()
        return 0, 0, 1
    if not target_url:
        log.warning(f"    Keine Ziel-URL → Seite ohne Bookmark")
    content_blocks = [_notion_bookmark_block(target_url)] if target_url else None
    map_shortname = course_map.get(course_id, {}).get("shortname", "")
    resource = {
        "cmid": cmid, "course_id": course_id,
        "name": name, "file_name": None,
        "course_shortname": map_shortname or db_course_shortname or "",
    }
    try:
        notion_id = notion_create_lw_page(
            resource, None, active.get(course_id),
            content_blocks=content_blocks,
        )
    except Exception as e:
        log.error(f"    Notion-Fehler: {e}")
        conn.execute("UPDATE resources SET status = 'error' WHERE cmid = ?", (cmid,))
        conn.commit()
        return 0, 0, 1
    conn.execute(
        "UPDATE resources SET notion_id = ?, status = 'synced' WHERE cmid = ?",
        (notion_id, cmid),
    )
    conn.commit()
    log.info(f"    ✓ URL-Seite angelegt")
    return 0, 1, 0
```

### Schritt 8: Handler `_push_page()`
- Datei: `learnweb_sync.py`
- Änderung: Analog `_push_url()`, aber Inhalt via `_extract_page_content()` → `_notion_paragraph_block(text)`. Kategorie via bestehende `_guess_kategorie(name)`-Heuristik.

### Schritt 9: Handler `_push_folder()`
- Datei: `learnweb_sync.py`
- Änderung: Extrahiert Dateiliste via `_extract_folder_files()`. Iteriert pro Datei: Download (analog `_download_resource()`, direkt auf `pluginfile.php`-URL) → Upload → Notion-Seite.
- Synthetische cmids: `{folder_cmid}_file_{idx}`.
- INSERT-Logik: Pro Datei `INSERT OR IGNORE INTO resources (cmid, course_id, name, modtype, status) VALUES (...)` – idempotent.
- Re-Run: Bereits gesyncte `_file_{idx}`-Einträge (notion_id IS NOT NULL) werden durch WHERE-Klausel übersprungen. Neue Dateien (höherer idx) ergänzt. Entfernte bleiben als verwaiste Einträge (kein Delete).
- Idempotenz: Scraper kennt nur Folder-cmid; `_file_`-Einträge ausschließlich von `_push_folder()` verwaltet.
- Folder-Aktivität selbst als `synced` markiert.

### Schritt 10: `resource`-Logik in `_push_resource()` extrahieren
- Datei: `learnweb_sync.py`
- Änderung: Refactoring – bestehende Resource-Push-Logik aus `cmd_push()`-Loop in eigene Funktion `_push_resource()` mit einheitlicher Handler-Signatur verschieben. Keine funktionale Änderung.

### Schritt 11: Counter-Logik in `cmd_push()`
- Datei: `learnweb_sync.py`
- Änderung: Alle Handler geben `(pushed, no_file, errors): tuple[int, int, int]` zurück. Dispatcher aggregiert via `p, n, e = handler(...); pushed += p; no_file += n; errors += e`. Zusätzlich `modtype_counts: dict[str, int]` für Summary-Print mit Modtype-Aufschlüsselung.

### Schritt 12: Tests
- Datei: `tests/test_learnweb_sync.py`
- Änderung: Unit- und Integration-Tests für neue Funktionalität:
  - `test_extract_url_target_redirect`: Redirect 301/302
  - `test_extract_url_target_html_workaround`: HTML-Parsing `urlworkaround`-Div
  - `test_extract_page_content`: Textextraktion aus `generalbox`-Div
  - `test_extract_folder_files`: Dateilisten-Extraktion mit `pluginfile.php`-Links
  - `test_cmd_push_handles_url_modtype`: Integration URL-Push
  - `test_cmd_push_handles_page_modtype`: Integration Page-Push
  - `test_cmd_push_handles_folder_modtype`: Integration Folder-Push mit Multi-File

### Schritt 13: Architekturplan aktualisieren
- Datei: `docs/PLAN.md`
- Änderung: Aktivitätstypen-Tabelle ergänzen (`url`/`page`/`folder` → Phase 2+). Phase 5 Referenz auf url/page/folder entfernen.

## Testkriterien
- [ ] `_extract_url_target()` liefert korrekte URL bei: (a) 302-Redirect, (b) `urlworkaround`-HTML, (c) fehlend → None
- [ ] `_extract_page_content()` liefert Plaintext bei `generalbox`, leerer Inhalt → None
- [ ] `_extract_folder_files()` liefert Dateiliste bei `pluginfile.php`-Links, leerer Folder → None
- [ ] `cmd_push()` verarbeitet `url`-Einträge: Notion-Seite mit Bookmark, kein File-Upload, `status='synced'`
- [ ] `cmd_push()` verarbeitet `page`-Einträge: Notion-Seite mit Text-Content, kein File-Upload, `status='synced'`
- [ ] `cmd_push()` verarbeitet `folder`-Einträge: Eine Notion-Seite pro Datei, Dateien heruntergeladen + hochgeladen
- [ ] `cmd_push()` verarbeitet `resource`-Einträge weiterhin korrekt (Regression)
- [ ] Bestehende Tests laufen grün: `./.venv/bin/python -m unittest tests.test_learnweb_sync`
- [ ] Live-Test gegen AFW-2026_1: ≥10 URL + 2 Page + 1 Folder erfolgreich gepusht
- [ ] Fehlerhafte Extraktionen → `status='error'` im Manifest, Push läuft weiter

## Abbruchbedingungen
- Stoppe wenn: Moodle-HTML-Struktur für `url`/`page`/`folder` fundamental abweicht → Abweichung dokumentieren, nicht raten
- Stoppe wenn: Notion API `children`-Blocks (Bookmark, Paragraph) unerwartet fehlschlagen → API-Version prüfen, ggf. auf Rich-Text-Fallback wechseln
- Bei Unklarheit: Nicht eigenmächtig weitermachen, Abweichung dokumentieren
- Bei fehlgeschlagenem Test: Nicht den nächsten Schritt beginnen, Fehler zuerst beheben
