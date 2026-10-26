"""Read Wikipedia album infoboxes and track lists through the MediaWiki Action API."""
from __future__ import annotations

import re
import time
from datetime import datetime
from html import unescape
from html.parser import HTMLParser
from urllib.parse import quote, unquote, urlsplit

from music_info_common import MusicInfoError, normalized


class WikipediaError(MusicInfoError):
    pass


class WikipediaUnavailable(WikipediaError):
    pass


def tidy(value: str) -> str:
    return re.sub(r"\s+", " ", unescape(value)).strip()


class WikiHTML(HTMLParser):
    """Keep tables and links; ignore reference footnotes and scripts."""
    def __init__(self, html: str):
        super().__init__(convert_charrefs=True)
        self.tables: list[dict] = []
        self.links: list[dict] = []
        self.stack: list[dict] = []
        self.anchor = None
        self.skip = 0
        self.headings = {}
        self.heading_level = None
        self.heading_text = ""
        self.feed(html)

    def handle_starttag(self, tag, attributes):
        attrs = dict(attributes)
        if tag in ("sup", "script", "style"):
            self.skip += 1
        if self.skip:
            return
        if re.fullmatch(r"h[2-6]", tag):
            self.heading_level = int(tag[1])
            self.heading_text = ""
        if tag == "table":
            table = {"attrs": attrs, "rows": [], "row": None, "cell": None,
                     "headings": dict(self.headings)}
            self.tables.append(table)
            self.stack.append(table)
        elif tag == "tr" and self.stack:
            self.stack[-1]["row"] = []
        elif tag in ("th", "td") and self.stack:
            cell = {"tag": tag, "attrs": attrs, "raw": "", "links": [], "images": []}
            self.stack[-1]["cell"] = cell
        elif tag == "a":
            self.anchor = {"href": attrs.get("href", ""), "text": ""}
            self.links.append(self.anchor)
            if self.stack and self.stack[-1]["cell"] is not None:
                self.stack[-1]["cell"]["links"].append(self.anchor)
        elif tag == "img" and self.stack and self.stack[-1]["cell"] is not None:
            self.stack[-1]["cell"]["images"].append(attrs)
        elif tag in ("br", "li", "div", "p"):
            self.handle_data("\n")

    def handle_endtag(self, tag):
        if tag in ("sup", "script", "style"):
            self.skip = max(0, self.skip - 1)
            return
        if self.skip:
            return
        if re.fullmatch(r"h[2-6]", tag) and self.heading_level is not None:
            level = self.heading_level
            self.headings = {key: value for key, value in self.headings.items() if key < level}
            self.headings[level] = tidy(self.heading_text)
            self.heading_level = None
        if tag == "a":
            self.anchor = None
        if not self.stack:
            return
        table = self.stack[-1]
        if tag in ("th", "td") and table["cell"] is not None:
            cell = table["cell"]
            cell["text"] = tidy(cell["raw"])
            if table["row"] is not None:
                table["row"].append(cell)
            table["cell"] = None
        elif tag == "tr" and table["row"] is not None:
            if table["row"]:
                table["rows"].append(table["row"])
            table["row"] = None
        elif tag == "table":
            self.stack.pop()

    def handle_data(self, data):
        if self.skip:
            return
        if self.heading_level is not None:
            self.heading_text += data
        if self.stack and self.stack[-1]["cell"] is not None:
            self.stack[-1]["cell"]["raw"] += data
        if self.anchor is not None:
            self.anchor["text"] += data


def wiki_title(href: str) -> str:
    parsed = urlsplit(href)
    if parsed.netloc and parsed.netloc not in ("en.wikipedia.org", "ko.wikipedia.org"):
        return ""
    path = parsed.path
    if path.startswith("./"):
        path = "/wiki/" + path[2:]
    if not path.startswith("/wiki/"):
        return ""
    return unquote(path[6:]).replace("_", " ")


def page_url(language: str, title: str, revision: int | None = None) -> str:
    if revision:
        return f"https://{language}.wikipedia.org/w/index.php?oldid={revision}"
    return f"https://{language}.wikipedia.org/wiki/" + quote(title.replace(" ", "_"))


