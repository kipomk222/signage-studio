"""ステップ12: 書き出し。用途ごとの音量基準にそろえ、完成動画を作る。"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

from app.pipeline.ffmpeg import PipelineError, duration_of, ffmpeg_base, run
from app.pipeline.normalize import SAMPLE_RATE
from app.platform import PlatformProfile


@dataclass(frozen=True)
class Target:
    key: str
    label: str
    lufs: float
    true_peak: float
    lra: float = 11.0


TARGETS = {
    "store": Target("store", "店頭用", -16.0, -1.5),
    "youtube": Target("youtube", "YouTube用", -14.0, -1.0),
}


@dataclass
class ExportResult:
    target: Target
    path: Path
    duration: float
    size_bytes: int
    lufs: float
    true_peak: float

    def summary(self) -> str:
        m, s = divmod(round(self.duration), 60)
        mb = self.size_bytes / 1_000_000
        return (f"{self.target.label}: {m}分{s:02d}秒 / {mb:.1f}MB / "
                f"音量 {self.lufs:.1f} LUFS（目標 {self.target.lufs:.0f}）・ピーク {self.true_peak:.1f} dBTP")


def _last_json(text: str) -> dict:
    start = text.rfind("{")
    end = text.rfind("}")
    if start < 0 or end < start:
        raise PipelineError("音量を測れませんでした。", "もう一度お試しください。", text[-2000:])
    return json.loads(text[start:end + 1])


def measure_loudnorm(profile: PlatformProfile, src: Path, t: Target) -> dict:
    result = run(
        [*ffmpeg_base(profile.ffmpeg), "-i", str(src), "-vn",
         "-af", f"loudnorm=I={t.lufs}:TP={t.true_peak}:LRA={t.lra}:print_format=json", "-f", "null", "-"],
        what="音量を測れませんでした。",
    )
    return _last_json(result.stderr)


def measure_output(profile: PlatformProfile, path: Path) -> tuple[float, float]:
    """書き出した動画の実測（統合ラウドネス, トゥルーピーク）。"""
    result = run(
        [*ffmpeg_base(profile.ffmpeg), "-i", str(path), "-vn", "-af", "ebur128=peak=true", "-f", "null", "-"],
        what="完成した動画の音量を測れませんでした。",
    )
    summary = result.stderr[result.stderr.rfind("Summary:"):]
    i = re.search(r"I:\s*(-?[\d.]+|-inf) LUFS", summary)
    p = re.search(r"Peak:\s*(-?[\d.]+|-inf) dBFS", summary)
    to_f = lambda m: -120.0 if (m is None or m.group(1) == "-inf") else float(m.group(1))  # noqa: E731
    return to_f(i), to_f(p)


def export_video(profile: PlatformProfile, src: Path, out_dir: Path, base_name: str, target_key: str) -> ExportResult:
    t = TARGETS[target_key]
    out_dir.mkdir(parents=True, exist_ok=True)
    dst = out_dir / f"{base_name}_{t.label}.mp4"
    m = measure_loudnorm(profile, src, t)
    loudnorm = (f"loudnorm=I={t.lufs}:TP={t.true_peak}:LRA={t.lra}"
                f":measured_I={m['input_i']}:measured_TP={m['input_tp']}"
                f":measured_LRA={m['input_lra']}:measured_thresh={m['input_thresh']}"
                f":offset={m['target_offset']}:linear=true,aresample={SAMPLE_RATE}")
    run(
        [*ffmpeg_base(profile.ffmpeg), "-i", str(src), "-map", "0:v:0", "-map", "0:a:0",
         *profile.video_args("final"), "-af", loudnorm,
         "-c:a", "aac", "-b:a", "192k", "-ar", str(SAMPLE_RATE), "-ac", "2",
         "-movflags", "+faststart", str(dst)],
        what=f"{t.label}の動画を書き出せませんでした。",
        how="保存先の空き容量を確認して、もう一度お試しください。",
    )
    lufs, peak = measure_output(profile, dst)
    return ExportResult(target=t, path=dst, duration=duration_of(profile.ffprobe, dst),
                        size_bytes=dst.stat().st_size, lufs=lufs, true_peak=peak)
