"""FFmpeg / ffprobe の呼び出しをまとめる。"""

from __future__ import annotations

import json
import logging
import subprocess
from pathlib import Path

from app.errors import UserError
from app.jobs import run_process

log = logging.getLogger(__name__)


class PipelineError(UserError):
    """動画処理の途中で起きた問題。detail は調査用（画面には出さない）。"""

    def __init__(self, what: str, how: str, detail: str = "") -> None:
        super().__init__(what, how)
        self.detail = detail


DEFAULT_HOW = "素材ファイルが壊れていないか確認して、もう一度お試しください。"


def run(args: list[str], what: str, how: str = DEFAULT_HOW, timeout: float | None = None,
        output: Path | None = None, cwd: Path | None = None) -> subprocess.CompletedProcess:
    """コマンドを実行し、失敗したら PipelineError にする。

    output: このコマンドが書き出すファイル。失敗・中止したときは、これだけを消す（書きかけを残さない）。
    cwd: コマンドを動かすフォルダ（テロップのファイルを近い場所で渡すときに使う）。
    """
    try:
        result = run_process(args, timeout=timeout, cwd=cwd)  # 中止ボタンで止められる
    except subprocess.TimeoutExpired as exc:
        _remove_partial(output, args)
        raise PipelineError(what, "時間がかかりすぎたため中止しました。もう一度お試しください。", str(exc)) from exc
    except BaseException:  # 中止されたときも、書きかけのファイルを残さない
        _remove_partial(output, args)
        raise
    if result.returncode != 0:
        _remove_partial(output, args)
        raise PipelineError(what, how, (result.stderr or "")[-4000:])
    return result


PROTECTED_DIRS = {"素材", "声"}  # 取り込んだ素材・声の置き場所（ここのファイルは消さない）


def _inputs(args: list[str]) -> set[Path]:
    return {Path(args[i + 1]).resolve() for i, a in enumerate(args[:-1]) if a == "-i"}


def _remove_partial(output: Path | None, args: list[str]) -> None:
    """途中で止まった FFmpeg の書きかけの出力を消す（次回、壊れたファイルを使わないように）。

    消すのは output にはっきり渡したファイルだけ。素材・声のフォルダの中と、入力のファイルは決して消さない。
    """
    if output is None:
        return
    out = Path(output).resolve()
    if out.parent.name in PROTECTED_DIRS or out in _inputs(args):
        log.warning("書きかけを消すのをやめました（素材・入力のファイルのため）: %s", out)
        return
    try:
        out.unlink(missing_ok=True)
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


def _number(value) -> float | None:
    try:
        n = float(value)
    except (TypeError, ValueError):  # "N/A" など
        return None
    return n if n > 0 else None


def video_duration(ffprobe: str, path: Path, fps: float = 30) -> float:
    """映像の長さ（つなぐ位置・声やテロップの時刻の計算に使う）。

    映像の長さが取れない形式では、コマ数 ÷ fps、それも駄目なら全体の長さを使う。
    """
    info = probe(ffprobe, path)
    video = next((s for s in info.get("streams", []) if s.get("codec_type") == "video"), {})
    d = _number(video.get("duration"))
    if d is None and (frames := _number(video.get("nb_frames"))):
        d = frames / fps
    return d if d is not None else float(info["format"]["duration"])
