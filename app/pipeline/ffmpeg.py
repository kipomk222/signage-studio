"""FFmpeg / ffprobe の呼び出しをまとめる。"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from app.errors import UserError
from app.jobs import run_process


class PipelineError(UserError):
    """動画処理の途中で起きた問題。detail は調査用（画面には出さない）。"""

    def __init__(self, what: str, how: str, detail: str = "") -> None:
        super().__init__(what, how)
        self.detail = detail


DEFAULT_HOW = "素材ファイルが壊れていないか確認して、もう一度お試しください。"


def run(args: list[str], what: str, how: str = DEFAULT_HOW, timeout: float | None = None) -> subprocess.CompletedProcess:
    """コマンドを実行し、失敗したら PipelineError にする。"""
    try:
        result = run_process(args, timeout=timeout)  # 中止ボタンで止められる
    except subprocess.TimeoutExpired as exc:
        _remove_partial(args)
        raise PipelineError(what, "時間がかかりすぎたため中止しました。もう一度お試しください。", str(exc)) from exc
    except BaseException:  # 中止されたときも、書きかけのファイルを残さない
        _remove_partial(args)
        raise
    if result.returncode != 0:
        _remove_partial(args)
        raise PipelineError(what, how, (result.stderr or "")[-4000:])
    return result


def _remove_partial(args: list[str]) -> None:
    """途中で止まった FFmpeg の書きかけの出力を消す（次回、壊れたファイルを使わないように）。"""
    out = args[-1] if args else ""
    if Path(args[0]).stem.startswith("ffmpeg") and out not in ("-", "") and not out.startswith("-"):
        try:
            Path(out).unlink(missing_ok=True)
        except OSError:  # Windows で画面が開いている途中のファイルは消せない。元の問題を伝える方を優先する
            pass


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
