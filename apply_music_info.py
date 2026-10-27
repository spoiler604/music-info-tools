from __future__ import annotations

import argparse
import base64
import mimetypes
import queue
import re
import shutil
import sys
import threading
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from music_info_common import MusicInfoError, clean_filename, default_data_root, load_csv, normalized, split_people
from music_matching import artist_keys, choose_row, row_track_number, strip_track_prefix, track_number


SUPPORTED = {".mp3", ".aac", ".flac", ".m4a", ".mp4", ".opus", ".ogg", ".oga"}
DATA_ROOT = default_data_root()


@dataclass
class PlanItem:
    path: Path
    row: dict[str, str] | None
    status: str
    reason: str = ""


def require_mutagen():
    try:
        import mutagen
    except ImportError as exc:
        raise MusicInfoError(
            "태그 입력에는 mutagen이 필요합니다.\n"
            "python -m pip install -r requirements.txt 를 실행해 주세요."
        ) from exc
    return mutagen


def read_metadata(path: Path) -> dict[str, str]:
    mutagen = require_mutagen()
    try:
        audio = mutagen.File(path, easy=True)
    except Exception:
        audio = None
    tags = audio.tags if audio is not None and audio.tags else {}
    def first(*keys, join=False):
        for key in keys:
            value = tags.get(key)
            if value:
                values = value if isinstance(value, (list, tuple)) else [value]
                values = [str(item).strip() for item in values if str(item).strip()]
                if values:
                    return "; ".join(values) if join else values[0]
        return ""
    result = {"title": first("title", "Title", "WM/Title", "TIT2"),
              "album": first("album", "WM/AlbumTitle", "TALB"),
              "artist": first("artist", "Author", "WM/Author", "TPE1", join=True),
              "albumartist": first("albumartist", "album artist", "WM/AlbumArtist", "TPE2", join=True),
              "tracknumber": first("tracknumber", "track", "WM/TrackNumber", "TRCK")}
    stem = strip_track_prefix(path.stem)
    if " - " in stem:
        guessed_artist, guessed_title = (part.strip() for part in stem.split(" - ", 1))
        result["artist"] = result["artist"] or guessed_artist
        result["title"] = result["title"] or guessed_title
    return result


def infer_translated_album(items, rows: list[dict[str, str]]) -> str:
    """Identify a translated local album only from a complete, unique track-number signature."""
    numbered = [(track_number(path, metadata), metadata) for path, metadata, _, _ in items]
    numbers = [number for number, _ in numbered if number is not None]
    if len(numbers) < 2 or len(numbers) != len(items) or len(set(numbers)) != len(numbers):
        return ""
    tagged_people = set()
    local_albums = set()
    for _, metadata in numbered:
        tagged_people |= artist_keys(metadata.get("artist", "")) | artist_keys(metadata.get("albumartist", ""))
        if normalized(metadata.get("album", "")):
            local_albums.add(normalized(metadata["album"]))
    if len(local_albums) > 1:
        return ""
    albums: dict[str, list[dict[str, str]]] = {}
    for row in rows:
        albums.setdefault(row["앨범 이름"], []).append(row)
    candidates = []
    wanted = set(numbers)
    for album_name, album_rows in albums.items():
        csv_numbers = [row_track_number(row) for row in album_rows]
        if any(number is None for number in csv_numbers) or len(set(csv_numbers)) != len(csv_numbers):
            continue
        if set(csv_numbers) != wanted:
            continue
        people = set()
        for row in album_rows:
            people |= artist_keys(row.get("아티스트 이름", "")) | artist_keys(row.get("앨범 아티스트 이름", ""))
        if tagged_people and people and not tagged_people.intersection(people):
            continue
        candidates.append(album_name)
    return candidates[0] if len(candidates) == 1 else ""


