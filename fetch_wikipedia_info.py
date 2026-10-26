"""Wikipedia-first collector; MusicBrainz supplies only missing metadata."""
from __future__ import annotations

import argparse
import queue
import re
import sys
import threading
from pathlib import Path
from urllib.parse import urlsplit

from fetch_music_info import MusicBrainz, collection_paths, cover_filename, relative_cover
from music_info_common import COLUMNS, MusicInfoError, atomic_csv, normalized, unique_rows
from wikipedia_api import Wikipedia, WikipediaError


EXTRA_COLUMNS = ("발매일", "수집 출처", "위키백과 URL", "MusicBrainz URL", "앨범아트 출처", "앨범아트 라이선스", "검토 메모")


class MusicBrainzSupplement(MusicBrainz):
    def __init__(self, contact="", timeout=20):
        import requests
        self.session = requests.Session()
        contact = re.sub(r"[\r\n]", "", contact).strip()
        self.session.headers["User-Agent"] = (
            "MusicInfoTools/1.1 (https://github.com/spoiler604/music-info-tools"
            + (f"; {contact}" if contact else "") + ")")
        self.timeout = timeout
        self.last_request = 0.0

    def album(self, title: str, artists: list[str]) -> dict:
        quoted_title = title.replace('"', " ")
        names = " OR ".join('artist:"' + name.replace('"', " ") + '"' for name in artists if name)
        data = self.get("release", {"query": f'release:"{quoted_title}" AND ({names})', "limit": 10})
        wanted = {normalized(name) for name in artists if name}
        matches = []
        for item in data.get("releases", []):
            people = {normalized(person.get("name") or (person.get("artist") or {}).get("name", ""))
                      for person in item.get("artist-credit", []) if isinstance(person, dict)}
            if normalized(item.get("title")) == normalized(title) and wanted.intersection(people):
                matches.append(item)
        if not matches:
            return {}
        chosen = min(matches, key=lambda item: (item.get("date") or "9999", item.get("id", "")))
        release_id = chosen["id"]
        inc = "recordings+recording-level-rels+work-rels+work-level-rels+artist-rels+release-groups+genres"
        release = self.get("release/" + release_id, {"inc": inc})
        tracks = []
        for medium in release.get("media", []):
            for track in medium.get("tracks", []):
                composers = []
                for relation in (track.get("recording") or {}).get("relations", []):
                    for role in (relation.get("work") or {}).get("relations", []):
                        name = (role.get("artist") or {}).get("name", "")
                        if role.get("type") in ("composer", "writer", "lyricist") and name and name not in composers:
                            composers.append(name)
                tracks.append({"title": track.get("title") or (track.get("recording") or {}).get("title", ""),
                               "number": str(track.get("number") or track.get("position") or len(tracks) + 1),
                               "composer": "; ".join(composers)})
        genres = ((release.get("release-group") or {}).get("genres") or []) + (release.get("genres") or [])
        genre = max(genres, key=lambda item: item.get("count", 0)).get("name", "") if genres else ""
        date = (release.get("release-group") or {}).get("first-release-date") or release.get("date") or chosen.get("date", "")
        return {"tracks": tracks, "genre": genre, "date": date,
                "url": "https://musicbrainz.org/release/" + release_id}


