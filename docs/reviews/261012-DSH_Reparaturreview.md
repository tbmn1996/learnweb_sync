# Unabhängige Abschlussprüfung: LearnWeb-Semesterreparatur

**Datum:** 12.10.2026 · **Prüfstand:** HEAD `825c13d`, Arbeitsstand mit 30 Änderungen
**Eingefrorene Hashes:** `learnweb_sync.py` `f5d0e061…`, `server.py` `d0d0fae3…`,
`tests/test_repair_acceptance.py` `c0daba5b…` (Arbeitsstand während des Testlaufs unverändert)
**Testlauf (vorgeschriebener Befehl, Belegwurzel wegen Sandbox-Schreibgrenze auf `/tmp` umgelenkt):**
`testlauf -C /Users/thomasniermann/Scripts/learnweb_sync -c '.venv/bin/python tests/run_offline.py' --beleg-wurzel /tmp/learnweb-final-review/testlauf-belege`
→ **197 Tests, PASS**, Beleg `…/20261012-180727-learnweb-final-review-r3-0a16/`.
**Eigene Proben:** ausschließlich `/tmp/learnweb-final-review/probes/`, offline, read-only gegen das Repo.

## Urteil

Die vier zuletzt noch roten Abnahmetests sind behoben; die vollständige Offline-Suite ist grün.
Die unabhängigen Proben bestätigen die Kernverträge: Notion-Namensnormalisierung (inkl. der zwei
live belegten Fälle `PraÌsentation…Kopie.pdf`/`.PDF`) führt zu keinem erneuten Upload, der
Wiederanlauf nach verlorenem Checkpoint erzeugt keine doppelten Seiten/Anlagen, F14-No-Match
liefert Exit 3 mit JSON ohne Schreibzugriff, Manifestmigration ist additiv, die Multipart-Bytefolge
ist über die echte Upload-Logik geprüft und der Gunicorn-Workerersatz über echte Prozesse
(Stub-CLI). **Zwei materielle Restpunkte** bleiben, beide mit
Repro belegt; der zweite ist ein enger, aber echter Erhaltungsverstoß.

## Materielle Befunde (priorisiert)

### B1 — Permanente Providergrenzen halten jeden Lauf rot und lassen den Worker wiederholt ersetzen; der OR-Ordner wird pro Lauf komplett neu geladen
**Datei:Zeile:** `learnweb_sync.py:3115` (Ordner von der Terminal-Skip-Regel ausgenommen),
`learnweb_sync.py:2737` + `:2754` (Download vor dem gespeicherten Blocked-Kurzschluss),
`learnweb_sync.py:2816` (`folder_files_blocked`, `retryable=0`), `learnweb_sync.py:3202`
(`cmd_run` → Exit 1), `server.py:356` + `:364` (Schwelle → `os._exit(1)`).

**Nutzerwirkung:** Notion lehnt `.ipynb` und `.pkl` namentlich ab (Primärbeleg
`/tmp/learnweb-railway-diagnose/original-format-probe.jsonl`: HTTP 400, „extension … not
supported“). Für den aktiven Kurs OR (`93329`, laut `docs/reviews/261012-Reparatur-Abnahme.md:19`
weiter eingeschaltet) bleibt Ordner `4117100` damit dauerhaft `terminal_error`. Jeder Werktagslauf
lädt alle 18 Originale erneut von LearnWeb (blockierte Dateien werden erst nach dem Download
erkannt), meldet einen Fehler und endet mit Exit 1. Ab Erreichen der Fehlerschwelle (Default 3,
`SYNC_FAILURE_EXIT_THRESHOLD`) ersetzt `_finish_sync` den Gunicorn-Worker nach **jedem** weiteren
Lauf (Zähler überlebt im Checkpoint); `/health` bleibt 200, `last_run.outcome` dauerhaft `failed`. Das widerspricht der Plan-Formulierung „kein endloser
Retry einer harten Providergrenze“ (F04) und „bekannten Hinweisstatus nicht als wiederholten
technischen Ausfall zählen“ (F09), auch wenn Exit 1 aus F02 ableitbar ist.

