"""ステップ10: 冒頭タイトル（約3秒）とエンドカード（約5秒・ロゴのみ）。

タイトルは本編の最初の場面をぼかして暗くした上に、商品名（大）と型番（小）を重ねる。
エンドカードは白地にロゴを中央に置く。ロゴがまだなければ無地の仮カードにする。
"""

from __future__ import annotations

from pathlib import Path

from app.pipeline.ffmpeg import ffmpeg_base, run
from app.pipeline.fonts import FONTS_DIR, choose_font, font_size
from app.pipeline.normalize import AUDIO_ARGS, FPS, HEIGHT, SAMPLE_RATE, WIDTH
from app.pipeline.telop_render import EVENTS, HEADER, dialogue, style_line
from app.project import TelopStyle
from app.platform import PlatformProfile, ffmpeg_rel_path

ROOT = Path(__file__).resolve().parents[2]
LOGO_DIR = ROOT / "assets" / "logo"
CORNER_LOGO_DIR = ROOT / "assets" / "corner_logo"   # 左上にずっと出すロゴ（なければ LOGO_DIR のもの）
CORNER_WIDTH = {"small": 220, "medium": 280, "large": 360}
CORNER_MARGIN = 36
TITLE_SECONDS, END_SECONDS = 3.0, 5.0
LOGO_EXTS = {".png", ".webp", ".jpg", ".jpeg"}


def find_logo(folder: Path | None = None) -> Path | None:
    folder = folder or LOGO_DIR
    if not folder.exists():
        return None
    logos = sorted(p for p in folder.iterdir() if p.suffix.lower() in LOGO_EXTS)
    return logos[0] if logos else None


def find_corner_logo(folder: Path | None = None, fallback: Path | None = None) -> Path | None:
    """左上に出すロゴ。左上用のフォルダが空なら、エンドカード用のロゴ。"""
    return find_logo(folder or CORNER_LOGO_DIR) or find_logo(fallback or LOGO_DIR)


def corner_logo_image(logo: Path, dst: Path, size: str = "medium", plate: bool = True) -> Path:
    """左上に重ねる画像（決まった横幅にそろえ、必要なら角の丸い白い下地を付ける）。"""
    from PIL import Image, ImageDraw
    img = Image.open(logo).convert("RGBA")
    width = CORNER_WIDTH.get(size, CORNER_WIDTH["medium"])
    bbox = img.getbbox()  # 透明な余白は切る
    if bbox:
        img = img.crop(bbox)
    img = img.resize((width, max(1, round(img.height * width / img.width))), Image.LANCZOS)
    if plate:
        pad = round(width * 0.06)
        out = Image.new("RGBA", (img.width + pad * 2, img.height + pad * 2), (0, 0, 0, 0))
        ImageDraw.Draw(out).rounded_rectangle([0, 0, out.width - 1, out.height - 1], radius=pad, fill=(255, 255, 255, 225))
        out.alpha_composite(img, (pad, pad))
        img = out
    dst.parent.mkdir(parents=True, exist_ok=True)
    img.save(dst)
    return dst


def corner_overlay(label_in: str, corner_input: int, label_out: str = "[v]") -> str:
    """filter_complex の一部: 映像 label_in の左上に、入力番号 corner_input の画像を重ねる。"""
    return f"{label_in}[{corner_input}:v]overlay={CORNER_MARGIN}:{CORNER_MARGIN}:format=auto,format=yuv420p{label_out}"


def _silence() -> list[str]:
    return ["-f", "lavfi", "-i", f"anullsrc=r={SAMPLE_RATE}:cl=stereo"]


