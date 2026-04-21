# URL-Aktivitäten als `Ziel-URL`-Property speichern
> Quelle: Notion Coding Pipeline – 2026-04-21
> Repo: https://github.com/tbmn1996/learnweb_sync
> Notion-Seite: https://www.notion.so/URL-Aktivit-ten-als-Ziel-URLProperty-speichern

## Kontext
- Projekt: `tbmn1996/learnweb_sync` (Ref `bedaad569`)
- Relevante Dateien:
  - `learnweb_sync.py` — enthält `_build_lw_page_properties`, `notion_create_lw_page`, `notion_update_lw_page`, `_push_url_activity`, `_push_page_activity`, `_build_bookmark_block`, Dispatcher in `cmd_push` (`handlers`-Dict mit `resource`/`folder`/`url`/`page`).
  - `tests/test_learnweb_sync.py` — zentrale Testdatei (~67 KB).
  - `docs/PLAN.md` — Architekturplan inkl. Feldmapping für `Learnweb Inhalte`.
  - `README.md` — CLI- und Deployment-Dokumentation.
- Abhängigkeiten: Notion API (aktuell Version `2022-06-28`), bestehende Notion-DB adressiert über Env `NOTION_LW_DB_ID`.
- Architektur-Entscheidungen:
  - **Additiver keyword-only Parameter** `target_url: str | None = None` in `_build_lw_page_properties`, `notion_create_lw_page`, `notion_update_lw_page` → keine Regression für `resource`, `folder`, `page`.
  - **`Ziel-URL`-Property nur setzen, wenn `target_url` truthy ist** — ein explizites `None` darf keinen Key erzeugen.
  - **Bookmark-Block + `notion_append_page_children`-Aufruf im URL-Pfad ersatzlos entfernen** — keine parallele Haltung, sonst doppelte Sichtbarkeit / inkonsistente Wahrheit.
  - **`_build_bookmark_block()` löschen** — nach Umbau keine Call-Sites mehr.
  - **Kein Backfill**: `_push_url_activity` überspringt bereits `notion_id IS NOT NULL` → Altbestand bleibt bewusst unverändert.
  - **Schema-Migration außerhalb des Codes**: `Ziel-URL` (Typ `URL`) muss vor jedem Deploy in der aktiv genutzten DB (`NOTION_LW_DB_ID`) manuell angelegt werden.
  - **Pre-Flight-Schema-Check in `cmd_push()`**: einmalig via `loadDataSource` prüfen, ob `TARGET_URL_PROPERTY` im Schema existiert → Early-Exit mit klarer Logmeldung, verhindert kaskadierende 400-`validation_error`-Fehler pro Row.
  - **Property-Name via Env-Var entkoppeln**: `TARGET_URL_PROPERTY = os.getenv("NOTION_LW_TARGET_URL_PROPERTY", "Ziel-URL")` als Modulkonstante; Code + Tests referenzieren ausschließlich diese Konstante.

## Implementierungsschritte

### Schritt 1: Vorbedingung (manuell in Notion)
- Datei: — (Notion-UI)
- Änderung: In der via `NOTION_LW_DB_ID` adressierten DB (aktuell TESTING) die Property `Ziel-URL` als `URL` anlegen.
- UI-Check: Property taucht in der Schema-Leiste auf.
- Vor jedem Prod-Switch dieselbe Property in der dortigen DB ebenfalls anlegen — der Code erzeugt das Schema nicht selbst.

### Schritt 2: Modulkonstante einführen
- Datei: `learnweb_sync.py`
- Änderung: Am Kopf der Datei Modulkonstante definieren, sodass Code + Tests ausschließlich diese Konstante referenzieren (statt des String-Literals `"Ziel-URL"`).
- Code-Snippet:
  ```python
  TARGET_URL_PROPERTY = os.getenv("NOTION_LW_TARGET_URL_PROPERTY", "Ziel-URL")
  ```

### Schritt 3: `_build_lw_page_properties` erweitern
- Datei: `learnweb_sync.py`
- Änderung: Signatur um keyword-only Parameter `target_url: str | None = None` ergänzen. Direkt vor dem `if course_notion_page_id:`-Block einfügen:
  ```python
  if target_url:
      properties[TARGET_URL_PROPERTY] = {"url": target_url}
  ```
