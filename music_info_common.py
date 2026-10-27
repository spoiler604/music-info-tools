from __future__ import annotations

import csv
import os
import re
import tempfile
import unicodedata
from pathlib import Path


COLUMNS = (
    "트랙 이름",
    "앨범 이름",
    "아티스트 이름",
    "앨범 아티스트 이름",
    "작곡가",
    "발매연도",
    "트랙 번호",
    "장르",
    "앨범아트 파일",
)

OPTIONAL_MATCH_COLUMNS = (
    "대체 트랙 이름",
    "대체 앨범 이름",
)


class MusicInfoError(Exception):
    pass


def default_data_root() -> Path:
    """Use the parent music folder when this tool lives in its own subfolder."""
    tool = Path(__file__).resolve().parent
    if tool.name == "음악정보도구":
        return tool.parent
    return tool


def normalized(value: object) -> str:
    text = unicodedata.normalize("NFKD", str(value or "")).casefold()
    return "".join(ch for ch in text if ch.isalnum() and not unicodedata.combining(ch))


def clean_filename(value: object, fallback: str = "cover") -> str:
    text = re.sub(r'[\x00-\x1f\\/:*?"<>|]', "", str(value or "")).strip(" .")
    text = text[:120].rstrip(" .") or fallback
    if re.fullmatch(r"(?:CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\..*)?", text, re.I):
        text = "_" + text
    return text


def split_people(value: object) -> list[str]:
    return [part.strip() for part in re.split(r"\s*[;,]\s*", str(value or "")) if part.strip()]


def load_csv(path: Path) -> list[dict[str, str]]:
    try:
        with Path(path).open(encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            missing = [column for column in COLUMNS if column not in (reader.fieldnames or [])]
            if missing:
                raise MusicInfoError("CSV 필수 열이 없습니다: " + ", ".join(missing))
            optional = [column for column in OPTIONAL_MATCH_COLUMNS if column in (reader.fieldnames or [])]
            rows = [{column: (row.get(column) or "").strip() for column in (*COLUMNS, *optional)} for row in reader]
    except OSError as exc:
        raise MusicInfoError(f"CSV를 읽지 못했습니다: {path}\n{exc}") from exc
    if not rows:
        raise MusicInfoError("CSV에 곡 정보가 없습니다.")
    return rows


def atomic_csv(path: Path, rows: list[dict[str, object]], *, columns=COLUMNS) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(prefix=".music-info-", suffix=".csv", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=columns, extrasaction="ignore")
            writer.writeheader()
            for row in rows:
                writer.writerow({column: row.get(column, "") for column in columns})
        os.replace(temp, path)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


def unique_rows(rows: list[dict[str, object]]) -> list[dict[str, object]]:
    result: list[dict[str, object]] = []
    seen: set[tuple[str, ...]] = set()
    for row in rows:
        key = tuple(normalized(row.get(column, "")) for column in
                    ("트랙 이름", "앨범 이름", "아티스트 이름"))
        if not key[0] or not key[1] or key in seen:
            continue
        seen.add(key)
        result.append(row)
    return result
