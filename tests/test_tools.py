import csv
import tempfile
import shutil
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import apply_music_info as apply
import fetch_music_info as fetch
from music_matching import strip_track_prefix
from music_info_common import (COLUMNS, OPTIONAL_MATCH_COLUMNS, MusicInfoError, atomic_csv,
                               clean_filename, load_csv, normalized, unique_rows)


def row(title="Song", album="Album"):
    return {column: "" for column in COLUMNS} | {
        "트랙 이름": title, "앨범 이름": album, "아티스트 이름": "Artist",
        "앨범 아티스트 이름": "Artist", "발매연도": "2004", "트랙 번호": "1",
        "장르": "Hip Hop", "앨범아트 파일": "cover.jpg",
    }


class CommonTests(unittest.TestCase):
    def test_normalized_handles_case_punctuation_and_accents(self):
        self.assertEqual(normalized("Madvillainy: Démo"), "madvillainydemo")

    def test_atomic_csv_roundtrip_and_validation(self):
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as folder:
            path = Path(folder) / "info.csv"
            atomic_csv(path, [row()])
            self.assertEqual(load_csv(path)[0]["트랙 번호"], "1")
            path.write_text("wrong\nvalue\n", encoding="utf-8")
            with self.assertRaises(MusicInfoError):
                load_csv(path)

    def test_optional_alternate_names_are_loaded(self):
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as folder:
            path = Path(folder) / "info.csv"
            item = row() | {"대체 트랙 이름": "노래", "대체 앨범 이름": "음반"}
            atomic_csv(path, [item], columns=(*COLUMNS, *OPTIONAL_MATCH_COLUMNS))
            loaded = load_csv(path)[0]
        self.assertEqual(loaded["대체 트랙 이름"], "노래")
        self.assertEqual(loaded["대체 앨범 이름"], "음반")

    def test_unique_rows_removes_exact_release_duplicate_only(self):
        rows = [row(), row(), row(album="Other Album")]
        self.assertEqual(len(unique_rows(rows)), 2)


