"""FFmpeg / ffprobe の呼び出しをまとめる。"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from app.errors import UserError


class PipelineError(UserError):
    """動画処理の途中で起きた問題。detail は調査用（画面には出さない）。"""

    def __init__(self, what: str, how: str, detail: str = "") -> None:
        super().__init__(what, how)
        self.detail = detail


DEFAULT_HOW = "素材ファイルが壊れていないか確認して、もう一度お試しください。"


def run(args: list[str], what: str, how: str = DEFAULT_HOW, timeout: float | None = None) -> subprocess.CompletedProcess:
    """コマンドを実行し、失敗したら PipelineError にする。"""
    try:
        result = subprocess.run(args, capture_output=True, text=True, encoding="utf-8",
                                errors="replace", timeout=timeout, check=False)
    except subprocess.TimeoutExpired as exc:
        raise PipelineError(what, "時間がかかりすぎたため中止しました。もう一度お試しください。", str(exc)) from exc
    if result.returncode != 0:
        raise PipelineError(what, how, (result.stderr or "")[-4000:])
    return result


def ffmpeg_base(ffmpeg: str) -> list[str]:
    return [ffmpeg, "-hide_banner", "-nostdin", "-y"]


def probe(ffprobe: str, path: Path) -> dict:
    result = run(
        [ffprobe, "-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(path)],
        what=f"「{Path(path).name}」を読み込めませんでした。",
        how="動画ファイルが壊れていないか確認してください。",
    )
    return json.loads(result.stdout)


def duration_of(ffprobe: str, path: Path) -> float:
    return float(probe(ffprobe, path)["format"]["duration"])
