#!/usr/bin/env python3
"""Create a reviewable Japanese episode from topic-specific Google News RSS feeds."""

from __future__ import annotations

import argparse
from datetime import date, datetime, timedelta, timezone
from difflib import SequenceMatcher
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
import json
from pathlib import Path
import random
import re
import sys
import time
import unicodedata
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urljoin, urlparse
from urllib.request import Request, urlopen
import xml.etree.ElementTree as ET
from zoneinfo import ZoneInfo

try:
    from googlenewsdecoder import gnewsdecoder
except ImportError:
    gnewsdecoder = None


ROOT = Path(__file__).resolve().parents[1]
PREFERENCES = ROOT / "news-preferences.json"
EPISODES_INDEX = ROOT / "episodes.json"
USER_AGENT = "myradio-news/1.0 (+https://github.com/quu-maker/myradio)"
WEEKDAYS_JA = "月火水木金土日"
GOSSIP_MARKERS = (
    "熱愛", "不倫", "ゴシップ", "炎上", "スキャンダル", "交際", "破局", "暴露",
    "かわいすぎる", "美人すぎる", "神スイング", "タレント", "グラビア", "アイドル",
)
LOW_INFORMATION_TITLE_MARKERS = (
    "指数情報・推移", "株価・株式情報", "株価情報・推移", "指数情報", "株価情報",
    "株価チャート", "ゴルフサバイバル", "ウエハース", "カード付き", "カード付",
    "夜９時", "夜10時", "夜１０時",
)
TOPIC_EXCLUDE_MARKERS = {
    "日本の主要ニュース": (
        "社会人野球", "野球", "プロ野球", "高校野球", "ウエハース", "フットサル",
        "サッカー", "バレーボール", "当日券", "チケット販売", "株価", "株式",
        "自己株", "業績予想", "決算", "話題株", "株探",
    ),
    "ゴルフ・バスケ": ("フットサル", "サッカー", "バレーボール"),
}
GENERIC_HEADLINE_TOKENS = {
    "ニュース", "速報", "話題", "日本", "本日", "今日", "最新", "発表", "公式",
    "女子", "ゴルフ", "バスケ", "試合", "ツアー", "株価", "株式", "金融",
    "日経", "日経平均", "日経平均株価", "平均", "topix", "steam", "ゲーム", "アップデート",
    "about", "news", "update", "official",
}
GREETINGS = (
    "おはようございます。ものラジオです。",
    "ものラジオから、今日のニュースをお届けします。",
    "今日もものラジオの時間です。気になる話題を見ていきましょう。",
    "ものラジオをお聴きのみなさん、おはようございます。",
    "朝のニュースは、ものラジオからお届けします。",
    "おはようございます。今日のものラジオを始めます。",
    "今日も気になるニュースをお届けする、ものラジオです。",
)
CORNERS_FILE = Path(__file__).resolve().parent / "corners.json"
QUIZ_NUMBER_PATTERN = re.compile(
    r"(\d[\d,]*\.?\d*)\s*(億円|万円|％|%|万人|人|台|件|倍|割|チーム|タイトル|ポイント)"
)
QUIZ_UNIT_SCORE = {
    "億円": 6, "％": 5, "%": 5, "万人": 5, "人": 4, "倍": 4, "割": 3,
    "台": 3, "件": 3, "チーム": 2, "タイトル": 2, "ポイント": 2, "万円": 2,
}


def load_corners() -> dict:
    """コーナー用の素材（雑談・運勢・感想）を読む。読めなければコーナーなしで続ける。"""
    try:
        data = read_json(CORNERS_FILE)
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


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


