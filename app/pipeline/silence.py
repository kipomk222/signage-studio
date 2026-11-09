"""ステップ5: 無音カット（ジャンプカット）。

話していない区間を見つけて詰める。発話の前後には pad 秒だけ余白を残す。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from app.pipeline.ffmpeg import duration_of, ffmpeg_base, run
from app.pipeline.normalize import audio_args_for, exact_length_filters, frame_count
from app.platform import PlatformProfile

Range = tuple[float, float]


@dataclass(frozen=True)
class SilenceParams:
    noise_db: float = -35.0      # これより小さい音を無音とみなす
    min_silence: float = 0.4     # この秒数以上続く無音だけを切る
    pad: float = 0.15            # 発話の前後に残す秒数


# 画面の「無音カットの強さ」と対応
STRENGTH = {
    "weak": SilenceParams(-40.0, 0.6, 0.20),
    "normal": SilenceParams(-35.0, 0.4, 0.15),
    "strong": SilenceParams(-32.0, 0.3, 0.12),
}
TIGHTEST_MIN_SILENCE = 0.3   # 目標尺を超えたときに詰める下限
OVER_CUT_RATIO = 0.5         # 元の半分以下になったら「切りすぎ注意」
MIN_PIECE = 0.2              # これより短い断片は捨てる


@dataclass
class CutResult:
    src: Path
    dst: Path
    original: float
    kept: float
    ranges: list[Range]
    no_speech: bool = False  # 話し声が見つからず、そのまま残した

    @property
    def over_cut(self) -> bool:
        return not self.no_speech and self.kept < self.original * OVER_CUT_RATIO


_START = re.compile(r"silence_start:\s*(-?[\d.]+)")
_END = re.compile(r"silence_end:\s*(-?[\d.]+)")


def parse_silences(stderr: str, duration: float) -> list[Range]:
    silences: list[Range] = []
    start: float | None = None
    for line in stderr.splitlines():
        if (m := _START.search(line)):
            start = max(0.0, float(m.group(1)))
        elif (m := _END.search(line)) and start is not None:
            silences.append((start, min(duration, float(m.group(1)))))
            start = None
    if start is not None:  # 最後まで無音のまま終わった
        silences.append((start, duration))
    return silences


def detect_silences(profile: PlatformProfile, path: Path, params: SilenceParams, duration: float) -> list[Range]:
    result = run(
        [*ffmpeg_base(profile.ffmpeg), "-i", str(path), "-vn",
         "-af", f"silencedetect=n={params.noise_db}dB:d={params.min_silence}", "-f", "null", "-"],
        what=f"「{path.name}」の無音部分を調べられませんでした。",
    )
    return parse_silences(result.stderr, duration)


def keep_ranges(silences: list[Range], duration: float, pad: float) -> list[Range]:
    """無音区間の外側（＝話している区間）に余白を足し、重なりをまとめて返す。"""
    speech: list[Range] = []
    cursor = 0.0
    for s, e in sorted(silences):
        if s > cursor:
            speech.append((cursor, s))
        cursor = max(cursor, e)
    if cursor < duration:
        speech.append((cursor, duration))

    padded = [(max(0.0, s - pad), min(duration, e + pad)) for s, e in speech]
    merged: list[Range] = []
    for s, e in padded:
        if merged and s <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], e))
        else:
            merged.append((s, e))
    return [(round(s, 3), round(e, 3)) for s, e in merged if e - s >= MIN_PIECE]


def render_ranges(profile: PlatformProfile, src: Path, dst: Path, ranges: list[Range]) -> None:
    """残す範囲だけをつないだ動画を作る。長さはコマ数ぴったりにそろえる。"""
    frames = frame_count(sum(e - s for s, e in ranges))
    v_end, a_end = exact_length_filters(frames)
    parts, labels = [], []
    for i, (s, e) in enumerate(ranges):
        parts.append(f"[0:v]trim=start={s}:end={e},setpts=PTS-STARTPTS[v{i}]")
        parts.append(f"[0:a]atrim=start={s}:end={e},asetpts=PTS-STARTPTS[a{i}]")
        labels.append(f"[v{i}][a{i}]")
    graph = (";".join(parts) + f";{''.join(labels)}concat=n={len(ranges)}:v=1:a=1[cv][ca];"
             f"[cv]{v_end}[v];[ca]{a_end}[a]")
    run(
        [*ffmpeg_base(profile.ffmpeg), "-i", str(src), "-filter_complex", graph,
         "-map", "[v]", "-map", "[a]", "-frames:v", str(frames), *profile.video_args("work"),
         *audio_args_for(dst), str(dst)],
        what=f"「{src.name}」の無音部分を詰められませんでした。",
        output=dst,
    )


def jumpcut(profile: PlatformProfile, src: Path, dst: Path, params: SilenceParams) -> CutResult:
    dst.parent.mkdir(parents=True, exist_ok=True)
    duration = duration_of(profile.ffprobe, src)
    ranges = keep_ranges(detect_silences(profile, src, params, duration), duration, params.pad)
    no_speech = not ranges
    if no_speech:  # 話し声のない映像（実演カットなど）はそのまま使う
        ranges = [(0.0, round(duration, 3))]
    render_ranges(profile, src, dst, ranges)
    kept = sum(e - s for s, e in ranges)
    return CutResult(src=src, dst=dst, original=duration, kept=kept, ranges=ranges, no_speech=no_speech)
