"""Offline-Unit-Tests für den Transkriptions-Worker (Paket `transcription/`).

Kein Netz, kein Notion, kein Whisper-Modell. Deckt ab: Recording-Key/Dedupe,
Zustandsautomat + State-DB-Migration, Notion-Block-Chunking (Notion-Limits),
Whisper-Segment-Normalisierung, den Opencast-Parser (beide Formate) und den
neuen YouTube-Transkriptions-Pfad (Parser, Subtitle-Kaskade, Discovery,
Process-Branch, CLI-Guards).

Stil bewusst wie tests/test_learnweb_sync.py (unittest.TestCase).
"""
import http.cookiejar
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import learnweb_sync as lws
import requests
from bs4 import BeautifulSoup
from transcription import downloader, manifest, notion_blocks, recordings, transcriber, youtube
from transcription.types import Recording, Segment


def _block_texts(blocks: list[dict]) -> list[str]:
    """Extrahiert den zusammengesetzten Text je Notion-Block (rich_text-content)."""
    out = []
    for b in blocks:
        rich = b.get(b.get("type"), {}).get("rich_text", [])
        out.append("".join(x.get("text", {}).get("content", "") for x in rich))
    return out


def _rec(cmid="100", episode_id="ep-aaa", media_url="https://cdn/x.mp4", **kw) -> Recording:
    """Baut ein minimales Recording für Key-/State-Tests."""
    return Recording(
        cmid=cmid,
        title=kw.get("title", "Test-Aufzeichnung"),
        source_url=kw.get("source_url", "https://lw/view.php?id=1"),
        course_id=kw.get("course_id", "900"),
        episode_id=episode_id,
        media_url=media_url,
    )


