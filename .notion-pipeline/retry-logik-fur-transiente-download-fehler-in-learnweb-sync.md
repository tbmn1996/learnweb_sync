# Retry-Logik für transiente Download-Fehler in learnweb_sync
> Quelle: Notion Coding Pipeline – 2026-04-21
> Repo: https://github.com/tbmn1996/learnweb_sync
> Notion-Seite: https://www.notion.so/

## Kontext
- Projekt: learnweb_sync
- Relevante Dateien:
  - `learnweb_sync.py` – enthält `_download_resource()` (Z. 800–862) und `cmd_push()` (Z. 1026–1041)
- Abhängigkeiten: `requests` (pip, via `.venv`), `time` (stdlib, bereits importiert), `BeautifulSoup` (bereits importiert)
- Architektur-Entscheidungen:
  - Retry innerhalb `_download_resource()` statt Decorator (nur ein Aufrufpunkt → minimale Komplexität).
  - Explizites Exception-Tuple `(Timeout, ChunkedEncodingError, ConnectionError)` statt `RequestException`, damit HTTP 4xx/5xx nicht geretried werden.
  - Nach erschöpften Retries `raise last_exc` statt `return None` (Aufrufer unterscheidet Exception vs. None).
  - `try/finally` mit `resp.close()` schützt vor Connection-Leak bei jeder Exception-Art.
  - Content-Length-Check nach `iter_content()` fängt Silent Truncation ab → `ConnectionError` → automatischer Retry (nur wenn Server Content-Length sendet).

## Cross-Ticket-Koordination
- Bezug: Ticket B „learnweb_sync: Support für url/page/folder Modtypes im Push" (Status „In Umsetzung").
- Merge-Reihenfolge: Dieses Ticket A (Retry) zuerst mergen, B setzt danach auf die Retry-aktive Version auf.
- Nach Merge von B liegt die `try/except`-Kaskade um den Download-Aufruf in `_push_resource()` statt direkt in `cmd_push()`; Retry-Logik bleibt in `_download_resource()` funktional.
- MANDATORY für Ticket B: `_push_folder()` muss `_download_resource()` wiederverwenden oder die identische Retry-Logik bekommen. Empfohlen: `_download_resource()` in B um optionalen Parameter `direct_url: str | None = None` erweitern.
- Test-Datei `tests/test_learnweb_sync.py`: Merge-Konflikte primär in Imports/Fixtures erwartet.
- Line-Number-Diskrepanz (A: Z. 800–862, B: ~Z. 480) → Funktionsname ist maßgeblich.

## Implementierungsschritte

