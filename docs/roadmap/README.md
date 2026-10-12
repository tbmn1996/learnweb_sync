# Roadmap

Stand: 12.10.2026. Maßstab bleibt das bestehende [Projektziel](../PLAN.md#kontext--problem).
Der separate Semesterreparatur-Auftrag ist im freigegebenen
[Reparaturbericht](../reviews/261012-Reparatur-Abnahme.md) festgehalten. Diese Roadmap bleibt
bei den bereits bestehenden Zukunftsvorhaben und ändert das Projektziel nicht.
Die abgeschlossenen Lieferumfänge bleiben in den
[historischen Entwicklungsphasen](../PLAN.md#historische-entwicklungsphasen) erhalten.

## Bestehende Vorhaben

| Vorhaben | Zustand | Herkunft und Abhängigkeit |
|---|---|---|
| [Notion-Button](notion-button.md) | zurückgestellt | Frühere Phase 4; funktionierender Railway-Webhook vorausgesetzt |
| [Foren und Aufgaben](foren-aufgaben.md) | Zukunftsidee, Umfang offen | Frühere Phase 5; stabiler produktiver Basis-Sync vorausgesetzt |
| [Ziel-URL-Backfill](ziel-url-backfill.md) | Spezifikation vorhanden, nicht umgesetzt | Bestehende Pipeline-Datei; produktives Manifest und Schreibfreigabe erforderlich |
| [YouTube-Retry](youtube-retry.md) | Code committed, gezielte Tests bestanden; Issue offen | Bestehendes GitHub-Issue #7 |
| [YouTube-Discovery](youtube-discovery.md) | Code committed, gezielter Test bestanden; Issue offen | Bestehendes GitHub-Issue #8 |

Die API-Version `2022-06-28` bleibt eine bestehende offene Entscheidung in
[PLAN.md](../PLAN.md#offene-punkte--entscheidungen), kein beauftragtes Upgrade.
Audit-Befunde und ihre anschließende Reparatur sind im [Prüfbericht](../PRUEFBERICHT.md) und
[Reparaturbericht](../reviews/261012-Reparatur-Abnahme.md) getrennt dokumentiert. Neue,
unabhängige Zukunftsvorhaben brauchen weiterhin ausdrücklichen Auftrag oder Freigabe.

## Pflege und Form

1. Vor jeder Änderung die jeweilige Herkunft und den tatsächlichen Implementierungsstand lesen.
   Zustände in Übersicht und Vorhabendatei zusammen aktualisieren; einen Vorschlag nicht als
   freigegeben behandeln. „Erledigt“ braucht den zum Vorhaben passenden Funktionsnachweis.
2. Die Verzeichnisform gilt, weil der vorhandene Backfill mehrere Teile und eigene
   Abnahmekriterien enthält. Sobald ein Vorhaben solche Merkmale oder gegenseitige Verweise
   hat, bleibt es bei Übersicht plus je einer Vorhabendatei, unabhängig von der Anzahl.
3. Beim Formwechsel alle Vorhaben, Inhalte, Zustände und gültigen Links erhalten und die
   Projektanleitung nachziehen. Namen bleiben kurz und beschreibend; frühere Phasennummern
   stehen in den betroffenen Dateien. Löschungen bleiben freigabepflichtig.
4. Neue Vorhaben und Änderungen am bestehenden Projektziel brauchen ausdrücklichen Auftrag
   oder Freigabe. Laufbezogene Agentenaufteilung, Testlogs und Schätzungen gehören nicht hierher.