class MatchingTests(unittest.TestCase):
    def setUp(self):
        self.index = {normalized("Song"): [row("Song", "Album A"), row("Song", "Album B")]}

    def test_album_tag_resolves_duplicate_title(self):
        chosen, error = apply.choose_row(self.index, Path("Song.flac"), {"title": "Song", "album": "Album B"})
        self.assertEqual(chosen["앨범 이름"], "Album B")
        self.assertEqual(error, "")

    def test_ambiguous_title_is_not_guessed(self):
        chosen, error = apply.choose_row(self.index, Path("Song.flac"), {"title": "Song", "album": ""})
        self.assertIsNone(chosen)
        self.assertIn("확정하지 못함", error)

    def test_artist_tag_resolves_same_title_across_artists(self):
        first = row("Intro", "Debut")
        first["아티스트 이름"] = "First Artist"
        second = row("Intro", "Debut")
        second["아티스트 이름"] = "Second Artist feat. Guest"
        index = {normalized("Intro"): [first, second]}
        chosen, _ = apply.choose_row(index, Path("Intro.flac"),
                                     {"title": "Intro", "album": "Debut", "artist": "Second Artist"})
        self.assertEqual(chosen["아티스트 이름"], "Second Artist feat. Guest")

    def test_tagless_artist_title_filename_resolves_artist(self):
        with patch.object(apply, "require_mutagen") as mutagen:
            mutagen.return_value.File.return_value = None
            metadata = apply.read_metadata(Path("First Artist - Intro.flac"))
        self.assertEqual(metadata["artist"], "First Artist")
        self.assertEqual(metadata["title"], "Intro")

    def test_track_number_is_read_from_audio_metadata(self):
        with patch.object(apply, "require_mutagen") as mutagen:
            mutagen.return_value.File.return_value = SimpleNamespace(tags={"tracknumber": ["2/12"]})
            metadata = apply.read_metadata(Path("번역된 제목.flac"))
        self.assertEqual(metadata["tracknumber"], "2/12")

    def test_filename_cleanup_matches_feature_and_number_prefix(self):
        index = {normalized("Song"): [row()]}
        chosen, _ = apply.choose_row(index, Path("01 - Artist - Song (feat. Guest).flac"),
                                     {"title": "", "album": ""})
        self.assertEqual(chosen["트랙 이름"], "Song")

    def test_plan_uses_folder_album_vote(self):
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as folder:
            music = Path(folder)
            first = music / "Unique.flac"
            second = music / "Song.flac"
            first.write_bytes(b"")
            second.write_bytes(b"")
            rows = [row("Unique", "Album B"), *self.index[normalized("Song")]]
            metadata = {first: {"title": "Unique", "album": ""}, second: {"title": "Song", "album": ""}}
            with patch.object(apply, "read_metadata", side_effect=lambda path: metadata[path]):
                plan = apply.make_plan(music, rows)
            self.assertEqual(plan[1].row["앨범 이름"], "Album B")

    def test_plan_infers_unique_translated_album_from_complete_track_numbers(self):
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as folder:
            music = Path(folder) / "번역된 앨범명"
            music.mkdir()
            paths = [music / f"0{number} - 번역 제목 {number}.flac" for number in range(1, 4)]
            for path in paths:
                path.write_bytes(b"")
            rows = []
            for number in range(1, 4):
                item = row(f"English Song {number}", "English Album")
                item["트랙 번호"] = str(number)
                rows.append(item)
            other = row("Other Song", "Short Album")
            other["트랙 번호"] = "1"
            rows.append(other)
            metadata = {path: {"title": "", "album": "번역된 앨범명", "artist": "Artist",
                               "albumartist": "", "tracknumber": ""} for path in paths}
            with patch.object(apply, "read_metadata", side_effect=lambda path: metadata[path]):
                plan = apply.make_plan(music, rows)
            self.assertTrue(all(item.row and item.row["앨범 이름"] == "English Album" for item in plan))
            self.assertTrue(all("번역 제목 대신 트랙 번호" in item.reason for item in plan))

    def test_translated_album_signature_must_be_unique(self):
        items = []
        for number in (1, 2):
            metadata = {"title": "", "album": "번역 앨범", "artist": "Artist",
                        "albumartist": "", "tracknumber": ""}
            items.append((Path(f"0{number} - 번역 곡.flac"), metadata, None, ""))
        rows = []
        for album in ("Album A", "Album B"):
            for number in (1, 2):
                item = row(f"Song {number}", album)
                item["트랙 번호"] = str(number)
                rows.append(item)
        self.assertEqual(apply.infer_translated_album(items, rows), "")

    def test_backup_folder_is_excluded_from_recursive_scan(self):
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as folder:
            music = Path(folder)
            backup = music / "backup"
            backup.mkdir()
            (music / "Song.flac").write_bytes(b"")
            (backup / "Song.flac").write_bytes(b"")
            with patch.object(apply, "read_metadata", return_value={"title": "Song", "album": "Album"}):
                plan = apply.make_plan(music, [row()], backup)
            self.assertEqual(len(plan), 1)