def is_article(title: str) -> bool:
    return bool(title) and not re.match(
        r"^(?:File|Image|Category|Template|Help|Talk|Special|Wikipedia|User|Module|Portal|"
        r"파일|분류|틀|도움말|토론|특수|위키백과|사용자|모듈|포털):", title, re.I)


def infobox(document: WikiHTML) -> dict:
    table = next((item for item in document.tables
                  if "infobox" in item["attrs"].get("class", "").split()), None)
    result = {"fields": {}, "performers": [], "name": "", "kind": "", "cover_file": ""}
    if table is None:
        return result
    for cells in table["rows"]:
        for cell in cells:
            classes = cell["attrs"].get("class", "")
            if "infobox-above" in classes:
                result["name"] = cell["text"]
            if "infobox-subheader" in classes or ("infobox-header" in classes and re.search(r"\bby\b|음반|싱글|앨범", cell["text"], re.I)):
                result["kind"] = cell["text"]
                if re.search(r"\bby\b|음반|싱글", cell["text"], re.I):
                    people = [tidy(link["text"]) for link in cell["links"]
                                                if normalized(link["text"]) not in {
                                                    "studioalbum", "album", "ep", "single", "mixtape", "livealbum",
                                                    "compilationalbum", normalized("정규 음반"), normalized("음반"), normalized("싱글")}]
                    if not people and re.search(r"\bby\b", cell["text"], re.I):
                        people = [re.split(r"\bby\b", cell["text"], maxsplit=1, flags=re.I)[1].strip()]
                    result["performers"].extend(people)
            if cell["images"] and not result["cover_file"]:
                result["cover_file"] = next((wiki_title(link["href"]) for link in cell["links"]
                                              if wiki_title(link["href"]).startswith(("File:", "파일:"))), "")
        if len(cells) >= 2 and cells[0]["tag"] == "th":
            label = normalized(cells[0]["text"])
            result["fields"][label] = cells[1]
            if label in {normalized(value) for value in ("artist", "음악가", "가수", "아티스트", "연주자")}:
                values = [tidy(link["text"]) for link in cells[1]["links"]]
                result["performers"].extend(values or [cells[1]["text"]])
    result["performers"] = list(dict.fromkeys(value for value in result["performers"] if value))
    return result


def field(box: dict, *names: str) -> dict:
    for name in names:
        if normalized(name) in box["fields"]:
            return box["fields"][normalized(name)]
    return {"text": "", "raw": "", "links": []}


def release_date(text: str) -> str:
    # Prefer machine-readable dates included by Wikipedia's start-date templates.
    for expression in (r"(\d{4})-(\d{1,2})-(\d{1,2})", r"(\d{4})년\s*(\d{1,2})월\s*(\d{1,2})일"):
        match = re.search(expression, text)
        if match:
            try:
                return datetime(*(int(part) for part in match.groups())).strftime("%Y-%m-%d")
            except ValueError:
                return ""
    english = re.search(r"\b(?:\d{1,2}\s+[A-Za-z]+\s+\d{4}|[A-Za-z]+\s+\d{1,2},?\s+\d{4})\b", text)
    if english:
        value = english[0].replace(",", "")
        for pattern in ("%d %B %Y", "%B %d %Y", "%d %b %Y", "%b %d %Y"):
            try:
                return datetime.strptime(value, pattern).strftime("%Y-%m-%d")
            except ValueError:
                pass
    match = re.search(r"\b(?:19|20)\d{2}\b", text)
    return match[0] if match else ""


