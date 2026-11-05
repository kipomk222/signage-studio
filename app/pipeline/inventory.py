"""ステップ1: 素材の仕分け。

各クリップの長さ・向き・音量を調べ、除外候補に理由を付ける。ファイルは消さない。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from app.pipeline.ffmpeg import ffmpeg_base, probe, run
from app.platform import PlatformProfile

VIDEO_EXTS = {".mp4", ".mov", ".m4v", ".mts", ".m2ts", ".avi", ".mkv", ".3gp"}
MIN_SECONDS = 3.0
SILENT_DB = -50.0


@dataclass
class ClipInfo:
    path: Path
    duration: float
    width: int  # 回転を反映した見た目の幅
    height: int
    fps: float
    has_audio: bool
    mean_db: float | None
    size_bytes: int
    exclude_reasons: list[str] = field(default_factory=list)
    silent: bool = False  # 話し声がない（実演・手元アップなど、差し込みに使う映像）

    @property
    def vertical(self) -> bool:
        return self.height > self.width

    @property
    def excluded(self) -> bool:
        return bool(self.exclude_reasons)


def natural_key(path: Path) -> list:
    """「2.mp4」が「10.mp4」より先に来る並べ方。"""
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", path.name)]


def list_videos(folder: Path) -> list[Path]:
    return sorted(
        (p for p in Path(folder).iterdir() if p.is_file() and p.suffix.lower() in VIDEO_EXTS
         and not p.name.startswith(".")),
        key=natural_key,
    )


def _rotation(stream: dict) -> int:
    rotate = stream.get("tags", {}).get("rotate")
    if rotate is not None:
        return int(float(rotate)) % 360
    for side in stream.get("side_data_list", []):
        if "rotation" in side:
            return int(float(side["rotation"])) % 360
    return 0


def _fps(stream: dict) -> float:
    num, _, den = stream.get("avg_frame_rate", "0/1").partition("/")
    try:
        return float(num) / float(den or 1) if float(den or 1) else 0.0
    except ValueError:
        return 0.0


def mean_volume(profile: PlatformProfile, path: Path) -> float | None:
    result = run(
        [*ffmpeg_base(profile.ffmpeg), "-i", str(path), "-vn", "-af", "volumedetect", "-f", "null", "-"],
        what=f"「{path.name}」の音を調べられませんでした。",
    )
    m = re.search(r"mean_volume:\s*(-?[\d.]+|-inf) dB", result.stderr)
    if not m:
        return None
    return -120.0 if m.group(1) == "-inf" else float(m.group(1))


def probe_clip(profile: PlatformProfile, path: Path) -> ClipInfo:
    info = probe(profile.ffprobe, path)
    video = next((s for s in info["streams"] if s.get("codec_type") == "video"), None)
    if video is None:
        raise ValueError("no video stream")
    has_audio = any(s.get("codec_type") == "audio" for s in info["streams"])
    w, h = int(video["width"]), int(video["height"])
    if _rotation(video) in (90, 270):
        w, h = h, w
    return ClipInfo(
        path=Path(path),
        duration=float(info["format"].get("duration") or video.get("duration") or 0),
        width=w,
        height=h,
        fps=_fps(video),
        has_audio=has_audio,
        mean_db=mean_volume(profile, path) if has_audio else None,
        size_bytes=Path(path).stat().st_size,
    )


def scan_folder(profile: PlatformProfile, folder: Path) -> list[ClipInfo]:
    """素材フォルダを調べ、除外候補に理由を付けて返す（名前順）。"""
    clips: list[ClipInfo] = []
    seen: dict[tuple[int, float], ClipInfo] = {}
    for path in list_videos(folder):
        try:
            clip = probe_clip(profile, path)
        except Exception:  # 壊れたファイルは除外候補として残す
            clip = ClipInfo(path=path, duration=0, width=0, height=0, fps=0,
                            has_audio=False, mean_db=None, size_bytes=path.stat().st_size,
                            exclude_reasons=["動画として読み込めませんでした"])
            clips.append(clip)
            continue
        if clip.duration < MIN_SECONDS:
            clip.exclude_reasons.append(f"{MIN_SECONDS:.0f}秒未満の短いクリップです")
        if not clip.has_audio or (clip.mean_db is not None and clip.mean_db <= SILENT_DB):
            clip.silent = True  # 除外はしない（差し込み用の映像として使う）
        key = (clip.size_bytes, round(clip.duration, 2))
        if key in seen:
            clip.exclude_reasons.append(f"「{seen[key].path.name}」と同じ内容のようです")
        else:
            seen[key] = clip
        clips.append(clip)
    return clips
