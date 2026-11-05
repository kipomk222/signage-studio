"""テロップを ASS 字幕にして、動画に焼き込む。白文字＋黒縁取り、画面下部中央。"""

from __future__ import annotations

from pathlib import Path

from app.pipeline.ffmpeg import ffmpeg_base, run
from app.pipeline.fonts import FONTS, FONTS_DIR, ass_size, choose_font, font_size
from app.pipeline.normalize import HEIGHT, WIDTH
from app.platform import PlatformProfile, ffmpeg_filter_path
from app.project import Telop

OUTLINE = 6
SHADOW = 2
MARGIN_V = 90        # 下からの余白（1080p）
FADE_MS = 120

HEADER = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {WIDTH}
PlayResY: {HEIGHT}
WrapStyle: 2
ScaledBorderAndShadow: yes
YCbCr Matrix: TV.709

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
"""
EVENTS = """
[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""


def ass_time(t: float) -> str:
    cs = max(0, round(t * 100))
    h, cs = divmod(cs, 360000)
    m, cs = divmod(cs, 6000)
    s, cs = divmod(cs, 100)
    return f"{h}:{m:02d}:{s:02d}.{cs:02d}"


def style_line(font_id: str, size: int = 72, align: int = 2, margin_v: int = MARGIN_V,
               outline: int = OUTLINE) -> str:
    f = FONTS[font_id]
    return (f"Style: {font_id},{f.family},{ass_size(font_id, size)},&H00FFFFFF,&H00FFFFFF,&H00000000,&H80000000,"
            f"{-1 if f.bold else 0},0,0,0,100,100,0,0,1,{outline},{SHADOW},{align},80,80,{margin_v},1\n")


def escape_text(text: str) -> str:
    return text.replace("\\", "＼").replace("{", "｛").replace("}", "｝").replace("\n", "\\N")


def dialogue(start: float, end: float, style: str, text: str, size: int, extra: str = "") -> str:
    tags = f"{{\\fs{ass_size(style, size)}\\fad({FADE_MS},{FADE_MS}){extra}}}"
    return f"Dialogue: 0,{ass_time(start)},{ass_time(end)},{style},,0,0,0,,{tags}{escape_text(text)}\n"


def resolve_font(t: Telop, category: str, model_number: str) -> str:
    return t.font if t.font in FONTS else choose_font(t.text, "telop", category, model_number)


def build_telop_ass(telops: list[Telop], path: Path, category: str = "", model_number: str = "") -> Path:
    used = {resolve_font(t, category, model_number) for t in telops}
    lines = [HEADER, *(style_line(f) for f in sorted(used)), EVENTS]
    for t in sorted(telops, key=lambda x: x.start):
        font = resolve_font(t, category, model_number)
        lines.append(dialogue(t.start, t.end, font, t.text, font_size(t.text)))
    path.write_text("".join(lines), encoding="utf-8-sig")
    return path


def burn_ass(profile: PlatformProfile, src: Path, ass: Path, dst: Path) -> Path:
    vf = f"ass={ffmpeg_filter_path(ass)}:fontsdir={ffmpeg_filter_path(FONTS_DIR)}"
    run([*ffmpeg_base(profile.ffmpeg), "-i", str(src), "-vf", vf,
         *profile.video_args("work"), "-c:a", "copy", str(dst)],
        what="テロップを動画に入れられませんでした。",
        how="テロップに特殊な記号が入っていないか確認して、もう一度お試しください。")
    return dst
