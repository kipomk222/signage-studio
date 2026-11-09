"""ステップ11: BGM ミックス。

BGM をループさせ、話している間は自動で音量を下げる（ダッキング）。
冒頭2秒でフェードイン、末尾3秒でフェードアウト。
"""

from __future__ import annotations

import shutil
from pathlib import Path

from app.pipeline.ffmpeg import duration_of, ffmpeg_base, run
from app.pipeline.normalize import AUDIO_ARGS, SAMPLE_RATE
from app.platform import PlatformProfile

AUDIO_EXTS = {".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg"}
LEVELS = {"small": 0.10, "medium": 0.15, "large": 0.22}  # 画面の「小・中・大」
FADE_IN, FADE_OUT = 2.0, 3.0


def find_bgm_files(folder: Path) -> list[Path]:
    folder = Path(folder)
    if not folder.exists():
        return []
    return sorted(p for p in folder.iterdir() if p.is_file() and p.suffix.lower() in AUDIO_EXTS)


def build_graph(duration: float, volume: float) -> str:
    fade_out_start = max(0.0, duration - FADE_OUT)
    return ";".join([
        "[0:a]asplit=2[voice][key]",
        (f"[1:a]aresample={SAMPLE_RATE},aformat=channel_layouts=stereo,volume={volume},"
         f"atrim=0:{duration:.3f},asetpts=PTS-STARTPTS,"
         f"afade=t=in:d={FADE_IN},afade=t=out:st={fade_out_start:.3f}:d={FADE_OUT}[bgm]"),
        "[bgm][key]sidechaincompress=threshold=0.05:ratio=8:attack=200:release=1000[duck]",
        "[voice][duck]amix=inputs=2:duration=first:dropout_transition=0:normalize=0[a]",
    ])


def mix_bgm(profile: PlatformProfile, video: Path, bgm: Path | None, dst: Path, level: str = "medium") -> Path:
    dst.parent.mkdir(parents=True, exist_ok=True)
    if bgm is None:
        shutil.copy2(video, dst)
        return dst
    duration = duration_of(profile.ffprobe, video)
    run(
        [*ffmpeg_base(profile.ffmpeg), "-i", str(video), "-stream_loop", "-1", "-i", str(bgm),
         "-filter_complex", build_graph(duration, LEVELS[level]),
         "-map", "0:v", "-map", "[a]", "-c:v", "copy", *AUDIO_ARGS, "-t", f"{duration:.3f}", str(dst)],
        what="BGM を重ねられませんでした。",
        how="BGM のファイルが再生できるか確認してください。",
        output=dst,
    )
    return dst
