"""時間のかかる処理の進み具合と中止。

画面のボタンから始めた処理は Job の中で動く。Job を「中止」すると、
いま動いている FFmpeg や Claude Code を止め、Cancelled を起こして処理を抜ける。
"""

from __future__ import annotations

import subprocess
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path

from app.errors import UserError

_local = threading.local()


class Cancelled(UserError):
    def __init__(self) -> None:
        super().__init__("処理を中止しました。", "続けるときは、もう一度ボタンを押してください。")


Progress = Callable[[float, str], None]


class Job:
    def __init__(self, on_progress: Progress | None = None) -> None:
        self._event = threading.Event()
        self._on_progress = on_progress
        self.fraction = 0.0
        self.message = ""
        self.started = time.monotonic()

    def cancel(self) -> None:
        self._event.set()

    @property
    def cancelled(self) -> bool:
        return self._event.is_set()

    def check(self) -> None:
        if self.cancelled:
            raise Cancelled()

    def step(self, fraction: float, message: str) -> None:
        """進み具合（0〜1）と、いまやっていることを知らせる。"""
        self.check()
        self.fraction, self.message = max(self.fraction, min(1.0, fraction)), message
        if self._on_progress:
            self._on_progress(self.fraction, message)

    def remaining_text(self) -> str:
        """残り時間の目安（「約3分」など）。まだ分からなければ空文字。"""
        elapsed = time.monotonic() - self.started
        if self.fraction < 0.08 or elapsed < 5:
            return ""
        left = elapsed * (1 - self.fraction) / self.fraction
        return f"残り約{max(1, round(left / 60))}分" if left >= 60 else "まもなく終わります"

    @contextmanager
    def active(self) -> Iterator["Job"]:
        previous = getattr(_local, "job", None)
        _local.job = self
        try:
            yield self
        finally:
            _local.job = previous


def current_job() -> Job | None:
    return getattr(_local, "job", None)


def checkpoint() -> None:
    """中止されていたらここで止める。"""
    if (job := current_job()) is not None:
        job.check()


def run_process(cmd: list[str], *, input: str | None = None, cwd: Path | None = None,
                timeout: float | None = None) -> subprocess.CompletedProcess:
    """subprocess.run と同じだが、Job が中止されたら外部プログラムを止める。"""
    job = current_job()
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE if input is not None else None,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd=cwd,
                            text=True, encoding="utf-8", errors="replace")
    deadline = None if timeout is None else time.monotonic() + timeout
    pending = input  # 入力は最初の1回だけ渡す（2回目以降は続きを待つだけ）
    while True:
        try:
            out, err = proc.communicate(input=pending, timeout=0.5)
            return subprocess.CompletedProcess(cmd, proc.returncode, out, err)
        except subprocess.TimeoutExpired:
            pending = None
            if job is not None and job.cancelled:
                proc.kill()
                proc.communicate()
                raise Cancelled() from None
            if deadline is not None and time.monotonic() > deadline:
                proc.kill()
                proc.communicate()
                raise subprocess.TimeoutExpired(cmd, timeout) from None
