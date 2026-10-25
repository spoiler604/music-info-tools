"""Symmetric title normalization and conservative approximate song matching."""
from __future__ import annotations

import re
from difflib import SequenceMatcher
from pathlib import Path

from music_info_common import normalized


BRACKETS = re.compile(r"\([^()]*\)|\[[^\[\]]*\]|（[^（）]*）|【[^【】]*】")
FEATURE = re.compile(r"\b(?:feat(?:uring)?\.?|ft\.?)\s+.+$", re.I)
NOISE = re.compile(
    r"^(?:(?:official\s+)?(?:music\s+)?(?:audio|video|lyrics?|visuali[sz]er|mv|m/v)"
    r"|official|hd|hq|4k|1080p|\d{4}\s+remaster(?:ed)?|remaster(?:ed)?(?:\s+\d{4})?)$", re.I)
VERSION = re.compile(
    r"\b(?:remix|mix|live|acoustic|instrumental|demo|karaoke|remake|sped\s*up|slowed|"
    r"radio\s*edit|extended|clean|explicit|version|ver\.?|part\s*\d+|pt\.?\s*\d+|vol\.?\s*\d+)\b"
    r"|라이브|리믹스|어쿠스틱|반주|인스트|데모", re.I)


def strip_track_prefix(text: str) -> str:
    # Keep genuine titles such as "7 rings", "99 Problems" and "1989".
    return re.sub(r"^\s*(?:\d{1,3}\s*[.\-_)]+\s*|0\d{1,2}\s+)(?=\S)", "", text).strip()


def artist_keys(value: str) -> set[str]:
    parts = re.split(r"[;,/&]|\s+(?:feat\.?|ft\.?|featuring)\s+", value or "", flags=re.I)
    return {normalized(value), *(normalized(part) for part in parts)} - {""}


def cleaned_title(text: str) -> str:
    def clean_bracket(match):
        content = match.group()[1:-1].strip()
        if FEATURE.match(content) or NOISE.fullmatch(content):
            return " "
        return match.group()
    text = BRACKETS.sub(clean_bracket, text)
    def clean_feature(match):
        # "Song feat. Guest (Live)" must retain the recording qualifier.
        return " ".join(part.group() for part in BRACKETS.finditer(match.group())
                        if VERSION.search(part.group()[1:-1]))
    text = FEATURE.sub(clean_feature, text).strip(" -–—([{（")
    # Remove harmless release decorations at the end, including an unbracketed remaster.
    parts = re.split(r"\s+[-–—]\s+", text)
    while len(parts) > 1 and NOISE.fullmatch(parts[-1].strip()):
        parts.pop()
    return re.sub(r"\s+", " ", " - ".join(parts)).strip()


def version_key(text: str) -> tuple[str, ...]:
    text = cleaned_title(text)
    qualifiers = [match.group()[1:-1].strip() for match in BRACKETS.finditer(text)]
    qualifiers.extend(re.split(r"\s+[-–—]\s+", text)[1:])
    # Treat a bare suffix such as "Song live" as a recording qualifier too.
    bare = re.search(r"\s+(live|instrumental|acoustic|demo|remix|karaoke|clean|explicit)$", text, re.I)
    if bare:
        qualifiers.append(bare[1])
    return tuple(sorted({normalized(part) for part in qualifiers if VERSION.search(part)}))


def base_title(text: str) -> str:
    text = BRACKETS.sub(" ", cleaned_title(text))
    return normalized(re.split(r"\s+[-–—]\s+", text, maxsplit=1)[0])


def source_titles(path: Path, metadata: dict[str, str]) -> list[str]:
    stem = strip_track_prefix(path.stem)
    # The right-hand side of an artist-title filename is more informative than the full filename.
    file_title = stem.split(" - ", 1)[1].strip() if " - " in stem else stem
    return list(dict.fromkeys(value.strip() for value in (metadata.get("title", ""), file_title, stem)
                              if value.strip()))


def filename_title(path: Path) -> str:
    stem = strip_track_prefix(path.stem)
    return stem.split(" - ", 1)[1].strip() if " - " in stem else stem


def title_language(text: str) -> str:
    if re.search(r"[\uac00-\ud7a3]", text):
        return "ko"
    if re.search(r"[A-Za-z]", text):
        return "en"
    return ""


