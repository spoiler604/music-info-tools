from __future__ import annotations

import argparse
import hashlib
import os
import queue
import re
import sys
import threading
import time
from pathlib import Path
from typing import Callable

from music_info_common import (
    MusicInfoError, atomic_csv, clean_filename, default_data_root, normalized, unique_rows,
)


DATA_ROOT = default_data_root()


def require_dependencies():
    try:
        import requests
        from ytmusicapi import YTMusic
    except ImportError as exc:
        raise MusicInfoError(
            "정보 수집에는 requests와 ytmusicapi가 필요합니다.\n"
            "python -m pip install -r requirements.txt 를 실행해 주세요."
        ) from exc
    return requests, YTMusic


class MusicBrainz:
    def __init__(self, contact: str = "", timeout: int = 30):
        requests, _ = require_dependencies()
        suffix = contact.strip() or "local-desktop-tool"
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": f"MusicInfoTools/2.0 ({suffix})"})
        self.timeout = timeout
        self.last_request = 0.0

    def get(self, path: str, params: dict):
        wait = 1.1 - (time.monotonic() - self.last_request)
        if wait > 0:
            time.sleep(wait)
        response = self.session.get("https://musicbrainz.org/ws/2/" + path,
                                    params={**params, "fmt": "json"}, timeout=self.timeout)
        self.last_request = time.monotonic()
        response.raise_for_status()
        return response.json()

    def release_id(self, title: str, artists: list[str]) -> str | None:
        quoted = title.replace('"', " ")
        artist_query = " OR ".join(f'artist:"{name.replace(chr(34), " ")}"' for name in artists if name)
        query = f'release:"{quoted}"' + (f" AND ({artist_query})" if artist_query else "")
        releases = self.get("release", {"query": query, "limit": 10}).get("releases", [])
        target = normalized(title)
        exact = [item for item in releases if normalized(item.get("title")) == target]
        candidates = exact or [item for item in releases
                               if normalized(item.get("title")) in target or target in normalized(item.get("title"))]
        return candidates[0].get("id") if candidates else None

    def enrich(self, title: str, artists: list[str]) -> tuple[dict[str, str], str]:
        release_id = self.release_id(title, artists)
        if not release_id:
            return {}, ""
        inc = ("recordings+recording-level-rels+work-rels+work-level-rels+"
               "artist-rels+release-groups+genres")
        data = self.get(f"release/{release_id}", {"inc": inc})
        composers: dict[str, str] = {}
        for medium in data.get("media", []):
            for track in medium.get("tracks", []):
                names: list[str] = []
                recording = track.get("recording") or {}
                for relation in recording.get("relations", []):
                    work = relation.get("work") or {}
                    for work_relation in work.get("relations", []):
                        if work_relation.get("type") not in ("composer", "writer", "lyricist"):
                            continue
                        name = (work_relation.get("artist") or {}).get("name")
                        if name and name not in names:
                            names.append(name)
                composers[normalized(track.get("title"))] = "; ".join(names)
        genres = list((data.get("release-group") or {}).get("genres") or []) + list(data.get("genres") or [])
        genre = max(genres, key=lambda item: item.get("count", 0)).get("name", "").title() if genres else ""
        return composers, genre


def artist_result(yt, query: str) -> dict:
    if not normalized(query):
        raise MusicInfoError("가져올 아티스트 이름을 입력해 주세요.")
    results = yt.search(query, filter="artists") or []
    if not results:
        raise MusicInfoError(f"아티스트를 찾지 못했습니다: {query}")
    exact = [item for item in results if normalized(item.get("artist")) == normalized(query)]
    candidates = exact or results
    if len(candidates) != 1 and not exact:
        names = ", ".join(dict.fromkeys(item.get("artist", "") for item in candidates[:8]))
        raise MusicInfoError(f"아티스트를 하나로 확정하지 못했습니다: {query}\n검색 후보: {names}\n"
                             "YouTube Music에 표시되는 정확한 아티스트 이름을 입력해 주세요.")
    chosen = candidates[0]
    if not chosen.get("browseId"):
        raise MusicInfoError("검색 결과에 아티스트 ID가 없습니다.")
    return chosen


