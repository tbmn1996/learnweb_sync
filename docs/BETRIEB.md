# Betrieb und Semesterwechsel

Verantwortlich: Thomas Niermann. Quellprojekt:
[tbmn1996/learnweb_sync](https://github.com/tbmn1996/learnweb_sync).
Zweck: ausgewählte LearnWeb-Kurse in Notion verfügbar machen, ohne tägliches manuelles Nachsehen.
Architektur: [PLAN.md](PLAN.md). Aktuelle Nachweise und Grenzen: [PRUEFBERICHT.md](PRUEFBERICHT.md).
Reparaturabnahme: [Reparatur-Abnahme](reviews/261012-Reparatur-Abnahme.md).

## Die beiden Laufwege

| Komponente | Auslöser und Kennung | Eingaben / Ausgaben |
|---|---|---|
| Railway-Sync | `server.py`, ein Gunicorn-Worker; Scheduler oder `POST /webhook/sync` | LearnWeb und Notion-Konfiguration; Kurse, Dateien, Texte und URLs nach Notion; Manifest `/data/state.db` |
| Lokale Transkription | `.venv/bin/python learnweb_sync.py transcribe`; Vorlage `de.thomasn.learnweb-transcribe` | Opencast oder YouTube; Untertitel oder lokales Whisper; Inhaltsseite und Meeting-Notiz in Notion |

Railway-Zuordnung: Projekt `TBMN Cloud Tools` (`51622856-5637-4f68-91ca-ac4d1bf20ea5`),
Umgebung `production` (`b8273690-d20d-4910-93f1-41747623092f`),
Service `032cd137-e9a2-4e13-a97b-20a0a887f949`.
Die Zuordnung stammt aus der lokalen CLI-Konfiguration und GitHub-Deployment-Metadaten.

42 reguläre Semesterinhalte und ein Transkript sind live bestätigt. Der Quellenbestand umfasst 80
ältere Aktivitäten und drei bestätigte Legacy-Links. Insgesamt sind 106 Originaldateien bytegleich geprüft. 18 von Notion abgelehnte Originaldateien bleiben offen;
vorhandene Dubletten und manuelle Inhalte wurden erhalten.

Der Sollzeitplan ist Montag bis Freitag um 06:00 UTC (08:00 Sommerzeit, 07:00 Winterzeit in
Berlin); der Code-Default lautet `0 6 * * mon-fri`. Aktive Railway-Variablen, letzter Lauf und
abgeschlossene Wiederholungsprüfungen und Providergrenzen werden im [Reparaturbericht](reviews/261012-Reparatur-Abnahme.md)
geführt.

Codecommit `ae1b0a0` ist gepusht und deployed; die 210 Offline-Tests bestehen. Folder-Dateien
werden auch bei unveränderten Links auf Inhaltsänderungen geprüft. Lesende LearnWeb-GETs werden
bei Transportfehlern und ausgewählten HTTP-5xx-Antworten begrenzt wiederholt; Schreibvorgänge
nicht. Laufende Byte- und Folder-Abnahmen stehen im Reparaturbericht.

GitHub Actions ist ein manueller Trigger, kein zweiter Scheduler. `202` heißt gestartet,
`409` heißt bereits beschäftigt; beides bestätigt keinen erfolgreichen Import.

Die LaunchAgent-Vorlage sieht Start beim Laden sowie täglich 13:00/20:00 vor. Sie ist auf
diesem Mac am Prüfdatum weder unter `~/Library/LaunchAgents/` installiert noch geladen.
Ohne manuellen Aufruf gibt es hier daher keine automatische Transkription.

## Daten und Grenzen

1. Railway hält das produktive Manifest unter `/data/state.db`. Die lokale `state.db` ist
   unabhängig und enthält einen älteren Sync-Stand. Ein lokales `run` gegen dieselben
   Notion-Datenbanken kann bereits in Railway verarbeitete Inhalte erneut anlegen. Das
   Manifest nicht löschen, austauschen oder aus einer alten Kopie als aktuell behandeln.
2. `SyncContent` in **KurseLearnWeb** entscheidet über aktive Kurse. Neu angelegte Kursseiten
   starten mit `false`. `COURSE_MAP` steuert nur das zusätzliche Select-Feld `Kurs`;
   die Relation `KurseLearnWeb` wird unabhängig davon geschrieben.
   Der aktuelle lokale Reparaturcode ergänzt fehlende Select-Werte und Kursrelationen auf
   eindeutig zugeordneten bestehenden Seiten. Einen bereits befüllten, abweichenden Select-Wert
   überschreibt er nicht. Allgemeine Text- oder Zielinhaltsänderungen werden nicht vollständig
   nachgezogen. Die Korrektur ist mit `5547703` deployed. Der aktuelle Property- und
   Inhalts-Readback steht im Reparaturbericht.
3. `resource`, `url` und `page` werden initial importiert. Eine geänderte `resource`-Datei wird
   im aktuellen Code nur ersetzt, wenn die vorhandene Notion-Datei bytegleich zum letzten
   Manifest-Hash ist; manuell veränderte Dateien bleiben erhalten und werden gemeldet. Texte
   von `page` und Zieladressen von `url` werden nicht allgemein aktualisiert. `folder`-Dateien
   haben eigene Hash- und Wiederaufnahme-Checkpoints. Forumsbeiträge, Aufgabenfristen und
   Quizfragen werden nicht als Inhalt importiert. Commit `7d1db2f` ergänzt hashgesicherte
   Aktualisierungen von Originaldateien; `ae1b0a0` prüft Folder-Dateien auch bei unveränderten
   Links auf Inhaltsänderungen. 18 Provider-Blocker bleiben offen. Quellen-, Byte- und
   Duplikatabnahmen stehen im Reparaturbericht.
4. `Quell-Semester` wird aus dem Zeitpunkt des Imports/Updates berechnet, nicht aus dem
   historischen Kurssemester. Alte aktive Kurse oder ein Backfill können daher das aktuelle
   Semesterlabel erhalten. `CURRENT_SEMESTER_OVERRIDE` ist eine ausdrückliche Ausnahme;
   die frühere Variable `CURRENT_SEMESTER` hat keine Wirkung.
5. `scan` ist kein read-only Check: Ohne übergebene Kursmap läuft zuerst der schreibende
   Notion-Kursabgleich, danach wird das SQLite-Manifest geändert. Der aktuelle lokale
   Transkriptions-`--dry-run` verwendet in beiden Pfaden eine lesende Kursabfrage und eine
   In-Memory-Datenbank; er kann weiterhin Medien laden und Whisper ausführen. Diese Reparatur
   ist mit Deployment `5547703` bereitgestellt.

## Speicher und Verbindungen

20 MiB ist im bereitgestellten Reparaturcode nur die Schwelle zwischen Single-Part- und
Multipart-Upload. Größere Dateien werden gestreamt und in begrenzten Teilen hochgeladen, bis zur
Workspace-Grenze. Drei repräsentative Originale (zwei PDFs mit rund 50/61 MiB und ein MOV mit
rund 247 MiB) wurden auf markierten Testseiten bytegleich zurückgelesen. Das belegt diese drei
Dateien, nicht die maximale Workspace-Grenze. Notion lehnt `.ipynb` und `.pkl` trotz Originalname
und passendem MIME-Typ ab; gemäß Nutzerentscheidung werden sie weder umbenannt noch als ZIP
ersetzt und bleiben offene Anlagen.
Ein bekannter endgültiger Blocker wird pro Quell-URL gespeichert und bei Folgeläufen nicht
erneut geladen; andere Folder-Dateien werden trotzdem übernommen. Der Lauf bleibt als
`incomplete` sichtbar, löst aber keinen wiederholten Worker-Neustart aus.

`SYNC_RUN_TIMEOUT_SECONDS` begrenzt einen Railway-Lauf standardmäßig auf 1800 Sekunden.
Der bereitgestellte Reparaturcode sucht vor einer Neuanlage nach der eindeutigen Quellseite und
speichert ihre ID vor weiteren Änderungen. Der aktuelle Nachweis zur Duplikatfreiheit steht im
Reparaturbericht.
Nach Erreichen der Fehlerschwelle speichert der Server den Laufstatus und beendet den
Gunicorn-Worker, damit der Master ihn ersetzt. Dieser Restart-Pfad ist deployed, aber noch nicht
live abgenommen. Bekannte endgültige Providerblocker
erscheinen als `incomplete` im Laufstatus, bleiben sichtbar und lösen keine wiederholten
Worker-Neustarts aus.

| Ort | Bedeutung / Wachstum |
|---|---|
| `.env` lokal bzw. Railway-Variablen | Zugangsdaten und Konfiguration, niemals versionieren oder ausgeben |
| `state.db` und Railway `/data/state.db` | Dauerhafte, getrennte Ressourcen-/Transkript-Manifeste; wachsen mit dem Bestand |
| `/data/state.db.server-status.json` | Laufstatus-Checkpoint des bereitgestellten Reparaturcodes; wird beim ersten Lauf angelegt und wächst nicht mit Inhaltsdaten |
| `/data/state.lock`, `transcribe.lock` | Prozesssperren; lokal und Railway koordinieren sich nicht gegenseitig |
| `.transcribe_tmp/<key>/` | Temporäre Medien/Audio; reguläre Läufe räumen auf, harte Abbrüche können Reste hinterlassen |
| `downloads/` oder `DOWNLOAD_DIR` | ZIP-Exportziel; der lokale Reparaturcode legt es an und prüft Schreibbarkeit; kein laufender Backup-Dienst |
| stdout, optional `LOG_DIR`, `logs/transcribe.*.log` | Sync-/Worker-Ausgaben; bei Dateien ist keine Rotation im Repo eingerichtet |

Externe Verbindungen: Universität Münster/LearnWeb und deren Medienserver, Notion API,
für YouTube getrennte cookie-freie yt-dlp-Aufrufe, gegebenenfalls Modelldownload von Hugging Face.
Whisper verarbeitet Audio lokal. Ein `--dry-run` kann trotzdem Downloads und Transkription
ausführen. Die reale Laufzeit hängt von Datei, Hardware und vorhandenem Modellcache ab.

## Semester vorbereiten

1. LearnWeb-Kursliste live mit **KurseLearnWeb** abgleichen. Die konkret gewünschten Kurse
   festlegen; alte aktive Kurse nicht automatisch deaktivieren. Aktuelle Kandidaten stehen im
   [Prüfbericht](PRUEFBERICHT.md#semesteranschluss).
2. Im autorisierten produktiven Lauf `sync-courses` ausführen und neu angelegte Kursseiten
   zurücklesen. Danach ausschließlich freigegebene Kurse über `SyncContent` aktivieren und
   passende `COURSE_MAP`-Zuordnungen prüfen. Fehlende Select-Zuordnung blockiert den Import
   nicht, lässt aber das Feld `Kurs` leer.
3. Produktive DB-IDs, Volume, Zeitplan und Semester-Override im richtigen Railway-Service
   prüfen. Der private Service darf nicht über das katalogisierte R-Factory-Credential
   bearbeitet werden. Schemafelder und Relationsziele mit dem Live-Notion-Schema abgleichen.
4. Nach Freigabe eines echten Syncs Logs bis zum Ende lesen und je benötigtem Inhaltstyp
   Notion-Seite, Kursrelation und Anhang/Text/URL tatsächlich zurücklesen. Anschließend denselben
   Lauf wiederholen und auf Duplikate prüfen. Exitcode 0 und `/health` allein genügen derzeit nicht.
5. Transkription separat an einer freigegebenen Aufzeichnung abnehmen: lesbares Transkript,
   vollständiger Body und richtige Relationen. Vorher die Recovery-/Dry-Run-Befunde klären.
   Der Datei-Sync braucht keinen aktivierten Transkriptions-LaunchAgent.

## Prüfen, pausieren und wiederherstellen

Read-only Einstieg: `railway status --json` im Projekt, danach Service-/Umgebungszuordnung
prüfen und `railway logs --lines 100` lesen. Kein `railway up`, Redeploy oder Webhook-Aufruf
für eine reine Statusprüfung. Die dokumentierte Health-URL erst aus der realen Service-Domain
ableiten; sie ist nicht im Repo hinterlegt. Der Endpunkt prüft Konfiguration und beschreibbare
DB-, Lock- und Statuspfade; `content_verified=false` ist kein Nachweis vollständiger Inhalte.
Aktueller Deployment-, Scheduler- und Duplikatstatus stehen im Reparaturbericht.

Den lokalen Worker pausiert man, indem kein neuer CLI-Lauf gestartet wird. Einen laufenden
Worker abbrechen nur unter Beachtung des dokumentierten Recovery-Fehlers. Eine spätere
LaunchAgent-Aktivierung, Deaktivierung oder Plist-Änderung braucht eine eigene Freigabe.
Die Vorlage enthält die Installations- und Statusbefehle; ihre bloße Existenz aktiviert nichts.

Für Railway-Pause oder dauerhafte Deaktivierung zuerst den konkreten Service und einen
eventuell laufenden Sync identifizieren, dann den Dienst nach Freigabe stoppen. Das Volume
erhalten. Das Entfernen der GitHub-Action allein stoppt den internen Scheduler nicht.
Es gibt derzeit keinen dokumentierten, verifizierten Backup-/Restore-Lauf für das produktive
Volume. Vor einem Wiederaufbau zuerst dessen aktuellen Bestand sichern und prüfen.

Wiederherstellung: vorhandenes Repo am gewünschten Commit, autorisierte Installation aus
`requirements.txt`, bestehende private Konfiguration, persistentes Manifest und ein einzelner
Gunicorn-Worker. Lokale Whisper-Pakete nur aus `requirements-transcription.txt` und Systemtools
wie in der README. Keine Zugangswerte in Befehle, Dokumentation oder Git übernehmen.

## Unterstützte Agenten

Codex und Claude Code lesen dieselbe Projektanleitung über `AGENTS.md -> CLAUDE.md` und können
die CLI nativ nutzen. Agy kann das Python-CLI ebenfalls ausführen; der native Anleitungsabruf
und Connectorzugriff sind für dieses Projekt nicht geprüft. Accountzugriff und Freigabe für
Notion-Writes, Deployment oder LaunchAgent-Änderungen sind jeweils separat erforderlich.
