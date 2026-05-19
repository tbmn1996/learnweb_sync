# Page-iframe Target-URL Extractor

- **Status:** Fertig (gemergt 2026-05-19, PR #5 @ 8ab8df5)
- **GitHub Repo:** [tbmn1996/learnweb_sync](https://github.com/tbmn1996/learnweb_sync)
- **PR:** [#5](https://github.com/tbmn1996/learnweb_sync/pull/5) — `feat: extract iframe target for mod_page activities`
- **Dokumenttyp:** Konzept (retro)

## Ziel
Bei `modtype_page`-Aktivitäten den eingebetteten `<iframe src>` als Ziel-URL in Notion speichern. Auslöser: Die Livestream-Seite eines Kurses (cmid=4023900) zeigte als mod_page eine Electures-Aufzeichnung via iframe, blieb in Notion aber ohne Ziel-URL.

## Was implementiert wurde
- Neuer Helper `_extract_iframe_target(soup)` extrahiert externe iframe-URLs (z. B. Opencast / Electures-Embeds), gefiltert gegen Same-Host-iframes.
- `_extract_page_content` aufgespalten in `_extract_page_text_from_soup` + dünnen Wrapper, damit iframe- und Text-Extraktion einen gemeinsamen HTTP-Fetch teilen.
- `_push_page_activity` setzt jetzt `Ziel-URL` über `notion_create_lw_page`, wenn ein iframe-Embed erkannt wird; Fallback auf den bestehenden Textpfad sonst.
- 4 neue Unit-Tests: Opencast Happy-Path, Same-Host-Filter, kein iframe → Fallback, Push-Integration.

## Bekannter Scope-Cut
Bestehende Notion-Pages ohne Ziel-URL werden durch diese Änderung **nicht** rückwirkend gefüllt — der Sync läuft Cache-basiert über `state.db` und überspringt bekannte Pages. Backfill ist als separates Thema offen.

## Files
- `learnweb_sync.py` (+75/-13)
- `tests/test_learnweb_sync.py` (+126/-0)