- Wichtig: `target_url=None` darf keinen Key erzeugen.

### Schritt 4: `notion_create_lw_page` und `notion_update_lw_page` durchreichen
- Datei: `learnweb_sync.py`
- Änderung: Beide Funktionen um denselben keyword-only Parameter `target_url: str | None = None` erweitern und unverändert an `_build_lw_page_properties` weiterreichen.

### Schritt 5: Pre-Flight-Schema-Check in `cmd_push()`
- Datei: `learnweb_sync.py`
- Änderung: In `cmd_push()` **vor** dem Dispatcher einmalig das Schema der LW-DB laden und prüfen, ob `TARGET_URL_PROPERTY` im Schema vorhanden ist. Ergebnis cachen (keine zusätzlichen API-Calls pro Row).
- Code-Snippet (pseudocode-artig):
  ```python
  schema = notion_load_data_source(NOTION_LW_DB_ID)
  if TARGET_URL_PROPERTY not in schema["properties"]:
      log.error(
          f"Pflicht-Property '{TARGET_URL_PROPERTY}' fehlt in LW-DB → "
          f"Schema-Migration vor Deploy durchführen"
      )
      return  # Early-Exit, kein Row-Processing
  ```

### Schritt 6: `_push_url_activity` umbauen
- Datei: `learnweb_sync.py`
- Änderung:
  - Aufruf ersetzen durch:
    ```python
    notion_id = notion_create_lw_page(
        resource, None, course_notion_page_id, target_url=target_url
    )
    ```
  - `notion_append_page_children(...)`-Zeile und den zugehörigen `_build_bookmark_block`-Aufruf **entfernen**.
  - `_archive_page_after_failure`-Fallback aus dem URL-Pfad entfernen: einziger verbleibender API-Call ist `notion_create_lw_page` → schlägt er fehl, existiert keine `notion_id` → Archive-Branch unerreichbar. Except-Handler auf `_mark_activity_error` + Return reduzieren.
  - Log anpassen: `log.info("    ✓ URL-Seite angelegt (Ziel-URL gesetzt)")`.

### Schritt 7: `_build_bookmark_block` entfernen
- Datei: `learnweb_sync.py`
- Änderung: Funktion `_build_bookmark_block()` ersatzlos entfernen — keine weiteren Call-Sites im Modul.

### Schritt 8: Dokumentation aktualisieren
- Datei: `docs/PLAN.md`
  - Feldmapping-Tabelle um Zeile `Ziel-URL | url | _extract_url_target(), nur für modtype=url` erweitern.
  - Formulierung „Bookmark-/Paragraph-Blöcke“ im Sequenzdiagramm zu „Paragraph-Blöcke (nur page)“ präzisieren.
  - Altbestands-UI-Inkonsistenz dokumentieren: bereits synchronisierte `url`-Seiten behalten ihren Bookmark-Block im Seiteninhalt, neue Seiten zeigen die Zieladresse ausschließlich als `Ziel-URL`-Property.
- Datei: `README.md`
  - Abschnitt „Was das Skript macht“ klarstellen: `url`-Aktivitäten landen als Row mit `Ziel-URL`-Property, `page`-Aktivitäten weiterhin als Paragraph-Blöcke.

### Schritt 9: Tests
- Datei: `tests/test_learnweb_sync.py`
- Änderung:
  - Neuer Test `test_build_lw_page_properties_sets_target_url`: mit `target_url="https://example.com/doc"` → `properties[TARGET_URL_PROPERTY] == {"url": "https://example.com/doc"}`.
  - Neuer Test `test_build_lw_page_properties_without_target_url`: ohne `target_url` fehlt `TARGET_URL_PROPERTY` (Regression).
  - Bestehenden URL-Push-Test (Bookmark) ersetzen: Mock verifiziert, dass `notion_create_lw_page` mit `target_url="<externe URL>"` aufgerufen wird und `notion_append_page_children` für `modtype=url` **nicht** mehr aufgerufen wird (`mock.assert_not_called()`). DB-Status nach Run = `synced`.
  - Schema-Mismatch-Fehlerpfad-Test: `notion_create_lw_page` wirft `APIResponseError(code="validation_error")` → verifiziert: (a) `_mark_activity_error` wird mit sprechender Message aufgerufen, (b) keine `notion_id` gesetzt, (c) Folge-Aktivitäten laufen weiter (kein Hard-Abort).
  - Pre-Flight-Schema-Check-Test: `loadDataSource`-Mock liefert Schema ohne `Ziel-URL` → `cmd_push()` beendet sich mit klarer Error-Logmeldung und ohne Row-Verarbeitung.
  - Regressions-Guard gegen `_build_bookmark_block`-Reintroduktion:
    ```python
    assert not hasattr(learnweb_sync, "_build_bookmark_block")
    ```
  - `page`-Test muss weiter Paragraph-Blöcke anhängen; vorhandene `resource`- und `folder`-Tests bleiben unverändert grün.