def make_plan(music: Path, rows: list[dict[str, str]],
              exclude: Path | list[Path] | None = None) -> list[PlanItem]:
    music = Path(music)
    if not music.is_dir():
        raise MusicInfoError(f"음원 폴더를 찾지 못했습니다: {music}")
    index: dict[str, list[dict[str, str]]] = {}
    for row in rows:
        index.setdefault(normalized(row["트랙 이름"]), []).append(row)
    excluded = [Path(item).resolve() for item in (exclude if isinstance(exclude, list) else [exclude]) if item]
    files = sorted(path for path in music.rglob("*")
                   if path.is_file() and path.suffix.casefold() in SUPPORTED
                   and not any(path.resolve().is_relative_to(folder) for folder in excluded))
    if not files:
        raise MusicInfoError("지원하는 음원 파일이 없습니다.")
    preliminary = []
    votes: dict[Path, Counter] = {}
    for path in files:
        metadata = read_metadata(path)
        row, reason = choose_row(index, path, metadata)
        preliminary.append((path, metadata, row, reason))
        if row and not reason:
            votes.setdefault(path.parent, Counter())[row["앨범 이름"]] += 1
    grouped = {}
    for item in preliminary:
        grouped.setdefault(item[0].parent, []).append(item)
    inferred = {folder: infer_translated_album(items, rows) for folder, items in grouped.items()}
    plan = []
    for path, metadata, row, reason in preliminary:
        if row is None:
            album_votes = votes.get(path.parent, Counter()).most_common(2)
            vote = album_votes[0][0] if album_votes and (len(album_votes) == 1 or album_votes[0][1] > album_votes[1][1]) else ""
            vote = vote or inferred.get(path.parent, "")
            row, reason = choose_row(index, path, metadata, vote)
        plan.append(PlanItem(path, row, "ready" if row else "skip", reason))
    return plan


def art_index(*folders: Path) -> dict[str, Path]:
    result: dict[str, Path] = {}
    ambiguous: set[str] = set()
    for folder in folders:
        if folder and folder.is_dir():
            for path in folder.rglob("*"):
                if path.suffix.casefold() in (".jpg", ".jpeg", ".png"):
                    key = path.name.casefold()
                    if key in result and result[key].resolve() != path.resolve():
                        ambiguous.add(key)
                    result.setdefault(key, path)
    for key in ambiguous:
        result.pop(key, None)
    return result


def resolve_artwork(name: str, csv_path: Path, art_folder: Path,
                    index: dict[str, Path]) -> Path | None:
    if not name:
        return None
    reference = Path(name)
    if reference.suffix.casefold() not in (".jpg", ".jpeg", ".png"):
        return None
    candidates = [reference] if reference.is_absolute() else [csv_path.parent / reference, art_folder / reference]
    for path in candidates:
        if path.is_file():
            return path.resolve()
    # A missing path for a particular album must not pick another album's same-named image.
    if reference.is_absolute() or reference.parent != Path("."):
        return None
    return index.get(reference.name.casefold())


def default_backup_root(csv_path: Path) -> Path:
    csv_path = Path(csv_path).resolve()
    if normalized(csv_path.stem) == normalized(csv_path.parent.name):
        library = csv_path.parent.parent
    elif csv_path.parent.name == "음악정보":
        library = csv_path.parent
    else:
        library = DATA_ROOT / "음악정보"
    return library / "백업"


def backup_target(path: Path, row: dict[str, str], backup: Path) -> Path:
    artist = clean_filename(row.get("앨범 아티스트 이름") or row.get("아티스트 이름"), "아티스트 미상")
    album = clean_filename(row.get("앨범 이름"), "앨범 미상")
    return Path(backup) / f"백업_({artist})" / album / path.name


def same_content(first: Path, second: Path) -> bool:
    if first.stat().st_size != second.stat().st_size:
        return False
    with first.open("rb") as left, second.open("rb") as right:
        while True:
            block = left.read(1024 * 1024)
            if block != right.read(1024 * 1024):
                return False
            if not block:
                return True


def backup_file(path: Path, music: Path, backup: Path, row: dict[str, str]) -> Path:
    path = Path(path).resolve()
    if not path.is_relative_to(Path(music).resolve()):
        raise MusicInfoError(f"음원 폴더 밖의 파일은 백업할 수 없습니다: {path}")
    base = backup_target(path, row, Path(backup).resolve())
    base.parent.mkdir(parents=True, exist_ok=True)
    number = 1
    while True:
        target = base if number == 1 else base.with_name(f"{base.stem} ({number}){base.suffix}")
        if target.exists():
            if same_content(path, target):
                return target
            number += 1
            continue
        try:
            handle = target.open("xb")
        except FileExistsError:
            continue
        try:
            with handle, path.open("rb") as source:
                shutil.copyfileobj(source, handle, length=1024 * 1024)
            shutil.copystat(path, target)
        except BaseException:
            target.unlink(missing_ok=True)
            raise
        return target


