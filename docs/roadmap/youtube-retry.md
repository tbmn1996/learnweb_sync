# YouTube-Retry

- **Art:** Entwicklungsvorschlag
- **Zustand:** Code committed (`5547703`), gezielte Offline-Tests bestanden; GitHub-Issue offen
- **Hängt ab von:** keiner weiteren Erweiterung
- **Herkunft:** [GitHub-Issue #7](https://github.com/tbmn1996/learnweb_sync/issues/7)

Der lokale Code wiederholt passende transiente 403-/JavaScript-Challenge-Fehler höchstens
dreimal. Dauerhafte Fehler wie entfernte oder private Videos werden nicht wiederholt. Der
bestehende Cross-Run-Versuchszähler bleibt davon getrennt. Die Reparatur-Suite prüft begrenzte
Retries für transiente Fehler und keinen Retry für permanente Fehler. Ein Live-YouTube-Lauf
wurde nicht als Abnahme dokumentiert; Issue #7 ist weiterhin offen. Details und ursprüngliche
Abwägung bleiben im Issue.