def decode_article_urls(urls: list[str]) -> dict[str, str]:
    google_urls = list(dict.fromkeys(
        url for url in urls
        if urlparse(url).hostname in {"news.google.com", "news.google.co.jp"}
    ))
    if not google_urls or gnewsdecoder is None:
        return {}
    try:
        results = gnewsdecoder(google_urls, timeout=8.0)
    except Exception as exc:
        print(f"Googleニュースの記事リンクを変換できませんでした: {exc}", file=sys.stderr)
        return {}
    if not isinstance(results, list):
        return {}
    decoded: dict[str, str] = {}
    for original, result in zip(google_urls, results):
        if not isinstance(result, dict) or not result.get("success"):
            continue
        destination = str(result.get("decoded_url") or "")
        if urlparse(destination).scheme in {"http", "https"}:
            decoded[original] = destination
    return decoded


def relevant_title(topic_name: str, title: str) -> bool:
    if any(marker in title for marker in GOSSIP_MARKERS + LOW_INFORMATION_TITLE_MARKERS):
        return False
    if any(marker in title for marker in TOPIC_EXCLUDE_MARKERS.get(topic_name, ())):
        return False
    if topic_name == "ゴルフ・バスケ":
        golf_basketball_terms = (
            "ゴルフ", "ゴルファー", "ゴルフ場", "PGA", "LPGA", "バスケ", "バスケット",
            "NBA", "Bリーグ", "Wリーグ",
        )
        return any(term.casefold() in title.casefold() for term in golf_basketball_terms)
    return True


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
        description = (
            tags.get("og:description")
            or tags.get("description")
            or tags.get("twitter:description")
        )
        return {
            "url": final_url,
            "title": tags.get("og:title") or " ".join(parser.title_parts).strip() or None,
            "description": description,
            "image": image,
            "site": tags.get("og:site_name"),
            "icon": icon,
        }
    except (HTTPError, URLError, TimeoutError, OSError, UnicodeError):
        return {}


def normalize_headline(value: str, site: str = "") -> str:
    text = unicodedata.normalize("NFKC", clean_text(value, 400)).casefold()
    if site:
        text = re.sub(rf"\s*[-–—|｜]\s*{re.escape(site)}\s*$", "", text, flags=re.IGNORECASE)
    return re.sub(r"[^a-z0-9ぁ-んァ-ヶ一-龯々〆ヵヶー]+", "", text)


def headline_tokens(value: str, site: str = "") -> set[str]:
    text = unicodedata.normalize("NFKC", clean_text(value, 400)).casefold()
    if site:
        text = re.sub(rf"\s*[-–—|｜]\s*{re.escape(site)}\s*$", "", text, flags=re.IGNORECASE)
    tokens = re.findall(r"[a-z]+|\d+(?:\.\d+)?|[ぁ-ん]+|[ァ-ヶー]+|[一-龯々〆]+", text)
    return {token for token in tokens if len(token) >= 2 and token not in GENERIC_HEADLINE_TOKENS}


def duplicate_headline(left: dict, right: dict) -> bool:
    left_title = str(left.get("title") or left.get("label") or "")
    right_title = str(right.get("title") or right.get("label") or "")
    left_site = str(left.get("site") or "")
    right_site = str(right.get("site") or "")
    if (
        not left.get("historical")
        and not right.get("historical")
        and left.get("topic") == right.get("topic") == "金融・株式"
    ):
        market_drop_markers = ("急落", "暴落", "株価下落", "大幅安", "続落", "下げ幅")
        if any(marker in left_title for marker in market_drop_markers) and any(
            marker in right_title for marker in market_drop_markers
        ):
            return True
    left_norm = normalize_headline(left_title, left_site)
    right_norm = normalize_headline(right_title, right_site)
    if not left_norm or not right_norm:
        return False
    if left_norm == right_norm or SequenceMatcher(None, left_norm, right_norm).ratio() >= 0.72:
        return True

    shared = headline_tokens(left_title, left_site) & headline_tokens(right_title, right_site)
    shared_anchors = {token for token in shared if len(token) >= 4}
    if not left.get("historical") and not right.get("historical") and shared_anchors and (
        left.get("topic") == right.get("topic") or len(shared_anchors) >= 2
    ):
        return True

    left_grams = {left_norm[index:index + 2] for index in range(len(left_norm) - 1)}
    right_grams = {right_norm[index:index + 2] for index in range(len(right_norm) - 1)}
    if left_grams and right_grams:
        overlap = len(left_grams & right_grams)
        containment = overlap / min(len(left_grams), len(right_grams))
        jaccard = overlap / len(left_grams | right_grams)
        if containment >= 0.72 and jaccard >= 0.32:
            return True
    return False