### Schritt 10: Lokale Verifikation
- `./.venv/bin/python -m unittest tests.test_learnweb_sync` grün.
- Optional Smoke-Test: `python learnweb_sync.py push` gegen TESTING-DB mit einer frischen `url`-Aktivität → Notion-Seite zeigt `Ziel-URL`, kein Bookmark-Block im Inhalt.

## Testkriterien
- [ ] `Ziel-URL` existiert in der aktiven `Learnweb Inhalte`-DB als `URL`-Property (Notion-UI verifiziert).
- [ ] `_build_lw_page_properties(..., target_url="...")` liefert `{TARGET_URL_PROPERTY: {"url": "..."}}` im Properties-Dict.
- [ ] `_build_lw_page_properties(...)` ohne `target_url` enthält keinen `TARGET_URL_PROPERTY`-Key (Regression).
- [ ] `_push_url_activity()` erzeugt eine Notion-Seite mit gesetzter `Ziel-URL`-Property **und ohne** Bookmark-Block im Seiteninhalt.
- [ ] Für `modtype=url` wird `notion_append_page_children` nicht mehr aufgerufen (Mock-Assert).
- [ ] Manifest nach URL-Push: `status='synced'`, `notion_id` gesetzt, `file_hash = md5(target_url)`.
- [ ] `_push_page_activity`-Tests (Paragraph-Blöcke) laufen unverändert grün.
- [ ] Resource- und Folder-Tests laufen unverändert grün.
- [ ] `_build_bookmark_block` ist im Modul nicht mehr definiert (diff-geprüft **und** automatisch via `assert not hasattr(learnweb_sync, "_build_bookmark_block")`).
- [ ] `TARGET_URL_PROPERTY` ist als Modulkonstante definiert; Code und Tests referenzieren sie statt des String-Literals `"Ziel-URL"`.
- [ ] `cmd_push()` führt Pre-Flight-Schema-Check aus (`loadDataSource` → Property-Existenz-Prüfung); Early-Exit bei fehlender `Ziel-URL`-Property mit klarer Logmeldung.
- [ ] Schema-Mismatch-Test: `notion_create_lw_page` wirft `validation_error` → `_mark_activity_error` mit sprechender Message, keine `notion_id`, Folge-Aktivitäten unberührt.

## Abbruchbedingungen
- Stoppe, wenn die Notion-API beim Setzen der `Ziel-URL`-Property `validation_error` wirft → API-Version (`2022-06-28`) und Property-Name gegen das aktive Schema (via `loadDataSource`) prüfen statt raten.
- Stoppe, wenn im Test-Setup bereits synchronisierte URL-Seiten auftauchen, die backfilled werden sollen → Scope ist explizit „kein Backfill“; solche Anforderung erst dokumentieren, dann separat planen.
- Stoppe, wenn beim Löschen von `_build_bookmark_block` ein weiterer Caller auftaucht (z. B. später ergänzter Handler) → Abhängigkeit dokumentieren, Entfernung zurückstellen.
- Bei Fehlschlag im Dispatcher-Flow von `cmd_push` mit gemischten Modtypes: erst Ursache isolieren (Logs + gezielter Unit-Test), nicht den nächsten Schritt beginnen.
- Bei Unklarheit: Abweichung dokumentieren und Rückfrage stellen, nicht eigenmächtig weitermachen.