def releases_for(yt, artist: dict) -> list[dict]:
    items: list[dict] = []
    for name in ("albums", "singles"):
        section = artist.get(name) or {}
        current = section.get("results") or []
        if section.get("browseId") and section.get("params"):
            try:
                current = yt.get_artist_albums(section["browseId"], section["params"]) or current
            except Exception:
                pass
        items.extend(current)
    seen: set[str] = set()
    result = []
    for item in items:
        key = item.get("browseId")
        if key and key not in seen:
            seen.add(key)
            result.append(item)
    return result


def save_cover(session, album: dict, art_dir: Path, album_artist: str = "", *, filename: str = "") -> str:
    thumbnails = album.get("thumbnails") or []
    if not thumbnails:
        return ""
    art_dir.mkdir(parents=True, exist_ok=True)
    # Artist is part of the filename so different artists with an identically named
    # release cannot silently share a cover in a combined library.
    filename = filename or clean_filename(
        f"{album.get('year', '')} - {album.get('title', '')} - {album_artist}"
    ) + ".jpg"
    path = art_dir / filename
    if path.is_file() and path.stat().st_size > 0:
        return filename
    original = thumbnails[-1].get("url", "")
    large = original.rsplit("=", 1)[0] + "=w1200-h1200-l100-rj" if "googleusercontent.com" in original else original
    for url in dict.fromkeys((large, original)):
        if not url:
            continue
        try:
            response = session.get(url, timeout=30)
            if response.ok and response.content:
                path.write_bytes(response.content)
                return filename
        except Exception:
            continue
    return ""


def collection_paths(artist_name: str, root: Path | None = None,
                     output: Path | None = None) -> tuple[Path, Path]:
    name = clean_filename(artist_name, "아티스트")
    folder = (Path(root) if root is not None else DATA_ROOT / "음악정보") / name
    folder = folder.resolve()
    return folder, Path(output).resolve() if output is not None else folder / (name + ".csv")


def cover_filename(title: str, release_id: str, used: dict[str, str]) -> str:
    name = clean_filename(title, "앨범")
    digest = hashlib.sha256(release_id.encode("utf-8")).hexdigest()[:10]
    for candidate in (name, f"{name} [{digest}]"):
        owner = used.get(candidate.casefold())
        if owner is None or owner == release_id:
            used[candidate.casefold()] = release_id
            return candidate + ".jpg"
    raise MusicInfoError(f"앨범아트 파일 이름이 충돌합니다: {title}")


def relative_cover(path: Path, csv_path: Path) -> str:
    try:
        return Path(os.path.relpath(path, csv_path.parent)).as_posix()
    except ValueError:  # An explicitly selected cover directory may be on a different drive.
        return str(path.resolve())


