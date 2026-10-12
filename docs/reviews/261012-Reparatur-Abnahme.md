# Semesterreparatur: Abnahme vom 12.10.2026

## Ergebnis und verbleibende Grenze

Der reparierte Code `ae1b0a050c0a7796abc338ac5e2c48f351181b3a` ist gepusht und auf dem
bestehenden privaten Railway-Dienst bereitgestellt. **210 unabhängige Offline-Tests bestehen.**
Die vier neuen Semesterkurse und ihre 42 zugänglichen Datei-/Text-/URL-Inhalte sind vorhanden.
Alle **106 übertragbaren Originaldateien** der sechs aktiven Kurse sind bytegleich geprüft.
**18 Originaldateien in zwei OR-Aktivitäten bleiben wegen einer Notion-API-Formatgrenze offen.**

Der echte Schedulerlauf auf `7d1db2f` startete um 16:49 UTC und endete um 16:58:40 UTC:
0 neue Seiten, 3 aktualisierte PDFs, 116 unveränderte Inhalte, ausschließlich 2 Inhaltsblocker,
Exit 2, `outcome=incomplete`, `consecutive_failures=0`. Scheduler und Gunicorn blieben aktiv.

**Letzter vollständiger Kontrolllauf:** Auf `8a9cecc` von 17:32:55 bis 17:42:26 UTC:
1 tatsächlich neue LearnWeb-Aktivität erkannt und angelegt, 0 Dateiänderungen, 119 unveränderte
Inhalte, ausschließlich die zwei bekannten Formatblocker. Drei Verbindungsabbrüche wurden
erfolgreich wiederholt; keine technischen Restfehler und kein Worker-Neustart.
Der anschließende Fünf-Zeilen-Folder-Fix `ae1b0a0` ist zusätzlich an allen drei Semesterordnern
live geprüft: dieselben Seiten und 23 Anlagen, kein erneuter Upload.

Der normale Zeitplan **Montag–Freitag, 06:00 UTC** ist auf dem finalen Code zurückgelesen.
Nächster Termin laut APScheduler und aktiver Konfiguration: **13.10.2026, 06:00 UTC / 08:00 MESZ**.
Die vorgezogenen Prüftermine sind beendet. Vollständige maschinenlesbare Belege:
[261012-Betriebsabnahme.json](261012-Betriebsabnahme.json).

## Auftrag und Freigabe

Die Plannotator-Freigabe ersetzte die frühere Grenze „nur prüfen und dokumentieren“.
Sie umfasst Reparatur, zugehörigen Commit/Push, Deployment und kontrollierte Notion-Abnahme.
Thomas priorisierte die neuen Kurse und ihre Inhalte und erlaubte ausschließlich direkte
Originaldateien, keinen ZIP-Ersatz. Er hat die vier neuen `SyncContent`-Häkchen selbst aktiviert;
OR und Data Management waren ebenfalls eingeschaltet. Bestehende Nutzertexte, manuell geänderte
Dateien und vorhandene Altduplikate wurden erhalten. Kein Mac-Autostart wurde aktiviert.

## Kurs- und Inhaltsabnahme

| Umfang | Nachweis |
|---|---|
| Neue Kurse | 96289 Analysis, 96296 Lineare Algebra, 94909 Informatik I, 94906 Java wurden durch den normalen bereitgestellten Dienst angelegt. |
| Neue Semesterinhalte | 42 zugängliche Datei-/Text-/URL-Seiten; korrekte Kursrelationen, 30 bytegleiche PDFs, 8 vollständige Textseiten und 24 korrekte Zieladressen. Zwei Zieladressen wurden über ihre kanonische Weiterleitung bestätigt. |
| Wiederaufnahme des neuen Semesters | Dieselben 41 Seiten-IDs und 29 Anlagen nach den Reparaturen; Kurs-Selects QM1/Inf1 gezielt ergänzt. Zusätzlich ein geprüfter Transkript-Eintrag. |
| Echte neue Aktivität während des Tests | 4261934 „KUKUwaya – A Chalkidis Initiative e.V.“ im Analysis-Kurs wurde vom normalen Dienst selbst erkannt. Zusätzliche PDF mit 692.493 Bytes unabhängig bytegleich zurückgelesen. |
| Ältere aktive Kurse | 44 eindeutige OR-Aktivitäten und 36 Data-Management-Aktivitäten; 76 übertragbare Originaldateien bytegleich. Drei alte URL-Seiten enthalten weiterhin ihre korrekten Bookmark-Blöcke statt einer befüllten Ziel-URL-Property. |
| Bewusste Begrenzung | FAQ 4207906 bleibt wegen tatsächlicher BWL-Gruppenbeschränkung `deferred`. Forum/Quiz/Assignment-Inhalte gehören weiterhin nicht zum freigegebenen Datei-/Text-/URL-Importer. |

