"""Mac / Windows の差分をまとめるモジュール。

OS による違いはこのファイルにだけ書く（CLAUDE.md の作業ルール）。
他のモジュールは get_profile() が返す PlatformProfile を使う。
"""

from __future__ import annotations

import platform as _platform
import shutil
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from functools import lru_cache
from pathlib import Path

from app.errors import UserError


class OSKind(str, Enum):
    MAC = "mac"
    WINDOWS = "windows"
    LINUX = "linux"  # 開発・テスト用（利用者の PC ではない）


class SetupError(UserError):
    """起動時の環境チェックで見つかった問題。"""


# 書き出しに使う方式の優先順。先頭から順に試し、使えなければ次へ。
ENCODER_CANDIDATES: dict[OSKind, list[str]] = {
    OSKind.MAC: ["h264_videotoolbox", "libx264"],
    OSKind.WINDOWS: ["h264_amf", "libx264"],
    OSKind.LINUX: ["libx264"],
}

# 文字起こしの方式
WHISPER_BACKEND: dict[OSKind, str] = {
    OSKind.MAC: "mlx-whisper",
    OSKind.WINDOWS: "faster-whisper",
    OSKind.LINUX: "faster-whisper",
}

# 文字起こしのモデル。初回だけダウンロードされ、以降は PC に保存したものを使う
WHISPER_MODEL: dict[OSKind, str] = {
    OSKind.MAC: "mlx-community/whisper-large-v3-turbo",
    OSKind.WINDOWS: "large-v3-turbo",
    OSKind.LINUX: "large-v3-turbo",
}

FFMPEG_INSTALL_HINT: dict[OSKind, str] = {
    OSKind.MAC: "ターミナルで「brew install ffmpeg」を実行してから、もう一度起動してください。",
    OSKind.WINDOWS: "コマンドプロンプトで「winget install ffmpeg」を実行してから、もう一度起動してください。",
    OSKind.LINUX: "パッケージマネージャーで ffmpeg を入れてから、もう一度起動してください。",
}


def detect_os(system: str | None = None) -> OSKind:
    """いま動いている PC の種類を返す。system はテスト用。"""
    name = (system if system is not None else _platform.system()).lower()
    if name == "darwin":
        return OSKind.MAC
    if name == "windows":
        return OSKind.WINDOWS
    return OSKind.LINUX


def find_ffmpeg(os_kind: OSKind, which: Callable[[str], str | None] = shutil.which) -> tuple[str, str]:
    """ffmpeg と ffprobe の場所を返す。見つからなければ SetupError。"""
    ffmpeg = which("ffmpeg")
    ffprobe = which("ffprobe")
    if not ffmpeg or not ffprobe:
        raise SetupError(
            "動画を処理するソフト（FFmpeg）が見つかりません。",
            FFMPEG_INSTALL_HINT[os_kind],
        )
    return ffmpeg, ffprobe


def list_encoders(ffmpeg: str) -> set[str]:
    """FFmpeg が対応している映像エンコーダ名の一覧。"""
    out = subprocess.run(
        [ffmpeg, "-hide_banner", "-encoders"],
        capture_output=True, text=True, timeout=30, check=False,
    ).stdout
    return parse_encoder_list(out)


def parse_encoder_list(text: str) -> set[str]:
    names: set[str] = set()
    lines = text.splitlines()
    # 凡例（" V..... = Video" など）は区切り線 "------" より上にあるので読み飛ばす
    for i, line in enumerate(lines):
        if line.strip().startswith("---"):
            lines = lines[i + 1:]
            break
    for line in lines:
        parts = line.split()
        # 例: " V....D libx264   libx264 H.264 ..."
        if len(parts) >= 2 and len(parts[0]) == 6 and parts[0].startswith("V"):
            names.add(parts[1])
    return names


