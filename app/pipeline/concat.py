"""ステップ6: 結合。クリップの間に短いクロスフェードを入れてつなぐ。"""

from __future__ import annotations

import shutil
from pathlib import Path

from app.pipeline.ffmpeg import ffmpeg_base, run, video_duration
from app.pipeline.normalize import AUDIO_ARGS
from app.platform import PlatformProfile

FADE = 0.3


def xfade_offsets(durations: list[float], fade: float) -> list[float]:
    """k 本目のつなぎ目（0始まり）が始まる時刻。"""
    offsets, total = [], 0.0
    for k, d in enumerate(durations[:-1]):
        total += d
        offsets.append(round(total - (k + 1) * fade, 3))
    return offsets


def build_graph(durations: list[float], fade: float) -> tuple[str, str, str]:
    """filter_complex の文字列と、最後の映像・音声ラベルを返す。"""
    parts: list[str] = []
    v_prev, a_prev = "[0:v]", "[0:a]"
    for k, offset in enumerate(xfade_offsets(durations, fade)):
        v_out, a_out = f"[v{k + 1}]", f"[a{k + 1}]"
        parts.append(f"{v_prev}[{k + 1}:v]xfade=transition=fade:duration={fade}:offset={offset}{v_out}")
        parts.append(f"{a_prev}[{k + 1}:a]acrossfade=d={fade}{a_out}")
        v_prev, a_prev = v_out, a_out
    return ";".join(parts), v_prev, a_prev


def concat_clips(profile: PlatformProfile, clips: list[Path], dst: Path, fade: float = FADE) -> float:
    """つないだ動画を dst に書き出し、その長さ（秒）を返す。"""
    if not clips:
        raise ValueError("no clips")
    dst.parent.mkdir(parents=True, exist_ok=True)
    if len(clips) == 1:
        if clips[0].suffix.lower() == dst.suffix.lower():
            shutil.copy2(clips[0], dst)
        else:  # 途中のピース（.mov）1本だけ: 形式をそろえる
            run([*ffmpeg_base(profile.ffmpeg), "-i", str(clips[0]), "-c:v", "copy", *AUDIO_ARGS, str(dst)],
                what="クリップをつなげられませんでした。", output=dst)
        return video_duration(profile.ffprobe, dst)

    # 声・テロップの時刻と同じ測り方（映像の長さ）で、つなぐ位置を決める
    durations = [video_duration(profile.ffprobe, c) for c in clips]
    graph, v, a = build_graph(durations, fade)
    args = [*ffmpeg_base(profile.ffmpeg)]
    for c in clips:
        args += ["-i", str(c)]
    args += ["-filter_complex", graph, "-map", v, "-map", a,
             *profile.video_args("work"), *AUDIO_ARGS, str(dst)]
    run(args, what="クリップをつなげられませんでした。", output=dst)
    return video_duration(profile.ffprobe, dst)