def apply_tags(path: Path, row: dict[str, str], artwork: Path | None) -> None:
    require_mutagen()
    from mutagen.flac import FLAC, Picture
    from mutagen.id3 import APIC, ID3, ID3NoHeaderError, TALB, TCOM, TCON, TIT2, TPE1, TPE2, TRCK, TDRC
    from mutagen.mp4 import MP4, MP4Cover
    from mutagen.oggopus import OggOpus
    from mutagen.oggvorbis import OggVorbis

    ext = path.suffix.casefold()
    artists = "; ".join(split_people(row["아티스트 이름"]))
    album_artists = "; ".join(split_people(row["앨범 아티스트 이름"]))
    mime = mimetypes.guess_type(artwork.name)[0] if artwork else None
    art_bytes = artwork.read_bytes() if artwork else None

    if ext in (".mp3", ".aac"):
        try:
            tags = ID3(path)
        except ID3NoHeaderError:
            tags = ID3()
        frames = (("TIT2", TIT2, row["트랙 이름"]), ("TALB", TALB, row["앨범 이름"]),
                  ("TPE1", TPE1, artists), ("TPE2", TPE2, album_artists),
                  ("TCOM", TCOM, row["작곡가"]), ("TDRC", TDRC, row["발매연도"]),
                  ("TCON", TCON, row["장르"]), ("TRCK", TRCK, row["트랙 번호"]))
        for key, frame, value in frames:
            if value:
                tags.setall(key, [frame(encoding=3, text=value)])
        if art_bytes:
            tags.delall("APIC")
            tags.add(APIC(encoding=3, mime=mime or "image/jpeg", type=3, desc="Cover", data=art_bytes))
        tags.save(path, v2_version=3)
        return

    if ext == ".flac":
        audio = FLAC(path)
    elif ext == ".opus":
        audio = OggOpus(path)
    elif ext in (".ogg", ".oga"):
        audio = OggVorbis(path)
    elif ext in (".m4a", ".mp4"):
        audio = MP4(path)
        mapping = {"\xa9nam": row["트랙 이름"], "\xa9alb": row["앨범 이름"],
                   "\xa9ART": artists, "aART": album_artists, "\xa9wrt": row["작곡가"],
                   "\xa9day": row["발매연도"], "\xa9gen": row["장르"]}
        for key, value in mapping.items():
            if value:
                audio[key] = [value]
        if row["트랙 번호"]:
            match = re.match(r"\d+", row["트랙 번호"])
            if match:
                audio["trkn"] = [(int(match.group()), 0)]
        if art_bytes:
            image_format = MP4Cover.FORMAT_PNG if mime == "image/png" else MP4Cover.FORMAT_JPEG
            audio["covr"] = [MP4Cover(art_bytes, imageformat=image_format)]
        audio.save()
        return
    else:
        raise MusicInfoError(f"지원하지 않는 형식입니다: {ext}")

    mapping = {"title": row["트랙 이름"], "album": row["앨범 이름"], "artist": artists,
               "albumartist": album_artists, "composer": row["작곡가"], "date": row["발매연도"],
               "genre": row["장르"], "tracknumber": row["트랙 번호"]}
    for key, value in mapping.items():
        if value:
            audio[key] = value
    if art_bytes:
        picture = Picture()
        picture.type = 3
        picture.mime = mime or "image/jpeg"
        picture.desc = "Cover"
        picture.data = art_bytes
        if ext == ".flac":
            audio.clear_pictures()
            audio.add_picture(picture)
        else:
            audio["metadata_block_picture"] = [base64.b64encode(picture.write()).decode("ascii")]
    audio.save()