class TestDownloader(unittest.TestCase):
    """Cookie-Export und secret-sichere yt-dlp-Fehler."""

    def test_netscape_cookie_flags_are_loadable_and_preserved(self):
        session = requests.Session()
        session.cookies.set(
            "host-cookie", "host-value", domain="example.com", path="/", secure=False
        )
        session.cookies.set(
            "domain-cookie", "domain-value", domain=".example.org", path="/secure", secure=True
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            cookie_path = Path(temp_dir) / "cookies.txt"
            downloader._session_cookies_to_netscape(session, cookie_path)

            self.assertTrue(cookie_path.read_text(encoding="utf-8").endswith("\n"))
            jar = http.cookiejar.MozillaCookieJar(str(cookie_path))
            jar.load(ignore_discard=True, ignore_expires=True)

        cookies = {cookie.name: cookie for cookie in jar}
        self.assertEqual(cookies["host-cookie"].domain, "example.com")
        self.assertFalse(cookies["host-cookie"].domain_initial_dot)
        self.assertFalse(cookies["host-cookie"].secure)
        self.assertEqual(cookies["domain-cookie"].domain, ".example.org")
        self.assertTrue(cookies["domain-cookie"].domain_initial_dot)
        self.assertTrue(cookies["domain-cookie"].secure)

    def test_download_error_redacts_cookie_record_values_and_urls(self):
        cookie_value = "session-secret-value"
        media_url = "https://cdn.example/video.mp4?token=query-secret"
        session = requests.Session()
        session.cookies.set(
            "MoodleSession", cookie_value, domain="example.com", path="/", secure=True
        )
        stderr = (
            ("noise before final error\n" * 200)
            + "yt_dlp.cookies.CookieLoadError: failed to load cookies\n"
            + "ERROR: invalid cookies from "
            + media_url
            + ": 'example.com\tFALSE\t/\tTRUE\t0\tMoodleSession\t"
            + cookie_value
            + "'\n"
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            with patch(
                "transcription.downloader.subprocess.run",
                return_value=SimpleNamespace(returncode=1, stdout="", stderr=stderr),
            ) as run_mock:
                with self.assertRaises(RuntimeError) as caught:
                    downloader.download_media(
                        session,
                        _rec(media_url=media_url),
                        Path(temp_dir),
                    )

            cmd = run_mock.call_args.args[0]
            cookie_path = Path(cmd[cmd.index("--cookies") + 1])
            self.assertFalse(cookie_path.exists())
            self.assertEqual(cmd[cmd.index("-f") + 1], "best")

        message = str(caught.exception)
        self.assertIn("failed to load cookies", message)
        self.assertIn("<redacted-url>", message)
        self.assertNotIn(cookie_value, message)
        self.assertNotIn(media_url, message)
        self.assertNotIn("query-secret", message)
        self.assertNotIn("example.com\tFALSE", message)

    def test_youtube_title_probe_is_cookie_free(self):
        with patch(
            "transcription.downloader.subprocess.run",
            return_value=SimpleNamespace(
                returncode=0, stdout="  Echter YouTube-Titel  \n", stderr=""
            ),
        ) as run_mock:
            title = downloader.fetch_youtube_title(
                "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
            )

        self.assertEqual(title, "Echter YouTube-Titel")
        cmd = run_mock.call_args.args[0]
        self.assertIn("--skip-download", cmd)
        self.assertIn("--print", cmd)
        self.assertNotIn("--cookies", cmd)


class TestRecordingKey(unittest.TestCase):
    """Recording-Key + Diskriminator (Dedupe-Identität, Plan §3)."""

    def test_key_deterministic(self):
        rec = _rec()
        k1 = manifest.recording_key(rec.cmid, manifest.discriminator_for(rec))
        k2 = manifest.recording_key(rec.cmid, manifest.discriminator_for(rec))
        self.assertEqual(k1, k2)
        self.assertTrue(k1.startswith("100-"))

    def test_key_varies_with_cmid(self):
        a = _rec(cmid="100")
        b = _rec(cmid="200")
        ka = manifest.recording_key(a.cmid, manifest.discriminator_for(a))
        kb = manifest.recording_key(b.cmid, manifest.discriminator_for(b))
        self.assertNotEqual(ka, kb)

    def test_discriminator_stable(self):
        rec = _rec()
        self.assertEqual(manifest.discriminator_for(rec), manifest.discriminator_for(rec))

    def test_key_suffix_is_12_hex(self):
        rec = _rec()
        key = manifest.recording_key(rec.cmid, manifest.discriminator_for(rec))
        suffix = key.split("-", 1)[1]
        self.assertEqual(len(suffix), 12)   # sha1(discriminator)[:12]
        int(suffix, 16)                     # muss hexadezimal sein

    def test_discriminator_varies_with_episode(self):
        a = _rec(episode_id="ep-aaa")
        b = _rec(episode_id="ep-bbb")
        self.assertNotEqual(manifest.discriminator_for(a), manifest.discriminator_for(b))


class TestStateMachine(unittest.TestCase):
    """Zustandsautomat + atomares Claiming auf einer frisch migrierten DB."""

    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        lws.init_transcribe_schema(self.conn)
        self.rec = _rec()
        self.key = manifest.recording_key(self.rec.cmid, manifest.discriminator_for(self.rec))

    def tearDown(self):
        self.conn.close()

    def test_upsert_pending(self):
        manifest.upsert_pending(self.conn, self.rec, self.key)
        row = manifest.get(self.conn, self.key)
        self.assertIsNotNone(row)
        self.assertEqual(row["status"], "pending")
        self.assertEqual(row["cmid"], "100")

    def test_claim_is_atomic(self):
        manifest.upsert_pending(self.conn, self.rec, self.key)
        self.assertTrue(manifest.claim(self.conn, self.key))    # erster claim gewinnt
        self.assertFalse(manifest.claim(self.conn, self.key))   # zweiter scheitert
        self.assertEqual(manifest.get(self.conn, self.key)["status"], "claimed")

    def test_set_status_persists_fields(self):
        manifest.upsert_pending(self.conn, self.rec, self.key)
        manifest.set_status(self.conn, self.key, "meeting_created",
                            meeting_page_id="pg-123", total_block_count=7)
        row = manifest.get(self.conn, self.key)
        self.assertEqual(row["status"], "meeting_created")
        self.assertEqual(row["meeting_page_id"], "pg-123")
        self.assertEqual(row["total_block_count"], 7)

    def test_is_done_only_terminal(self):
        manifest.upsert_pending(self.conn, self.rec, self.key)
        self.assertFalse(manifest.is_done(self.conn, self.key))
        manifest.set_status(self.conn, self.key, "done")
        self.assertTrue(manifest.is_done(self.conn, self.key))

    def test_failed_counts_as_done(self):
        manifest.upsert_pending(self.conn, self.rec, self.key)
        manifest.set_status(self.conn, self.key, "failed",
                            failure_stage="download", failure_reason="boom")
        self.assertTrue(manifest.is_done(self.conn, self.key))
        self.assertEqual(manifest.get(self.conn, self.key)["failure_stage"], "download")

    def test_reset_for_force(self):
        manifest.upsert_pending(self.conn, self.rec, self.key)
        manifest.set_status(self.conn, self.key, "done", appended_block_count=5)
        manifest.reset_for_force(self.conn, self.key)
        self.assertEqual(manifest.get(self.conn, self.key)["status"], "pending")


class TestSchemaMigration(unittest.TestCase):
    """init_transcribe_schema auf bestehender state.db darf nichts beschädigen."""

    def test_migration_keeps_existing_table(self):
        conn = sqlite3.connect(":memory:")
        # Bestehende resources-Tabelle simulieren (wie in echter state.db).
        conn.execute("CREATE TABLE resources (cmid TEXT, modtype TEXT)")
        conn.execute("INSERT INTO resources VALUES ('1','opencast')")
        conn.commit()
        lws.init_transcribe_schema(conn)
        tabs = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertIn("transcripts", tabs)
        self.assertIn("resources", tabs)
        # resources-Inhalt unangetastet
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM resources").fetchone()[0], 1)
        conn.close()

    def test_idempotent(self):
        conn = sqlite3.connect(":memory:")
        lws.init_transcribe_schema(conn)
        lws.init_transcribe_schema(conn)  # zweiter Aufruf darf nicht crashen
        self.assertTrue(conn.execute(
            "SELECT 1 FROM sqlite_master WHERE name='transcripts'").fetchone())
        conn.close()


class TestNotionBlocks(unittest.TestCase):
    """Block-Bau hält Notion-Limits ein (≤2000 Zeichen/rich_text, Timestamps)."""

    def test_format_timestamp(self):
        self.assertEqual(notion_blocks.format_timestamp(0), "00:00")
        self.assertEqual(notion_blocks.format_timestamp(65), "01:05")
        self.assertEqual(notion_blocks.format_timestamp(3661), "1:01:01")

    def test_timestamp_prefix_present(self):
        segs = [Segment(0.0, 5.0, "Guten Morgen.")]
        blocks = notion_blocks.build_transcript_blocks(segs, with_timestamps=True)
        self.assertTrue(_block_texts(blocks)[0].startswith("[00:00]"))

    def test_no_timestamp_when_disabled(self):
        segs = [Segment(0.0, 5.0, "Guten Morgen.")]
        blocks = notion_blocks.build_transcript_blocks(segs, with_timestamps=False)
        self.assertNotIn("[00:00]", _block_texts(blocks)[0])

    def test_long_text_chunked_under_notion_limit(self):
        # Ein einzelnes sehr langes Segment muss in mehrere Blöcke gesplittet werden.
        segs = [Segment(0.0, 30.0, "Wort " * 1000)]  # ~5000 Zeichen
        blocks = notion_blocks.build_transcript_blocks(segs, max_chars=1900)
        self.assertGreater(len(blocks), 1)
        for text in _block_texts(blocks):
            self.assertLessEqual(len(text), 2000)  # Notion-Hard-Limit

    def test_no_100_block_cap_no_transcript_loss(self):
        # KRITISCH: lange Transkripte dürfen NICHT bei 100 Blöcken abgeschnitten
        # werden (sonst Datenverlust beim Append). 250 Segmente alle 31 s -> ~125
        # Absätze (2 Segmente je Absatz, start-basierte 30-s-Grenze).
        segs = [Segment(i * 31.0, i * 31.0 + 2.0, f"Satz {i}.") for i in range(250)]
        blocks = notion_blocks.build_transcript_blocks(segs)
        self.assertGreater(len(blocks), 100)
        self.assertIn("Satz 249", _block_texts(blocks)[-1])  # letztes Segment erhalten

    def test_umlauts_preserved(self):
        segs = [Segment(0.0, 5.0, "Übung über Größen, schön äöüß.")]
        blocks = notion_blocks.build_transcript_blocks(segs)
        self.assertIn("Größen", _block_texts(blocks)[0])
        self.assertIn("äöüß", _block_texts(blocks)[0])

    def test_empty_segments(self):
        self.assertEqual(notion_blocks.build_transcript_blocks([]), [])

    def test_group_paragraphs_breaks_on_duration(self):
        # Absatz bricht, wenn kumulierte Dauer >= paragraph_seconds erreicht.
        # Kurze Segmente alle 31 s -> je 2 pro Absatz.
        segs = [Segment(i * 31.0, i * 31.0 + 2.0, f"S{i}.") for i in range(4)]
        paras = notion_blocks.group_paragraphs(segs, paragraph_seconds=30.0)
        self.assertEqual(len(paras), 2)  # [S0,S1], [S2,S3]

    def test_group_paragraphs_merges_close_segments(self):
        # Drei Segmente innerhalb eines 45-s-Fensters -> ein Absatz.
        segs = [Segment(0.0, 5.0, "A."), Segment(6.0, 10.0, "B."), Segment(40.0, 45.0, "C.")]
        paras = notion_blocks.group_paragraphs(segs, paragraph_seconds=30.0)
        self.assertEqual(len(paras), 1)


class TestSegmentNormalization(unittest.TestCase):
    """Beide Whisper-Backends auf {start,end,text} normalisieren, leere verwerfen."""

    def test_mlx_dict_format(self):
        raw = [{"start": 0.0, "end": 2.0, "text": " Hallo "},
               {"start": 2.0, "end": 3.0, "text": "   "}]  # zweites = leer
        segs = transcriber._normalize_mlx_segments(raw)
        self.assertEqual(len(segs), 1)
        self.assertEqual(segs[0], Segment(0.0, 2.0, "Hallo"))

    def test_faster_object_format(self):
        class FS:
            def __init__(self, a, b, t):
                self.start, self.end, self.text = a, b, t
        segs = transcriber._normalize_faster_segments([FS(0.0, 1.0, "Welt"), FS(1.0, 2.0, "")])
        self.assertEqual(len(segs), 1)
        self.assertEqual(segs[0], Segment(0.0, 1.0, "Welt"))

    def test_empty_input(self):
        self.assertEqual(transcriber._normalize_mlx_segments([]), [])
        self.assertEqual(transcriber._normalize_faster_segments([]), [])


class TestOpencastParser(unittest.TestCase):
    """Opencast-Discovery-Parser: neues window.episode- + altes Listenformat."""

    WINDOW = (
        '<html><script>window.episode = {"metadata":{"id":'
        '"11111111-2222-3333-4444-555555555555","title":"Test VL",'
        '"duration":120.0,"preview":"https://cdn.ex/p.jpg"},"streams":'
        '[{"sources":{"mp4":[{"src":"https:\\/\\/cdn.ex\\/video.mp4",'
        '"res":{"w":1280,"h":720}}]}}]};</script></html>'
    )
    LEGACY = (
        '<html><table>'
        '<a href="/LearnWeb/learnweb2/mod/opencast/view.php?id=5&e='
        'aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee">Vorlesung 1</a>'
        '<a href="/LearnWeb/learnweb2/mod/opencast/view.php?id=5&e='
        'ffffffff-1111-2222-3333-444444444444">Vorlesung 2</a>'
        '</table></html>'
    )

    def test_window_episode_format(self):
        eps = recordings.parse_opencast_episodes(self.WINDOW, base_url="https://lw.test")
        self.assertEqual(len(eps), 1)
        ep = eps[0]
        self.assertEqual(ep["title"], "Test VL")
        self.assertEqual(ep["episode_id"], "11111111-2222-3333-4444-555555555555")
        self.assertTrue(ep["media_url"].endswith("video.mp4"))  # \/ korrekt entschachtelt

    def test_legacy_list_format(self):
        eps = recordings.parse_opencast_episodes(self.LEGACY, base_url="https://lw.test")
        self.assertEqual(len(eps), 2)
        self.assertEqual(eps[0]["title"], "Vorlesung 1")
        self.assertIsNone(eps[0]["media_url"])  # Listenformat hat noch keine Stream-URL

    def test_is_media_url(self):
        self.assertTrue(recordings.is_media_url("https://x/v.mp4"))
        self.assertTrue(recordings.is_media_url("https://x/a.m4a"))
        self.assertTrue(recordings.is_media_url("https://x/v.mp4?token=1"))
        self.assertFalse(recordings.is_media_url("https://x/page.html"))
        self.assertFalse(recordings.is_media_url(""))

    def test_empty_html(self):
        self.assertEqual(recordings.parse_opencast_episodes("", base_url=""), [])


class TestYoutubeParseId(unittest.TestCase):
    """parse_youtube_id: alle unterstützten URL-Formen + Negativfälle."""

    def test_youtu_be(self):
        self.assertEqual(
            youtube.parse_youtube_id("https://youtu.be/dQw4w9WgXcQ"), "dQw4w9WgXcQ"
        )

    def test_youtu_be_with_query(self):
        self.assertEqual(
            youtube.parse_youtube_id("https://youtu.be/dQw4w9WgXcQ?t=30"), "dQw4w9WgXcQ"
        )

    def test_watch_with_v_and_list(self):
        self.assertEqual(
            youtube.parse_youtube_id(
                "https://www.youtube.com/watch?v=dQw4w9WgXcQ&list=PL123"
            ),
            "dQw4w9WgXcQ",
        )

    def test_embed(self):
        self.assertEqual(
            youtube.parse_youtube_id("https://www.youtube.com/embed/dQw4w9WgXcQ"),
            "dQw4w9WgXcQ",
        )

    def test_nocookie_embed(self):
        self.assertEqual(
            youtube.parse_youtube_id(
                "https://www.youtube-nocookie.com/embed/dQw4w9WgXcQ"
            ),
            "dQw4w9WgXcQ",
        )

    def test_shorts(self):
        self.assertEqual(
            youtube.parse_youtube_id("https://www.youtube.com/shorts/dQw4w9WgXcQ"),
            "dQw4w9WgXcQ",
        )

    def test_invalid_url_returns_none(self):
        self.assertIsNone(youtube.parse_youtube_id("https://example.com/video"))

    def test_empty_string_returns_none(self):
        self.assertIsNone(youtube.parse_youtube_id(""))

    def test_none_returns_none(self):
        self.assertIsNone(youtube.parse_youtube_id(None))


class TestYoutubeExtractLinks(unittest.TestCase):
    """extract_youtube_links: <a>/<iframe>, Dedup, kanonische URL, Titel."""

    def test_extracts_a_tag_link_with_title(self):
        html = '<a href="https://youtu.be/dQw4w9WgXcQ">Vorlesung 1</a>'
        links = youtube.extract_youtube_links(html)
        self.assertEqual(len(links), 1)
        self.assertEqual(links[0]["video_id"], "dQw4w9WgXcQ")
        self.assertEqual(links[0]["url"], "https://www.youtube.com/watch?v=dQw4w9WgXcQ")
        self.assertEqual(links[0]["title"], "Vorlesung 1")

    def test_extracts_iframe_with_title_attribute(self):
        html = (
            '<iframe src="https://www.youtube.com/embed/dQw4w9WgXcQ" '
            'title="Eingebettetes Video"></iframe>'
        )
        links = youtube.extract_youtube_links(html)
        self.assertEqual(len(links), 1)
        self.assertEqual(links[0]["title"], "Eingebettetes Video")

    def test_dedups_by_video_id_keeps_first_title(self):
        html = (
            '<a href="https://youtu.be/dQw4w9WgXcQ">Erster Titel</a>'
            '<iframe src="https://www.youtube.com/embed/dQw4w9WgXcQ" title="Zweiter Titel">'
            "</iframe>"
        )
        links = youtube.extract_youtube_links(html)
        self.assertEqual(len(links), 1)
        self.assertEqual(links[0]["title"], "Erster Titel")

    def test_title_whitespace_cleaned(self):
        html = '<a href="https://youtu.be/dQw4w9WgXcQ">  Vorlesung\n   1  </a>'
        links = youtube.extract_youtube_links(html)
        self.assertEqual(links[0]["title"], "Vorlesung 1")

    def test_no_links_returns_empty(self):
        self.assertEqual(youtube.extract_youtube_links("<p>kein Video hier</p>"), [])

    def test_empty_html_returns_empty(self):
        self.assertEqual(youtube.extract_youtube_links(""), [])

    def test_preserves_first_occurrence_order(self):
        html = (
            '<a href="https://youtu.be/aaaaaaaaaaa">A</a>'
            '<a href="https://youtu.be/bbbbbbbbbbb">B</a>'
        )
        links = youtube.extract_youtube_links(html)
        self.assertEqual([l["video_id"] for l in links], ["aaaaaaaaaaa", "bbbbbbbbbbb"])


class TestYoutubeParseSubtitles(unittest.TestCase):
    """parse_youtube_subtitles: json3 + vtt, Fehlerfälle -> []."""

    def test_json3_basic(self):
        raw = (
            '{"events":[{"tStartMs":0,"dDurationMs":5000,'
            '"segs":[{"utf8":"Guten Morgen"}]}]}'
        )
        segs = youtube.parse_youtube_subtitles(raw, "json3")
        self.assertEqual(len(segs), 1)
        self.assertEqual(segs[0].start, 0.0)
        self.assertEqual(segs[0].end, 5.0)
        self.assertEqual(segs[0].text, "Guten Morgen")

    def test_json3_concatenates_multiple_segs(self):
        raw = (
            '{"events":[{"tStartMs":1000,"dDurationMs":2000,'
            '"segs":[{"utf8":"Hallo "},{"utf8":"Welt"}]}]}'
        )
        segs = youtube.parse_youtube_subtitles(raw, "json3")
        self.assertEqual(segs[0].text, "Hallo Welt")

    def test_json3_skips_events_without_text(self):
        raw = '{"events":[{"tStartMs":0,"dDurationMs":1000,"segs":[{"utf8":"  "}]}]}'
        self.assertEqual(youtube.parse_youtube_subtitles(raw, "json3"), [])

    def test_json3_broken_json_returns_empty(self):
        self.assertEqual(youtube.parse_youtube_subtitles("{not valid json", "json3"), [])

    def test_json3_non_object_root_returns_empty(self):
        self.assertEqual(youtube.parse_youtube_subtitles("[]", "json3"), [])

    def test_json3_skips_non_object_events(self):
        raw = (
            '{"events":[null,42,"text",'
            '{"tStartMs":0,"dDurationMs":1000,"segs":[{"utf8":"Gültig"}]}]}'
        )
        segs = youtube.parse_youtube_subtitles(raw, "json3")
        self.assertEqual([segment.text for segment in segs], ["Gültig"])

    def test_vtt_basic(self):
        raw = (
            "WEBVTT\n\n"
            "00:00:00.000 --> 00:00:05.000\n"
            "Guten Morgen.\n\n"
            "00:00:05.000 --> 00:00:10.000\n"
            "Nächstes Cue.\n"
        )
        segs = youtube.parse_youtube_subtitles(raw, "vtt")
        self.assertEqual(len(segs), 2)
        self.assertEqual(segs[0].start, 0.0)
        self.assertEqual(segs[0].end, 5.0)
        self.assertEqual(segs[0].text, "Guten Morgen.")
        self.assertEqual(segs[1].text, "Nächstes Cue.")

    def test_vtt_strips_inline_tags(self):
        raw = (
            "WEBVTT\n\n"
            "00:00:00.000 --> 00:00:02.000\n"
            "<c.colorE5E5E5>Hallo</c> <b>Welt</b>\n"
        )
        segs = youtube.parse_youtube_subtitles(raw, "vtt")
        self.assertEqual(segs[0].text, "Hallo Welt")

    def test_vtt_broken_returns_empty(self):
        self.assertEqual(youtube.parse_youtube_subtitles("WEBVTT\n\nkeine Timings", "vtt"), [])

    def test_unknown_format_returns_empty(self):
        self.assertEqual(youtube.parse_youtube_subtitles("irgendwas", "srt"), [])

    def test_empty_input_returns_empty(self):
        self.assertEqual(youtube.parse_youtube_subtitles("", "json3"), [])
        self.assertEqual(youtube.parse_youtube_subtitles("   ", "vtt"), [])


class TestYoutubeSubtitleLangs(unittest.TestCase):
    """_youtube_subtitle_langs: Default-Ableitung aus WHISPER_LANGUAGE vs. expliziter Override."""

    def test_default_uses_whisper_language_de(self):
        with patch.object(lws, "WHISPER_LANGUAGE", "de"), \
             patch.dict(lws.os.environ, {}, clear=False):
            lws.os.environ.pop("YT_SUBTITLE_LANGS", None)
            self.assertEqual(lws._youtube_subtitle_langs(), "de,en")

    def test_explicit_env_override_wins(self):
        with patch.object(lws, "WHISPER_LANGUAGE", "de"), \
             patch.dict(lws.os.environ, {"YT_SUBTITLE_LANGS": "fr,es"}):
            self.assertEqual(lws._youtube_subtitle_langs(), "fr,es")

    def test_whisper_language_fr_without_explicit_env(self):
        with patch.object(lws, "WHISPER_LANGUAGE", "fr"), \
             patch.dict(lws.os.environ, {}, clear=False):
            lws.os.environ.pop("YT_SUBTITLE_LANGS", None)
            self.assertEqual(lws._youtube_subtitle_langs(), "fr,en")


class TestParseSectionCourseId(unittest.TestCase):
    """_parse_section_course_id: body-class zuerst, dann courseId im Inline-JS, sonst None."""

    def test_body_class_course_id(self):
        html = '<html><body class="path-mod-url course-91465 limitedwidth"></body></html>'
        soup = BeautifulSoup(html, "html.parser")
        self.assertEqual(lws._parse_section_course_id(soup, html), "91465")

    def test_inline_js_course_id_without_body_class(self):
        html = '<html><body class="limitedwidth"></body><script>var x = {courseId: 91465};</script></html>'
        soup = BeautifulSoup(html, "html.parser")
        self.assertEqual(lws._parse_section_course_id(soup, html), "91465")

    def test_no_course_id_returns_none(self):
        html = '<html><body class="limitedwidth"></body></html>'
        soup = BeautifulSoup(html, "html.parser")
        self.assertIsNone(lws._parse_section_course_id(soup, html))

    def test_ignores_navigation_course_view_links(self):
        # Navigationsmenü-Link auf einen FREMDEN Kurs darf NICHT als Quelle dienen.
        html = (
            '<html><body class="course-91465">'
            '<a href="/course/view.php?id=12345">Anderer Kurs</a>'
            "</body></html>"
        )
        soup = BeautifulSoup(html, "html.parser")
        self.assertEqual(lws._parse_section_course_id(soup, html), "91465")


class TestExtractSectionCourseName(unittest.TestCase):
    """_extract_section_course_name: liest Kursnamen aus dem <title>-Tag."""

    def test_extracts_middle_title_segment(self):
        html = "<html><head><title>Section: VL 3 | Informatik 1 | Learnweb</title></head></html>"
        soup = BeautifulSoup(html, "html.parser")
        self.assertEqual(lws._extract_section_course_name(soup), "Informatik 1")

    def test_missing_title_returns_none(self):
        soup = BeautifulSoup("<html><head></head></html>", "html.parser")
        self.assertIsNone(lws._extract_section_course_name(soup))

    def test_title_without_enough_segments_returns_none(self):
        soup = BeautifulSoup("<html><head><title>Nur ein Teil</title></head></html>", "html.parser")
        self.assertIsNone(lws._extract_section_course_name(soup))


def _section_html_with_url_activity(cmid="3001", name="Video-Link"):
    """Minimal-HTML: ein <li class=course-section> mit einer mod/url-Aktivität ohne href."""
    return f"""
    <html><body>
      <li class="section course-section" data-sectionname="General">
        <ul data-for="cmlist">
          <li data-for="cmitem" data-id="{cmid}" class="activity url modtype_url">
            <div data-activityname="{name}"></div>
          </li>
        </ul>
      </li>
    </body></html>
    """


class TestDiscoverYoutubeRecordings(unittest.TestCase):
    """discover_youtube_recordings: direkte Links + mod/url-Auflösung, Dedupe, restricted-Skip."""

    def test_direct_link_in_html_produces_recording(self):
        html = '<html><body><a href="https://youtu.be/dQw4w9WgXcQ">Mein Video</a></body></html>'
        soup = BeautifulSoup(html, "html.parser")
        session = MagicMock()
        recs = lws.discover_youtube_recordings(
            session, soup, html, course_id="900", course_shortname="Inf1", course_name="Informatik 1"
        )
        self.assertEqual(len(recs), 1)
        rec = recs[0]
        self.assertEqual(rec.cmid, "yt")
        self.assertEqual(rec.episode_id, "dQw4w9WgXcQ")
        self.assertEqual(rec.source_kind, "youtube")
        self.assertEqual(rec.media_url, "https://www.youtube.com/watch?v=dQw4w9WgXcQ")
        self.assertEqual(rec.title, "Mein Video")

    def test_missing_html_title_uses_cookie_free_metadata_probe(self):
        html = '<html><body><a href="https://youtu.be/dQw4w9WgXcQ"></a></body></html>'
        soup = BeautifulSoup(html, "html.parser")

        with patch(
            "transcription.downloader.fetch_youtube_title",
            return_value="Titel aus YouTube",
        ) as title_probe:
            recs = lws.discover_youtube_recordings(
                MagicMock(), soup, html, course_id="900"
            )

        self.assertEqual(recs[0].title, "Titel aus YouTube")
        title_probe.assert_called_once_with(
            "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
        )

    def test_failed_title_probe_uses_stable_generic_title(self):
        html = (
            '<html><body><iframe '
            'src="https://www.youtube.com/embed/dQw4w9WgXcQ" '
            'title="YouTube video player"></iframe></body></html>'
        )
        soup = BeautifulSoup(html, "html.parser")

        with patch(
            "transcription.downloader.fetch_youtube_title", return_value=None
        ):
            recs = lws.discover_youtube_recordings(
                MagicMock(), soup, html, course_id="900"
            )

        self.assertEqual(recs[0].title, "Aufzeichnung dQw4w9WgXcQ")

    def test_mod_url_activity_resolved_to_youtube(self):
        html = _section_html_with_url_activity(cmid="3001", name="VL-Aufzeichnung")
        soup = BeautifulSoup(html, "html.parser")
        session = MagicMock()

        with patch.object(
            lws, "_extract_url_target", return_value="https://youtu.be/bbbbbbbbbbb"
        ):
            recs = lws.discover_youtube_recordings(
                session, soup, html, course_id="900"
            )

        self.assertEqual(len(recs), 1)
        self.assertEqual(recs[0].episode_id, "bbbbbbbbbbb")
        # Aktivitätsname ist der bessere Titel als ein evtl. fehlender Linktext.
        self.assertEqual(recs[0].title, "VL-Aufzeichnung")

    def test_restricted_activity_without_view_url_is_skipped(self):
        # _extract_activities liefert view_url=None für gesperrte Aktivitäten ohne Link.
        html = _section_html_with_url_activity(cmid="3002", name="Gesperrt")
        soup = BeautifulSoup(html, "html.parser")
        session = MagicMock()

        restricted_activity = {
            "cmid": "3002",
            "course_id": "900",
            "course_name": "Informatik 1",
            "modtype": "url",
            "name": "Gesperrt",
            "section": "General",
            "view_url": None,
            "restricted": True,
            "availability_text": "Verfügbar ab ...",
        }
        with patch.object(lws, "_extract_activities", return_value=[restricted_activity]), \
             patch.object(lws, "_extract_url_target") as target_mock:
            recs = lws.discover_youtube_recordings(session, soup, html, course_id="900")

        self.assertEqual(recs, [])
        target_mock.assert_not_called()

    def test_dedup_by_video_id_across_both_sources(self):
        # Direkter Link UND mod/url-Aktivität zeigen auf dasselbe Video -> nur 1 Recording.
        html = (
            '<a href="https://youtu.be/dQw4w9WgXcQ">Direktlink</a>'
            + _section_html_with_url_activity(cmid="3003", name="Auch dieses Video")
        )
        soup = BeautifulSoup(html, "html.parser")
        session = MagicMock()

        with patch.object(
            lws, "_extract_url_target", return_value="https://youtu.be/dQw4w9WgXcQ"
        ):
            recs = lws.discover_youtube_recordings(session, soup, html, course_id="900")

        self.assertEqual(len(recs), 1)
        # Erster Treffer (direkter Link) behält seinen Titel NICHT überschrieben durch
        # mod/url, weil das Video bereits über die direkte Quelle gefunden wurde —
        # die mod/url-Logik aktualisiert den Titel aber, wenn ein Aktivitätsname vorliegt.
        self.assertEqual(recs[0].episode_id, "dQw4w9WgXcQ")

    def test_non_youtube_url_target_ignored(self):
        html = _section_html_with_url_activity(cmid="3004", name="Externe Seite")
        soup = BeautifulSoup(html, "html.parser")
        session = MagicMock()

        with patch.object(
            lws, "_extract_url_target", return_value="https://example.com/folie.pdf"
        ):
            recs = lws.discover_youtube_recordings(session, soup, html, course_id="900")

        self.assertEqual(recs, [])

    def test_url_target_resolution_exception_is_skipped_not_raised(self):
        html = _section_html_with_url_activity(cmid="3005", name="Kaputter Link")
        soup = BeautifulSoup(html, "html.parser")
        session = MagicMock()

        with patch.object(lws, "_extract_url_target", side_effect=RuntimeError("network down")):
            recs = lws.discover_youtube_recordings(session, soup, html, course_id="900")

        self.assertEqual(recs, [])


class TestFetchYoutubeSubtitlesCascade(unittest.TestCase):
    """fetch_youtube_subtitles: manuelle Subs zuerst, dann auto, sonst None."""

    def _rec_yt(self):
        return Recording(
            cmid="yt", title="YT-Video", source_url="https://youtu.be/dQw4w9WgXcQ",
            course_id="900", episode_id="dQw4w9WgXcQ",
            media_url="https://www.youtube.com/watch?v=dQw4w9WgXcQ", source_kind="youtube",
        )

    def test_valid_manual_subs_short_circuit_auto_pass(self):
        rec = self._rec_yt()
        with tempfile.TemporaryDirectory() as tmp:
            dest_dir = Path(tmp)

            def fake_run(cmd, **kwargs):
                # Erwartet --write-subs (manuell): Datei in subs_manual ablegen.
                if "--write-subs" in cmd:
                    out_dir = dest_dir / "subs_manual"
                    out_dir.mkdir(parents=True, exist_ok=True)
                    (out_dir / "subs.de.json3").write_text(
                        '{"events":[{"tStartMs":0,"dDurationMs":1000,'
                        '"segs":[{"utf8":"Manueller Text"}]}]}',
                        encoding="utf-8",
                    )
                else:
                    self.fail("Auto-Pass darf bei gültigen manuellen Subs nicht laufen")
                return SimpleNamespace(returncode=0, stdout="", stderr="")

            with patch("transcription.downloader.subprocess.run", side_effect=fake_run):
                segs = downloader.fetch_youtube_subtitles(rec, dest_dir, langs="de,en")

        self.assertIsNotNone(segs)
        self.assertEqual(segs[0].text, "Manueller Text")

    def test_manual_missing_falls_back_to_auto(self):
        rec = self._rec_yt()
        with tempfile.TemporaryDirectory() as tmp:
            dest_dir = Path(tmp)

            def fake_run(cmd, **kwargs):
                if "--write-subs" in cmd:
                    # Manuell: kein Untertitel verfügbar -> keine Datei erzeugen.
                    return SimpleNamespace(returncode=0, stdout="", stderr="")
                elif "--write-auto-subs" in cmd:
                    out_dir = dest_dir / "subs_auto"
                    out_dir.mkdir(parents=True, exist_ok=True)
                    (out_dir / "subs.de.json3").write_text(
                        '{"events":[{"tStartMs":0,"dDurationMs":1000,'
                        '"segs":[{"utf8":"Auto-Text"}]}]}',
                        encoding="utf-8",
                    )
                    return SimpleNamespace(returncode=0, stdout="", stderr="")
                self.fail("unerwarteter Aufruf")

            with patch("transcription.downloader.subprocess.run", side_effect=fake_run):
                segs = downloader.fetch_youtube_subtitles(rec, dest_dir, langs="de,en")

        self.assertIsNotNone(segs)
        self.assertEqual(segs[0].text, "Auto-Text")

    def test_broken_manual_subs_fall_back_to_valid_auto_subs(self):
        rec = self._rec_yt()
        with tempfile.TemporaryDirectory() as tmp:
            dest_dir = Path(tmp)

            def fake_run(cmd, **kwargs):
                if "--write-subs" in cmd:
                    out_dir = dest_dir / "subs_manual"
                    out_dir.mkdir(parents=True, exist_ok=True)
                    (out_dir / "subs.de.json3").write_text("[]", encoding="utf-8")
                elif "--write-auto-subs" in cmd:
                    out_dir = dest_dir / "subs_auto"
                    out_dir.mkdir(parents=True, exist_ok=True)
                    (out_dir / "subs.de.json3").write_text(
                        '{"events":[{"tStartMs":0,"dDurationMs":1000,'
                        '"segs":[{"utf8":"Auto nach kaputtem Manual"}]}]}',
                        encoding="utf-8",
                    )
                return SimpleNamespace(returncode=0, stdout="", stderr="")

            with patch("transcription.downloader.subprocess.run", side_effect=fake_run):
                segs = downloader.fetch_youtube_subtitles(rec, dest_dir, langs="de,en")

        self.assertEqual(segs[0].text, "Auto nach kaputtem Manual")

    def test_configured_language_order_beats_filename_and_format_order(self):
        rec = self._rec_yt()
        with tempfile.TemporaryDirectory() as tmp:
            dest_dir = Path(tmp)

            def fake_run(cmd, **kwargs):
                out_dir = dest_dir / "subs_manual"
                out_dir.mkdir(parents=True, exist_ok=True)
                (out_dir / "subs.de.vtt").write_text(
                    "WEBVTT\n\n00:00:00.000 --> 00:00:01.000\nDeutsch\n",
                    encoding="utf-8",
                )
                (out_dir / "subs.en.json3").write_text(
                    '{"events":[{"tStartMs":0,"dDurationMs":1000,'
                    '"segs":[{"utf8":"English"}]}]}',
                    encoding="utf-8",
                )
                return SimpleNamespace(returncode=0, stdout="", stderr="")

            with patch("transcription.downloader.subprocess.run", side_effect=fake_run):
                segs = downloader.fetch_youtube_subtitles(rec, dest_dir, langs="en,de")

        self.assertEqual(segs[0].text, "English")

    def test_both_empty_returns_none(self):
        rec = self._rec_yt()
        with tempfile.TemporaryDirectory() as tmp:
            dest_dir = Path(tmp)

            def fake_run(cmd, **kwargs):
                # Keine Dateien erzeugen -> beide Pässe liefern nichts.
                return SimpleNamespace(returncode=0, stdout="", stderr="")

            with patch("transcription.downloader.subprocess.run", side_effect=fake_run):
                segs = downloader.fetch_youtube_subtitles(rec, dest_dir, langs="de,en")

        self.assertIsNone(segs)


class TestProcessRecordingYoutubeBranch(unittest.TestCase):
    """_process_recording: YouTube-Branch nutzt Untertitel zuerst, sonst Audio+Whisper."""

    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        lws.init_transcribe_schema(self.conn)
        self.rec = Recording(
            cmid="yt", title="YT-Video", source_url="https://youtu.be/dQw4w9WgXcQ",
            course_id="900", episode_id="dQw4w9WgXcQ",
            media_url="https://www.youtube.com/watch?v=dQw4w9WgXcQ", source_kind="youtube",
        )

    def tearDown(self):
        self.conn.close()

    def _modules(self, downloader_mock, manifest_mod, notion_blocks_mod, transcriber_mock):
        return (downloader_mock, manifest_mod, notion_blocks_mod, transcriber_mock)

    def test_subtitles_available_skips_download_and_whisper(self):
        downloader_mock = MagicMock()
        downloader_mock.fetch_youtube_subtitles.return_value = [
            Segment(0.0, 5.0, "Untertitel-Text"),
        ]
        transcriber_mock = MagicMock()

        with patch.object(lws, "TRANSCRIBE_WORK_DIR", Path(tempfile.mkdtemp())):
            with patch.object(
                lws, "_build_recording_content_properties", return_value={}
            ), patch.object(lws, "_build_meeting_properties", return_value={}), \
                 patch.object(lws, "_notion_find_page_by_quelle", return_value=None), \
                 patch.object(lws, "_notion_create_page", return_value="page-1"), \
                 patch.object(lws, "notion_append_page_children"):

                result = lws._process_recording(
                    self.conn, MagicMock(), self.rec,
                    course_page_id=None, force=False, dry_run=True,
                    modules=self._modules(downloader_mock, manifest, notion_blocks, transcriber_mock),
                    shutil_mod=MagicMock(),
                )

        self.assertEqual(result, "processed")
        downloader_mock.fetch_youtube_subtitles.assert_called_once()
        downloader_mock.download_youtube_audio.assert_not_called()
        downloader_mock.extract_audio.assert_not_called()
        transcriber_mock.transcribe.assert_not_called()

    def test_subtitles_missing_falls_back_to_audio_and_whisper(self):
        downloader_mock = MagicMock()
        downloader_mock.fetch_youtube_subtitles.return_value = None
        downloader_mock.download_youtube_audio.return_value = Path("/tmp/audio.m4a")
        downloader_mock.extract_audio.return_value = Path("/tmp/audio.wav")
        downloader_mock.probe_duration.return_value = 42.0
        transcriber_mock = MagicMock()
        transcriber_mock.transcribe.return_value = (
            [Segment(0.0, 5.0, "Whisper-Text")], "youtube-whisper",
        )

        with patch.object(lws, "TRANSCRIBE_WORK_DIR", Path(tempfile.mkdtemp())):
            with patch.object(
                lws, "_build_recording_content_properties", return_value={}
            ), patch.object(lws, "_build_meeting_properties", return_value={}), \
                 patch.object(lws, "_notion_find_page_by_quelle", return_value=None), \
                 patch.object(lws, "_notion_create_page", return_value="page-1"), \
                 patch.object(lws, "notion_append_page_children"):

                result = lws._process_recording(
                    self.conn, MagicMock(), self.rec,
                    course_page_id=None, force=False, dry_run=True,
                    modules=self._modules(downloader_mock, manifest, notion_blocks, transcriber_mock),
                    shutil_mod=MagicMock(),
                )

        self.assertEqual(result, "processed")
        downloader_mock.fetch_youtube_subtitles.assert_called_once()
        downloader_mock.download_youtube_audio.assert_called_once()
        downloader_mock.extract_audio.assert_called_once()
        transcriber_mock.transcribe.assert_called_once()


class TestTranscribeCliMutualExclusion(unittest.TestCase):
    """CLI: --cmid/--course/--url sind gegenseitig exklusiv (argparse-Gruppe)."""

    def _run_main_with_argv(self, argv):
        with patch.object(sys, "argv", ["learnweb_sync.py"] + argv):
            lws.main()

    def test_url_and_cmid_together_raises_systemexit(self):
        with patch.object(lws, "USERNAME", "user"), patch.object(lws, "PASSWORD", "pw"):
            with self.assertRaises(SystemExit):
                self._run_main_with_argv(
                    ["transcribe", "--url", "https://lw/section.php?id=1", "--cmid", "123"]
                )

    def test_force_with_url_without_cmid_or_course_is_allowed(self):
        # --force --url ohne --cmid/--course darf NICHT am Guard scheitern (URL
        # ist die Eingrenzung). cmd_transcribe_url wird als No-Op gepatcht, damit
        # nichts Echtes (Netz/Notion) läuft; wir prüfen nur den Aufruf.
        with patch.object(lws, "USERNAME", "user"), patch.object(lws, "PASSWORD", "pw"), \
             patch.object(lws, "cmd_transcribe_url") as transcribe_url_mock:
            self._run_main_with_argv(
                ["transcribe", "--force", "--url", "https://lw/section.php?id=1"]
            )

        transcribe_url_mock.assert_called_once()
        _, kwargs = transcribe_url_mock.call_args
        self.assertEqual(kwargs.get("url"), "https://lw/section.php?id=1")
        self.assertTrue(kwargs.get("force"))


class TestTranscribeUrlDryRunIsWriteFree(unittest.TestCase):
    """R2-1: --url --dry-run nutzt nur den schreibfreien Notion-Lookup, nie cmd_sync_courses."""

    def test_cmd_transcribe_url_uses_read_only_lookup_not_sync_courses(self):
        # cmd_transcribe_url ruft laut Quellcode notion_query_courses_db() (read-only)
        # auf, NIE cmd_sync_courses (das würde bei Bedarf Notion-Seiten anlegen/ändern).
        # Wir spy-en cmd_sync_courses und lassen ihn fehlschlagen, falls er aufgerufen
        # würde -- so wird ein versehentlicher Schreib-Pfad hart sichtbar.
        section_html = (
            '<html><body class="course-900">'
            '<a href="https://youtu.be/dQw4w9WgXcQ">Vorlesung 1</a>'
            "</body></html>"
        )
        fake_response = SimpleNamespace(text=section_html, raise_for_status=lambda: None)
        fake_session = MagicMock()
        fake_session.get.return_value = fake_response

        sync_courses_spy = MagicMock(side_effect=AssertionError(
            "cmd_sync_courses darf im --url-Dry-Run-Pfad NICHT aufgerufen werden"
        ))

        with patch.object(lws, "NOTION_TOKEN", "fake-token"), \
             patch.object(lws, "requests") as requests_mock, \
             patch.object(lws, "login", return_value=True), \
             patch.object(lws, "cmd_sync_courses", sync_courses_spy), \
             patch.object(
                 lws, "notion_query_courses_db",
                 return_value={"by_course_id": {}},
             ) as query_courses_mock, \
             patch.object(lws, "_acquire_transcribe_lock") as lock_mock, \
             patch.object(lws, "init_db") as init_db_mock, \
             patch.object(lws, "init_transcribe_schema"), \
             patch.object(lws, "_process_recording", return_value="processed") as process_mock, \
             patch.object(lws, "notion_create_course") as create_course_mock, \
             patch.object(lws, "notion_update_course") as update_course_mock, \
             patch.object(lws, "_notion_create_page") as create_page_mock, \
             patch.object(lws, "_notion_update_page_properties") as update_page_mock:

            requests_mock.Session.return_value = fake_session
            lock_mock.return_value = MagicMock()
            init_db_mock.return_value = MagicMock()

            lws.cmd_transcribe_url(
                url="https://lw/section.php?id=1", force=False, limit=1, dry_run=True
            )

        # Read-only Lookup wurde genutzt, der schreibende Kurs-Sync nie aufgerufen.
        query_courses_mock.assert_called_once()
        sync_courses_spy.assert_not_called()
        init_db_mock.assert_not_called()
        # Keine Notion-Schreibfunktion (auch nicht innerhalb von _process_recording,
        # das hier ohnehin gemockt ist) wurde direkt von cmd_transcribe_url aus erreicht.
        create_course_mock.assert_not_called()
        update_course_mock.assert_not_called()
        create_page_mock.assert_not_called()
        update_page_mock.assert_not_called()
        process_mock.assert_called_once()

    def test_dry_run_connection_uses_memory_database(self):
        with patch.object(lws, "init_db") as init_db_mock:
            conn = lws._open_transcribe_connection(dry_run=True)
        try:
            database_path = conn.execute("PRAGMA database_list").fetchone()[2]
            table = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='transcripts'"
            ).fetchone()
        finally:
            conn.close()

        init_db_mock.assert_not_called()
        self.assertEqual(database_path, "")
        self.assertEqual(table[0], "transcripts")


if __name__ == "__main__":
    unittest.main()
