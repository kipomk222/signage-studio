"""別録りの声: 前後の無音を切る・音量をそろえる・ファイル名から場面を当てる・録り直しから1つ選ぶ。"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path

from app.pipeline.ffmpeg import duration_of, ffmpeg_base, run
from app.pipeline.silence import SilenceParams, detect_silences
from app.platform import PlatformProfile
from app.project import Scene

AUDIO_EXTS = {".m4a", ".mp3", ".wav", ".aac", ".aif", ".aiff", ".caf", ".flac", ".ogg", ".opus"}
EDGE = SilenceParams(noise_db=-40, min_silence=0.25, pad=0.12)
VOICE_LUFS = -16


@dataclass
class VoiceInfo:
    duration: float      # 切ったあとの長さ
    trim_start: float
    trim_end: float


def edge_trim(profile: PlatformProfile, src: Path) -> VoiceInfo:
    """前後の無音の位置を調べる（中の間はそのまま。読み方の間は残す）。"""
    total = duration_of(profile.ffprobe, src)
    silences = detect_silences(profile, src, EDGE, total)
    start, end = 0.0, total
    for s, e in sorted(silences):
        if s <= 0.05:
            start = max(start, e)
        if e >= total - 0.05:
            end = min(end, s)
    start, end = max(0.0, start - EDGE.pad), min(total, end + EDGE.pad)
    if end - start < 0.3:   # ほぼ無音: 切らない
        start, end = 0.0, total
    return VoiceInfo(round(end - start, 3), round(start, 3), round(end, 3))


def prepare_voice(profile: PlatformProfile, src: Path, dst: Path, info: VoiceInfo) -> Path:
    """前後を切り、音量をそろえた WAV（48kHz・2ch）を作る。"""
    dst.parent.mkdir(parents=True, exist_ok=True)
    part = dst.with_name(dst.stem + ".part.wav")
    run([*ffmpeg_base(profile.ffmpeg), "-i", str(src), "-vn",
         "-af", f"atrim=start={info.trim_start}:end={info.trim_end},asetpts=PTS-STARTPTS,"
                f"loudnorm=I={VOICE_LUFS}:TP=-2:LRA=11,aresample=48000",
         "-ac", "2", "-c:a", "pcm_s16le", str(part)],
        what=f"声のファイル「{src.name}」を読み込めませんでした。")
    part.replace(dst)
    return dst


def _norm(text: str) -> str:
    return unicodedata.normalize("NFKC", text).lower()


def scene_from_name(name: str, scenes: list[Scene]) -> Scene | None:
    """ファイル名から場面が分かれば返す（例「03_軽さ.m4a」「場面3.m4a」「軽さ.m4a」）。

    録音アプリが付けた名前（「新規録音 3」など）の数字は場面番号と見なさない。
    番号は構成の場面の番号（1から、あいさつも数える）。
    """
    stem = _norm(Path(name).stem).strip()
    m = re.match(r"^(\d{1,2})(?:[\s_\-.・]|$)", stem) or re.search(r"(?:場面|シーン|scene)\s*(\d{1,2})", stem)
    if m:
        n = int(m.group(1))
        if 1 <= n <= len(scenes) and scenes[n - 1].voice == "narration":
            return scenes[n - 1]
    hits = [s for s in scenes if s.voice == "narration" and len(_norm(s.title)) >= 2 and _norm(s.title) in stem]
    return hits[0] if len(hits) == 1 else None


def script_match(transcript: str, script: str) -> float:
    """録った声の文字起こしが、台本にどれだけ合うか（0〜1）。"""
    a = re.sub(r"[\s、。，．・「」！？!?]", "", _norm(transcript))
    b = re.sub(r"[\s、。，．・「」！？!?]", "", _norm(script))
    if not a or not b:
        return 0.0
    return SequenceMatcher(None, a, b, autojunk=False).ratio()


def recording_name(index: int, scene: Scene) -> str:
    """録音用の台本に書く、おすすめのファイル名（この名前なら確実に振り分けられる）。"""
    title = re.sub(r'[\\/:*?"<>|\s]+', "", scene.title) or "場面"
    return f"{index:02d}_{title}"
