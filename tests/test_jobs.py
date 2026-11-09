"""中止ボタンと進み具合のテスト。"""

import sys
import threading
import time

import pytest

from app.jobs import Cancelled, Job, run_process

SLOW_ECHO = [sys.executable, "-c", "import sys,time; d=sys.stdin.read(); time.sleep(1.2); print(d.upper())"]


def test_input_survives_waiting():
    """入力を渡したプロセスを何度も待っても壊れない（Claude Code への依頼はこの形）。"""
    r = run_process(SLOW_ECHO, input="hello")
    assert r.returncode == 0 and r.stdout.strip() == "HELLO"


def test_cancel_stops_running_process():
    job, result = Job(), {}

    def go():
        with job.active():
            try:
                run_process([sys.executable, "-c", "import time; time.sleep(30)"])
            except Cancelled as e:
                result["err"] = e

    t = threading.Thread(target=go)
    t.start()
    time.sleep(0.8)
    t0 = time.monotonic()
    job.cancel()
    t.join(5)
    assert "err" in result and time.monotonic() - t0 < 2
    assert "中止" in result["err"].what


def test_timeout():
    import subprocess
    with pytest.raises(subprocess.TimeoutExpired):
        run_process([sys.executable, "-c", "import time; time.sleep(5)"], timeout=1)


def test_progress_and_check():
    seen = []
    job = Job(lambda f, m: seen.append((f, m)))
    job.step(0.3, "a")
    job.step(0.2, "b")       # 進み具合は戻らない
    assert seen == [(0.3, "a"), (0.3, "b")]
    job.cancel()
    with pytest.raises(Cancelled):
        job.step(0.5, "c")


def test_cancel_leaves_no_half_written_video(tmp_path):
    """中止したとき、FFmpeg の書きかけのファイルを残さない（次回それを使って失敗しないように）。"""
    from app.pipeline.ffmpeg import PipelineError, run
    out = tmp_path / "half.mp4"
    job = Job()
    args = ["ffmpeg", "-hide_banner", "-y", "-re", "-f", "lavfi", "-i", "testsrc2=d=20", "-c:v", "libx264",
            "-preset", "ultrafast", str(out)]
    threading.Timer(1.5, job.cancel).start()
    with job.active(), pytest.raises(Cancelled):
        run(args, what="テスト", output=out)
    assert not out.exists()
    with pytest.raises(PipelineError):
        run(["ffmpeg", "-hide_banner", "-y", "-i", str(tmp_path / "ない.mp4"), str(out)], what="テスト", output=out)
    assert not out.exists()


def test_failed_command_never_deletes_material_or_inputs(tmp_path):
    """失敗しても、消すのは output にはっきり渡したファイルだけ。素材・声・入力のファイルは消さない。"""
    from app.pipeline.ffmpeg import PipelineError, run
    bad = ["ffmpeg", "-hide_banner", "-y", "-f", "lavfi", "-i", "nosuchfilter", ]
    for folder in ("素材", "声"):
        keep = tmp_path / folder / "IMG_0001.MOV"
        keep.parent.mkdir()
        keep.write_text("元の素材")
        with pytest.raises(PipelineError):
            run([*bad, str(keep)], what="テスト", output=keep)
        assert keep.exists()
    src = tmp_path / "入力.mp4"
    src.write_text("入力")
    with pytest.raises(PipelineError):
        run(["ffmpeg", "-hide_banner", "-y", "-i", str(src), str(src)], what="テスト", output=src)
    assert src.exists()
    last = tmp_path / "最後の引数.mp4"   # output を渡さなければ、最後の引数でも消さない
    last.write_text("x")
    with pytest.raises(PipelineError):
        run([*bad, str(last)], what="テスト")
    assert last.exists()