def execute(music: Path, csv_path: Path, art_folder: Path, *, commit: bool = False,
            backup: Path | None = None, backup_enabled: bool = True,
            log: Callable[[str], None] = print) -> tuple[int, int]:
    music = Path(music).resolve()
    rows = load_csv(csv_path)
    automatic_backup = default_backup_root(csv_path).resolve()
    backup_root = Path(backup).resolve() if backup is not None else automatic_backup
    if backup_enabled and (music == backup_root or music.is_relative_to(backup_root)):
        raise MusicInfoError("백업 위치는 음원 폴더와 다른 폴더 또는 음원 폴더의 전용 하위 폴더로 선택해 주세요.")
    plan = make_plan(music, rows, [backup_root, automatic_backup, DATA_ROOT / "태그입력_백업"])
    arts = art_index(Path(art_folder), Path(csv_path).resolve().parent / "앨범아트", music / "앨범아트")
    if backup_enabled:
        log(f"백업 위치: {backup_root} / 백업_(아티스트명) / 앨범명")
    success = skipped = 0
    for item in plan:
        if item.row is None:
            skipped += 1
            log(f"[건너뜀] {item.path.name}: {item.reason}")
            continue
        art_name = item.row["앨범아트 파일"]
        artwork = resolve_artwork(art_name, Path(csv_path).resolve(), Path(art_folder).resolve(), arts)
        suffix = "" if artwork or not art_name else f" · 앨범아트 없음: {art_name}"
        if item.reason:
            suffix += f" · {item.reason}"
        log(f"[{'적용' if commit else '미리보기'}] {item.path.name} → "
            f"{item.row['아티스트 이름']} - {item.row['트랙 이름']} [{item.row['앨범 이름']}]{suffix}")
        if commit:
            if backup_enabled:
                target = backup_file(item.path, music, backup_root, item.row)
                log(f"[백업] {item.path.name} → {target}")
            apply_tags(item.path, item.row, artwork)
        elif backup_enabled:
            log(f"[백업 예정] {backup_target(item.path, item.row, backup_root)}")
        success += 1
    log(f"완료: {success}개 {'적용' if commit else '매칭'}, {skipped}개 건너뜀")
    return success, skipped


def parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description="CSV 정보를 음원 태그와 앨범아트에 입력합니다.")
    default_csv = next((path for path in (
        DATA_ROOT / "music_info_new.csv", DATA_ROOT / "music_info.csv", DATA_ROOT / "mfdoom_songs.csv"
    ) if path.is_file()), DATA_ROOT / "music_info.csv")
    default_art = DATA_ROOT / ("앨범아트_new" if (DATA_ROOT / "앨범아트_new").is_dir() else "앨범아트")
    ap.add_argument("--music", type=Path, default=DATA_ROOT)
    ap.add_argument("--csv", type=Path, default=default_csv)
    ap.add_argument("--art", type=Path, default=default_art)
    ap.add_argument("--apply", action="store_true", help="실제로 저장 (지정하지 않으면 미리보기)")
    backup_options = ap.add_mutually_exclusive_group()
    backup_options.add_argument("--backup", type=Path, help="큰 백업 폴더 지정 (기본: 음악정보/백업)")
    backup_options.add_argument("--no-backup", action="store_true", help="적용 전 백업 끄기")
    ap.add_argument("--gui", action="store_true")
    return ap


