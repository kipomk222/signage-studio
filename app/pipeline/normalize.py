"""ステップ2: 正規化。全クリップを 1920×1080・30fps・音声 48kHz ステレオにそろえる。"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from app.pipeline.ffmpeg import ffmpeg_base, run
from app.pipeline.inventory import ClipInfo
from app.platform import PlatformProfile

WIDTH, HEIGHT, FPS, SAMPLE_RATE = 1920, 1080, 30, 48000
AUDIO_ARGS = ["-c:a", "aac", "-b:a", "192k", "-ar", str(SAMPLE_RATE), "-ac", "2"]

FitMode = Literal["pad", "crop"]


def video_filter(fit: FitMode) -> str:
    if fit == "crop":
        size = f"scale={WIDTH}:{HEIGHT}:force_original_aspect_ratio=increase,crop={WIDTH}:{HEIGHT}"
    else:  # 黒帯
        size = (f"scale={WIDTH}:{HEIGHT}:force_original_aspect_ratio=decrease,"
                f"pad={WIDTH}:{HEIGHT}:(ow-iw)/2:(oh-ih)/2:color=black")
    return f"{size},setsar=1,fps={FPS},format=yuv420p"


def normalize_clip(profile: PlatformProfile, clip: ClipInfo, dst: Path, fit: FitMode = "pad") -> Path:
    dst.parent.mkdir(parents=True, exist_ok=True)
    args = [*ffmpeg_base(profile.ffmpeg), "-i", str(clip.path)]
    if not clip.has_audio:  # 音のないクリップには無音を足して、結合できるようにする
        args += ["-f", "lavfi", "-i", f"anullsrc=r={SAMPLE_RATE}:cl=stereo"]
        args += ["-map", "0:v:0", "-map", "1:a:0", "-shortest"]
    else:
        args += ["-map", "0:v:0", "-map", "0:a:0"]
    args += ["-vf", video_filter(fit), *profile.video_args("work"), *AUDIO_ARGS, str(dst)]
    run(args, what=f"「{clip.path.name}」の形式をそろえられませんでした。")
    return dst