def combine(album: dict, supplement: dict, artist: str, default_genre="") -> list[dict]:
    wiki_tracks = album.get("tracks") or []
    tracks = wiki_tracks or supplement.get("tracks") or []
    by_title = {}
    for item in supplement.get("tracks", []):
        by_title.setdefault(normalized(item["title"]), []).append(item)
    date = album.get("date") or supplement.get("date", "")
    rows = []
    for track in tracks:
        matches = by_title.get(normalized(track["title"]), [])
        mb_track = matches[0] if len(matches) == 1 else {}
        composer = track.get("composer") or mb_track.get("composer", "")
        notes = []
        mb_date = supplement.get("date", "")
        if album.get("date") and mb_date and album["date"][:4] != mb_date[:4]:
            notes.append(f"발매연도 차이: 위키백과 {album['date']} / MusicBrainz {mb_date}; 위키백과 유지")
        if not wiki_tracks:
            notes.append("위키백과 트랙 표 없음; MusicBrainz 트랙 목록 사용")
        source = "Wikipedia" if wiki_tracks else "MusicBrainz (Wikipedia 음반 문서 기반)"
        if (not track.get("composer") and composer) or (not album.get("genre") and supplement.get("genre")) or (not album.get("date") and mb_date):
            source += " + MusicBrainz 보강"
        rows.append({"트랙 이름": track["title"], "앨범 이름": album["title"],
                     "아티스트 이름": album.get("artist") or artist,
                     "앨범 아티스트 이름": album.get("artist") or artist,
                     "작곡가": composer, "발매연도": date[:4], "트랙 번호": track.get("number", ""),
                     "장르": album.get("genre") or supplement.get("genre") or default_genre,
                     "앨범아트 파일": "", "발매일": date, "수집 출처": source,
                     "위키백과 URL": album["url"], "MusicBrainz URL": supplement.get("url", ""),
                     "앨범아트 출처": "", "앨범아트 라이선스": "", "검토 메모": " | ".join(notes)})
    return rows


def download_cover(client: Wikipedia, info: dict, folder: Path, stem: str) -> str:
    import requests
    if not info.get("url") or urlsplit(info["url"]).hostname != "upload.wikimedia.org":
        return ""
    for suffix in (".jpg", ".png"):
        existing = folder / (stem + suffix)
        if existing.is_file() and existing.stat().st_size:
            return existing.name
    try:
        response = client.session.get(info["url"], timeout=20)
        response.raise_for_status()
        content = response.content
        suffix = ".jpg" if content.startswith(b"\xff\xd8\xff") else ".png" if content.startswith(b"\x89PNG\r\n\x1a\n") else ""
        if not suffix:
            raise WikipediaError("지원하는 JPEG/PNG 앨범아트가 아닙니다.")
        folder.mkdir(parents=True, exist_ok=True)
        target = folder / (stem + suffix)
        with target.open("xb") as handle:
            handle.write(content)
        return target.name
    except FileExistsError:
        return stem + suffix
    except requests.RequestException as exc:
        raise WikipediaError(f"앨범아트 다운로드 실패 ({type(exc).__name__})") from exc


def collect_wikipedia(artist_name: str, *, root: Path | None = None, output: Path | None = None,
                      page="", language="ko", contact="", musicbrainz=True, covers=True,
                      default_genre="", overwrite=False, preview=False, max_pages=100, log=print):
    if not 1 <= max_pages <= 500:
        raise WikipediaError("문서 조회 한도는 1~500 범위여야 합니다.")
    client = Wikipedia(language=language, contact=contact)
    artist = client.artist_page(artist_name, page)
    artist_dir, output = collection_paths(artist["artist"], root, output)
    if output.exists() and not overwrite and not preview:
        raise MusicInfoError(f"이미 CSV가 있습니다: {output}\n기존 수집 결과 갱신 또는 --overwrite를 사용하세요.")
    log(f"아티스트: {artist['artist']}\n위키백과: {artist['url']}\n저장 예정: {output}")
    albums = client.albums(artist, max_pages, log)
    if not albums:
        raise WikipediaError("아티스트의 음반 목록에서 연주자가 일치하는 음반 문서를 찾지 못했습니다.")
    mb = MusicBrainzSupplement(contact) if musicbrainz else None
    rows = []
    used = {}
    cover_dir = artist_dir.parent / "앨범아트" / f"앨범아트_({artist_dir.name})"
    for number, album in enumerate(albums, 1):
        supplement = {}
        if mb:
            try:
                names = list(dict.fromkeys(value for value in [artist["artist"], artist.get("english_artist", ""),
                                            *album.get("artist", "").split("; ")] if value))
                supplement = mb.album(album["title"], names)
            except Exception as exc:
                log(f"MusicBrainz 보강 실패: {album['title']} ({exc}); 나머지는 위키백과 정보로 수집합니다.")
                mb = None
        album_rows = combine(album, supplement, artist["artist"], default_genre)
        if not album_rows:
            log(f"[건너뜀] {album['title']}: 위키백과·MusicBrainz 모두 트랙 목록 없음")
            continue
        if covers and not preview and album.get("cover_file"):
            try:
                info = client.image_info(album["language"], album["cover_file"])
                stem = Path(cover_filename(album["title"], album["url"], used)).stem
                image_name = download_cover(client, info, cover_dir, stem)
                for item in album_rows:
                    item["앨범아트 파일"] = relative_cover(cover_dir / image_name, output) if image_name else ""
                    item["앨범아트 출처"] = info.get("description_url", "")
                    item["앨범아트 라이선스"] = info.get("license", "")
            except (WikipediaError, OSError) as exc:
                log(f"앨범아트 건너뜀: {album['title']} ({exc})")
        rows.extend(album_rows)
        log(f"[{number}/{len(albums)}] {album['title']} · {len(album_rows)}곡 · {album['url']}")
    rows = unique_rows(rows)
    if not rows:
        raise WikipediaError("저장할 트랙 정보를 찾지 못했습니다.")
    if not preview:
        atomic_csv(output, rows, columns=(*COLUMNS, *EXTRA_COLUMNS))
    log(f"{'미리보기' if preview else '저장'} 완료: {len(rows)}곡 → {output}")
    return rows