**Repro:** `/tmp/learnweb-final-review/probes/probe_folder_blocker.py` — zwei Läufe, beide
`download_calls=1` (also erneuter Download), beide `folder_files_blocked`/`retryable=0`;
Live-Zeile in `/tmp/learnweb-railway-diagnose/resource-errors.json` (cmid `4117100`,
`notion_id: null`, status `error`). `test_partial_folder_keeps_success_and_does_not_retry_blocked_upload`
kodiert den Wiederholungszustand, prüft aber weder Download-Wiederholung noch Lauf-Exit.

### B2 — Wiederanlauf kann eine manuell angehängte gleichnamige Notion-Datei ersetzen
**Datei:Zeile:** `learnweb_sync.py:2651` (Ressource ruft `notion_update_lw_page` mit neuem Upload),
`learnweb_sync.py:1628-1639` (`notion_update_lw_page` entfernt vorhandene Anlagen allein aufgrund
des normalisierten Namens, ohne Eigentums-/Hash-Nachweis); Gegenbeispiel mit Nachweis:
`learnweb_sync.py:2780-2781` (Ordnerpfad verlangt `_remote_attachment_hash(remote) == previous['hash']`).

**Nutzerwirkung:** Im Checkpoint-Zustand „Seiten-ID lokal gespeichert, `file_name`/`file_hash`
noch NULL“ (harter Abbruch/Server-Timeout zwischen `_checkpoint_notion_page` und
`_mark_activity_synced`) findet der Wiederanlauf keine lokale Identität, lädt die Quelle erneut
hoch und schreibt die `LW Download`-Liste ohne die bestehende gleichnamige Anlage. Eine manuell
angehängte Datei gleichen Namens (z. B. eine eigene Korrektur zum selben Foliensatz) ist danach
aus der Seite verschwunden. Der Ordnerpfad schützt genau diesen Fall; der Ressourcenpfad nicht,
obwohl Übergabebericht und Nutzervorgabe „abweichende gleichnamige Anlagen bleiben erhalten“
zusagen.

**Repro:** `/tmp/learnweb-final-review/probes/probe_resource_checkpoint_replace.py` — realer
`notion_update_lw_page`-Aufruf: Ergebnis-PATCH enthält nur `new-upload`/`Own.pdf`,
`manual_file_survived=false`, Zeile wird `synced`; direkter Nachweis der Drop-Regel zusätzlich in
`/tmp/learnweb-final-review/probes/probe_resource_manual_replace.py`. Minimal korrigierbar, indem
der Ressourcenpfad den neuen Upload nur bei belegtem eigenem Altstand (`row["file_hash"]` ==
Remote-Hash) als Ersatz übergibt, sonst blockiert.

## Bereits bekannte, in der laufenden Restkorrektur behobene Fehler (keine neuen Befunde)

Die vier vorab gemeldeten FAILS sind im eingefrorenen Stand behoben und durch den Testlauf
bestätigt: normalisierter Anlagenname (`test_existing_identical_resource_with_notion_normalized_name_is_not_duplicated`),
Wiederfindung nach Checkpoint-Verlust (`test_recovered_remote_page_matches_identical_file_despite_normalized_name`),
geänderte eigene Ordnerdatei bei belegtem Alt-Hash (`test_owned_folder_file_update_preserves_manual_attachment`),
Serverzählung für `! Push: … N Fehler` (`test_actual_worker_is_replaced_and_status_survives`).
Ergänzend geprüft und **nicht** als Befund gewertet: Der in der Zwischeniteration eingeführte
Vorab-Lookup war mit zwei Tests unvereinbar (unvollständige Mocks); die Korrektur hat den
Produktivcode angepasst, die Tests blieben unverändert (`tests/test_learnweb_sync.py` hashgleich,
`test_repair_acceptance.py` nur um drei Vertragstests erweitert). Governance-Vorgabe „Testautor ≠
Implementierer“ ist damit gewahrt.

