"""Offline-Unit-Tests für den Transkriptions-Worker (Paket `transcription/`).

Kein Netz, kein Notion, kein Whisper-Modell. Deckt ab: Recording-Key/Dedupe,
Zustandsautomat + State-DB-Migration, Notion-Block-Chunking (Notion-Limits),
Whisper-Segment-Normalisierung und den Opencast-Parser (beide Formate).

Stil bewusst wie tests/test_learnweb_sync.py (unittest.TestCase).
"""
import sqlite3
import unittest

import learnweb_sync as lws
from transcription import manifest, notion_blocks, recordings, transcriber
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


if __name__ == "__main__":
    unittest.main()
