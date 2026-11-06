#!/usr/bin/env python3
"""Create a reviewable Japanese episode from topic-specific Google News RSS feeds."""

from __future__ import annotations

import argparse
from datetime import date, datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
import json
from pathlib import Path
import re
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urljoin, urlparse
from urllib.request import Request, urlopen
import xml.etree.ElementTree as ET
from zoneinfo import ZoneInfo


ROOT = Path(__file__).resolve().parents[1]
PREFERENCES = ROOT / "news-preferences.json"
EPISODES_INDEX = ROOT / "episodes.json"
USER_AGENT = "myradio-news/1.0 (+https://github.com/quu-maker/myradio)"
WEEKDAYS_JA = "月火水木金土日"
GOSSIP_MARKERS = ("熱愛", "不倫", "ゴシップ", "炎上", "スキャンダル", "交際", "破局", "暴露")


class PlainText(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        self.parts.append(data)


class PageMetadata(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.tags: dict[str, str] = {}
        self.title_parts: list[str] = []
        self.in_title = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = {key.lower(): value for key, value in attrs if value is not None}
        if tag.lower() == "meta":
            name = (values.get("property") or values.get("name") or "").lower()
            content = values.get("content", "").strip()
            if name and content:
                self.tags.setdefault(name, content)
        elif tag.lower() == "link":
            rel = values.get("rel", "").lower()
            href = values.get("href", "").strip()
            if href and ("icon" in rel):
                self.tags.setdefault("icon", href)
        elif tag.lower() == "title":
            self.in_title = True

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() == "title":
            self.in_title = False

    def handle_data(self, data: str) -> None:
        if self.in_title:
            self.title_parts.append(data)


def read_json(path: Path) -> object:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def clean_text(value: str, limit: int = 1200) -> str:
    parser = PlainText()
    try:
        parser.feed(value or "")
    except Exception:
        return re.sub(r"\s+", " ", value or "").strip()[:limit]
    return re.sub(r"\s+", " ", " ".join(parser.parts)).strip()[:limit]


def http_get(url: str, timeout: int = 15, max_bytes: int = 1_000_000) -> tuple[bytes, str, str]:
    request = Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/rss+xml, application/xml, text/html;q=0.9, */*;q=0.8"})
    with urlopen(request, timeout=timeout) as response:
        data = response.read(max_bytes + 1)
        if len(data) > max_bytes:
            data = data[:max_bytes]
        content_type = response.headers.get_content_type()
        return data, response.geturl(), content_type


def parse_pub_date(value: str) -> datetime | None:
    if not value:
        return None
    try:
        parsed = parsedate_to_datetime(value)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except (TypeError, ValueError, OverflowError):
        return None


def resolve_article_url(url: str) -> str:
    if urlparse(url).hostname not in {"news.google.com", "news.google.co.jp"}:
        return url
    try:
        _, final_url, _ = http_get(url, timeout=8, max_bytes=32_000)
        if urlparse(final_url).hostname not in {"news.google.com", "news.google.co.jp"}:
            return final_url
    except (HTTPError, URLError, TimeoutError, OSError):
        pass
    return url


def fetch_page_metadata(url: str) -> dict[str, str | None]:
    if urlparse(url).scheme not in {"http", "https"}:
        return {}
    try:
        body, final_url, content_type = http_get(url, timeout=7, max_bytes=450_000)
        if "html" not in content_type:
            return {}
        parser = PageMetadata()
        parser.feed(body.decode("utf-8", errors="replace"))
        tags = parser.tags
        icon = urljoin(final_url, tags["icon"]) if tags.get("icon") else None
        image = urljoin(final_url, tags["og:image"]) if tags.get("og:image") else None
        return {
            "url": final_url,
            "title": tags.get("og:title") or " ".join(parser.title_parts).strip() or None,
            "description": tags.get("og:description") or tags.get("description"),
            "image": image,
            "site": tags.get("og:site_name"),
            "icon": icon,
        }
    except (HTTPError, URLError, TimeoutError, OSError, UnicodeError):
        return {}


def historical_urls() -> set[str]:
    urls: set[str] = set()
    if not EPISODES_INDEX.exists():
        return urls
    try:
        index = read_json(EPISODES_INDEX)
    except (OSError, json.JSONDecodeError):
        return urls
    if not isinstance(index, list):
        return urls
    for entry in index[:10]:
        if not isinstance(entry, dict) or not isinstance(entry.get("date"), str):
            continue
        path = ROOT / "episodes" / f"{entry['date']}-ja.json"
        if not path.exists():
            continue
        try:
            episode = read_json(path)
        except (OSError, json.JSONDecodeError):
            continue
        for item in episode.get("news", []) if isinstance(episode, dict) else []:
            if isinstance(item, dict) and isinstance(item.get("url"), str):
                urls.add(item["url"])
    return urls


def fetch_candidates(preferences: dict, now: datetime) -> list[dict]:
    lookback_hours = int(preferences.get("lookback_hours", 36))
    cutoff = now.astimezone(timezone.utc) - timedelta(hours=lookback_hours)
    lookback_days = max(1, (lookback_hours + 23) // 24)
    per_topic = int(preferences.get("articles_per_topic", 6))
    candidates: list[dict] = []
    seen_urls = historical_urls()
    seen_titles: set[str] = set()

    for topic in preferences.get("topics", []):
        query = urlencode({
            "q": f"({topic['search_query']}) when:{lookback_days}d",
            "hl": "ja",
            "gl": "JP",
            "ceid": "JP:ja",
        })
        feed_url = f"https://news.google.com/rss/search?{query}"
        try:
            body, _, _ = http_get(feed_url, timeout=20, max_bytes=1_500_000)
            root = ET.fromstring(body)
        except (HTTPError, URLError, TimeoutError, OSError, ET.ParseError) as exc:
            print(f"RSS取得を見送ります（{topic['name']}）: {exc}", file=sys.stderr)
            continue

        topic_items = 0
        for item in root.findall("./channel/item"):
            title = clean_text(item.findtext("title") or "", 300)
            link = (item.findtext("link") or "").strip()
            description = clean_text(item.findtext("description") or "", 1200)
            if any(marker in title for marker in GOSSIP_MARKERS):
                continue
            published = parse_pub_date(item.findtext("pubDate") or "")
            source_node = item.find("source")
            site = clean_text((source_node.text or "") if source_node is not None else "", 120)
            if not title or not link or not published or published < cutoff:
                continue

            direct_url = resolve_article_url(link)
            if direct_url in seen_urls or title.casefold() in seen_titles:
                continue
            seen_urls.add(direct_url)
            seen_titles.add(title.casefold())
            metadata = fetch_page_metadata(direct_url)
            final_url = str(metadata.get("url") or direct_url)
            if final_url in historical_urls():
                continue

            candidates.append({
                "id": f"story-{len(candidates) + 1:03d}",
                "topic": topic["name"],
                "title": str(metadata.get("title") or title)[:300],
                "site": str(metadata.get("site") or site or urlparse(final_url).hostname or "ニュースサイト")[:120],
                "description": clean_text(str(metadata.get("description") or description), 1200),
                "url": final_url,
                "image": metadata.get("image"),
                "icon": metadata.get("icon"),
                "published_at": published.isoformat(),
            })
            topic_items += 1
            if topic_items >= per_topic:
                break

    candidates.sort(key=lambda story: story["published_at"], reverse=True)
    return candidates


def script_line(text: str) -> dict[str, str]:
    return {"say": text, "show": text}


def shorten_summary(value: str, limit: int = 130) -> str:
    summary = clean_text(value, 1200)
    summary = re.sub(r"https?://\S+", "", summary).strip()
    if not re.search(r"[ぁ-んァ-ヶ一-龯]", summary):
        return ""
    if len(summary) <= limit:
        return summary
    boundary = max(summary.rfind(mark, 0, limit) for mark in "。！？")
    if boundary >= limit // 2:
        return summary[: boundary + 1]
    return summary[: limit - 1].rstrip("、，, ") + "…"


def generate_script(preferences: dict, stories: list[dict], episode_date: date) -> dict:
    station = read_json(ROOT / "station.json")
    max_stories = int(preferences.get("max_stories", 15))
    topic_names = [topic["name"] for topic in preferences["topics"]]
    by_topic = {name: [] for name in topic_names}
    for story in stories:
        if story["topic"] in by_topic:
            by_topic[story["topic"]].append(story)

    selected: list[dict] = []
    while len(selected) < max_stories:
        added = False
        for topic_name in topic_names:
            if by_topic[topic_name] and len(selected) < max_stories:
                selected.append(by_topic[topic_name].pop(0))
                added = True
        if not added:
            break

    if not selected:
        raise RuntimeError("読み上げられるニュース候補がありません")

    station_name = station.get("name", "ものラジオ")
    date_text = f"{episode_date.month}月{episode_date.day}日"
    weekday = WEEKDAYS_JA[episode_date.weekday()]
    news = []
    for story in selected:
        title = clean_text(story["title"], 240).rstrip("。.!！?？")
        summary = shorten_summary(story.get("description", ""))
        lines = [script_line(f"{story['topic']}。{title}。")]
        if summary and summary.casefold() not in title.casefold():
            lines.append(script_line(summary))
        points = [title]
        details = [summary or title]
        news.append({
            "story_id": story["id"],
            "label": title[:100],
            "points": points,
            "details": details,
            "lines": lines,
        })

    finance_index = next(
        (index for index, item in enumerate(news) if selected[index]["topic"] == "金融・株式"),
        0,
    )
    return {
        "title": f"{date_text}のニュース",
        "opening": [
            script_line(f"{station_name}です。{date_text}{weekday}曜日、おはようございます。"),
            script_line(f"今日は{len(news)}本のニュースをお伝えします。"),
        ],
        "news": news,
        "closing": [script_line(f"以上、{station_name}でした。気になるニュースは、出典の記事もご確認ください。")],
        "highlight": finance_index,
    }


def validate_script(script: dict, stories: list[dict], max_stories: int) -> None:
    story_ids = {story["id"] for story in stories}
    selected = script.get("news")
    if not isinstance(selected, list) or not selected:
        raise RuntimeError("ニュース候補から記事を選べませんでした")
    if len(selected) > max_stories:
        raise RuntimeError("生成されたニュース本数が設定上限を超えています")
    used: set[str] = set()
    for item in selected:
        story_id = item.get("story_id")
        if story_id not in story_ids or story_id in used:
            raise RuntimeError("生成結果の story_id が候補と一致しないか、重複しています")
        used.add(story_id)
        if not item.get("lines") or not item.get("points"):
            raise RuntimeError(f"ニュース「{item.get('label', '')}」に要点か読み上げ文がありません")
        if len(item["details"]) != len(item["points"]):
            raise RuntimeError(f"ニュース「{item.get('label', '')}」の詳細と要点の数が一致しません")
    if not script.get("opening") or not script.get("closing"):
        raise RuntimeError("番組の冒頭か締めの文がありません")
    if not 0 <= int(script.get("highlight", -1)) < len(selected):
        script["highlight"] = 0


def create_episode(script: dict, stories: list[dict], episode_date: date) -> dict:
    by_id = {story["id"]: story for story in stories}
    news = []
    for item in script["news"]:
        story = by_id[item["story_id"]]
        news.append({
            "label": item["label"],
            "topic": story["topic"],
            "points": item["points"],
            "details": item["details"],
            "url": story["url"],
            "lines": item["lines"],
            "link": {
                "url": story["url"],
                "title": story["title"],
                "site": story["site"],
                "description": story["description"],
                "image": story["image"],
                "icon": story["icon"],
            },
        })
    return {
        "format": "monoradio/news-1",
        "date": episode_date.isoformat(),
        "lang": "ja",
        "title": script["title"],
        "opening": script["opening"],
        "news": news,
        "highlight": int(script["highlight"]),
        "closing": script["closing"],
        "made_at": datetime.now(ZoneInfo("Asia/Tokyo")).isoformat(timespec="seconds"),
    }


def update_episode_index(episode_date: date, title: str, target_seconds: int) -> None:
    index = read_json(EPISODES_INDEX)
    if not isinstance(index, list):
        raise RuntimeError("episodes.json は配列である必要があります")
    date_text = episode_date.isoformat()
    index = [entry for entry in index if entry.get("date") != date_text]
    index.insert(0, {"date": date_text, "ja": {"title": title, "seconds": target_seconds}})
    index.sort(key=lambda entry: entry.get("date", ""), reverse=True)
    write_json(EPISODES_INDEX, index)


def main() -> int:
    parser = argparse.ArgumentParser(description="ジャンル設定に沿って確認用ニュース台本を作ります")
    parser.add_argument("--date", help="番組の日付（YYYY-MM-DD）。省略すると設定したタイムゾーンの今日です")
    args = parser.parse_args()
    preferences = read_json(PREFERENCES)
    if not isinstance(preferences, dict) or preferences.get("format") != "myradio/news-preferences-1":
        raise RuntimeError("news-preferences.json の形式が正しくありません")
    topics = preferences.get("topics")
    if not isinstance(topics, list) or not topics:
        raise RuntimeError("news-preferences.json の topics にジャンルを1つ以上設定してください")
    for topic in topics:
        if not isinstance(topic, dict) or not topic.get("name") or not topic.get("search_query"):
            raise RuntimeError("各ジャンルに name と search_query を設定してください")

    zone = ZoneInfo(preferences.get("timezone", "Asia/Tokyo"))
    episode_date = date.fromisoformat(args.date) if args.date else datetime.now(zone).date()
    episode_path = ROOT / "episodes" / f"{episode_date.isoformat()}-ja.json"
    if episode_path.exists():
        print(f"{episode_date.isoformat()} の台本はすでにあります。更新を作りません")
        return 0

    stories = fetch_candidates(preferences, datetime.now(timezone.utc))
    if not stories:
        raise RuntimeError("指定ジャンルの新しいニュース候補が見つかりませんでした")
    script = generate_script(preferences, stories, episode_date)
    validate_script(script, stories, int(preferences.get("max_stories", 15)))
    episode = create_episode(script, stories, episode_date)
    write_json(episode_path, episode)
    update_episode_index(episode_date, episode["title"], int(preferences.get("target_duration_seconds", 300)))
    print(f"台本を作成しました: {episode_path.relative_to(ROOT)}")
    print("出典:")
    for item in episode["news"]:
        print(f"- {item['label']} ({item['topic']}): {item['url']}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, KeyError, TypeError, ValueError, RuntimeError) as exc:
        print(f"ニュース台本の作成を止めました: {exc}", file=sys.stderr)
        raise SystemExit(1)