Der abschließende Notion-Abgleich enthält exakt die erwarteten 136 Seiten: 122 reguläre
Quellaktivitäten, ein Transkript und 13 bereits bestehende Doppelzuordnungen. Alle früheren
Seiten-IDs bleiben erhalten; keine neuen Duplikate. Die LearnWeb-Übersicht wiederholt
Aktivitäten in verschachtelten Abschnitten. Maßgeblich ist
die eindeutige `cmid`, nicht die Zahl der Listeneinträge. Im OR-Altbestand bestehen bereits
13 zusätzliche Notion-Zeilen mit doppelter Quellidentität. Die im Manifest eindeutig gebundene
Seite wurde abgeglichen; keine überzählige Nutzerseite wurde gelöscht.

Datierte Erstabnahme der neuen PDFs: [261012-Semester-Abnahme.json](261012-Semester-Abnahme.json).
Der finale Bytevergleich bestätigte erneut 28 PDFs; bei einer Datei brach LearnWeb die Verbindung
ab. Ein gezielter Nachtest bestätigte auch diese Datei bytegleich. Es gab keinen Inhaltsunterschied.

## Noch offene Originaldateien

Notions File Upload API lehnt `.ipynb` und `.pkl` mit unverändertem Namen und passendem MIME-Typ
ab. HTTP 400 meldet eine nicht unterstützte Dateiendung. Der offizielle Single-Part-Weg ohne
vorab gesetzten optionalen Dateinamen wurde ebenfalls geprüft: Create akzeptiert JSON, Send
lehnt die unveränderte `.ipynb`-Endung ab. Das ist keine fehlende lokale Konfiguration.

