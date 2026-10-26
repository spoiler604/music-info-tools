import csv
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import requests

import fetch_wikipedia_info as collector
from music_info_common import COLUMNS, MusicInfoError, load_csv
from wikipedia_api import (WikiHTML, Wikipedia, WikipediaError, WikipediaUnavailable,
                           infobox, field, release_date, release_links, track_list)


# Invented metadata fixtures: no article bodies or song lyrics.
ALBUM_HTML = '''<table class="infobox vevent">
<tr><th class="infobox-above">Example Album</th></tr>
<tr><td><a href="/wiki/File:Example.jpg"><img src="//upload.wikimedia.org/example.jpg" /></a></td></tr>
<tr><th class="infobox-header description"><a href="/wiki/Album">Studio album</a> by <a href="/wiki/Example_Artist">Example Artist</a></th></tr>
<tr><th>Released</th><td>16 November 2004<sup>[1]</sup></td></tr>
<tr><th>Genre</th><td><a href="/wiki/Hip_hop">Hip hop</a></td></tr></table>
<h2>Track listing</h2><table class="tracklist"><tr><th>No.</th><th>Title</th><th>Writer(s)</th><th>Length</th></tr>
<tr><th scope="row">1.</th><td>“First Track”<sup>[2]</sup></td><td>First Writer</td><td>2:00</td></tr>
<tr><th scope="row">2.</th><td>"Second Track"</td><td></td><td>3:00</td></tr>
<tr><th>Total length:</th><td colspan="3">5:00</td></tr></table>
<h2>Bonus discs</h2><table><tr><th>No.</th><th>Title</th></tr>
<tr><td>1.</td><td>Bonus Remix</td></tr></table>'''


def album():
    return {"title": "Example Album", "artist": "Example Artist", "date": "2004-11-16", "genre": "Hip hop",
            "tracks": [{"title": "First Track", "number": "1", "composer": "Wiki Writer"},
                       {"title": "Second Track", "number": "2", "composer": ""}],
            "language": "en", "cover_file": "File:Example.jpg", "url": "https://en.wikipedia.org/w/index.php?oldid=123"}


class ParserTests(unittest.TestCase):
    def test_album_infobox_and_primary_tracks_ignore_footnotes_and_bonus_discs(self):
        document = WikiHTML(ALBUM_HTML)
        box = infobox(document)
        self.assertEqual(box["name"], "Example Album")
        self.assertEqual(box["performers"], ["Example Artist"])
        self.assertEqual(box["cover_file"], "File:Example.jpg")
        self.assertEqual(field(box, "released")["text"], "16 November 2004")
        tracks = track_list(document)
        self.assertEqual([item["title"] for item in tracks], ["First Track", "Second Track"])
        self.assertEqual(tracks[0]["composer"], "First Writer")

    def test_date_formats_and_invalid_dates(self):
        for text, expected in (("2004-11-16", "2004-11-16"), ("2004년 11월 16일", "2004-11-16"),
                               ("16 November 2004", "2004-11-16"), ("November 16, 2004", "2004-11-16"),
                               ("2004", "2004"), ("2004-19-99", "")):
            with self.subTest(text=text):
                self.assertEqual(release_date(text), expected)

    def test_discography_keeps_colons_and_skips_chart_header_links(self):
        html = '''<table><tr><th rowspan="2">Title</th><th>Charts</th></tr>
        <tr><th><a href="/wiki/Billboard_200">US</a></th></tr>
        <tr><th scope="row"><a href="/wiki/Example:_Album">Example: Album</a></th><td>1</td></tr></table>'''
        self.assertEqual(release_links(WikiHTML(html)), ["Example: Album"])

    def test_korean_track_headers_and_explicit_artist_cell(self):
        html = '''<table class="infobox"><tr><th>음악가</th><td>가수 이름</td></tr></table>
        <table><tr><th>번호</th><th>제목</th><th>작곡</th></tr>
        <tr><td>1</td><td>테스트 곡</td><td>작곡가</td></tr></table>'''
        self.assertEqual(infobox(WikiHTML(html))["performers"], ["가수 이름"])
        self.assertEqual(track_list(WikiHTML(html))[0]["composer"], "작곡가")

    def test_korean_album_header_and_hash_number_column(self):
        html = '''<table class="infobox"><tr><th class="infobox-header description">
        <a href="/wiki/가수_이름">가수 이름</a>의 <a href="/wiki/음반">정규 음반</a></th></tr></table>
        <h2>수록곡</h2><table class="tracklist"><tr><th>#</th><th>제목</th><th>작사</th><th>작곡</th></tr>
        <tr><td>1.</td><td>〈테스트 곡〉</td><td>작사가</td><td>작곡가</td></tr></table>'''
        document = WikiHTML(html)
        self.assertEqual(infobox(document)["performers"], ["가수 이름"])
        self.assertEqual(track_list(document)[0], {"title": "테스트 곡", "number": "1", "composer": "작곡가"})

    def test_second_disc_is_kept_but_deluxe_bonus_section_is_not(self):
        table = lambda title: '<table><tr><th>No.</th><th>Title</th></tr><tr><td>1.</td><td>' + title + '</td></tr></table>'
        html = '<h2>Track listing</h2><h3>Disc one</h3>' + table('First') + '<h3>Disc two</h3>' + table('Second')
        html += '<h3>Deluxe bonus tracks</h3>' + table('Extra')
        self.assertEqual([item["title"] for item in track_list(WikiHTML(html))], ["First", "Second"])