def parser():
    ap = argparse.ArgumentParser(description="위키백과 음반·트랙 정보를 수집하고 MusicBrainz로 빈 정보를 보강합니다.")
    ap.add_argument("--artist", default="")
    ap.add_argument("--page", default="", help="위키백과 아티스트 문서의 정확한 제목 (동명 아티스트 구분)")
    ap.add_argument("--language", choices=("ko", "en"), default="ko", help="한국어 검색 실패 시 영어로 검색")
    ap.add_argument("--root", type=Path, default=None)
    ap.add_argument("--output", type=Path)
    ap.add_argument("--contact", default="")
    ap.add_argument("--default-genre", default="")
    ap.add_argument("--no-musicbrainz", action="store_true")
    ap.add_argument("--no-covers", action="store_true")
    ap.add_argument("--overwrite", action="store_true")
    ap.add_argument("--preview", action="store_true", help="API 조회만 수행하며 CSV·커버 파일은 저장하지 않음")
    ap.add_argument("--max-pages", type=int, default=100)
    ap.add_argument("--gui", action="store_true")
    return ap


def run_gui():
    import tkinter as tk
    from tkinter import filedialog, messagebox, scrolledtext, ttk
    from fetch_music_info import DATA_ROOT
    window = tk.Tk()
    window.title("위키백과 음악 정보 가져오기")
    window.geometry("850x720")
    outer = ttk.Frame(window, padding=18)
    outer.pack(fill="both", expand=True)
    outer.columnconfigure(1, weight=1)
    values = {"artist": tk.StringVar(), "page": tk.StringVar(), "language": tk.StringVar(value="ko"),
              "root": tk.StringVar(value=str(DATA_ROOT / "음악정보")), "contact": tk.StringVar(),
              "default_genre": tk.StringVar()}
    controls = []
    for number, (key, label) in enumerate((("artist", "아티스트"), ("page", "문서 제목 (선택)"),
                                         ("root", "전체 저장 위치"), ("contact", "연락처 (선택)"),
                                         ("default_genre", "기본 장르 (선택)"))):
        ttk.Label(outer, text=label).grid(row=number, column=0, sticky="w", padx=(0, 12), pady=5)
        entry = ttk.Entry(outer, textvariable=values[key])
        entry.grid(row=number, column=1, sticky="ew")
        controls.append(entry)
    choose = ttk.Button(outer, text="선택", command=lambda: values["root"].set(filedialog.askdirectory() or values["root"].get()))
    choose.grid(row=2, column=2, padx=8)
    controls.append(choose)
    ttk.Label(outer, text="위키백과 언어").grid(row=5, column=0, sticky="w", pady=5)
    languages = ttk.Combobox(outer, textvariable=values["language"], values=("ko", "en"), state="readonly", width=8)
    languages.grid(row=5, column=1, sticky="w")
    controls.append(languages)
    switches = {"musicbrainz": tk.BooleanVar(value=True), "covers": tk.BooleanVar(value=True),
                "overwrite": tk.BooleanVar(value=False)}
    for number, (key, label) in enumerate((("musicbrainz", "MusicBrainz로 빈 작곡가·장르·발매일·트랙 목록 보강"),
                                         ("covers", "위키백과 앨범아트 저장"),
                                         ("overwrite", "기존 아티스트 CSV 갱신")), 6):
        check = ttk.Checkbutton(outer, text=label, variable=switches[key])
        check.grid(row=number, column=0, columnspan=3, sticky="w", pady=3)
        controls.append(check)
    status = tk.StringVar(value="위키백과 → MusicBrainz 순서로 수집합니다. 먼저 API 미리보기를 실행하세요.")
    ttk.Label(outer, textvariable=status, wraplength=790).grid(row=10, column=0, columnspan=3, sticky="w", pady=10)
    output = scrolledtext.ScrolledText(outer, state="disabled", height=22)
    output.grid(row=11, column=0, columnspan=3, sticky="nsew")
    outer.rowconfigure(11, weight=1)
    events = queue.Queue()
    busy = False
    def start(preview):
        nonlocal busy
        settings = {key: value.get().strip() for key, value in values.items()}
        if not settings["artist"]:
            messagebox.showinfo("아티스트", "아티스트 이름을 입력해 주세요.")
            return
        settings["artist_name"] = settings.pop("artist")
        settings["root"] = Path(settings["root"])
        settings.update({key: value.get() for key, value in switches.items()})
        settings.update(preview=preview, log=lambda text: events.put(("log", text)))
        for control in controls:
            control.configure(state="disabled")
        busy = True
        status.set("위키백과 음반·트랙 문서를 조회하고 있습니다.")
        def work():
            try:
                rows = collect_wikipedia(**settings)
                events.put(("done", f"{'미리보기' if preview else '저장'} 완료: {len(rows)}곡"))
            except Exception as exc:
                events.put(("error", str(exc)))
        threading.Thread(target=work, daemon=True).start()
    buttons = ttk.Frame(outer)
    buttons.grid(row=9, column=0, columnspan=3, sticky="ew", pady=8)
    for label, preview in (("1. API 미리보기", True), ("2. 정보 가져와 저장", False)):
        button = ttk.Button(buttons, text=label, command=lambda value=preview: start(value))
        button.pack(side="left", fill="x", expand=True, padx=4)
        controls.append(button)
    def pump():
        nonlocal busy
        try:
            while True:
                kind, text = events.get_nowait()
                output.configure(state="normal")
                output.insert("end", text + "\n")
                output.see("end")
                output.configure(state="disabled")
                if kind != "log":
                    busy = False
                    for control in controls:
                        control.configure(state="readonly" if control is languages else "normal")
                    status.set(text)
                    if kind == "error":
                        messagebox.showerror("수집 오류", text)
        except queue.Empty:
            pass
        window.after(100, pump)
    def close():
        if busy:
            messagebox.showinfo("작업 중", "현재 수집이 끝난 후 창을 닫아 주세요.")
        else:
            window.destroy()
    window.protocol("WM_DELETE_WINDOW", close)
    window.after(100, pump)
    window.mainloop()


def main(argv=None):
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except AttributeError:
            pass
    args = parser().parse_args(argv)
    if args.gui:
        run_gui()
        return 0
    try:
        collect_wikipedia(args.artist, root=args.root, output=args.output, page=args.page,
                          language=args.language, contact=args.contact, musicbrainz=not args.no_musicbrainz,
                          covers=not args.no_covers, default_genre=args.default_genre,
                          overwrite=args.overwrite, preview=args.preview, max_pages=args.max_pages)
        return 0
    except (MusicInfoError, OSError, ValueError, ImportError) as exc:
        print("오류:", exc)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
