#!/usr/bin/env python3
"""Render a Japanese episode to MP3 and write timings from the returned audio."""

from __future__ import annotations

import argparse
from datetime import datetime
import json
import os
from pathlib import Path
import sys
from typing import Any
from zoneinfo import ZoneInfo

import lameenc
from piper import PiperVoice, SynthesisConfig


ROOT = Path(__file__).resolve().parents[1]
PREFERENCES = ROOT / "news-preferences.json"
EPISODES_INDEX = ROOT / "episodes.json"
INITIAL_SILENCE_SECONDS = 0.6
LINE_GAP_SECONDS = 0.25
FINAL_SILENCE_SECONDS = 0.6


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def episode_lines(episode: dict) -> list[tuple[int, str, str]]:
    flattened: list[tuple[int, str, str]] = []
    part = 0
    for line in episode.get("opening", []):
        flattened.append((part, line["say"], line["show"]))
    for news_index, news in enumerate(episode.get("news", [])):
        part = news_index + 1
        for line in news.get("lines", []):
            flattened.append((part, line["say"], line["show"]))
    part = len(episode.get("news", [])) + 1
    for line in episode.get("closing", []):
        flattened.append((part, line["say"], line["show"]))
    return flattened


def choose_episode(target_date: str | None) -> tuple[dict, Path]:
    index = read_json(EPISODES_INDEX)
    if not isinstance(index, list):
        raise RuntimeError("episodes.json は配列である必要があります")
    if target_date:
        dates = [target_date]
    else:
        dates = [entry["date"] for entry in index if isinstance(entry, dict) and entry.get("ja", {}).get("title")]
    for date_text in dates:
        path = ROOT / "episodes" / f"{date_text}-ja.json"
        if not path.exists():
            continue
        episode = read_json(path)
        if episode.get("format") == "monoradio/news-1" and episode.get("lang") == "ja":
            return episode, path
    raise RuntimeError("音声化できる日本語のエピソードが見つかりません")


def update_index(date_text: str, title: str, seconds: float) -> None:
    index = read_json(EPISODES_INDEX)
    for entry in index:
        if entry.get("date") == date_text:
            entry.setdefault("ja", {})["title"] = title
            entry["ja"]["seconds"] = round(seconds, 3)
            break
    write_json(EPISODES_INDEX, index)


def render_episode(episode: dict, episode_path: Path, preferences: dict) -> Path:
    lines = episode_lines(episode)
    if not lines:
        raise RuntimeError("台本に読み上げる文がありません")

    model_path = Path(os.environ.get("PIPER_MODEL_PATH", ""))
    if not model_path.is_file():
        raise RuntimeError("Piperの日本語音声モデルがありません。GitHub Actionsの音声準備を確認してください")
    voice = PiperVoice.load(model_path)
    sample_rate = int(voice.config.sample_rate)
    sample_width = 2
    channels = 1
    bytes_per_second = sample_rate * sample_width * channels
    synthesis = SynthesisConfig(speaker_id=int(preferences.get("piper_speaker", 0)))

    pcm_parts: list[bytes] = []
    timeline = 0.0
    initial = round(INITIAL_SILENCE_SECONDS * sample_rate) * sample_width * channels
    pcm_parts.append(bytes(initial))
    timeline += initial / bytes_per_second

    lines_timed: list[dict] = []
    part_ranges: dict[int, list[float]] = {}
    for part, say, show in lines:
        if not say.strip() or not show.strip():
            raise RuntimeError("say と show が空の行があります")
        chunks = list(voice.synthesize(say, synthesis))
        if not chunks:
            raise RuntimeError("Piperから音声が返りませんでした")
        if any(
            chunk.sample_rate != sample_rate
            or chunk.sample_width != sample_width
            or chunk.sample_channels != channels
            for chunk in chunks
        ):
            raise RuntimeError("Piper音声の形式が回の途中で変わりました")
        audio = b"".join(chunk.audio_int16_bytes for chunk in chunks)
        if not audio:
            raise RuntimeError("Piperから空の音声が返りました")
        duration = len(audio) / bytes_per_second
        start = timeline
        end = start + duration
        pcm_parts.append(audio)
        lines_timed.append({"start": round(start, 3), "end": round(end, 3), "show": show, "part": part})
        if part not in part_ranges:
            part_ranges[part] = [start, end]
        else:
            part_ranges[part][1] = end
        timeline = end
        gap = round(LINE_GAP_SECONDS * sample_rate) * sample_width * channels
        pcm_parts.append(bytes(gap))
        timeline += gap / bytes_per_second

    final_silence = round(FINAL_SILENCE_SECONDS * sample_rate) * sample_width * channels
    pcm_parts.append(bytes(final_silence))
    seconds = timeline + final_silence / bytes_per_second

    segments: list[dict] = []
    news_count = len(episode.get("news", []))
    for part in range(news_count + 2):
        bounds = part_ranges.get(part)
        if not bounds:
            continue
        start = bounds[0]
        next_bounds = part_ranges.get(part + 1)
        end = next_bounds[0] if next_bounds else seconds
        if part == 0:
            segments.append({"kind": "opening", "index": None, "start": round(start, 3), "end": round(end, 3)})
        elif part == news_count + 1:
            segments.append({"kind": "closing", "index": None, "start": round(start, 3), "end": round(end, 3)})
        else:
            segments.append({"kind": "news", "index": part - 1, "start": round(start, 3), "end": round(end, 3)})

    pcm = b"".join(pcm_parts)
    date_text = episode["date"]
    audio_dir = ROOT / "audio"
    audio_dir.mkdir(parents=True, exist_ok=True)
    audio_path = audio_dir / f"{date_text}-ja.mp3"
    encoder = lameenc.Encoder()
    encoder.set_in_sample_rate(sample_rate)
    encoder.set_channels(channels)
    encoder.set_bit_rate(64)
    encoder.set_quality(2)
    with audio_path.open("wb") as audio_file:
        audio_file.write(encoder.encode(pcm))
        audio_file.write(encoder.flush())

    episode["seconds"] = round(seconds, 3)
    episode["lines_timed"] = lines_timed
    episode["segments"] = segments
    episode["made_at"] = datetime.now(ZoneInfo(preferences.get("timezone", "Asia/Tokyo"))).isoformat(timespec="seconds")
    write_json(episode_path, episode)
    update_index(date_text, episode["title"], seconds)
    return audio_path


def main() -> int:
    parser = argparse.ArgumentParser(description="日本語の台本から再生音声と字幕タイミングを作ります")
    parser.add_argument("--date", help="音声化する日付（YYYY-MM-DD）。省略すると最新の日本語回です")
    args = parser.parse_args()
    preferences = read_json(PREFERENCES)
    episode, episode_path = choose_episode(args.date)
    audio_path = ROOT / "audio" / f"{episode['date']}-ja.mp3"
    if episode.get("lines_timed") and audio_path.exists():
        print(f"音声はすでにあります: {audio_path.relative_to(ROOT)}")
        return 0
    rendered = render_episode(episode, episode_path, preferences)
    print(f"音声を作成しました: {rendered.relative_to(ROOT)} ({episode['seconds']}秒)")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, KeyError, TypeError, ValueError, RuntimeError, TimeoutError) as exc:
        print(f"音声の作成を止めました: {exc}", file=sys.stderr)
        raise SystemExit(1)
