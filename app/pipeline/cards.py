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
from app.platform import PlatformProfile, ffmpeg_filter_path

ROOT = Path(__file__).resolve().parents[2]
LOGO_DIR = ROOT / "assets" / "logo"
TITLE_SECONDS, END_SECONDS = 3.0, 5.0
LOGO_EXTS = {".png", ".webp", ".jpg", ".jpeg"}


def find_logo(folder: Path = LOGO_DIR) -> Path | None:
    if not folder.exists():
        return None
    logos = sorted(p for p in folder.iterdir() if p.suffix.lower() in LOGO_EXTS)
    return logos[0] if logos else None


def _silence() -> list[str]:
    return ["-f", "lavfi", "-i", f"anullsrc=r={SAMPLE_RATE}:cl=stereo"]


def build_title_ass(path: Path, title: str, subtitle: str, category: str, model_number: str) -> Path:
    t_font = choose_font(title, "title", category, model_number)
    s_font = choose_font(subtitle, "subtitle", category, model_number)
    t_size, s_size = font_size(title, role="title"), font_size(subtitle, role="subtitle")
    lines = [HEADER,
             style_line(t_font, t_size, align=5, margin_v=0, outline=8),
             style_line(s_font, s_size, align=5, margin_v=0, outline=5), EVENTS]
    cy = HEIGHT // 2
    lines.append(dialogue(0, TITLE_SECONDS, t_font, title, t_size, f"\\pos({WIDTH // 2},{cy - 30})"))
    if subtitle:
        lines.append(dialogue(0, TITLE_SECONDS, s_font, subtitle, s_size,
                              f"\\pos({WIDTH // 2},{cy + t_size // 2 + 40})"))
    path.write_text("".join(lines), encoding="utf-8-sig")
    return path


def make_title_card(profile: PlatformProfile, main_video: Path, title: str, subtitle: str,
                    dst: Path, category: str = "", model_number: str = "") -> Path:
    ass = build_title_ass(dst.with_suffix(".ass"), title, subtitle, category, model_number)
    vf = (f"boxblur=24:2,eq=brightness=-0.28:saturation=0.8,"
          f"ass={ffmpeg_filter_path(ass)}:fontsdir={ffmpeg_filter_path(FONTS_DIR)},"
          f"fps={FPS},format=yuv420p")
    run([*ffmpeg_base(profile.ffmpeg), "-i", str(main_video), *_silence(),
         "-map", "0:v:0", "-map", "1:a:0", "-vf", f"trim=0:0.04,setpts=PTS-STARTPTS,tpad=stop_mode=clone:stop_duration={TITLE_SECONDS},{vf}",
         "-t", f"{TITLE_SECONDS}", *profile.video_args("work"), *AUDIO_ARGS, str(dst)],
        what="冒頭のタイトルを作れませんでした。")
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
    run(cmd, what="エンドカードを作れませんでした。", how="ロゴの画像（PNG）が開けるか確認してください。")
    return dst