### Schritt 1: Retry-Loop in `_download_resource()` einbauen
- Datei: `learnweb_sync.py`
- Funktion: `_download_resource()` (Z. 800–862)
- Änderung: Gesamten Funktionsinhalt in Retry-Loop wrappen.
  - Konstanten: `MAX_RETRIES = 3`, `BACKOFF = [2, 4, 8]`, `RETRYABLE = (requests.exceptions.Timeout, requests.exceptions.ChunkedEncodingError, requests.exceptions.ConnectionError)`.
  - Bisherigen Funktionskörper in `for attempt in range(MAX_RETRIES): try: ... except RETRYABLE: ... finally: resp.close()` einbetten.
  - `return None`-Pfade (kein Download-Link, Datei zu groß) bleiben außerhalb des Retry und verlassen Funktion sofort.
  - Bei `RETRYABLE`-Exception: loggen mit Versuchsnummer (inkl. `type(exc).__name__`), `time.sleep(BACKOFF[attempt])` warten; bei letztem Versuch ebenfalls loggen („Retries erschöpft").
  - Nach erschöpften Retries: `raise last_exc`.
  - Nach `iter_content()`: `if content_length and len(file_bytes) < content_length: raise requests.exceptions.ConnectionError(...)` → triggert Retry bei Silent Truncation.
- Code-Snippet:
  ```python
  def _download_resource(
      session: requests.Session, view_url: str
  ) -> tuple[bytes, str] | None:
      """
      Lädt eine Moodle-Ressource herunter.
      Gibt (file_bytes, filename) zurück, oder None bei Fehler.
      Bei transienten Netzwerkfehlern: bis zu 3 Versuche mit exponential backoff (2s/4s/8s).
      """
      MAX_RETRIES = 3
      BACKOFF = [2, 4, 8]
      RETRYABLE = (
          requests.exceptions.Timeout,
          requests.exceptions.ChunkedEncodingError,
          requests.exceptions.ConnectionError,
      )

      last_exc: Exception | None = None
      for attempt in range(MAX_RETRIES):
          resp = None
          try:
              resp = session.get(view_url, allow_redirects=True, stream=True, timeout=60)
              resp.raise_for_status()

              if "text/html" in resp.headers.get("Content-Type", ""):
                  html = resp.text
                  resp.close()
                  soup = BeautifulSoup(html, "html.parser")
                  dl_link = soup.find("a", href=re.compile(r"pluginfile\.php"))
                  if not dl_link:
                      dl_link = soup.find("a", href=re.compile(r"forcedownload"))
                  if not dl_link:
                      log.warning(f"Kein Download-Link in HTML-Seite: {view_url}")
                      return None
                  file_url = dl_link["href"]
                  if not file_url.startswith("http"):
                      file_url = BASE_URL + file_url
                  resp = session.get(file_url, allow_redirects=True, stream=True, timeout=60)
                  resp.raise_for_status()

              cd = resp.headers.get("Content-Disposition", "")
              m = re.search(r"filename\*?=(?:UTF-8'')?[\"']?([^\"';\r\n]+)", cd, re.IGNORECASE)
              if m:
                  filename = m.group(1).strip().strip("\"'")
              else:
                  url_path = resp.url.split("?")[0].rstrip("/")
                  filename = url_path.split("/")[-1] or "download.bin"

              MAX_DOWNLOAD_BYTES = 20 * 1024 * 1024
              content_length = int(resp.headers.get("Content-Length", 0))
              if content_length > MAX_DOWNLOAD_BYTES:
                  log.warning(
                      f"Datei zu groß ({content_length / 1024 / 1024:.1f} MB) – überspringe: {filename}"
                  )
                  return None

              file_bytes = b"".join(resp.iter_content(chunk_size=256 * 1024))

              if content_length and len(file_bytes) < content_length:
                  raise requests.exceptions.ConnectionError(
                      f"Unvollständiger Download: {len(file_bytes)}/{content_length} Bytes – {filename}"
                  )

              if len(file_bytes) > MAX_DOWNLOAD_BYTES:
                  log.warning(
                      f"Datei zu groß nach Download ({len(file_bytes) / 1024 / 1024:.1f} MB) – überspringe: {filename}"
                  )
                  return None

              return file_bytes, filename

          except RETRYABLE as exc:
              last_exc = exc
              if attempt < MAX_RETRIES - 1:
                  wait = BACKOFF[attempt]
                  log.warning(
                      f"    Download-Fehler (Versuch {attempt + 1}/{MAX_RETRIES}): {type(exc).__name__}: {exc} – warte {wait}s"
                  )
                  time.sleep(wait)
              else:
                  log.warning(
                      f"    Download-Fehler (Versuch {attempt + 1}/{MAX_RETRIES}): {type(exc).__name__}: {exc} – Retries erschöpft"
                  )
          finally:
              if resp is not None:
                  resp.close()

      raise last_exc
  ```

### Schritt 2: Aufrufer prüfen (keine Änderung)
- Datei: `learnweb_sync.py`
- Funktion: `cmd_push()` bzw. `_push_resource()` nach Merge von Ticket B (Z. 1026–1041 Pre-B-Stand)
- Änderung: Keine Änderung nötig.
  - Prüfung: `except Exception as e` fängt `raise last_exc` korrekt → `status='error'` gesetzt.
  - Prüfung: `if result is None` fängt `return None`-Pfade (Datei zu groß, kein Link) korrekt.
- Hinweis: Nach Merge von Ticket B liegt die `try/except`-Kaskade in `_push_resource()`; Aufrufvertrag (`tuple[bytes, str] | None` oder Exception) bleibt identisch.

### Schritt 3: Post-Deploy – fehlgeschlagene Items nachholen
- Befehl: `cd /Users/thomasniermann/Scripts/learnweb_sync && .venv/bin/python learnweb_sync.py push`
- Items mit `status='error'` und `notion_id IS NULL` werden automatisch neu versucht.

## Testkriterien
- [ ] `_download_resource()` gibt bei erfolgreichem Download weiterhin `(file_bytes, filename)` zurück.
- [ ] Bei transientem Fehler (Timeout/ConnectionError/ChunkedEncodingError) wird bis zu 3x mit Delay 2s/4s/8s retried.
- [ ] Bei HTTP 4xx/5xx (HTTPError via `raise_for_status()`) wird nicht geretried → Exception propagiert sofort.
- [ ] Bei „Datei zu groß" wird nicht geretried → `return None` sofort.
- [ ] Bei „Kein Download-Link in HTML" wird nicht geretried → `return None` sofort.
- [ ] Nach 3 fehlgeschlagenen Retries wird `last_exc` geraised → `cmd_push()` loggt error und setzt `status='error'`.
- [ ] Kein neuer Import nötig (`time` bereits vorhanden).
- [ ] Rückgabetyp `tuple[bytes, str] | None` bleibt unverändert.
- [ ] Log-Output enthält Versuchsnummer bei Retry (`Versuch 1/3`, `Versuch 2/3`, `Versuch 3/3 – Retries erschöpft`).
- [ ] Bei unvollständigem Download (len < Content-Length) wird automatisch retried (`ConnectionError`).
- [ ] Bei fehlendem Content-Length-Header (0) wird kein Truncation-Check ausgelöst.
- [ ] `resp.close()` wird via `finally` bei jeder Exception aufgerufen (nicht nur RETRYABLE).

## Abbruchbedingungen
- Stoppe wenn: `requests.exceptions.ChunkedEncodingError` nicht im installierten `requests`-Package existiert (Versionskonflikt) → Exception-Tuple anpassen.
- Stoppe wenn: `_download_resource()` nach Änderung einen anderen Rückgabetyp hat als `tuple[bytes, str] | None`.
- Stoppe wenn: Tests zeigen, dass `HTTPError` fälschlicherweise geretried wird.
- Bei Unklarheit: STOP → Abweichung dokumentieren → nicht eigenmächtig weitermachen.