def track_list(document: WikiHTML) -> list[dict]:
    tracks = []
    primary_section = None
    for table in document.tables:
        heading = table.get("headings", {}).get(2, "")
        if primary_section is not None and heading != primary_section:
            continue
        context = " ".join(table.get("headings", {}).values())
        if tracks and re.search(r"bonus|deluxe|anniversary|reissue|remix|보너스|리믹스|재발매", context, re.I):
            continue
        header = None
        for cells in table["rows"]:
            labels = [normalized(cell["text"]) for cell in cells]
            title_index = next((i for i, value in enumerate(labels) if value in
                                {normalized(item) for item in ("title", "track", "song", "제목", "곡명", "곡제목")}), None)
            if title_index is not None:
                header = {"title": title_index,
                          "number": next((i for i, value in enumerate(labels) if value in
                                          {normalized(item) for item in ("no", "number", "track no", "번호", "트랙")}
                                          or cells[i]["text"] in ("#", "＃")), None),
                          "writers": next((i for i, value in enumerate(labels) if value in
                                           {normalized(item) for item in ("writers", "writer", "작사작곡", "작곡", "작곡가")}), None)}
                continue
            if header is None or len(cells) <= header["title"]:
                continue
            number_cell = cells[header["number"]]["text"] if header["number"] is not None and len(cells) > header["number"] else ""
            number = re.match(r"^\s*(\d+)[.)]?\s*$", number_cell)
            if not number:
                continue
            title = cells[header["title"]]["text"].strip(' "“”‘’《》〈〉')
            if not title or normalized(title) in {"totallength", "총재생시간"}:
                continue
            if primary_section is None:
                primary_section = heading
            writer_index = header["writers"]
            composers = cells[writer_index]["text"] if writer_index is not None and len(cells) > writer_index else ""
            tracks.append({"title": title, "number": str(int(number[1])), "composer": composers})
    return tracks


def release_links(document: WikiHTML) -> list[str]:
    selected = []
    for table in document.tables:
        title_column = None
        for cells in table["rows"]:
            labels = [normalized(cell["text"]) for cell in cells]
            found = next((i for i, value in enumerate(labels) if value in
                          {normalized(item) for item in ("title", "album", "albums", "single", "음반", "앨범", "제목")}), None)
            if found is not None:
                title_column = found
                continue
            if all(cell["tag"] == "th" and cell["attrs"].get("scope") != "row" for cell in cells):
                continue
            if title_column is not None and len(cells) > title_column:
                for link in cells[title_column]["links"]:
                    title = wiki_title(link["href"])
                    if is_article(title):
                        selected.append(title)
                        break
    if not selected:
        selected = [wiki_title(link["href"]) for link in document.links]
    return list(dict.fromkeys(title for title in selected if is_article(title)))


