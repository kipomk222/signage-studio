"""ステップ2: 正規化。全クリップを 1920×1080・30fps・音声 48kHz ステレオにそろえる。"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from app.pipeline.ffmpeg import ffmpeg_base, run
from app.pipeline.inventory import ClipInfo
from app.platform import PlatformProfile

WIDTH, HEIGHT, FPS, SAMPLE_RATE = 1920, 1080, 30, 48000
AUDIO_ARGS = ["-c:a", "aac", "-b:a", "192k", "-ar", str(SAMPLE_RATE), "-ac", "2"]
PIECE_AUDIO_ARGS = ["-c:a", "pcm_s16le", "-ar", str(SAMPLE_RATE), "-ac", "2"]  # 途中のピース（.mov）用


def audio_args_for(dst: Path) -> list[str]:
    """途中のピース（.mov）は音を区切りのない形式で持つ（AAC の約0.021秒単位の切れ目をためないため）。"""
    return PIECE_AUDIO_ARGS if dst.suffix.lower() == ".mov" else AUDIO_ARGS


def frame_count(seconds: float) -> int:
    """その長さのコマ数（1秒30コマ）。"""
    return max(1, round(seconds * FPS))


def exact_length_filters(frames: int) -> tuple[str, str]:
    """映像と音の長さを、コマ数ぴったりにそろえるフィルター（映像の末尾, 音の末尾）。

    映像は最後の絵を少しだけ延ばしてから -frames:v で切り、音は無音を足してからサンプル数で切る。
    """
    samples = round(frames / FPS * SAMPLE_RATE)
    return (f"fps={FPS},tpad=stop_mode=clone:stop_duration=0.2",
            f"apad,atrim=end_sample={samples},asetpts=PTS-STARTPTS")

FitMode = Literal["pad", "crop"]


# HDR → 普通の映像の色（mobius は明るい所をなめらかに抑え、商品が暗くなりにくい）
TONEMAP = ("zscale=t=linear:npl=100,format=gbrpf32le,zscale=p=bt709,"
           "tonemap=mobius:desat=0,zscale=t=bt709:m=bt709:r=tv,format=yuv420p,")
WIDE_TO_709 = "zscale=p=bt709:t=bt709:m=bt709:r=tv,format=yuv420p,"
COLOR_TAGS = ["-color_primaries", "bt709", "-color_trc", "bt709", "-colorspace", "bt709"]
NORM_VERSION = "v2"  # 正規化の中身を変えたら上げる（古い正規化済みの素材を作り直す）

HDR_NO_ZSCALE_NOTE = ("HDR の映像は、色が薄く見えることがあります。"
                      "iPhone の「HDR ビデオ」をオフにして撮るか、FFmpeg を入れ直してください。")


def color_filter(color: str, has_zscale: bool) -> str:
    """色を普通の映像（bt709）にそろえるフィルター（いらなければ空）。"""
    if not has_zscale:
        return ""
    return {"hdr": TONEMAP, "wide": WIDE_TO_709}.get(color, "")


def video_filter(fit: FitMode, color: str = "sdr", has_zscale: bool = True) -> str:
    if fit == "crop":
        size = f"scale={WIDTH}:{HEIGHT}:force_original_aspect_ratio=increase,crop={WIDTH}:{HEIGHT}"
    else:  # 黒帯
        size = (f"scale={WIDTH}:{HEIGHT}:force_original_aspect_ratio=decrease,"
                f"pad={WIDTH}:{HEIGHT}:(ow-iw)/2:(oh-ih)/2:color=black")
    return f"{color_filter(color, has_zscale)}{size},setsar=1,fps={FPS},format=yuv420p"


def normalize_clip(profile: PlatformProfile, clip: ClipInfo, dst: Path, fit: FitMode = "pad") -> Path:
    dst.parent.mkdir(parents=True, exist_ok=True)
    args = [*ffmpeg_base(profile.ffmpeg), "-i", str(clip.path)]
    if not clip.has_audio:  # 音のないクリップには無音を足して、結合できるようにする
        args += ["-f", "lavfi", "-i", f"anullsrc=r={SAMPLE_RATE}:cl=stereo"]
        args += ["-map", "0:v:0", "-map", "1:a:0", "-shortest"]
    else:
        args += ["-map", "0:v:0", "-map", "0:a:0"]
    args += ["-vf", video_filter(fit, clip.color, profile.has_zscale), *profile.video_args("work"),
             *COLOR_TAGS, *AUDIO_ARGS, str(dst)]
    run(args, what=f"「{clip.path.name}」の形式をそろえられませんでした。", output=dst)
    return dst


def norm_path(work: Path, clip_file: str, fit: str) -> Path:
    """正規化済みの素材の置き場所（版の印つき）。"""
    return work / "norm" / f"{Path(clip_file).stem}_{fit}_{NORM_VERSION}.mp4"