class MergeTests(unittest.TestCase):
    def test_wikipedia_values_win_and_musicbrainz_only_fills_missing_fields(self):
        wiki = album()
        mb = {"genre": "Rock", "date": "2012-01-01", "url": "https://musicbrainz.org/release/example",
              "tracks": [{"title": "First Track", "number": "9", "composer": "Other Writer"},
                         {"title": "Second Track", "number": "10", "composer": "MB Writer"}]}
        rows = collector.combine(wiki, mb, "Example Artist")
        self.assertEqual(rows[0]["작곡가"], "Wiki Writer")
        self.assertEqual(rows[1]["작곡가"], "MB Writer")
        self.assertEqual(rows[0]["장르"], "Hip hop")
        self.assertEqual(rows[0]["발매연도"], "2004")
        self.assertEqual(rows[0]["트랙 번호"], "1")
        self.assertIn("발매연도 차이", rows[0]["검토 메모"])

    def test_musicbrainz_track_fallback_and_no_fuzzy_composer_assignment(self):
        wiki = album()
        wiki["tracks"] = []
        wiki["date"] = ""
        wiki["genre"] = ""
        mb = {"tracks": [{"title": "First Track", "number": "1", "composer": "MB Writer"}],
              "genre": "Jazz", "date": "2004-11-16"}
        rows = collector.combine(wiki, mb, "Example Artist")
        self.assertEqual(rows[0]["작곡가"], "MB Writer")
        self.assertIn("트랙 표 없음", rows[0]["검토 메모"])
        self.assertEqual(rows[0]["장르"], "Jazz")
        self.assertEqual(rows[0]["발매연도"], "2004")
        wiki = album()
        rows = collector.combine(wiki, {"tracks": [{"title": "Second Tracks", "composer": "Wrong"}]}, "Example Artist")
        self.assertEqual(rows[1]["작곡가"], "")

    def test_musicbrainz_release_lookup_requires_title_and_artist(self):
        client = collector.MusicBrainzSupplement()
        data = {"releases": [{"title": "Example Album", "id": "wrong", "artist-credit": [{"name": "Other Artist"}]}]}
        with patch.object(client, "get", return_value=data) as get:
            self.assertEqual(client.album("Example Album", ["Example Artist"]), {})
        self.assertEqual(get.call_count, 1)

    def test_musicbrainz_supplement_reads_release_tracks_and_original_date(self):
        client = collector.MusicBrainzSupplement()
        search = {"releases": [{"title": "Example Album", "id": "right", "date": "2012-01-01",
                                "artist-credit": [{"name": "Example Artist"}]}]}
        detail = {"date": "2012-01-01", "genres": None,
                  "release-group": {"first-release-date": "2004-11-16",
                                    "genres": [{"name": "Hip hop", "count": 4}]},
                  "media": [{"tracks": [{"title": "First Track", "number": "1",
                    "recording": {"relations": [{"work": {"relations": [
                        {"type": "composer", "artist": {"name": "MB Composer"}}]}}]}}]}]}
        with patch.object(client, "get", side_effect=[search, detail]):
            result = client.album("Example Album", ["Example Artist"])
        self.assertEqual(result["date"], "2004-11-16")
        self.assertEqual(result["genre"], "Hip hop")
        self.assertEqual(result["tracks"][0]["composer"], "MB Composer")
        self.assertEqual(result["url"], "https://musicbrainz.org/release/right")