class FlexibleMatchingTests(unittest.TestCase):
    def choose(self, title, rows, *, artist="Artist", album="Album", filename=None):
        index = {}
        for item in rows:
            index.setdefault(normalized(item["트랙 이름"]), []).append(item)
        return apply.choose_row(index, Path(filename or (title + ".flac")),
                                {"title": title, "artist": artist, "album": album})

    def test_decorations_are_removed_on_either_side(self):
        for source, target in (
            ("Song", "Song (feat. Guest)"),
            ("Song (ft. Guest)", "Song"),
            ("Song [Official Music Video]", "Song"),
            ("Song", "Song - 2011 Remastered"),
            ("Song (2020 Remaster)", "Song"),
            ("Song", "Song（feat. Guest）"),
        ):
            with self.subTest(source=source, target=target):
                chosen, reason = self.choose(source, [row(target)])
                self.assertIsNotNone(chosen)
                self.assertIn("표기 정리", reason)

    def test_subtitle_needs_artist_or_album_context(self):
        chosen, reason = self.choose("Northern Lights", [row("Northern Lights (Under the Sky)")])
        self.assertIsNotNone(chosen)
        self.assertIn("부제", reason)
        chosen, _ = self.choose("Northern Lights", [row("Northern Lights (Under the Sky)")],
                                artist="", album="")
        self.assertIsNone(chosen)

    def test_minor_typo_matches_with_context_and_reports_reason(self):
        chosen, reason = self.choose("Bohemain Rhapsody", [row("Bohemian Rhapsody")])
        self.assertIsNotNone(chosen)
        self.assertIn("제목 유사도", reason)
        self.assertIn("아티스트 일치", reason)

    def test_two_nearby_candidates_are_not_picked_arbitrarily(self):
        chosen, reason = self.choose("Northern Lightx", [row("Northern Lights"), row("Northern Lightz")])
        self.assertIsNone(chosen)
        self.assertIn("확정하지 못함", reason)

    def test_recording_versions_remain_distinct(self):
        for source, target in (
            ("Northern Lights", "Northern Lights (Live)"),
            ("Northern Lights (Remix)", "Northern Lights"),
            ("Northern Lights (Madlib Remix)", "Northern Lights (Other Remix)"),
            ("Northern Lights (Instrumental)", "Northern Lights"),
            ("Northern Lights (Clean)", "Northern Lights (Explicit)"),
            ("Northern Lights (Part 1)", "Northern Lights (Part 2)"),
            ("Northern Lights feat. Guest (Live)", "Northern Lights"),
        ):
            with self.subTest(source=source, target=target):
                chosen, _ = self.choose(source, [row(target)])
                self.assertIsNone(chosen)

    def test_typo_in_same_live_version_can_match(self):
        chosen, _ = self.choose("Bohemain Rhapsody (Live)", [row("Bohemian Rhapsody (Live)")])
        self.assertIsNotNone(chosen)

    def test_conflicting_artist_rejects_even_unique_title(self):
        chosen, _ = self.choose("Song", [row()], artist="Someone Else")
        self.assertIsNone(chosen)

    def test_embedded_title_matches_even_when_filename_uses_another_language(self):
        cases = (
            ("English Song", "한글 제목.flac"),
            ("한글 제목", "English Song.flac"),
        )
        for title, filename in cases:
            with self.subTest(title=title, filename=filename):
                chosen, _ = self.choose(title, [row(title)], filename=filename)
                self.assertIsNotNone(chosen)

    def test_alternate_track_title_matches_tagless_translated_filename(self):
        target = row("Light Rains") | {"대체 트랙 이름": "빛이 내리네"}
        chosen, reason = self.choose("", [target], artist="", album="", filename="빛이 내리네.flac")
        self.assertEqual(chosen, target)
        self.assertIn("대체 제목 일치", reason)

    def test_alternate_album_name_resolves_duplicate_primary_title(self):
        first = row("Intro", "New Day") | {"대체 앨범 이름": "새로운 날"}
        second = row("Intro", "Other Album")
        chosen, _ = self.choose("Intro", [first, second], album="새로운 날")
        self.assertEqual(chosen, first)

    def test_translated_filename_can_match_unique_track_number_with_context(self):
        target = row("English Song")
        target["트랙 번호"] = "2"
        index = {normalized(target["트랙 이름"]): [target]}
        chosen, reason = apply.choose_row(index, Path("02 - 한글 제목.flac"),
                                          {"title": "", "artist": "Artist", "album": "Album"})
        self.assertEqual(chosen, target)
        self.assertIn("번역 제목 대신 트랙 번호 2", reason)

    def test_track_number_fallback_requires_artist_or_album_context(self):
        target = row("English Song")
        target["트랙 번호"] = "2"
        index = {normalized(target["트랙 이름"]): [target]}
        chosen, _ = apply.choose_row(index, Path("02 - 한글 제목.flac"),
                                     {"title": "", "artist": "", "album": ""})
        self.assertIsNone(chosen)

    def test_short_titles_and_numbered_tracks_are_not_fuzzy_matched(self):
        for source, target in (("Home", "Hope"), ("Chapter 1", "Chapter 2")):
            with self.subTest(source=source, target=target):
                self.assertIsNone(self.choose(source, [row(target)])[0])

    def test_exact_title_wins_over_similar_title(self):
        chosen, _ = self.choose("Northern Lights", [row("Northern Lightz"), row("Northern Lights")])
        self.assertEqual(chosen["트랙 이름"], "Northern Lights")

    def test_no_artist_tags_are_required_for_harmless_decorations(self):
        chosen, reason = self.choose("Song", [row("Song (feat. Guest)")], artist="", album="")
        self.assertIsNotNone(chosen)
        self.assertIn("표기 정리", reason)

    def test_track_prefix_keeps_numbers_that_belong_to_title(self):
        for title in ("7 rings", "99 Problems", "1989"):
            self.assertEqual(strip_track_prefix(title), title)
        for title in ("01 - Song", "01 Song", "001. Song"):
            self.assertEqual(strip_track_prefix(title), "Song")

    def test_identical_csv_rows_do_not_create_ambiguity(self):
        chosen, _ = self.choose("Song", [row(), row()])
        self.assertIsNotNone(chosen)

    def test_plan_preview_explains_non_exact_match_without_writing_audio(self):
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as folder:
            music = Path(folder)
            path = music / "01 - Artist - Bohemain Rhapsody.flac"
            path.write_bytes(b"original")
            csv_path = music / "info.csv"
            atomic_csv(csv_path, [row("Bohemian Rhapsody")])
            logs = []
            with patch.object(apply, "read_metadata", return_value={
                    "title": "Bohemain Rhapsody", "artist": "Artist", "album": "Album"}), \
                    patch.object(apply, "apply_tags") as write:
                counts = apply.execute(music, csv_path, music / "covers", log=logs.append)
            self.assertEqual(counts, (1, 0))
            self.assertTrue(any("제목 유사도" in text for text in logs))
            write.assert_not_called()
            self.assertEqual(path.read_bytes(), b"original")