1. [OR-Ordner 4117100](https://www.uni-muenster.de/LearnWeb/learnweb2/mod/folder/view.php?id=4117100): 16 Notebooks und `dorf_adjazenz.pkl`. `blackbox.py` ist direkt angehängt
   und bytegleich geprüft.
2. [OR-Ressource 4095464](https://www.uni-muenster.de/LearnWeb/learnweb2/mod/resource/view.php?id=4095464): `Wilantis_Netz.ipynb`.

Diese 18 Originale bleiben gemäß Thomas' Entscheidung offen. Kein Umbenennen, Konvertieren oder
Verpacken als ZIP. Der Dienst markiert sie nicht als vollständig. Unveränderte bekannte Blocker
werden nicht erneut hochgeladen; ein blockierter Ordner-Quelllink wird nicht erneut heruntergeladen.
Der Gesamtzustand bleibt `incomplete`, ohne solche Blocker als technischen Worker-Ausfall zu zählen.

## Reparaturen und geprüfte Verträge

| Befunde | Ergebnis und Abnahme |
|---|---|
| F01, F07, F08 | Kursanlage, Auswahlgrenze, gezielter Metadatenabgleich und explizites `mon-fri`; vier neue Kurse, reale Notion-Relationen/Selects und echter Schedulerlauf geprüft. |
| F02, F09, F10 | Fehlerweitergabe, Startkonfiguration, geschützter Webhook, persistenter Laufstatus und wirksamer Gunicorn-Workerersatz. Echte lokale Gunicorn-Prozesse geprüft; Health behauptet keine Inhaltsvollständigkeit. |
| F03–F06, F17 | Upload ohne Datei kann nicht `synced` werden; direkte Originale, Multipart, Teilfortschritt, Remote-Wiederfindung und Schutz vor falschen Entfernt-Markierungen. HTML-Dashboard 4164189 und Python-Datei live bytegleich. Originalformatgrenzen bleiben offen. |
| F11–F15 | Schreibfreier Dry-Run, Recovery, gezielter Force-Ersatz, No-Match-Status, YouTube-Deduplizierung/Retry und ZIP-Exportziel geprüft. Live-Opencast-Normalpfad und Wiederholung bestanden; keine produktiven Nutzertexte per Force ersetzt. |
| F16 | Bestehende Server-Abhängigkeiten lokal vervollständigt; Code-/Testautor getrennt, sichere Offline-Suite, lebende Projektdokumentation, Links und gemeinsamer Claudex-Integritätscheck geprüft. |

Die frische [DSH-Abschlussprüfung](261012-DSH_Reparaturreview.md) fand zwei zusätzliche
Erhaltungs-/Betriebsfehler. Beide wurden vor Commit `5547703` durch eine getrennte Codeinstanz
korrigiert und unabhängig getestet: Dateiersatz braucht einen gespeicherten Eigentumsnachweis
plus erneuten Remote-Hashvergleich; bekannte Inhaltsblocker dürfen keine Neustartschleife auslösen.

Der Livevergleich fand danach drei veraltete OR-PDFs (4132772, 4117079, 4117080). Ihre Notionbytes
stimmten mit den gespeicherten alten Original-Hashes überein. Commit `7d1db2f` übernimmt deshalb
aktualisierte Originale am selben Objekt, auch bei geändertem Dateinamen. Manuell geänderte
Dateien und zusätzliche Anlagen bleiben geschützt. Alle drei aktuellen PDFs sind live bytegleich.

Fünf Data-Management-Verweise im Manifest zeigten noch auf nicht erreichbare alte Notion-IDs.
Vorhandene eindeutige Nachfolger wurden anhand Datenbank, Kurs-ID, `Nr` und aktueller
Originalbytes belegt. Nach konsistenter Sicherung wurden ausschließlich diese fünf lokalen
Zuordnungen abgeglichen. Bei einer Datei war auch der alte Manifest-Hash veraltet; die aktuelle
Zielseite war bereits bytegleich zur Quelle. Keine Notion-Seite wurde dafür neu angelegt.

Ein unveränderter Kontrolllauf auf `7d1db2f` erzeugte 0 neue Seiten und 0 Dateiänderungen,
erhielt aber einen LearnWeb-HTTP-500 und einen Verbindungsabbruch beim Kurslesen. Commit
`8a9cecc` ergänzt dafür höchstens drei GET-Versuche bei Transportfehlern und HTTP
500/502/503/504. HTTP 401/403/404 scheitern sofort, Login-POST und Notion-Writes werden nicht
wiederholt. Verlinkte Ressourcen vervielfachen die Retry-Grenze nicht. Endgültige Fehler bleiben sichtbar.

Ein verschärfter Test belegte außerdem, dass ein gleicher Folder-Dateiname und Link keine
gleichen Bytes garantieren. `ae1b0a0` entfernt dafür ausschließlich den früheren Frühausstieg;
der bereits geprüfte per-Datei-Vergleich erkennt jetzt auch solche Änderungen. Unveränderte
Originale erzeugen keinen Upload; bekannte Formatblocker werden weiterhin vorher ausgespart.

## Zusätzliche Live-Nachweise

1. **Große Originale:** 52.499.379 und 64.354.556 Bytes große PDFs sowie ein 259.168.242 Bytes
   großes MOV mehrteilig hochgeladen und vollständig per SHA-256 zurückverglichen. Alle bytegleich.
   Der Quellkurs blieb inaktiv; die drei markierten Testseiten wurden archiviert und zurückgelesen.
2. **Bestehende Notion-Anlagen:** Zweite Original-PDF per PATCH ergänzt; erste und zweite Datei
   vollständig und bytegleich erhalten. Nur die markierte Testseite wurde danach archiviert.
3. **Lokale Transkription:** Aktive Opencast-Aufzeichnung 4151310, rund 383 Sekunden, 12 von 12
   Textblöcken in Notion zurückgelesen. Wiederholung ohne zweite Seite. Keine LaunchAgent-Aktivierung.

## Maßgebliche Testlauf-Belege

Die vollständigen Belege liegen unter `~/.local/state/testlauf/belege/`.

| Beleg | Aussage |
|---|---|
| `20261012-194838-folder-source-content-complete-07d5` | Finale 210 Tests und Diffprüfung; auch Folder-Änderungen bei identischen Links sowie schreibfreie Wiederholung unveränderter Dateien. |
| `20261012-195758-folder-on-ready-deployment-7c59` | Gezielte Live-Abnahme der einzigen Änderung nach dem vollständigen Kontrolllauf: alle drei Semesterordner, 23 Dateien, unveränderte IDs/Anlagen. |
| `20261012-195758-new-arrival-on-ready-deployment-27da` | Tatsächlich neu eingestellte und automatisch importierte KUKUwaya-PDF bytegleich. |
| `20261012-200511-final-inventory-no-new-duplicates-df8b` | 136 erwartete Seiten, keine neuen Duplikate, 42 reguläre Semesterinhalte plus ein Transkript. |
| `20261012-192110-learnweb-read-retry-final-sol-b6e8` | 210 Offline-Tests und Diffprüfung bestanden, separater Codeautor. |
| `20261012-185112-release-head-offline-check-d689` | Damaliger Stand: 205 Tests, Dokumentationslinks und Claudex-Integrität bestanden. |
| `20261012-190004-existing-active-files-after-refresh-2d33` | 80 ältere Aktivitäten; 76 Byte-Matches, 3 korrekte Links, 18 ausdrücklich offene Originale. |
| `20261012-190148-semester-originals-final-live-054b` + `20261012-190424-final-original-targeted-retry-fceb` | 28 sofortige plus 1 gezielter Byte-Match der neuen Semester-PDFs. |
| `20261012-184807-semester-pages-preserved-live-d21f` | 41 Seiten-IDs, 29 Anlagen, Kursrelationen und Zieladressen erhalten; korrekte Selects. |
| `20261012-182132-multipart-originals-live-b638` | Drei große Originaldateien bytegleich, Testseiten archiviert. |
| `20261012-182229-existing-file-patch-live-a33b` | Bestehende gehostete Datei bei Ergänzung erhalten, beide Byte-Matches. |
| `20261012-184356-target-id-reconcile-source-verified-81c5` | Fünf Manifest-Verweise nach Quellen-/Byteprüfung und Backup korrigiert. |
| `20261012-180200-live-active-transcription-f9d5` + `20261012-180451-live-transcription-repeat-2d7d` | Echte lokale Transkription und duplikatfreie Wiederholung. |

## Datenhaltung und Rückweg

Das produktive Manifest bleibt `/data/state.db`; die lokale ältere `state.db` wurde nicht
hochgeladen. Vor den Eingriffen wurden konsistente Railway-Sicherungen mit `quick_check=ok` erstellt:

1. `/data/learnweb-backups/20261012T135025Z-before-manual-sync.db`, SHA-256
   `6806e0788d87262426ad84084c65cba39f0f626541d26de0157cd72dca1d5048`.
2. `/data/learnweb-backups/20261012T162000Z-before-repaired-sync.db`, 619 Ressourcen, SHA-256
   `09f94e478a42aa9baac9cd6d8309a32d76794a6a83c30cbe9f48ebff54df8709`.
3. `/data/learnweb-backups/20261012-before-page-id-reconcile.db` vor dem gezielten ID-Abgleich.

Keine bestehenden Nutzerseiten gelöscht, keine Datenbank geleert, keine Credentials rotiert und
keine neuen kostenpflichtigen Dienste eingerichtet. `page`-Text und `url`-Zieländerungen werden
weiterhin nicht allgemein aktualisiert. Eine echte YouTube-Providerprobe ist nicht Teil der
Live-Nachweise; die reparierten Retry-/Dedupe-Verträge sind offline geprüft.
