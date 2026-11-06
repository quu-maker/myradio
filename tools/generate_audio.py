#!/usr/bin/env python3
"""Render a Japanese episode to MP3 and write timings from the returned audio."""

from __future__ import annotations

import argparse
import base64
from datetime import datetime
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from typing import Any
from zoneinfo import ZoneInfo

import websocket


ROOT = Path(__file__).resolve().parents[1]
PREFERENCES = ROOT / "news-preferences.json"
EPISODES_INDEX = ROOT / "episodes.json"
SAMPLE_RATE = 24_000
BYTES_PER_SECOND = SAMPLE_RATE * 2  # mono signed 16-bit PCM
INITIAL_SILENCE_SECONDS = 0.6
LINE_GAP_SECONDS = 0.25
FINAL_SILENCE_SECONDS = 0.6


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def receive_event(ws: websocket.WebSocket, expected: set[str] | None = None) -> dict:
    deadline = time.monotonic() + 180
    while time.monotonic() < deadline:
        try:
            message = ws.recv()
        except websocket.WebSocketTimeoutException:
            continue
        if not message:
            raise RuntimeError("Realtime API との接続が応答前に閉じました")
        event = json.loads(message)
        event_type = event.get("type")
        if event_type == "error":
            detail = event.get("error", {}).get("message", "詳細不明")
            raise RuntimeError(f"Realtime API が音声生成を拒否しました: {detail}")
        if event_type == "response.done":
            status = event.get("response", {}).get("status")
            if status != "completed":
                raise RuntimeError(f"音声生成が完了しませんでした（status={status}）")
        if expected is None or event_type in expected:
            return event
    raise TimeoutError("Realtime API の応答が時間内に届きませんでした")


class SpeechSession:
    def __init__(self, api_key: str, preferences: dict) -> None:
        self.model = os.environ.get("OPENAI_SPEECH_MODEL", preferences.get("speech_model", "gpt-realtime-2.1-mini"))
        self.voice = preferences.get("speech_voice", "marin")
        self.instructions = preferences.get(
            "speech_instructions",
            "日本語の文を、入力どおりに読み上げてください。言葉を足したり省いたりしないでください。",
        )
        url = f"wss://api.openai.com/v1/realtime?model={self.model}"
        self.ws = websocket.create_connection(
            url,
            header=[f"Authorization: Bearer {api_key}"],
            timeout=30,
            enable_multithread=True,
        )
        receive_event(self.ws, {"session.created"})
        self.ws.send(json.dumps({
            "type": "session.update",
            "session": {
                "type": "realtime",
                "output_modalities": ["audio"],
                "audio": {
                    "output": {
                        "format": {"type": "audio/pcm", "rate": SAMPLE_RATE},
                        "voice": self.voice,
                    }
                },
                "instructions": self.instructions,
            },
        }))
        receive_event(self.ws, {"session.updated"})

    def render_line(self, text: str) -> bytes:
        self.ws.send(json.dumps({
            "type": "conversation.item.create",
            "item": {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": text}],
            },
        }))
        self.ws.send(json.dumps({
            "type": "response.create",
            "response": {"output_modalities": ["audio"]},
        }))
        audio_parts: list[bytes] = []
        while True:
            event = receive_event(self.ws)
            if event.get("type") == "response.output_audio.delta":
                audio_parts.append(base64.b64decode(event["delta"]))
            elif event.get("type") == "response.done":
                break
        audio = b"".join(audio_parts)
        if not audio or len(audio) % 2:
            raise RuntimeError("Realtime API から有効なPCM音声が届きませんでした")
        return audio

    def close(self) -> None:
        try:
            self.ws.close()
        except Exception:
            pass


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


def render_episode(episode: dict, episode_path: Path, preferences: dict, api_key: str) -> Path:
    lines = episode_lines(episode)
    if not lines:
        raise RuntimeError("台本に読み上げる文がありません")

    session = SpeechSession(api_key, preferences)
    pcm_parts: list[bytes] = []
    timeline = 0.0
    initial = round(INITIAL_SILENCE_SECONDS * BYTES_PER_SECOND)
    pcm_parts.append(bytes(initial))
    timeline += initial / BYTES_PER_SECOND

    lines_timed: list[dict] = []
    part_ranges: dict[int, list[float]] = {}
    try:
        for part, say, show in lines:
            if not say.strip() or not show.strip():
                raise RuntimeError("say と show が空の行があります")
            audio = session.render_line(say)
            duration = len(audio) / BYTES_PER_SECOND
            start = timeline
            end = start + duration
            pcm_parts.append(audio)
            lines_timed.append({"start": round(start, 3), "end": round(end, 3), "show": show, "part": part})
            if part not in part_ranges:
                part_ranges[part] = [start, end]
            else:
                part_ranges[part][1] = end
            timeline = end
            gap = round(LINE_GAP_SECONDS * BYTES_PER_SECOND)
            pcm_parts.append(bytes(gap))
            timeline += gap / BYTES_PER_SECOND
    finally:
        session.close()

    final_silence = round(FINAL_SILENCE_SECONDS * BYTES_PER_SECOND)
    pcm_parts.append(bytes(final_silence))
    seconds = timeline + final_silence / BYTES_PER_SECOND

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
    subprocess.run(
        [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
            "-f", "s16le", "-ar", str(SAMPLE_RATE), "-ac", "1", "-i", "pipe:0",
            "-codec:a", "libmp3lame", "-b:a", "64k", str(audio_path),
        ],
        input=pcm,
        check=True,
    )

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
    api_key = os.environ.get("OPENAI_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY が未設定です。GitHub Actions の Secrets に追加してください")
    preferences = read_json(PREFERENCES)
    episode, episode_path = choose_episode(args.date)
    audio_path = ROOT / "audio" / f"{episode['date']}-ja.mp3"
    if episode.get("lines_timed") and audio_path.exists():
        print(f"音声はすでにあります: {audio_path.relative_to(ROOT)}")
        return 0
    rendered = render_episode(episode, episode_path, preferences, api_key)
    print(f"音声を作成しました: {rendered.relative_to(ROOT)} ({episode['seconds']}秒)")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, KeyError, TypeError, ValueError, RuntimeError, TimeoutError, subprocess.CalledProcessError, websocket.WebSocketException) as exc:
        print(f"音声の作成を止めました: {exc}", file=sys.stderr)
        raise SystemExit(1)