class ClientTests(unittest.TestCase):
    def test_cache_user_agent_and_error_circuit_breaker(self):
        client = Wikipedia("en")
        self.assertIn("MusicInfoTools", client.session.headers["User-Agent"])
        good = Mock()
        good.json.return_value = {"query": {"pages": []}}
        with patch.object(client.session, "get", return_value=good) as get:
            client.request("en", action="query", titles="Example")
            client.request("en", action="query", titles="Example")
        self.assertEqual(get.call_count, 1)
        bad = Mock()
        bad.raise_for_status.side_effect = requests.HTTPError("429")
        with patch.object(client.session, "get", return_value=bad) as get, patch("wikipedia_api.time.sleep"):
            for _ in range(2):
                with self.assertRaises(WikipediaUnavailable):
                    client.request("en", action="query", titles="Other")
        self.assertEqual(get.call_count, 1)

    def test_album_with_wrong_performer_is_not_collected(self):
        client = Wikipedia("en")
        document = WikiHTML(ALBUM_HTML)
        artist = {"language": "en", "artist": "Other Artist", "aliases": ["Other Artist"],
                  "title": "Other Artist", "sections": [],
                  "document": WikiHTML('<a href="/wiki/Example_Album">Example Album</a>')}
        with patch.object(client, "parse", return_value={"title": "Example Album", "revision": 12, "document": document}):
            self.assertEqual(client.albums(artist, log=lambda _: None), [])

    def test_search_rejects_ambiguous_artist_and_supports_english_fallback(self):
        client = Wikipedia("ko")
        profile = {"title": "Example Artist", "revision": 12, "sections": [],
                   "document": WikiHTML('<table class="infobox"><tr><th>Genres</th><td>Jazz</td></tr></table>')}
        with patch.object(client, "request", side_effect=[{"query": {"pages": []}},
                    {"query": {"pages": [{"title": "Example Artist"}]}}]), patch.object(client, "parse", return_value=profile):
            self.assertEqual(client.find_artist("Example Artist")["language"], "en")
        client = Wikipedia("en")
        with patch.object(client, "request", return_value={"query": {"pages": [
                    {"title": "Example Artist (singer)"}, {"title": "Example Artist (rapper)"}]}}), \
                patch.object(client, "parse", return_value=profile):
            with self.assertRaises(WikipediaError):
                client.find_artist("Example Artist")


class CollectionTests(unittest.TestCase):
    def client(self):
        client = Mock()
        client.artist_page.return_value = {"artist": "Example Artist", "url": "https://en.wikipedia.org/wiki/Example_Artist"}
        client.albums.return_value = [album()]
        client.image_info.return_value = {"url": "https://upload.wikimedia.org/example.jpg",
                                          "description_url": "https://en.wikipedia.org/wiki/File:Example.jpg", "license": "Example license"}
        response = Mock()
        response.content = b"\xff\xd8\xffexample"
        client.session.get.return_value = response
        return client

    def test_saves_artist_csv_and_central_artwork_with_provenance(self):
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as folder:
            root = Path(folder)
            with patch.object(collector, "Wikipedia", return_value=self.client()):
                collector.collect_wikipedia("Example Artist", root=root, musicbrainz=False, log=lambda _: None)
            csv_path = root / "Example Artist" / "Example Artist.csv"
            self.assertEqual(len(load_csv(csv_path)), 2)
            with csv_path.open(encoding="utf-8-sig", newline="") as handle:
                reader = csv.DictReader(handle)
                self.assertEqual(reader.fieldnames[:9], list(COLUMNS))
                rows = list(reader)
            self.assertEqual(rows[0]["발매일"], "2004-11-16")
            self.assertTrue(rows[0]["위키백과 URL"].startswith("https://en.wikipedia.org/"))
            self.assertEqual(rows[0]["앨범아트 라이선스"], "Example license")
            self.assertTrue((csv_path.parent / rows[0]["앨범아트 파일"]).is_file())
            self.assertEqual(list(csv_path.parent.iterdir()), [csv_path])

    def test_preview_performs_no_file_writes_or_cover_download(self):
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as folder:
            root = Path(folder)
            client = self.client()
            with patch.object(collector, "Wikipedia", return_value=client):
                rows = collector.collect_wikipedia("Example Artist", root=root, preview=True,
                                                   musicbrainz=False, log=lambda _: None)
            self.assertEqual(len(rows), 2)
            self.assertEqual(list(root.iterdir()), [])
            client.image_info.assert_not_called()
            client.session.get.assert_not_called()

    def test_musicbrainz_failure_keeps_wikipedia_data_and_existing_csv_is_protected(self):
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as folder:
            root = Path(folder)
            mb = Mock()
            mb.album.side_effect = requests.ConnectionError("offline")
            with patch.object(collector, "Wikipedia", return_value=self.client()), \
                    patch.object(collector, "MusicBrainzSupplement", return_value=mb):
                rows = collector.collect_wikipedia("Example Artist", root=root, covers=False, log=lambda _: None)
            self.assertEqual(len(rows), 2)
            csv_path = root / "Example Artist" / "Example Artist.csv"
            before = csv_path.read_bytes()
            with patch.object(collector, "Wikipedia", return_value=self.client()):
                with self.assertRaises(MusicInfoError):
                    collector.collect_wikipedia("Example Artist", root=root, musicbrainz=False, log=lambda _: None)
            self.assertEqual(csv_path.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
