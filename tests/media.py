"""テスト用の試験動画を FFmpeg で作る。

話し声の代わりに、決まった時間だけ鳴る音を入れる。無音区間の位置が分かっているので、
無音カットの結果を数値で確かめられる。
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

# 8秒のクリップで「話している」区間（合計 4.8 秒）
SPEECH = [(0.5, 2.5), (3.5, 5.0), (6.2, 7.5)]
# 2本目は話す位置を変える（合計 6.3 秒）
SPEECH2 = [(0.3, 3.0), (4.0, 7.6)]


def _speech_expr(segments: list[tuple[float, float]]) -> str:
    gate = "+".join(f"between(t,{a},{b})" for a, b in segments)
    # 声の代わりに 220Hz と 330Hz を混ぜた音。区間外はごく小さなノイズ（約 -66dB）
    return f"0.35*(sin(2*PI*220*t)+0.6*sin(2*PI*330*t))*({gate})+0.0005*(random(0)-0.5)"


def _run(args: list[str]) -> None:
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", *args], check=True)


def make_clip(path: Path, seconds: float, size: str = "640x360",
              speech: list[tuple[float, float]] | None = None, audio: bool = True) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    args = ["-f", "lavfi", "-i", f"testsrc2=size={size}:rate=30:duration={seconds}"]
    if audio:
        expr = _speech_expr(speech) if speech else "0"
        args += ["-f", "lavfi", "-i", f"aevalsrc=exprs='{expr}':s=44100:d={seconds}"]
    args += ["-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p"]
    if audio:
        args += ["-c:a", "aac", "-shortest"]
    _run(args + [str(path)])
    return path


def make_bgm(path: Path, seconds: float = 6.0, freqs: tuple[int, ...] = (262, 330, 392)) -> Path:
    """BGM の代わり。一定の音量で鳴り続ける和音。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    expr = "0.2*(" + "+".join(f"sin(2*PI*{f}*t)" for f in freqs) + ")"
    _run(["-f", "lavfi", "-i", f"aevalsrc=exprs='{expr}':s=44100:d={seconds}", "-c:a", "aac", str(path)])
    return path


def make_sample_folder(folder: Path) -> dict[str, Path]:
    """素材フォルダ一式。名前順に並ぶよう番号を付ける。"""
    clips = {
        "talk1": make_clip(folder / "01_talk.mp4", 8, speech=SPEECH),
        "vertical": make_clip(folder / "02_vertical.mp4", 8, size="360x640", speech=SPEECH),
        "short": make_clip(folder / "03_short.mp4", 2, speech=[(0.2, 1.8)]),
        "silent": make_clip(folder / "04_silent.mp4", 5, speech=None),
        "talk2": make_clip(folder / "05_talk.mp4", 8, speech=SPEECH2),
    }
    dup = folder / "06_copy_of_talk1.mp4"
    shutil.copy2(clips["talk1"], dup)
    clips["duplicate"] = dup
    (folder / "メモ.txt").write_text("動画以外のファイルは無視される", encoding="utf-8")
    return clips
