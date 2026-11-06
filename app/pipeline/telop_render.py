"""テロップを ASS 字幕にして、動画に焼き込む。画面下部中央。

色はプロジェクトの TelopStyle（カラーコード）で決め、テロップごとに上書きできる。
「**ことば**」で囲んだ部分は強調色になる。帯の色を指定すると、文字の後ろに帯を敷く。
"""

from __future__ import annotations

import re
from pathlib import Path

from app.pipeline.colors import to_ass, to_ass_tag
from app.pipeline.ffmpeg import ffmpeg_base, run
from app.pipeline.fonts import FONTS, FONTS_DIR, ass_size, choose_font, font_size, plain_text
from app.pipeline.normalize import HEIGHT, WIDTH
from app.platform import PlatformProfile, ffmpeg_filter_path
from app.project import Telop, TelopStyle

OUTLINE = 6
SHADOW = 2
MARGIN_V = 90        # 下からの余白（1080p）
FADE_MS = 120
BAND_PAD = 22        # 帯の、文字からのはみ出し

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
EMPHASIS = re.compile(r"\*\*(.+?)\*\*", re.S)


def ass_time(t: float) -> str:
    cs = max(0, round(t * 100))
    h, cs = divmod(cs, 360000)
    m, cs = divmod(cs, 6000)
    s, cs = divmod(cs, 100)
    return f"{h}:{m:02d}:{s:02d}.{cs:02d}"


def style_line(font_id: str, size: int = 72, align: int = 2, margin_v: int = MARGIN_V,
               outline: int = OUTLINE, style: TelopStyle | None = None, name: str = "") -> str:
    f, st = FONTS[font_id], style or TelopStyle()
    return (f"Style: {name or font_id},{f.family},{ass_size(font_id, size)},"
            f"{to_ass(st.text_color)},{to_ass(st.text_color)},{to_ass(st.outline_color)},&H80000000,"
            f"{-1 if f.bold else 0},0,0,0,100,100,0,0,1,{outline},{SHADOW},{align},80,80,{margin_v},1\n")


def band_style_line(font_id: str, style: TelopStyle, align: int = 2, margin_v: int = MARGIN_V) -> str:
    """帯用: 文字は透明にして、四角い縁取り（BorderStyle=3）を帯として使う。"""
    f = FONTS[font_id]
    return (f"Style: band_{font_id},{f.family},72,&HFF000000,&HFF000000,"
            f"{to_ass(style.band_color, style.band_opacity)},&HFF000000,"
            f"{-1 if f.bold else 0},0,0,0,100,100,0,0,3,{BAND_PAD},0,{align},80,80,{margin_v},1\n")


def escape_text(text: str) -> str:
    return text.replace("\\", "＼").replace("{", "｛").replace("}", "｝").replace("\n", "\\N")


def colored_text(text: str, base: str, emphasis: str) -> str:
    """**ことば** を強調色にし、それ以外は文字色にする。"""
    out, pos = [], 0
    for m in EMPHASIS.finditer(text):
        out.append(escape_text(text[pos:m.start()]))
        out.append(f"{{\\c{to_ass_tag(emphasis)}}}{escape_text(m.group(1))}{{\\c{to_ass_tag(base)}}}")
        pos = m.end()
    out.append(escape_text(text[pos:]))
    return "".join(out)


def dialogue(start: float, end: float, style: str, text: str, size: int, extra: str = "",
             font_id: str = "", colors: tuple[str, str, str] | None = None, layer: int = 1) -> str:
    """colors: (文字, 縁取り, 強調)。None なら強調の印を外して文字だけにする（帯用）。"""
    tags = f"\\fs{ass_size(font_id or style, size)}\\fad({FADE_MS},{FADE_MS}){extra}"
    if colors:
        text_c, outline_c, emph_c = colors
        tags += f"\\c{to_ass_tag(text_c)}\\3c{to_ass_tag(outline_c)}"
        body = colored_text(text, text_c, emph_c)
    else:
        body = escape_text(plain_text(text))
    return f"Dialogue: {layer},{ass_time(start)},{ass_time(end)},{style},,0,0,0,,{{{tags}}}{body}\n"


def resolve_font(t: Telop, category: str, model_number: str) -> str:
    return t.font if t.font in FONTS else choose_font(plain_text(t.text), "telop", category, model_number)


def telop_colors(t: Telop, style: TelopStyle) -> tuple[str, str, str]:
    return (t.text_color or style.text_color, t.outline_color or style.outline_color,
            t.emphasis_color or style.emphasis_color)


def build_telop_ass(telops: list[Telop], path: Path, category: str = "", model_number: str = "",
                    style: TelopStyle | None = None) -> Path:
    style = style or TelopStyle()
    used = sorted({resolve_font(t, category, model_number) for t in telops})
    lines = [HEADER, *(style_line(f, style=style) for f in used)]
    if style.band_color:
        lines += [band_style_line(f, style) for f in used]
    lines.append(EVENTS)
    for t in sorted(telops, key=lambda x: x.start):
        font = resolve_font(t, category, model_number)
        size = font_size(plain_text(t.text))
        if style.band_color:
            lines.append(dialogue(t.start, t.end, f"band_{font}", t.text, size, font_id=font, layer=0))
        lines.append(dialogue(t.start, t.end, font, t.text, size, colors=telop_colors(t, style)))
    path.write_text("".join(lines), encoding="utf-8-sig")
    return path


def burn_ass(profile: PlatformProfile, src: Path, ass: Path, dst: Path, corner: Path | None = None) -> Path:
    """テロップを焼き込む。corner（画像）があれば、同じ処理の中で左上にロゴを重ねる。"""
    vf = f"ass={ffmpeg_filter_path(ass)}:fontsdir={ffmpeg_filter_path(FONTS_DIR)}"
    if corner is not None:
        from app.pipeline.cards import corner_overlay
        video = ["-i", str(corner), "-filter_complex", f"[0:v]{vf}[t];" + corner_overlay("[t]", 1),
                 "-map", "[v]", "-map", "0:a?"]
    else:
        video = ["-vf", vf]
    run([*ffmpeg_base(profile.ffmpeg), "-i", str(src), *video,
         *profile.video_args("work"), "-c:a", "copy", str(dst)],
        what="テロップを動画に入れられませんでした。",
        how="テロップに特殊な記号が入っていないか確認して、もう一度お試しください。")
    return dst
