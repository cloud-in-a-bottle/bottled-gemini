from __future__ import annotations

import os
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path
from unittest.mock import patch

import feed
from feed import ATOM_NS
from feed import GENERATED_FEED_FILENAME
from feed import refresh_feed


class FeedTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.content_dir = Path(self.temp_dir.name)
        (self.content_dir / "posts").mkdir()

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def _write(self, name: str, content: str) -> None:
        (self.content_dir / "posts" / name).write_text(content, encoding="utf-8")

    def _channel(self) -> ET.Element:
        channel = ET.parse(self.content_dir / "feed.rss").getroot().find("channel")
        self.assertIsNotNone(channel)
        return channel  # type: ignore[return-value]

    def test_generates_newest_first_rss_feed_from_dated_posts(self) -> None:
        self._write("2026-09-02-second-post.gmi", "# Second post\n\nThe newer summary.\n")
        self._write("2026-09-01-first-post.gmi", "# First & <best> post\n\nThe older summary.\n")
        self._write("notes.gmi", "# Not a dated post\n")

        signature = refresh_feed(self.content_dir, "capsule.example", None)

        self.assertEqual(len(signature), 2)
        self.assertEqual(os.readlink(self.content_dir / "feed.rss"), GENERATED_FEED_FILENAME)
        self.assertTrue((self.content_dir / GENERATED_FEED_FILENAME).is_file())
        channel = self._channel()
        self.assertEqual(channel.findtext("title"), "capsule.example gemlog")
        self.assertEqual(channel.findtext("link"), "gemini://capsule.example/")
        self.assertEqual(
            channel.find(f"{{{ATOM_NS}}}link").attrib["href"],  # type: ignore[union-attr]
            "gemini://capsule.example/feed.rss",
        )
        items = channel.findall("item")
        self.assertEqual([item.findtext("title") for item in items], ["Second post", "First & <best> post"])
        self.assertEqual(items[0].findtext("link"), "gemini://capsule.example/posts/2026-09-02-second-post.gmi")
        self.assertEqual(items[0].findtext("pubDate"), "Wed, 02 Sep 2026 00:00:00 GMT")
        self.assertEqual(items[1].findtext("description"), "The older summary.")

    def test_uses_filename_when_post_has_no_heading(self) -> None:
        self._write("2026-09-03-fallback-title.gmi", "A post without a heading.\n")

        refresh_feed(self.content_dir, "capsule.example", None)

        item = self._channel().find("item")
        self.assertEqual(item.findtext("title"), "Fallback Title")  # type: ignore[union-attr]
        self.assertEqual(item.findtext("description"), "A post without a heading.")  # type: ignore[union-attr]

    def test_skips_rewrite_when_sources_are_unchanged(self) -> None:
        self._write("2026-09-04-one.gmi", "# One\n")
        signature = refresh_feed(self.content_dir, "capsule.example", None)
        feed_path = self.content_dir / "feed.rss"
        first_mtime = feed_path.stat().st_mtime_ns

        next_signature = refresh_feed(self.content_dir, "capsule.example", signature)

        self.assertEqual(next_signature, signature)
        self.assertEqual(feed_path.stat().st_mtime_ns, first_mtime)

    def test_preserves_an_existing_user_feed(self) -> None:
        feed_path = self.content_dir / "feed.rss"
        feed_path.write_text("manually maintained feed\n", encoding="utf-8")
        self._write("2026-09-04-one.gmi", "# Generated post\n")

        refresh_feed(self.content_dir, "capsule.example", None)

        self.assertEqual(feed_path.read_text(), "manually maintained feed\n")
        self.assertFalse((self.content_dir / GENERATED_FEED_FILENAME).exists())

    def test_manual_feed_wins_first_generation_race(self) -> None:
        feed_path = self.content_dir / "feed.rss"
        original_link = feed._create_managed_feed_link

        def racing_link(path: Path) -> bool:
            path.write_text("manual feed created concurrently\n", encoding="utf-8")
            return original_link(path)

        with patch.object(feed, "_create_managed_feed_link", side_effect=racing_link):
            refresh_feed(self.content_dir, "capsule.example", None)

        self.assertEqual(feed_path.read_text(), "manual feed created concurrently\n")
        self.assertFalse(feed_path.is_symlink())

    def test_manual_replacement_of_generated_link_is_preserved(self) -> None:
        post = self.content_dir / "posts" / "2026-09-04-one.gmi"
        post.write_text("# First title\n", encoding="utf-8")
        signature = refresh_feed(self.content_dir, "capsule.example", None)
        feed_path = self.content_dir / "feed.rss"
        feed_path.unlink()
        feed_path.write_text("manual replacement\n", encoding="utf-8")
        post.write_text("# Recovered title\n", encoding="utf-8")

        refresh_feed(self.content_dir, "capsule.example", signature)

        self.assertEqual(feed_path.read_text(), "manual replacement\n")
        self.assertFalse(feed_path.is_symlink())

    def test_in_place_edit_of_generated_feed_is_preserved(self) -> None:
        post = self.content_dir / "posts" / "2026-09-04-one.gmi"
        post.write_text("# First title\n", encoding="utf-8")
        signature = refresh_feed(self.content_dir, "capsule.example", None)
        feed_path = self.content_dir / "feed.rss"
        feed_path.write_text("manual in-place edit\n", encoding="utf-8")
        post.write_text("# New post title\n", encoding="utf-8")

        refresh_feed(self.content_dir, "capsule.example", signature)

        self.assertEqual(feed_path.read_text(), "manual in-place edit\n")
        self.assertTrue(feed_path.is_symlink())

    def test_ignores_hidden_and_symlinked_post_directories(self) -> None:
        hidden = self.content_dir / "posts" / ".drafts"
        hidden.mkdir()
        (hidden / "2026-09-04-secret.gmi").write_text("# Secret draft\n", encoding="utf-8")
        with tempfile.TemporaryDirectory() as outside_dir:
            outside = Path(outside_dir)
            (outside / "2026-09-05-outside.gmi").write_text("# Outside post\n", encoding="utf-8")
            (self.content_dir / "posts" / "linked").symlink_to(outside, target_is_directory=True)

            refresh_feed(self.content_dir, "capsule.example", None)

        self.assertEqual(self._channel().findall("item"), [])

    def test_detects_same_size_rewrite_with_preserved_mtime(self) -> None:
        post = self.content_dir / "posts" / "2026-09-04-one.gmi"
        post.write_text("# One\n", encoding="utf-8")
        signature = refresh_feed(self.content_dir, "capsule.example", None)
        original = post.stat()
        post.write_text("# Two\n", encoding="utf-8")
        post_stat = post.stat()
        os.utime(post, ns=(post_stat.st_atime_ns, original.st_mtime_ns))

        next_signature = refresh_feed(self.content_dir, "capsule.example", signature)

        self.assertNotEqual(next_signature, signature)
        self.assertEqual(self._channel().findtext("item/title"), "Two")

    def test_sanitizes_xml_controls_and_ignores_headings_in_code_blocks(self) -> None:
        self._write(
            "2026-09-04-safe.gmi",
            "```example\n# Hidden heading\n```\n#Visible\x00 heading\n\n"
            "Summary with \x01 control and <script>visible</script>.\n",
        )

        refresh_feed(self.content_dir, "capsule.example", None)

        item = self._channel().find("item")
        self.assertEqual(item.findtext("title"), "Visible\uFFFD heading")  # type: ignore[union-attr]
        self.assertEqual(
            item.findtext("description"),  # type: ignore[union-attr]
            "Summary with \uFFFD control and &lt;script&gt;visible&lt;/script&gt;.",
        )


if __name__ == "__main__":
    unittest.main()