class FetchTests(unittest.TestCase):
    class FakeYT:
        def __init__(self, results):
            self.results = results

        def search(self, query, filter=None):
            return self.results

    def test_exact_artist_is_selected(self):
        results = [{"artist": "Another Band", "browseId": "1"},
                   {"artist": "Beyoncé", "browseId": "2"}]
        self.assertEqual(fetch.artist_result(self.FakeYT(results), "beyonce")["browseId"], "2")

    def test_ambiguous_artist_is_not_selected_arbitrarily(self):
        results = [{"artist": "The Comets", "browseId": "1"},
                   {"artist": "Comets Orchestra", "browseId": "2"}]
        with self.assertRaises(MusicInfoError):
            fetch.artist_result(self.FakeYT(results), "Comets")


class OrganizedBackupTests(unittest.TestCase):
    def fixture(self, folder):
        root = Path(folder)
        music = root / "music"
        music.mkdir()
        source = music / "Song.flac"
        source.write_bytes(b"original audio")
        csv_path = root / "音楽情報" / "Artist" / "Artist.csv"
        atomic_csv(csv_path, [row()])
        return music, source, csv_path

    def test_default_backup_follows_csv_library_including_custom_root(self):
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as folder:
            music, source, csv_path = self.fixture(folder)
            expected = csv_path.parent.parent / "백업" / "백업_(Artist)" / "Album" / source.name
            def write_tags(path, item, art):
                self.assertEqual(expected.read_bytes(), b"original audio")
                path.write_bytes(b"updated audio")
            with patch.object(apply, "read_metadata", return_value={"title": "Song", "artist": "Artist"}), \
                    patch.object(apply, "apply_tags", side_effect=write_tags):
                apply.execute(music, csv_path, Path(folder), commit=True, log=lambda _: None)
            self.assertEqual(source.read_bytes(), b"updated audio")
            self.assertEqual(expected.read_bytes(), b"original audio")

    def test_repeated_backup_reuses_identical_bytes_and_preserves_changed_versions(self):
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as folder:
            music, source, csv_path = self.fixture(folder)
            backup = apply.default_backup_root(csv_path)
            first = apply.backup_file(source, music, backup, row())
            self.assertEqual(apply.backup_file(source, music, backup, row()), first)
            source.write_bytes(b"different audio")
            second = apply.backup_file(source, music, backup, row())
            self.assertNotEqual(first, second)
            self.assertEqual(first.read_bytes(), b"original audio")
            self.assertEqual(second.read_bytes(), b"different audio")
            self.assertEqual(apply.backup_file(source, music, backup, row()), second)

    def test_artist_and_album_both_separate_backups(self):
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as folder:
            music, source, csv_path = self.fixture(folder)
            backup = apply.default_backup_root(csv_path)
            original = apply.backup_file(source, music, backup, row())
            second_row = row()
            second_row["앨범 아티스트 이름"] = "Other Artist"
            other_artist = apply.backup_file(source, music, backup, second_row)
            other_album = apply.backup_file(source, music, backup, row(album="Other Album"))
            self.assertEqual(len({original, other_artist, other_album}), 3)
            self.assertEqual(other_artist.parent.parent.name, "백업_(Other Artist)")
            self.assertEqual(other_album.parent.name, "Other Album")

    def test_same_filename_from_different_source_folders_keeps_both(self):
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as folder:
            music, source, csv_path = self.fixture(folder)
            other = music / "another" / source.name
            other.parent.mkdir()
            other.write_bytes(b"another source")
            backup = apply.default_backup_root(csv_path)
            first = apply.backup_file(source, music, backup, row())
            second = apply.backup_file(other, music, backup, row())
            self.assertNotEqual(first, second)
            self.assertEqual(first.read_bytes(), b"original audio")
            self.assertEqual(second.read_bytes(), b"another source")

    def test_preview_lists_target_without_creating_backup(self):
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as folder:
            music, source, csv_path = self.fixture(folder)
            logs = []
            with patch.object(apply, "read_metadata", return_value={"title": "Song", "artist": "Artist"}), \
                    patch.object(apply, "apply_tags") as write:
                apply.execute(music, csv_path, Path(folder), log=logs.append)
            self.assertFalse(apply.default_backup_root(csv_path).exists())
            self.assertTrue(any("백업 예정" in text and "백업_(Artist)" in text for text in logs))
            write.assert_not_called()

    def test_backup_failure_stops_before_tag_write(self):
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as folder:
            music, source, csv_path = self.fixture(folder)
            with patch.object(apply, "read_metadata", return_value={"title": "Song", "artist": "Artist"}), \
                    patch.object(apply, "backup_file", side_effect=OSError("copy failed")), \
                    patch.object(apply, "apply_tags") as write:
                with self.assertRaises(OSError):
                    apply.execute(music, csv_path, Path(folder), commit=True, log=lambda _: None)
            write.assert_not_called()
            self.assertEqual(source.read_bytes(), b"original audio")

    def test_disabled_backup_still_excludes_previous_backups_from_input_scan(self):
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as folder:
            music, source, csv_path = self.fixture(folder)
            apply.backup_file(source, music, apply.default_backup_root(csv_path), row())
            with patch.object(apply, "read_metadata", return_value={"title": "Song", "artist": "Artist"}), \
                    patch.object(apply, "apply_tags") as write, \
                    patch.object(apply, "backup_file") as backup:
                counts = apply.execute(Path(folder), csv_path, Path(folder), commit=True,
                                       backup_enabled=False, log=lambda _: None)
            self.assertEqual(counts, (1, 0))
            self.assertEqual(write.call_count, 1)
            backup.assert_not_called()

    def test_custom_backup_root_keeps_organized_layout(self):
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as folder:
            music, source, csv_path = self.fixture(folder)
            custom = Path(folder) / "custom backup"
            with patch.object(apply, "read_metadata", return_value={"title": "Song", "artist": "Artist"}), \
                    patch.object(apply, "apply_tags"):
                apply.execute(music, csv_path, Path(folder), commit=True, backup=custom, log=lambda _: None)
            self.assertEqual((custom / "백업_(Artist)" / "Album" / "Song.flac").read_bytes(), b"original audio")
            self.assertFalse(apply.default_backup_root(csv_path).exists())

    def test_failed_copy_removes_only_its_incomplete_backup(self):
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as folder:
            music, source, csv_path = self.fixture(folder)
            backup = apply.default_backup_root(csv_path)
            first = apply.backup_file(source, music, backup, row())
            source.write_bytes(b"new content to back up")
            def fail_copy(input_handle, output_handle, **kwargs):
                output_handle.write(b"partial")
                raise OSError("disk full")
            with patch.object(apply.shutil, "copyfileobj", side_effect=fail_copy):
                with self.assertRaises(OSError):
                    apply.backup_file(source, music, backup, row())
            self.assertEqual(first.read_bytes(), b"original audio")
            self.assertEqual(list(first.parent.iterdir()), [first])
            self.assertEqual(source.read_bytes(), b"new content to back up")