def encoder_works(ffmpeg: str, name: str) -> bool:
    """一覧に載っていても実際には動かない（ドライバ不足など）ことがあるので、1コマだけ試す。"""
    try:
        result = subprocess.run(
            [
                ffmpeg, "-hide_banner", "-loglevel", "error",
                "-f", "lavfi", "-i", "color=c=black:s=320x240:d=0.1",
                "-frames:v", "1", "-c:v", name, "-f", "null", "-",
            ],
            capture_output=True, timeout=30, check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


def pick_encoder(
    os_kind: OSKind,
    available: set[str],
    works: Callable[[str], bool],
) -> str:
    """候補を優先順に確かめ、使える最初の書き出し方式を返す。"""
    for name in ENCODER_CANDIDATES[os_kind]:
        if name in available and works(name):
            return name
    raise SetupError(
        "動画を書き出す方式が見つかりません。",
        "FFmpeg を入れ直してから、もう一度起動してください。",
    )


# 書き出し方式ごとの画質設定。"work" は途中ファイル（画質優先）、"final" は完成動画。
_ENCODER_ARGS: dict[str, dict[str, list[str]]] = {
    "h264_videotoolbox": {
        "work": ["-b:v", "20M"],
        "final": ["-b:v", "12M", "-maxrate", "16M", "-bufsize", "24M"],
    },
    "h264_amf": {
        "work": ["-rc", "cqp", "-qp_i", "16", "-qp_p", "18"],
        "final": ["-rc", "vbr_peak", "-b:v", "12M", "-maxrate", "16M"],
    },
    "libx264": {
        "work": ["-preset", "veryfast", "-crf", "16"],
        "final": ["-preset", "veryfast", "-crf", "20"],
    },
}


def video_encode_args(encoder: str, purpose: str) -> list[str]:
    """FFmpeg に渡す映像の書き出し指定（-c:v から -pix_fmt まで）。"""
    return ["-c:v", encoder, *_ENCODER_ARGS[encoder][purpose], "-pix_fmt", "yuv420p"]


def ffmpeg_filter_path(path: Path) -> str:
    """FFmpeg のフィルター（subtitles など）に渡すファイルの場所。

    Windows の「C:\\」の「:」と「\\」はフィルターの区切り文字とぶつかるので書き換える。
    """
    text = Path(path).resolve().as_posix()
    text = text.replace("\\", "/").replace(":", "\\:").replace("'", "'\\''")
    return f"'{text}'"


def open_folder_command(path: Path, os_kind: OSKind) -> list[str]:
    if os_kind is OSKind.MAC:
        return ["open", str(path)]
    if os_kind is OSKind.WINDOWS:
        return ["explorer", str(path)]
    return ["xdg-open", str(path)]


@dataclass(frozen=True)
class PlatformProfile:
    os_kind: OSKind
    whisper_backend: str
    whisper_model: str
    ffmpeg: str
    ffprobe: str
    video_encoder: str

    def video_args(self, purpose: str = "final") -> list[str]:
        return video_encode_args(self.video_encoder, purpose)

    def open_folder(self, path: Path) -> None:
        """完成したフォルダを Finder / エクスプローラーで開く。"""
        # explorer は正常でも終了コード 1 を返すため、結果は見ない
        subprocess.Popen(open_folder_command(Path(path), self.os_kind))


def build_profile(
    os_kind: OSKind,
    which: Callable[[str], str | None] = shutil.which,
    encoders: Callable[[str], set[str]] = list_encoders,
    works: Callable[[str, str], bool] = encoder_works,
) -> PlatformProfile:
    ffmpeg, ffprobe = find_ffmpeg(os_kind, which)
    encoder = pick_encoder(os_kind, encoders(ffmpeg), lambda n: works(ffmpeg, n))
    return PlatformProfile(
        os_kind=os_kind,
        whisper_backend=WHISPER_BACKEND[os_kind],
        whisper_model=WHISPER_MODEL[os_kind],
        ffmpeg=ffmpeg,
        ffprobe=ffprobe,
        video_encoder=encoder,
    )


@lru_cache(maxsize=1)
def get_profile() -> PlatformProfile:
    """起動中の PC の設定。初回だけ FFmpeg を調べ、以降は使い回す。"""
    return build_profile(detect_os())