## Verworfene bzw. korrigierte Behauptungen

- „16 ipynb + py direkt übernehmbar“ (`/tmp/learnweb-fix-sync-result.md:32`) ist durch den
  Primärbeleg widerlegt: Notion lehnt die Endung `.ipynb` unabhängig von MIME ab. Die
  Eltern-Abnahme (`docs/reviews/261012-Reparatur-Abnahme.md:39`) führt 16 + 1 zu Recht als offene
  Anlagen; die `.py`-Endung ist ebenfalls nicht belegt.
- „Zweiter Lauf ohne Duplikate“ aus der Live-Abnahme gilt für den **alten** Dienststand; für den
  neuen Stand ist es durch Tests/Proben gestützt, aber noch nicht deployt.
- `file`-typisierte Bestandsanlagen werden beim PATCH mit signierter URL zurückgeschrieben
  (`learnweb_sync.py:1638`) — offline nur gemockt; siehe Live-Grenzen.

## Offene Live-Abnahmen (nicht offline prüfbar; Parent)

1. Deployment des eingefrorenen Stands, echter Scheduler-Lauf Mo–Fr 06:00 UTC, Rücklesen von SHA,
   Zeitplan und `last_run`. Dabei Railway-Env prüfen: ein explizit gesetztes altes
   `CRON_SCHEDULE=0 6 * * 1-5` (numerische Wochentage) würde die F08-Korrektur aushebeln; fehlendes
   `SYNC_WEBHOOK_SECRET` macht `/health` 503 und startet den Scheduler nicht.
2. Mehrteiliger Upload + Byte-Rücklesen der 50/61-MiB-PDFs und des 247-MiB-MOV auf markierten
   Testseiten; ebenso `.py` und HTML-Dashboard `4164189`.
3. **PATCH-Rücklesen bestehender `file`-Anlagen:** Ob Notion das Zurückschreiben eines
   `file`-Objekts mit signierter URL akzeptiert, ist nicht offline belegbar. Falls nicht, bricht
   jeder Ordner-/Anlagen-Update mit vorhandener Notion-gehosteter Datei ab (400) — der
   risikoreichste ungeprüfte Punkt, da die Tests `_notion_request` mocken.
4. Kurs OR/Data Management: eindeutige cmid-Zuordnung; ob einer der IT-Sicherheits-Altduplikate
   (`79220`, Live-Log 10.10.) `SyncContent=true` hat — dann liefert der neue Kursabgleich
   (`learnweb_sync.py:1817`) dauerhaft Exit 1. Ebenso: unsynced Aktivitäten mit mehreren
   Notion-Seiten gleicher `Nr`/`Kurs-ID` stoppen jetzt hart statt zu duplizieren (gewollt, aber
   offen).
5. Transkription einer noch offenen Aufzeichnung; kein Force-Ersatz bestehender Nutzertexte.

## Grenzen dieser Prüfung

- Keine Live-/Netzprüfung, keine Secrets/`.env`, keine Repo-Änderungen, kein Commit/Deploy.
  Offenlegung: Ein Zwischenstand meiner Ressourcen-Probe rief ohne Mock zweimal `GET /v1/pages/…`
  gegen `api.notion.com` auf; beide Antworten waren 401 Unauthorized, es wurden keine Daten
  gelesen oder geschrieben. Die Probe wurde danach strikt offline gestellt.
- Worker-Grenzen laut Übergabe weiterhin gültig und nur eingeschränkt prüfbar: Alt-Transkripte ohne
  Eigentumsbeleg, unbegrenzte `attempts` nach hartem Absturz im Zustand `claimed`, fehlender
  Inhalts-Refresh bereits synchronisierter Einzelressourcen bei gleicher URL (Ordner werden jetzt
  aktualisiert).