def historical_articles() -> tuple[set[str], list[dict]]:
    urls: set[str] = set()
    headlines: list[dict] = []
    if not EPISODES_INDEX.exists():
        return urls, headlines
    try:
        index = read_json(EPISODES_INDEX)
    except (OSError, json.JSONDecodeError):
        return urls, headlines
    if not isinstance(index, list):
        return urls, headlines
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
            if not isinstance(item, dict):
                continue
            if isinstance(item.get("url"), str):
                urls.add(item["url"])
            link = item.get("link") if isinstance(item.get("link"), dict) else {}
            headlines.append({
                "title": str(link.get("title") or item.get("label") or ""),
                "site": str(link.get("site") or ""),
                "topic": str(item.get("topic") or ""),
                "historical": True,
            })
    return urls, headlines


def fetch_candidates(preferences: dict, now: datetime) -> list[dict]:
    lookback_hours = int(preferences.get("lookback_hours", 36))
    cutoff = now.astimezone(timezone.utc) - timedelta(hours=lookback_hours)
    lookback_days = max(1, (lookback_hours + 23) // 24)
    per_topic = int(preferences.get("articles_per_topic", 6))
    raw_candidates: list[dict] = []
    seen_urls, historical_headlines = historical_articles()
    seen_stories = list(historical_headlines)
    candidate_limit = max(per_topic, per_topic * int(preferences.get("candidate_multiplier", 2)))

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
            if not relevant_title(topic["name"], title):
                continue
            published = parse_pub_date(item.findtext("pubDate") or "")
            source_node = item.find("source")
            site = clean_text((source_node.text or "") if source_node is not None else "", 120)
            if not title or not link or not published or published < cutoff:
                continue

            candidate = {"title": title, "site": site, "topic": topic["name"], "historical": False}
            if link in seen_urls or any(duplicate_headline(candidate, old) for old in seen_stories):
                continue
            raw_candidates.append({
                "topic": topic["name"],
                "title": title,
                "site": site,
                "rss_description": description,
                "rss_url": link,
                "published_at": published.isoformat(),
            })
            seen_stories.append(candidate)
            topic_items += 1
            if topic_items >= candidate_limit:
                break

    decoded_urls = decode_article_urls([item["rss_url"] for item in raw_candidates])
    candidates: list[dict] = []
    seen_stories = list(historical_headlines)
    for raw in raw_candidates:
        rss_url = str(raw["rss_url"])
        direct_url = decoded_urls.get(rss_url, rss_url)
        metadata = {}
        if urlparse(direct_url).hostname not in {"news.google.com", "news.google.co.jp"}:
            metadata = fetch_page_metadata(direct_url)
        final_url = str(metadata.get("url") or direct_url)
        final_title = str(metadata.get("title") or raw["title"])[:300]
        final_site = str(metadata.get("site") or raw["site"] or urlparse(final_url).hostname or "ニュースサイト")[:120]
        if not relevant_title(str(raw["topic"]), final_title):
            continue
        candidate = {"title": final_title, "site": final_site, "topic": raw["topic"], "historical": False}
        if final_url in seen_urls or any(duplicate_headline(candidate, old) for old in seen_stories):
            continue

        rss_description = str(raw["rss_description"] or "")
        metadata_description = clean_text(str(metadata.get("description") or ""), 1200)
        candidates.append({
            "id": f"story-{len(candidates) + 1:03d}",
            "topic": raw["topic"],
            "title": final_title,
            "site": final_site,
            "description": metadata_description or rss_description,
            "summary_candidates": [metadata_description, rss_description],
            "url": final_url,
            "image": metadata.get("image"),
            "icon": metadata.get("icon"),
            "published_at": raw["published_at"],
        })
        seen_urls.add(rss_url)
        seen_urls.add(final_url)
        seen_stories.append(candidate)

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


def summary_repeats_title(title: str, summary: str, site: str = "") -> bool:
    title_norm = normalize_headline(title, site)
    summary_norm = normalize_headline(summary, site)
    if not title_norm or not summary_norm:
        return True
    if title_norm == summary_norm:
        return True
    if title_norm in summary_norm and len(summary_norm) - len(title_norm) < 24:
        return True
    if SequenceMatcher(None, title_norm, summary_norm).ratio() >= 0.76:
        return True
    title_grams = {title_norm[index:index + 2] for index in range(len(title_norm) - 1)}
    summary_grams = {summary_norm[index:index + 2] for index in range(len(summary_norm) - 1)}
    if title_grams and summary_grams:
        overlap = len(title_grams & summary_grams)
        containment = overlap / min(len(title_grams), len(summary_grams))
        jaccard = overlap / len(title_grams | summary_grams)
        return containment >= 0.84 and jaccard >= 0.58
    return False


def repeated_title_prefix_length(title: str, summary: str, site: str = "") -> int:
    title_norm = normalize_headline(title, site)
    summary_norm = normalize_headline(summary, site)
    if len(title_norm) < 10 or not summary_norm.startswith(title_norm):
        return 0

    normalized_prefix = ""
    for index, char in enumerate(summary):
        normalized_prefix += re.sub(
            r"[^a-z0-9ぁ-んァ-ヶ一-龯々〆ヵヶー]+",
            "",
            unicodedata.normalize("NFKC", char).casefold(),
        )
        if len(normalized_prefix) >= len(title_norm):
            return index + 1
    return 0


def sentence_repeats_title(title: str, sentence: str, site: str = "") -> bool:
    title_norm = normalize_headline(title, site)
    sentence_norm = normalize_headline(sentence, site)
    if len(sentence_norm) < 18 or not title_norm:
        return False
    shared_prefix = 0
    for left, right in zip(title_norm, sentence_norm):
        if left != right:
            break
        shared_prefix += 1
    return shared_prefix / min(len(title_norm), len(sentence_norm)) >= 0.8


def extract_explanation(title: str, candidates: list[str], site: str = "") -> str:
    for candidate in candidates:
        summary = clean_text(candidate, 1200)
        if not summary or not re.search(r"[ぁ-んァ-ヶ一-龯]", summary):
            continue
        if site:
            summary = re.sub(rf"\s*[-–—|｜]\s*{re.escape(site)}\s*$", "", summary, flags=re.IGNORECASE).strip()

        prefix_length = repeated_title_prefix_length(title, summary, site)
        if prefix_length:
            summary = re.sub(r"^[\s。、:：|｜–—-]+", "", summary[prefix_length:]).strip()
        if not summary:
            continue

        sentences = [part.strip() for part in re.split(r"(?<=[。！？])\s*", summary) if part.strip()]
        while len(sentences) > 1 and sentence_repeats_title(title, sentences[0], site):
            sentences.pop(0)
        summary = "".join(sentences)
        if summary_repeats_title(title, summary, site):
            continue
        if len(summary) < 30 or summary.endswith(("…", "...")):
            continue
        if re.match(r"^(?:「[^」]{1,40}」)?(?:は|とは).{0,120}(?:サイト|メディア|ブログ)", summary):
            continue

        return shorten_summary(summary, limit=220)
    return ""


def summary_points(summary: str) -> tuple[list[str], list[str | None]]:
    if not summary:
        return [], []
    sentences = [part.strip() for part in re.split(r"(?<=[。！？])\s*", summary) if part.strip()]
    point = sentences[0] if sentences else summary
    detail = "".join(sentences[1:]).strip()
    return [point], [detail or None]


def explain_importance(topic: str, title: str, summary: str) -> str:
    """Add a cautious, API-free reading guide based on the story's topic and wording."""
    text = unicodedata.normalize("NFKC", f"{title} {summary}")
    rules = {
        "金融・株式": [
            (r"金利|利上げ|利下げ|国債|利回り", "金利の見通しは、企業の借入コストや預金・住宅ローン、株価の評価に関わります。発表後に市場の予想がどう動くかが焦点です。"),
            (r"為替|円安|円高|ドル円|円相場", "為替は輸出企業の採算と輸入品の価格の両方に関わります。円の動きがどちらへ効くかを確認したい話題です。"),
            (r"日経|株価|株式|相場|指数|暴落|反発", "指数の見通しだけでなく、その根拠となる需給や企業業績も見ると、予想と実際の値動きを分けて考えられます。"),
            (r"決算|業績|利益|売上|配当", "実績と市場予想の差に加えて、会社が示す今後の見通しが株価の反応を見る手がかりです。"),
        ],
        "Steamの注目ゲーム": [
            (r"不具合|修正|障害|アップデート", "遊ぶ人にとっては、現在の不具合と修正状況、公式の回避策が、購入や再開を決める材料になります。"),
            (r"配信|発売|リリース|早期アクセス", "遊ぶか決めるときは、ゲームの特徴に加え、価格・対応機種・レビューを確認すると選びやすくなります。"),
            (r"無料|セール|価格|値下げ", "価格だけでなく、ゲーム内容やレビューも比べると、自分に合う作品か判断しやすくなります。"),
            (r"レビュー|評価|同時接続|ランキング", "人気の数字は注目度の目安です。実際の遊び方が自分の好みに合うかも、選ぶときのポイントです。"),
        ],
        "ゴルフ・バスケ": [
            (r"ドラフト|指名|エントリー|志望選手", "選手の登録と実際の指名は別の段階です。最終的にどのチームが指名するかが、次の注目点です。"),
            (r"首位|優勝|順位|決勝|プレーオフ|予選", "この結果が優勝争いや次の試合の条件にどうつながるかを見ると、大会での意味が分かります。"),
            (r"負傷|けが|怪我|復帰|離脱", "出場できる時期がチームの戦い方に関わるため、診断や復帰時期の続報が焦点です。"),
            (r"記録|連勝|連敗|スコア", "記録そのものに加え、順位や大会の流れにどうつながるかが見るポイントです。"),
        ],
        "日本の主要ニュース": [
            (r"需要.{0,30}供給|供給.{0,30}需要", "需要が伸びても供給側が追いつくかで、経済全体への効果は変わります。両方の動きが焦点です。"),
            (r"日銀|金利|物価|賃金|為替|円安", "物価や金利の動きが家計や企業にどこまで波及するかは、今後の政策や賃金の動きも見るポイントです。"),
            (r"法案|制度|政策|政府|改正|閣議|補助金", "制度の対象と始まる時期を押さえると、誰の暮らしや事業に関係するかが分かります。"),
            (r"地震|災害|大雨|台風|避難|事故", "被害の範囲と、交通・避難・支援に関する続報を確認すると、生活への影響をつかみやすくなります。"),
            (r"企業|工場|投資|雇用|事業", "実施時期や規模、地域や関係企業への広がりが、今後を見るポイントです。"),
        ],
    }
    for pattern, explanation in rules.get(topic, []):
        if re.search(pattern, text):
            return explanation
    if topic == "金融・株式":
        return "数字の増減だけでなく、その背景と今後の見通しを合わせて見ると、市場への影響を追いやすくなります。"
    if topic == "Steamの注目ゲーム":
        return "ゲームの特徴だけでなく、配信状況やプレイヤーの評価を確認すると、遊ぶか判断しやすくなります。"
    if topic == "ゴルフ・バスケ":
        return "選手の成績と大会・リーグでの位置づけを合わせると、このニュースの意味が見えやすくなります。"
    return "発表内容に加え、誰にどの範囲で関係するかを追うと、暮らしとのつながりを捉えやすくなります。"


def _pick(rng: random.Random, pool: list, used: set | None = None) -> str:
    options = [
        item for item in pool
        if isinstance(item, str) and item and (used is None or item not in used)
    ]
    if not options:
        # 使い切ったときは重複を許して選び直す（空の感想を出さないため）
        options = [item for item in pool if isinstance(item, str) and item]
    if not options:
        return ""
    choice = rng.choice(options)
    if used is not None:
        used.add(choice)
    return choice


def freetalk_lines(corners: dict, episode_date: date, rng: random.Random) -> list[dict]:
    """オープニングの短い雑談。記念日があればそれを、なければ季節か曜日の話題にする。"""
    if not corners:
        return []
    lines = ["ニュースの前に、少しだけ雑談の時間です。"]
    anniversary = (corners.get("anniversaries") or {}).get(episode_date.strftime("%m-%d"))
    if anniversary:
        lines.append(f"今日、{episode_date.month}月{episode_date.day}日は{anniversary}です。")
    else:
        seasonal = list((corners.get("seasonal") or {}).get(str(episode_date.month)) or [])
        weekday = list((corners.get("weekday") or {}).get(str(episode_date.weekday())) or [])
        pool = seasonal if episode_date.toordinal() % 2 == 0 else weekday
        topic_line = _pick(rng, pool) or _pick(rng, seasonal) or _pick(rng, weekday)
        if topic_line:
            lines.append(topic_line)
    followup = _pick(rng, list(corners.get("freetalk_followups") or []))
    if followup:
        lines.append(followup)
    return [script_line(text) for text in lines]


def reaction_for(
    corners: dict, topic: str, title: str, summary: str, rng: random.Random, used: set
) -> str:
    """ニュースへの短い感想。事実は足さず、パーソナリティの受け止めだけを返す。"""
    data = (corners.get("reactions") or {}).get(topic) or {}
    if not isinstance(data, dict):
        return ""
    text = f"{title} {summary}"
    for rule in data.get("rules") or []:
        if not isinstance(rule, dict):
            continue
        if any(str(marker) in text for marker in rule.get("match") or []):
            picked = _pick(rng, list(rule.get("lines") or []), used)
            if picked:
                return picked
    return _pick(rng, list(data.get("default") or []), used)


def build_quiz(
    stories_with_summaries: list[tuple[dict, str]],
) -> tuple[list[dict], list[dict]] | None:
    """今日のニュースの概要にある数字を1つ伏せてクイズにする。材料がなければ None。"""
    best: tuple[int, str, str, int, int, str] | None = None
    for story, summary in stories_with_summaries:
        if not summary:
            continue
        for sentence in (part.strip() for part in re.split(r"(?<=[。！？])\s*", summary)):
            if not 15 <= len(sentence) <= 110:
                continue
            for match in QUIZ_NUMBER_PATTERN.finditer(sentence):
                score = QUIZ_UNIT_SCORE.get(match.group(2), 1)
                if best is None or score > best[0]:
                    best = (
                        score,
                        str(story["topic"]),
                        sentence,
                        match.start(),
                        match.end(),
                        match.group(0).strip(),
                    )
    if best is None:
        return None
    _, topic, sentence, start, end, answer = best
    blanked = sentence[:start] + "○○" + sentence[end:]
    question = [
        script_line("ここで、今日のニュースからクイズです。"),
        script_line(f"{topic}のニュースから出題します。{blanked}この○○に入る数字は、いくつだったでしょう。"),
    ]
    answer_lines = [
        script_line(f"クイズの答え合わせです。答えは「{answer}」でした。"),
        script_line(f"元の文は、こうでした。{sentence}"),
    ]
    return question, answer_lines


def fortune_lines(corners: dict, rng: random.Random) -> list[dict]:
    """日替わりの運勢。日付で種を決める乱数から作るので、同じ日は同じ結果になる。"""
    fortunes = corners.get("fortunes") or []
    items = list(corners.get("lucky_items") or [])
    colors = list(corners.get("lucky_colors") or [])
    if not fortunes or not items or not colors:
        return []
    total = sum(int(weight) for _, weight in fortunes)
    roll = rng.randrange(total)
    fortune = str(fortunes[-1][0])
    for name, weight in fortunes:
        if roll < int(weight):
            fortune = str(name)
            break
        roll -= int(weight)
    lines = [
        "気軽に楽しむ、今日の運勢コーナーです。",
        f"今日の運勢は{fortune}。ラッキーアイテムは{_pick(rng, items)}、ラッキーカラーは{_pick(rng, colors)}です。",
    ]
    advice = _pick(rng, list(corners.get("fortune_advice") or []))
    if advice:
        lines.append(advice)
    return [script_line(text) for text in lines]


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
    corners = load_corners()
    rng = random.Random(episode_date.toordinal())
    used_reactions: set = set()
    quiz_sources: list[tuple[dict, str]] = []
    news = []
    for story in selected:
        title = clean_text(story["title"], 240).rstrip("。.!！?？")
        site = str(story.get("site") or "")
        if site:
            title = re.sub(rf"\s*[-–—|｜]\s*{re.escape(site)}\s*$", "", title, flags=re.IGNORECASE)
        title = title.rstrip("。.!！?？")
        summary_candidates = story.get("summary_candidates") or [story.get("description", "")]
        summary = extract_explanation(title, [str(value or "") for value in summary_candidates], site)
        importance = explain_importance(story["topic"], title, summary)
        reaction = reaction_for(corners, story["topic"], title, summary, rng, used_reactions)
        lines = [script_line(f"{story['topic']}。{title}。")]
        if summary:
            lines.append(script_line(f"記事の概要です。{summary}"))
        lines.append(script_line(f"ここが大事です。{importance}"))
        if reaction:
            lines.append(script_line(reaction))
        points, details = summary_points(summary)
        quiz_sources.append((story, summary))
        news.append({
            "story_id": story["id"],
            "label": title[:100],
            "importance": importance,
            "reaction": reaction,
            "points": points,
            "details": details,
            "lines": lines,
        })

    finance_index = next(
        (index for index, item in enumerate(news) if selected[index]["topic"] == "金融・株式"),
        0,
    )
    opening = [
        script_line(GREETINGS[episode_date.toordinal() % len(GREETINGS)].replace("ものラジオ", station_name)),
        script_line(f"{date_text}{weekday}曜日、今日は{len(news)}本のニュースをお伝えします。"),
    ]
    opening.extend(freetalk_lines(corners, episode_date, rng))

    # 締めは「クイズ出題 → 運勢（考える時間）→ 答え合わせ → 挨拶」の順に組む
    closing: list[dict] = []
    quiz = build_quiz(quiz_sources)
    if quiz:
        closing.extend(quiz[0])
    closing.extend(fortune_lines(corners, rng))
    if quiz:
        closing.extend(quiz[1])
    closing.append(script_line(f"以上、{station_name}でした。気になるニュースは、出典の記事もご確認ください。"))

    return {
        "title": f"{date_text}のニュース",
        "opening": opening,
        "news": news,
        "closing": closing,
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
        if not item.get("lines"):
            raise RuntimeError(f"ニュース「{item.get('label', '')}」に読み上げ文がありません")
        if len(item["details"]) != len(item["points"]):
            raise RuntimeError(f"ニュース「{item.get('label', '')}」の詳細と要点の数が一致しません")
        if not str(item.get("importance") or "").strip():
            raise RuntimeError(f"ニュース「{item.get('label', '')}」に注目点の説明がありません")
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
            "importance": item.get("importance", ""),
            "reaction": item.get("reaction", ""),
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
    parser.add_argument("--replace", action="store_true", help="同じ日付にある台本を新しい記事で作り直します")
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
    if episode_path.exists() and not args.replace:
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