def collect(artist_name: str, output: Path | None = None, art_dir: Path | None = None, *,
            root: Path | None = None, contact: str = "",
            musicbrainz: bool = True, covers: bool = True,
            default_genre: str = "",
            overwrite: bool = False,
            log: Callable[[str], None] = print) -> list[dict[str, object]]:
    requests, YTMusic = require_dependencies()
    yt = YTMusic()
    found = artist_result(yt, artist_name)
    canonical_name = found.get("artist") or artist_name
    artist_dir, output = collection_paths(canonical_name, root, output)
    if output.exists() and not overwrite:
        raise MusicInfoError(f"기존 CSV를 보호하기 위해 저장을 중단했습니다: {output}\n"
                             "다른 저장 폴더를 사용하거나 --overwrite를 지정해 주세요.")
    log(f"아티스트: {canonical_name}\n저장 폴더: {artist_dir}\n전체 곡 목록: {output}")
    artist = yt.get_artist(found["browseId"])
    releases = releases_for(yt, artist)
    if not releases:
        raise MusicInfoError("앨범과 싱글 목록을 찾지 못했습니다.")
    log(f"릴리스 {len(releases)}개를 찾았습니다.")
    mb = MusicBrainz(contact) if musicbrainz else None
    session = requests.Session()
    rows: list[dict[str, object]] = []
    used_covers: dict[str, str] = {}
    saved_albums = 0
    for number, release in enumerate(releases, 1):
        album_id = release.get("browseId")
        if not album_id:
            continue
        try:
            album = yt.get_album(album_id)
            title = album.get("title") or release.get("title") or ""
            album_people = [item.get("name", "") for item in (album.get("artists") or []) if item.get("name")]
            album_artist = ", ".join(album_people) or found.get("artist") or artist_name
            search_people = list(dict.fromkeys([artist_name, found.get("artist", ""), *album_people]))
            composers, genre = ({}, "")
            if mb:
                try:
                    composers, genre = mb.enrich(title, search_people)
                except Exception as exc:
                    log(f"  MusicBrainz 보강 실패: {title} ({exc})")
            genre = genre or default_genre.strip()
            image_name = cover_filename(title, album_id, used_covers)
            cover_root = Path(art_dir).resolve() if art_dir is not None else artist_dir.parent / "앨범아트"
            cover_dir = cover_root / f"앨범아트_({artist_dir.name})"
            cover = save_cover(session, album, cover_dir, album_artist,
                               filename=image_name) if covers else ""
            tracks = album.get("tracks") or []
            album_rows: list[dict[str, object]] = []
            for index, track in enumerate(tracks, 1):
                track_people = [item.get("name", "") for item in (track.get("artists") or []) if item.get("name")]
                track_name = track.get("title") or ""
                track_number = track.get("trackNumber") or index
                album_rows.append({
                    "트랙 이름": track_name,
                    "앨범 이름": title,
                    "아티스트 이름": ", ".join(track_people) or album_artist,
                    "앨범 아티스트 이름": album_artist,
                    "작곡가": composers.get(normalized(track_name), ""),
                    "발매연도": album.get("year") or "",
                    "트랙 번호": track_number,
                    "장르": genre,
                    "앨범아트 파일": relative_cover(cover_dir / cover, output) if cover else "",
                })
            album_rows = unique_rows(album_rows)
            if not album_rows:
                log(f"[{number}/{len(releases)}] 곡 정보 없음: {title}")
                continue
            rows.extend(album_rows)
            saved_albums += 1
            log(f"[{number}/{len(releases)}] {title}: {len(album_rows)}곡")
        except Exception as exc:
            log(f"[{number}/{len(releases)}] 건너뜀: {release.get('title', album_id)} ({exc})")
    rows = unique_rows(rows)
    if not rows:
        raise MusicInfoError("저장할 곡 정보를 수집하지 못했습니다.")
    atomic_csv(output, rows)
    log(f"완료: {saved_albums}개 앨범, {len(rows)}곡 → {output}")
    return rows


def parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description="YouTube Music과 MusicBrainz에서 음악 정보를 다시 수집합니다.")
    ap.add_argument("--artist", required=False, default="")
    ap.add_argument("--root", type=Path, default=DATA_ROOT / "음악정보", help="아티스트 폴더를 만들 저장 위치")
    ap.add_argument("--output", type=Path, default=None, help="전체 CSV 경로 직접 지정 (기본: 아티스트명/아티스트명.csv)")
    ap.add_argument("--art", type=Path, default=None, help="큰 앨범아트 폴더 지정 (그 안에 아티스트별 폴더 생성)")
    ap.add_argument("--contact", default="", help="MusicBrainz User-Agent에 넣을 연락처 이메일")
    ap.add_argument("--default-genre", default="", help="장르를 찾지 못했을 때 사용할 값")
    ap.add_argument("--no-musicbrainz", action="store_true")
    ap.add_argument("--no-covers", action="store_true")
    ap.add_argument("--overwrite", action="store_true", help="기존 출력 CSV 교체")
    ap.add_argument("--gui", action="store_true")
    return ap