class Wikipedia:
    def __init__(self, language="ko", contact="", timeout=15):
        if language not in ("ko", "en"):
            raise WikipediaError("위키백과 언어는 ko 또는 en이어야 합니다.")
        import requests
        self.session = requests.Session()
        contact = re.sub(r"[\r\n]", "", contact).strip()
        agent = "MusicInfoTools/1.1 (https://github.com/spoiler604/music-info-tools"
        self.session.headers["User-Agent"] = agent + (f"; {contact}" if contact else "") + ")"
        self.language = language
        self.timeout = timeout
        self.cache = {}
        self.last_request = 0.0
        self.failure = ""

    def request(self, language: str, **params):
        import requests
        if self.failure:
            raise WikipediaUnavailable(self.failure)
        params = {"format": "json", "formatversion": 2, "maxlag": 5, **params}
        key = (language, tuple(sorted(params.items())))
        if key in self.cache:
            return self.cache[key]
        wait = 0.3 - (time.monotonic() - self.last_request)
        if wait > 0:
            time.sleep(wait)
        try:
            response = self.session.get(f"https://{language}.wikipedia.org/w/api.php",
                                        params=params, timeout=self.timeout)
            response.raise_for_status()
            data = response.json()
        except (requests.RequestException, ValueError) as exc:
            self.failure = f"위키백과 {language} API 조회 실패 ({type(exc).__name__}); 잠시 후 다시 실행해 주세요."
            raise WikipediaUnavailable(self.failure) from exc
        finally:
            self.last_request = time.monotonic()
        if not isinstance(data, dict) or data.get("error"):
            error = (data.get("error") or {}).get("code", "invalid-response") if isinstance(data, dict) else "invalid-response"
            if error in ("maxlag", "ratelimited", "readonly", "invalid-response"):
                self.failure = f"위키백과 API 일시 중단: {error}; 잠시 후 다시 실행해 주세요."
                raise WikipediaUnavailable(self.failure)
            raise WikipediaError(f"위키백과 API 오류: {error}")
        self.cache[key] = data
        return data

    def parse(self, language: str, title: str, section: str | None = None) -> dict:
        params = {"action": "parse", "page": title, "redirects": 1, "prop": "text|sections|revid"}
        if section is not None:
            params["section"] = section
        result = self.request(language, **params).get("parse", {})
        text = result.get("text", "")
        if isinstance(text, dict):
            text = text.get("*", "")
        if not isinstance(text, str):
            raise WikipediaError("위키백과 문서 HTML을 해석하지 못했습니다.")
        return {"title": result.get("title", title), "revision": result.get("revid"),
                "sections": result.get("sections", []), "document": WikiHTML(text)}

    def find_artist(self, name: str) -> dict:
        if not normalized(name):
            raise WikipediaError("아티스트 이름을 입력해 주세요.")
        for language in dict.fromkeys((self.language, "en")):
            result = self.request(language, action="query", generator="search", gsrsearch=name,
                                  gsrnamespace=0, gsrlimit=5, prop="pageprops")
            candidates = []
            for page in result.get("query", {}).get("pages", []):
                if "disambiguation" in page.get("pageprops", {}):
                    continue
                bare = re.sub(r"\s*\([^)]*\)\s*$", "", page.get("title", ""))
                if normalized(bare) != normalized(name):
                    continue
                parsed = self.parse(language, page["title"])
                box = infobox(parsed["document"])
                musical = field(box, "genre", "genres", "장르", "instruments", "악기")["text"]
                occupation = field(box, "occupation", "occupations", "직업")["text"]
                if not musical and not re.search(r"singer|musician|rapper|composer|band|가수|래퍼|음악|작곡", occupation, re.I):
                    continue
                aliases = [name, bare]
                for labels in (("also known as", "other names", "다른 이름", "예명"),
                               ("member of", "formerly of", "소속 그룹")):
                    cell = field(box, *labels)
                    aliases.extend(tidy(value) for value in re.split(r"[\n;,]", cell["raw"]) if tidy(value))
                    aliases.extend(tidy(link["text"]) for link in cell["links"] if tidy(link["text"]))
                candidates.append({**parsed, "language": language, "artist": bare,
                                   "aliases": list(dict.fromkeys(aliases)),
                                   "url": page_url(language, parsed["title"], parsed["revision"])})
            if len(candidates) == 1:
                return self.complete_artist(candidates[0])
            if len(candidates) > 1:
                raise WikipediaError("동명 아티스트 문서가 여러 개입니다. 정확한 문서 제목을 --page로 지정해 주세요.")
        raise WikipediaError("일치하는 음악 아티스트 문서를 찾지 못했습니다. --page로 위키백과 문서 제목을 지정해 주세요.")

    def complete_artist(self, artist: dict) -> dict:
        if artist["language"] != "en":
            data = self.request(artist["language"], action="query", titles=artist["title"],
                                prop="langlinks", lllang="en", lllimit=1)
            for page in data.get("query", {}).get("pages", []):
                for link in page.get("langlinks", []):
                    title = link.get("title") or link.get("*", "")
                    name = re.sub(r"\s*\([^)]*\)\s*$", "", title)
                    if name:
                        artist["english_artist"] = name
                        artist["aliases"] = list(dict.fromkeys([*artist["aliases"], name]))
        return artist

    def artist_page(self, name: str, title: str = "") -> dict:
        if not title:
            return self.find_artist(name)
        parsed = self.parse(self.language, title)
        box = infobox(parsed["document"])
        if not box["fields"] or not (field(box, "genre", "genres", "장르")["text"] or field(box, "occupation", "occupations", "직업")["text"]):
            raise WikipediaError("지정한 문서에서 아티스트 정보상자를 찾지 못했습니다.")
        aliases = [name, re.sub(r"\s*\([^)]*\)$", "", parsed["title"])]
        for labels in (("also known as", "other names", "다른 이름", "예명"),
                       ("member of", "formerly of", "소속 그룹")):
            cell = field(box, *labels)
            aliases.extend(tidy(value) for value in re.split(r"[\n;,]", cell["raw"]) if tidy(value))
            aliases.extend(tidy(link["text"]) for link in cell["links"] if tidy(link["text"]))
        return self.complete_artist({**parsed, "language": self.language, "artist": name, "aliases": list(dict.fromkeys(aliases)),
                                     "url": page_url(self.language, parsed["title"], parsed["revision"])})

    def albums(self, artist: dict, limit=100, log=print) -> list[dict]:
        language = artist["language"]
        section = next((section for section in artist["sections"] if normalized(re.sub(r"<[^>]*>", "", section.get("line", ""))) in
                        {"discography", normalized("음반 목록"), normalized("음반"), normalized("디스코그래피")}), None)
        if section:
            document = self.parse(language, artist["title"], section["index"])["document"]
        else:
            document = artist["document"]
        # Follow only a discography page explicitly linked by the artist article.
        discographies = [wiki_title(link["href"]) for link in document.links
                         if re.search(r"discography|음반.?목록|디스코그래피", wiki_title(link["href"]), re.I)]
        for title in dict.fromkeys(discographies):
            if normalized(artist["artist"]) not in normalized(title):
                continue
            document = self.parse(language, title)["document"]
            break
        titles = [title for title in release_links(document)
                  if normalized(title) not in {normalized(value) for value in artist["aliases"]}
                  and not re.search(r"discography|음반.?목록", title, re.I)]
        results = []
        names = {normalized(value) for value in artist["aliases"]}
        for title in titles[:limit]:
            try:
                parsed = self.parse(language, title)
                box = infobox(parsed["document"])
                performers = {normalized(value) for value in box["performers"]}
                if not names.intersection(performers):
                    continue
                date_text = field(box, "released", "발매일", "발매", "출시일")["text"]
                date = release_date(date_text)
                genre = field(box, "genre", "genres", "장르")["text"]
                tracks = track_list(parsed["document"])
                if not date_text and not tracks:
                    continue
                album_title = (box["name"] or re.sub(r"\s*\([^)]*\)\s*$", "", parsed["title"])).strip('《》〈〉"“”')
                if not tracks and re.search(r"\bsingle\b|싱글", box["kind"], re.I):
                    tracks = [{"title": album_title, "number": "1",
                               "composer": field(box, "writer", "writers", "작사작곡", "작곡")["text"]}]
                results.append({"title": album_title, "artist": "; ".join(box["performers"]),
                                "date": date, "year": date[:4], "genre": genre, "tracks": tracks,
                                "cover_file": box["cover_file"], "language": language,
                                "url": page_url(language, parsed["title"], parsed["revision"])})
                log(f"위키백과: {album_title} · {len(tracks)}곡")
            except WikipediaUnavailable:
                raise
            except WikipediaError as exc:
                log(f"위키백과 문서 건너뜀: {title} ({exc})")
        if len(titles) > limit:
            log(f"문서 조회 한도 {limit}개에 도달했습니다. --max-pages로 늘릴 수 있습니다.")
        return results

    def image_info(self, language: str, filename: str) -> dict:
        if not filename:
            return {}
        data = self.request(language, action="query", titles=filename, prop="imageinfo", iiprop="url|extmetadata")
        for page in data.get("query", {}).get("pages", []):
            for info in page.get("imageinfo", []):
                if urlsplit(info.get("url", "")).hostname != "upload.wikimedia.org":
                    continue
                license_name = info.get("extmetadata", {}).get("LicenseShortName", {}).get("value", "")
                return {"url": info["url"], "description_url": info.get("descriptionurl", ""),
                        "license": tidy(re.sub(r"<[^>]*>", "", license_name))}
        return {}