def run_gui():
    import tkinter as tk
    from tkinter import filedialog, messagebox, scrolledtext, ttk

    root = tk.Tk()
    root.title("음악 정보 입력")
    root.geometry("850x650")
    frame = ttk.Frame(root, padding=18)
    frame.pack(fill="both", expand=True)
    frame.columnconfigure(1, weight=1)
    base = DATA_ROOT
    default_csv = next((path for path in (
        base / "music_info_new.csv", base / "music_info.csv", base / "mfdoom_songs.csv"
    ) if path.is_file()), base / "music_info.csv")
    default_art = base / ("앨범아트_new" if (base / "앨범아트_new").is_dir() else "앨범아트")
    music = tk.StringVar(value=str(base.resolve()))
    csv_path = tk.StringVar(value=str(default_csv.resolve()))
    art = tk.StringVar(value=str(default_art.resolve()))
    backup_enabled = tk.BooleanVar(value=True)
    backup = tk.StringVar(value=str(default_backup_root(Path(csv_path.get()))))
    last_automatic_backup = backup.get()
    def sync_backup_location(*_):
        nonlocal last_automatic_backup
        updated = str(default_backup_root(Path(csv_path.get())))
        if backup.get() == last_automatic_backup:
            backup.set(updated)
        last_automatic_backup = updated
    csv_path.trace_add("write", sync_backup_location)
    events: queue.Queue = queue.Queue()

    def row(index, label, variable, command):
        ttk.Label(frame, text=label).grid(row=index, column=0, sticky="w", padx=(0, 10), pady=6)
        ttk.Entry(frame, textvariable=variable).grid(row=index, column=1, sticky="ew")
        ttk.Button(frame, text="선택", command=command).grid(row=index, column=2, padx=(8, 0))

    row(0, "음원 폴더", music, lambda: music.set(filedialog.askdirectory() or music.get()))
    def choose_csv():
        selected = filedialog.askopenfilename(filetypes=[("CSV", "*.csv")])
        if selected:
            csv_path.set(selected)
            art.set(str(Path(selected).parent))
    row(1, "정보 CSV", csv_path, choose_csv)
    row(2, "앨범아트 폴더", art, lambda: art.set(filedialog.askdirectory() or art.get()))
    ttk.Checkbutton(frame, text="아티스트·앨범별 백업", variable=backup_enabled).grid(row=3, column=0, sticky="w")
    ttk.Entry(frame, textvariable=backup).grid(row=3, column=1, sticky="ew")
    ttk.Button(frame, text="선택", command=lambda: backup.set(filedialog.askdirectory() or backup.get())).grid(
        row=3, column=2, padx=(8, 0))
    status = tk.StringVar(value="미리보기에서 곡 매칭과 백업 예정 경로를 확인하세요. 백업은 음악정보/백업 아래에 정리합니다.")
    ttk.Label(frame, textvariable=status, wraplength=780).grid(row=5, column=0, columnspan=3, sticky="w", pady=8)
    logbox = scrolledtext.ScrolledText(frame, height=24, state="disabled")
    logbox.grid(row=6, column=0, columnspan=3, sticky="nsew")
    frame.rowconfigure(6, weight=1)

    def emit(text):
        events.put(("log", text))

    def run(commit):
        if commit and not messagebox.askyesno("태그 적용 확인", "미리보기를 확인했나요? 음원 파일의 태그를 실제로 변경합니다."):
            return
        settings = {"music": Path(music.get()), "csv_path": Path(csv_path.get()),
                    "art_folder": Path(art.get()), "commit": commit,
                    "backup": Path(backup.get()) if backup.get().strip() else None,
                    "backup_enabled": backup_enabled.get(), "log": emit}
        preview.configure(state="disabled")
        apply_button.configure(state="disabled")
        logbox.configure(state="normal")
        logbox.delete("1.0", "end")
        logbox.configure(state="disabled")
        def work():
            try:
                execute(**settings)
                events.put(("done", "태그 입력 완료" if commit else "미리보기 완료"))
            except Exception as exc:
                events.put(("error", str(exc)))
        threading.Thread(target=work, daemon=True).start()

    buttons = ttk.Frame(frame)
    buttons.grid(row=4, column=0, columnspan=3, sticky="ew", pady=10)
    preview = ttk.Button(buttons, text="1. 매칭 미리보기", command=lambda: run(False))
    preview.pack(side="left", expand=True, fill="x", padx=(0, 5))
    apply_button = ttk.Button(buttons, text="2. 태그 실제 적용", command=lambda: run(True))
    apply_button.pack(side="left", expand=True, fill="x", padx=(5, 0))

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
                    preview.configure(state="normal")
                    apply_button.configure(state="normal")
                    status.set(value)
                    if kind == "error":
                        messagebox.showerror("작업 오류", value)
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
        execute(args.music, args.csv, args.art, commit=args.apply, backup=args.backup,
                backup_enabled=not args.no_backup)
        return 0
    except (MusicInfoError, OSError, ValueError) as exc:
        print("오류:", exc)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