def run_gui():
    import tkinter as tk
    from tkinter import filedialog, messagebox, scrolledtext, ttk

    root = tk.Tk()
    root.title("음악 정보 다시 가져오기")
    root.geometry("820x710")
    frame = ttk.Frame(root, padding=18)
    frame.pack(fill="both", expand=True)
    frame.columnconfigure(1, weight=1)
    artist = tk.StringVar()
    save_root = tk.StringVar(value=str((DATA_ROOT / "음악정보").resolve()))
    preview_path = tk.StringVar()
    contact = tk.StringVar()
    default_genre = tk.StringVar()
    use_mb = tk.BooleanVar(value=True)
    use_covers = tk.BooleanVar(value=True)
    overwrite = tk.BooleanVar(value=False)
    events: queue.Queue = queue.Queue()

    def row(index, label, variable, choose=None):
        ttk.Label(frame, text=label).grid(row=index, column=0, sticky="w", pady=6, padx=(0, 10))
        ttk.Entry(frame, textvariable=variable).grid(row=index, column=1, sticky="ew")
        if choose:
            ttk.Button(frame, text="선택", command=choose).grid(row=index, column=2, padx=(8, 0))

    row(0, "아티스트", artist)
    row(1, "전체 저장 위치", save_root, lambda: save_root.set(filedialog.askdirectory() or save_root.get()))
    ttk.Label(frame, textvariable=preview_path, wraplength=740).grid(
        row=2, column=0, columnspan=3, sticky="w", pady=6)
    def update_preview(*_):
        name = clean_filename(artist.get().strip(), "아티스트명")
        base = Path(save_root.get())
        preview_path.set(f"CSV: {base / name / (name + '.csv')}\n"
                         f"커버: {base / '앨범아트' / ('앨범아트_(' + name + ')')}")
    artist.trace_add("write", update_preview)
    save_root.trace_add("write", update_preview)
    update_preview()
    row(3, "연락처 이메일", contact)
    row(4, "기본 장르 (선택)", default_genre)
    ttk.Checkbutton(frame, text="MusicBrainz에서 작곡가·장르 보강", variable=use_mb).grid(
        row=5, column=0, columnspan=2, sticky="w", pady=5)
    ttk.Checkbutton(frame, text="앨범아트 내려받기", variable=use_covers).grid(
        row=6, column=0, columnspan=2, sticky="w", pady=5)
    ttk.Checkbutton(frame, text="기존 수집 결과 갱신 (같은 아티스트의 CSV 교체)", variable=overwrite).grid(
        row=7, column=0, columnspan=2, sticky="w", pady=5)
    status = tk.StringVar(value="아티스트 폴더에 전체 CSV 하나를 저장합니다. 커버는 앨범아트/앨범아트_(아티스트명)에 모읍니다.")
    ttk.Label(frame, textvariable=status, wraplength=760).grid(row=9, column=0, columnspan=3, sticky="w", pady=8)
    logbox = scrolledtext.ScrolledText(frame, height=20, state="disabled")
    logbox.grid(row=10, column=0, columnspan=3, sticky="nsew")
    frame.rowconfigure(10, weight=1)

    def emit(text):
        events.put(("log", text))

    def start():
        if not artist.get().strip():
            messagebox.showinfo("아티스트 입력", "가져올 아티스트 이름을 입력해 주세요.")
            return
        if not save_root.get().strip():
            messagebox.showinfo("저장 위치", "전체 저장 위치를 선택해 주세요.")
            return
        settings = {"artist_name": artist.get().strip(), "root": Path(save_root.get()),
                    "contact": contact.get(), "musicbrainz": use_mb.get(),
                    "covers": use_covers.get(), "default_genre": default_genre.get(),
                    "overwrite": overwrite.get(), "log": emit}
        button.configure(state="disabled")
        status.set("수집 중입니다. MusicBrainz를 사용하면 릴리스마다 약간의 대기 시간이 있습니다.")
        def work():
            try:
                collect(**settings)
                events.put(("done", "정보 수집을 완료했습니다."))
            except Exception as exc:
                events.put(("error", str(exc)))
        threading.Thread(target=work, daemon=True).start()

    button = ttk.Button(frame, text="정보 가져오기", command=start)
    button.grid(row=8, column=0, columnspan=3, sticky="ew", pady=10)

    def pump():
        try:
            while True:
                kind, value = events.get_nowait()
                if kind == "log":
                    logbox.configure(state="normal")
                    logbox.insert("end", value + "\n")
                    logbox.see("end")
                    logbox.configure(state="disabled")
                else:
                    button.configure(state="normal")
                    status.set(value)
                    (messagebox.showinfo if kind == "done" else messagebox.showerror)("작업 결과", value)
        except queue.Empty:
            pass
        root.after(100, pump)

    root.after(100, pump)
    root.mainloop()


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
        collect(args.artist, args.output, args.art, root=args.root, contact=args.contact,
                musicbrainz=not args.no_musicbrainz, covers=not args.no_covers,
                default_genre=args.default_genre, overwrite=args.overwrite)
        return 0
    except (MusicInfoError, OSError, ValueError) as exc:
        print("오류:", exc)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
