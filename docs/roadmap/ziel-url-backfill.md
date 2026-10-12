# Ziel-URL-Backfill

- **Art:** Entwicklungsvorschlag
- **Zustand:** Spezifikation vorhanden, nicht umgesetzt; aktuelle Freigabe ungeklärt
- **Hängt ab von:** geprüftem produktivem Manifest, Ziel-URL-Schema und ausdrücklicher Schreibfreigabe
- **Herkunft:** [Pipeline-Spezifikation vom 20.05.2026](../../.notion-pipeline/feature-backfill-ziel-url-fur-bestehende-page-aktivitaten.md)

Leere `Ziel-URL`-Properties bestehender `url`-/`page`-Einträge sollten gezielt nachgefüllt
werden, ohne andere Properties oder Bookmark-Blöcke zu verändern. Der geplante Befehl
`backfill-ziel-url` existiert im aktuellen CLI nicht. Die vollständigen damaligen Schritte,
Abnahmekriterien und Abbruchbedingungen bleiben in der verlinkten Quelle erhalten.

Vor einer Umsetzung muss die Spezifikation mit dem heutigen Code abgeglichen werden:
Das CLI nutzt inzwischen argparse; außerdem ist das produktive Manifest vom lokalen getrennt.
Die bisher akzeptierte gemischte Darstellung alter URL-Seiten bleibt bis zu einem neuen Auftrag
bestehen. Die bloße Pipeline-Datei ist kein Nachweis für einen ausgeführten Backfill.