def build_title_ass(path: Path, title: str, subtitle: str, category: str, model_number: str,
                    style: TelopStyle | None = None) -> Path:
    style = style or TelopStyle()
    colors = (style.text_color, style.outline_color, style.emphasis_color)
    t_font = choose_font(title, "title", category, model_number)
    s_font = choose_font(subtitle, "subtitle", category, model_number)
    t_size, s_size = font_size(title, role="title"), font_size(subtitle, role="subtitle")
    lines = [HEADER,
             style_line(t_font, t_size, align=5, margin_v=0, outline=8, style=style, name="title"),
             style_line(s_font, s_size, align=5, margin_v=0, outline=5, style=style, name="subtitle"), EVENTS]
    cy = HEIGHT // 2
    lines.append(dialogue(0, TITLE_SECONDS, "title", title, t_size, f"\\pos({WIDTH // 2},{cy - 30})",
                          font_id=t_font, colors=colors))
    if subtitle:
        lines.append(dialogue(0, TITLE_SECONDS, "subtitle", subtitle, s_size,
                              f"\\pos({WIDTH // 2},{cy + t_size // 2 + 40})", font_id=s_font, colors=colors))
    path.write_text("".join(lines), encoding="utf-8-sig")
    return path


def make_title_card(profile: PlatformProfile, main_video: Path, title: str, subtitle: str,
                    dst: Path, category: str = "", model_number: str = "",
                    style: TelopStyle | None = None, corner: Path | None = None) -> Path:
    main_video, dst = Path(main_video).resolve(), Path(dst).resolve()  # FFmpeg を別の場所で動かすので絶対の場所で
    corner = Path(corner).resolve() if corner is not None else None
    ass = build_title_ass(dst.with_suffix(".ass"), title, subtitle, category, model_number, style)
    vf = (f"trim=0:0.04,setpts=PTS-STARTPTS,tpad=stop_mode=clone:stop_duration={TITLE_SECONDS},"
          f"boxblur=24:2,eq=brightness=-0.28:saturation=0.8,"
          f"ass={ffmpeg_rel_path(ass, ass.parent)}:fontsdir={ffmpeg_rel_path(FONTS_DIR, ass.parent)},"
          f"fps={FPS},format=yuv420p")
    args = [*ffmpeg_base(profile.ffmpeg), "-i", str(main_video), *_silence()]
    if corner is not None:  # 左上のロゴ
        args += ["-i", str(corner), "-filter_complex", f"[0:v:0]{vf}[b];" + corner_overlay("[b]", 2),
                 "-map", "[v]", "-map", "1:a:0"]
    else:
        args += ["-map", "0:v:0", "-map", "1:a:0", "-vf", vf]
    run([*args, "-t", f"{TITLE_SECONDS}", *profile.video_args("work"), *AUDIO_ARGS, str(dst)],
        what="冒頭のタイトルを作れませんでした。", output=dst, cwd=ass.parent)
    return dst


def make_end_card(profile: PlatformProfile, dst: Path, logo: Path | None = None) -> Path:
    if logo is None:  # ロゴが届くまでの仮カード
        args = ["-f", "lavfi", "-i", f"color=c=0x1a1a1a:s={WIDTH}x{HEIGHT}:r={FPS}:d={END_SECONDS}", *_silence(),
                "-map", "0:v", "-map", "1:a"]
        vf = "format=yuv420p"
    else:
        args = ["-f", "lavfi", "-i", f"color=c=white:s={WIDTH}x{HEIGHT}:r={FPS}:d={END_SECONDS}",
                "-loop", "1", "-i", str(logo), *_silence(), "-map", "[v]", "-map", "2:a"]
        vf = None
    cmd = [*ffmpeg_base(profile.ffmpeg), *args]
    if vf:
        cmd += ["-vf", vf]
    else:
        cmd += ["-filter_complex",
                "[1:v]scale=960:420:force_original_aspect_ratio=decrease[logo];"
                "[0:v][logo]overlay=(W-w)/2:(H-h)/2:shortest=1,format=yuv420p[v]"]
    cmd += ["-t", f"{END_SECONDS}", *profile.video_args("work"), *AUDIO_ARGS, str(dst)]
    run(cmd, what="エンドカードを作れませんでした。", how="ロゴの画像（PNG）が開けるか確認してください。", output=dst)
    return dst
