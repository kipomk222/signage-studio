"""Claude Code に素材の中身を伝えるための材料: クリップごとの映像の見本（コンタクトシート）。

2秒ほどおきに静止画を取り出し、時刻を書き込んで1枚にまとめる。
Claude Code はこれを見て、何が映っているか（話している人・手元アップ・実演など）を判断する。
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from app.pipeline.ffmpeg import ffmpeg_base, run
from app.pipeline.fonts import FONTS_DIR
from app.platform import PlatformProfile
from app.fileops import replace_file

TILE_W, TILE_H, COLS = 384, 216, 4
MAX_FRAMES = 12


def frame_times(duration: float) -> list[float]:
    step = max(2.0, duration / MAX_FRAMES)
    t, out = 0.3, []
    while t < duration - 0.1 and len(out) < MAX_FRAMES:
        out.append(round(t, 1))
        t += step
    return out or [0.0]


def contact_sheet(profile: PlatformProfile, video: Path, duration: float, dst: Path, title: str) -> Path:
    times = frame_times(duration)
    font = ImageFont.truetype(str(FONTS_DIR / "BIZUDPGothic-Bold.ttf"), 22)
    rows = (len(times) + COLS - 1) // COLS
    sheet = Image.new("RGB", (COLS * TILE_W, 40 + rows * TILE_H), (20, 20, 20))
    draw = ImageDraw.Draw(sheet)
    draw.text((10, 8), title, font=font, fill="white")
    with tempfile.TemporaryDirectory() as tmp:
        for i, t in enumerate(times):
            frame = Path(tmp) / f"{i}.jpg"
            run([*ffmpeg_base(profile.ffmpeg), "-ss", f"{t}", "-i", str(video), "-frames:v", "1",
                 "-vf", f"scale={TILE_W}:{TILE_H}", str(frame)], what="映像の見本を作れませんでした。", output=frame)
            x, y = (i % COLS) * TILE_W, 40 + (i // COLS) * TILE_H
            if frame.exists():
                sheet.paste(Image.open(frame).convert("RGB"), (x, y))
            draw.rectangle([x, y, x + 86, y + 30], fill=(0, 0, 0))
            draw.text((x + 6, y + 3), f"{t:.1f}秒", font=font, fill=(255, 220, 0))
    dst.parent.mkdir(parents=True, exist_ok=True)
    part = dst.with_name(dst.stem + ".part" + dst.suffix)
    sheet.save(part, quality=85)
    replace_file(part, dst)  # できあがってから名前を変える
    return dst