class OrganizedCollectionTests(unittest.TestCase):
    class Catalog:
        def search(self, query, filter=None):
            return [{"artist": "Test Artist", "browseId": "artist-1"}]

        def get_artist(self, artist_id):
            return {"albums": {"results": [
                {"title": "Night / Day", "browseId": "album-1"},
                {"title": "Night : Day", "browseId": "album-2"}]}}

        def get_album(self, album_id):
            title = "Night / Day" if album_id == "album-1" else "Night : Day"
            return {"title": title, "year": "2024", "artists": [{"name": "Test Artist"}],
                    "thumbnails": [{"url": "https://example.test/" + album_id}],
                    "tracks": [{"title": "Song " + album_id, "trackNumber": 1}]}

    def collect(self, root, **kwargs):
        response = SimpleNamespace(ok=True, content=b"cover bytes")
        session = SimpleNamespace(get=lambda *args, **options: response)
        requests = SimpleNamespace(Session=lambda: session)
        with patch.object(fetch, "require_dependencies", return_value=(requests, self.Catalog)):
            return fetch.collect("Test Artist", root=root, musicbrainz=False, log=lambda _: None, **kwargs)

    def test_only_artist_csv_and_central_artwork_are_created(self):
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as temporary:
            root = Path(temporary) / "library"
            self.collect(root)
            artist_dir = root / "Test Artist"
            master = artist_dir / "Test Artist.csv"
            rows = load_csv(master)
            self.assertEqual(len(rows), 2)
            cover_root = root / "앨범아트" / "앨범아트_(Test Artist)"
            self.assertEqual(len(list(cover_root.glob("*.jpg"))), 2)
            self.assertEqual(list(artist_dir.iterdir()), [master])
            self.assertFalse(list(root.rglob("*.json")))
            for item in rows:
                cover = apply.resolve_artwork(item["앨범아트 파일"], master, root, {})
                self.assertIsNotNone(cover)
                self.assertEqual(cover.parent, cover_root.resolve())
            # Relative paths must keep working after the complete library is moved.
            moved = Path(temporary) / "moved"
            shutil.copytree(root, moved)
            moved_csv = moved / "Test Artist" / "Test Artist.csv"
            for item in load_csv(moved_csv):
                cover = apply.resolve_artwork(item["앨범아트 파일"], moved_csv, moved, {})
                self.assertTrue(cover.is_relative_to(moved.resolve()))

    def test_rerun_requires_overwrite_and_keeps_simple_layout(self):
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as temporary:
            root = Path(temporary)
            self.collect(root)
            master = root / "Test Artist" / "Test Artist.csv"
            before = master.read_bytes()
            with self.assertRaises(MusicInfoError):
                self.collect(root)
            self.assertEqual(master.read_bytes(), before)
            self.collect(root, overwrite=True)
            self.assertEqual(list((root / "Test Artist").iterdir()), [master])
            self.assertEqual(len(list((root / "앨범아트" / "앨범아트_(Test Artist)").glob("*.jpg"))), 2)
            self.assertEqual(len(load_csv(master)), 2)

    def test_coverless_collection_only_creates_artist_csv(self):
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as temporary:
            root = Path(temporary)
            self.collect(root, covers=False)
            master = root / "Test Artist" / "Test Artist.csv"
            self.assertTrue(master.is_file())
            self.assertEqual(list(master.parent.iterdir()), [master])
            self.assertTrue(all(not item["앨범아트 파일"] for item in load_csv(master)))
            self.assertFalse((root / "앨범아트").exists())

    def test_explicit_csv_and_cover_roots_remain_supported(self):
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as temporary:
            root = Path(temporary)
            output = root / "custom.csv"
            art = root / "covers"
            self.collect(root / "library", output=output, art_dir=art)
            item = load_csv(output)[0]
            cover = apply.resolve_artwork(item["앨범아트 파일"], output, root, {})
            self.assertEqual(cover.parent, (art / "앨범아트_(Test Artist)").resolve())

    def test_missing_album_path_never_uses_other_albums_cover(self):
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as temporary:
            root = Path(temporary)
            wrong = root / "cover.jpg"
            wrong.write_bytes(b"other album")
            self.assertIsNone(apply.resolve_artwork("missing/cover.jpg", root / "Artist.csv", root,
                                                     {"cover.jpg": wrong}))

    def test_unsafe_windows_names_are_sanitized(self):
        self.assertEqual(clean_filename("CON"), "_CON")
        self.assertNotIn("/", clean_filename("../../A:B"))
        self.assertEqual(clean_filename("..."), "cover")


if __name__ == "__main__":
    unittest.main()