def title_forms(path: Path, metadata: dict[str, str]) -> list[str]:
    return list(dict.fromkeys(key for value in source_titles(path, metadata)
                              for key in (normalized(value), normalized(cleaned_title(value))) if key))


def context_for(row: dict[str, str], path: Path, metadata: dict[str, str], album_vote: str):
    tagged = artist_keys(metadata.get("artist", "")) | artist_keys(metadata.get("albumartist", ""))
    people = artist_keys(row.get("아티스트 이름", "")) | artist_keys(row.get("앨범 아티스트 이름", ""))
    artist = bool(tagged & people)
    conflict = bool(tagged and people and not artist)
    album_name = normalized(row.get("앨범 이름", ""))
    # Folder names can contain an artist/year prefix. Explicit album tags compare as whole values.
    album = bool(album_name and (
        album_name == normalized(metadata.get("album", ""))
        or album_name == normalized(album_vote)
        or (len(album_name) >= 4 and album_name in normalized(path.parent.name))
        or album_name == normalized(path.parent.name)))
    return artist, album, conflict


def title_score(source: str, target: str, supported: bool) -> tuple[float, str]:
    if normalized(source) == normalized(target):
        return 1.0, ""
    if version_key(source) != version_key(target):
        return 0.0, ""
    left, right = normalized(cleaned_title(source)), normalized(cleaned_title(target))
    if left and left == right:
        return 0.99, "제목 표기 정리"
    left_base, right_base = base_title(source), base_title(target)
    if supported and min(len(left_base), len(right_base)) >= 4 and left_base == right_base:
        return 0.96, "괄호·부제 차이"
    if min(len(left), len(right)) < 6 or min(len(left), len(right)) / max(len(left), len(right), 1) < 0.75:
        return 0.0, ""
    # Numbers in otherwise similar titles often identify different tracks (Part 1 / Part 2).
    if re.findall(r"\d+", left) != re.findall(r"\d+", right):
        return 0.0, ""
    score = SequenceMatcher(None, left, right, autojunk=False).ratio()
    threshold = 0.86 if supported else 0.96
    if score >= threshold and (supported or min(len(left), len(right)) >= 12):
        return score * 0.95, f"제목 유사도 {score:.0%}"
    return 0.0, ""


def choose_row(index: dict[str, list[dict[str, str]]], path: Path,
               metadata: dict[str, str], album_vote: str = "") -> tuple[dict[str, str] | None, str]:
    # Keep the original index interface; compare cleaned forms on BOTH sides.
    rows = []
    seen = set()
    for group in index.values():
        for row in group:
            identity = tuple(sorted(row.items()))
            if identity not in seen:
                rows.append(row)
                seen.add(identity)
    sources = source_titles(path, metadata)
    ranked = []
    for row in rows:
        file_language = title_language(filename_title(path))
        row_language = title_language(row["트랙 이름"])
        if file_language and row_language and file_language != row_language:
            continue
        artist, album, conflict = context_for(row, path, metadata, album_vote)
        if conflict:
            continue
        best_score, best_reason = 0.0, ""
        for source in sources:
            score, reason = title_score(source, row["트랙 이름"], artist or album)
            if score > best_score:
                best_score, best_reason = score, reason
        if best_score:
            ranked.append((best_score, artist, album, best_reason, row))
    if not ranked:
        return None, "제목·아티스트·앨범으로 일치하는 곡을 찾지 못함"
    # Prefer exact/normalized title matches over approximate matches, then use context.
    strong = [item for item in ranked if item[0] >= 0.99]
    pool = strong or ranked
    if any(item[1] for item in pool):
        pool = [item for item in pool if item[1]]
    if any(item[2] for item in pool):
        pool = [item for item in pool if item[2]]
    pool.sort(key=lambda item: item[0], reverse=True)
    best = pool[0]
    if len(pool) > 1 and (strong or best[0] - pool[1][0] < 0.06):
        names = " / ".join(f"{item[4]['아티스트 이름']} — {item[4]['트랙 이름']} [{item[4]['앨범 이름']}]"
                           for item in pool[:3])
        return None, "후보가 비슷해 곡을 확정하지 못함: " + names
    reason = best[3]
    if reason:
        evidence = "·".join(label for value, label in ((best[1], "아티스트 일치"), (best[2], "앨범 일치")) if value)
        reason += f"; {evidence}" if evidence else ""
    return best[4], reason
