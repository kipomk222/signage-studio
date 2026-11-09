"""本番前の見直し（Claude と Gemini のレビュー）で直したところのテスト。"""

from __future__ import annotations

import asyncio
import json
import os
import random
import subprocess
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.platform import get_profile


def _ffmpeg(*args: str) -> None:
    subprocess.run(["ffmpeg", "-v", "error", "-y", *args], check=True)


def _tags(path: Path) -> dict:
    out = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
                          "stream=pix_fmt,color_transfer,color_primaries,color_space", "-of", "json", str(path)],
                         capture_output=True, text=True, check=True).stdout
    return json.loads(out)["streams"][0]


# --- 1. HDR の映像 -------------------------------------------------------------------

def test_color_kind():
    from app.pipeline.inventory import color_kind
    assert color_kind({"color_transfer": "arib-std-b67", "color_primaries": "bt2020"}) == "hdr"
    assert color_kind({"color_transfer": "smpte2084"}) == "hdr"
    assert color_kind({"color_transfer": "bt709", "color_primaries": "bt2020"}) == "wide"  # 色の範囲だけ広い
    assert color_kind({"color_transfer": "bt709", "color_primaries": "bt709"}) == "sdr"
    assert color_kind({}) == "sdr"


@pytest.mark.parametrize("trc,kind", [("arib-std-b67", "hdr"), ("bt709", "wide")])
def test_hdr_and_wide_footage_become_normal_colors(tmp_path, trc, kind):
    from app.pipeline.inventory import probe_clip
    from app.pipeline.normalize import normalize_clip
    src = tmp_path / "iphone.mov"
    _ffmpeg("-f", "lavfi", "-i", "testsrc2=s=640x360:r=30:d=1", "-f", "lavfi", "-i", "sine=d=1",
            "-c:v", "libx265", "-pix_fmt", "yuv420p10le", "-x265-params", "log-level=error",
            "-color_primaries", "bt2020", "-color_trc", trc, "-colorspace", "bt2020nc", "-c:a", "aac", str(src))
    profile = get_profile()
    clip = probe_clip(profile, src)
    assert clip.color == kind
    out = normalize_clip(profile, clip, tmp_path / "norm.mp4")
    t = _tags(out)
    assert (t["pix_fmt"], t["color_transfer"], t["color_primaries"], t["color_space"]) == \
        ("yuv420p", "bt709", "bt709", "bt709")
    # 変換の機能（zscale）がない PC でも、止まらずに作れる
    out2 = normalize_clip(replace(profile, has_zscale=False), clip, tmp_path / "norm2.mp4")
    assert _tags(out2)["color_transfer"] == "bt709"


def test_zscale_is_detected_from_filter_list():
    from app.platform import OSKind, build_profile
    which = {"ffmpeg": "/x/ffmpeg", "ffprobe": "/x/ffprobe"}.get
    with_z = build_profile(OSKind.LINUX, which=which, encoders=lambda f: {"libx264"}, works=lambda f, n: True,
                           filters=lambda f: {"scale", "zscale"})
    without = build_profile(OSKind.LINUX, which=which, encoders=lambda f: {"libx264"}, works=lambda f, n: True,
                            filters=lambda f: {"scale"})
    assert with_z.has_zscale and not without.has_zscale


def test_old_normalized_files_are_made_again():
    from app.pipeline.normalize import norm_path
    assert norm_path(Path("work"), "IMG_1.MOV", "pad").name == "IMG_1_pad_v2.mp4"


# --- 2. 素材・声のコピー ---------------------------------------------------------------

def test_copy_in_keeps_same_file_and_numbers_different_ones(tmp_path):
    from app.fileops import copy_in
    a, b, c = (tmp_path / d / "IMG_0001.MOV" for d in ("a", "b", "c"))
    for i, f in enumerate((a, b, c)):
        f.parent.mkdir()
        f.write_bytes(b"x" * (10 + i))
    dst = tmp_path / "素材"
    assert copy_in(a, dst).name == "IMG_0001.MOV"
    assert copy_in(a, dst).name == "IMG_0001.MOV"            # 同じファイル: もう一度入れても増えない
    assert copy_in(b, dst).name == "IMG_0001_2.MOV"          # 同じ名前の別ファイル
    assert copy_in(c, dst).name == "IMG_0001_3.MOV"
    assert copy_in(b, dst).name == "IMG_0001_2.MOV"
    assert sorted(p.name for p in dst.iterdir()) == ["IMG_0001.MOV", "IMG_0001_2.MOV", "IMG_0001_3.MOV"]
    assert a.read_bytes() == b"x" * 10                       # 元のファイルはそのまま


def test_copy_in_ignores_half_copied_leftover(tmp_path):
    from app.fileops import copy_in
    src = tmp_path / "in" / "clip.mov"
    src.parent.mkdir()
    src.write_bytes(b"y" * 100)
    dst = tmp_path / "素材"
    dst.mkdir()
    (dst / "clip.mov.part").write_bytes(b"y" * 30)           # 前回、コピーの途中で止まった
    out = copy_in(src, dst)
    assert out.read_bytes() == b"y" * 100 and not (dst / "clip.mov.part").exists()


# --- 3. 時刻のずれ ------------------------------------------------------------------

def test_many_pieces_do_not_drift(tmp_path):
    """長さが半端なカットを25本つないでも、計算した時刻と実際の映像・音がずれない。"""
    from app.pipeline.edl import render_plan
    from app.pipeline.ffmpeg import probe, video_duration
    from app.project import EditPiece, EditPlan
    src = tmp_path / "src.mp4"
    _ffmpeg("-f", "lavfi", "-i", "testsrc2=s=320x180:r=30:d=20", "-f", "lavfi", "-i", "sine=f=440:d=20:r=48000",
            "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-c:a", "aac", "-ac", "2", str(src))
    rnd = random.Random(3)
    pieces = []
    for i in range(25):
        s, length = round(rnd.uniform(0, 15), 3), round(rnd.uniform(1.1, 3.7), 3)
        pieces.append(EditPiece(clip="src.mp4", start=s, end=round(s + length, 3), mute=i % 2 == 0,
                                speed=1.13 if i % 5 == 0 else 1.0, hold=0.37 if i % 7 == 0 else 0.0))
    profile = get_profile()
    joined, layouts, total = render_plan(profile, EditPlan(pieces=pieces), {"src.mp4": src},
                                         {"src.mp4": [(0.0, 20.0)]}, tmp_path)
    frame = 1 / 30
    pieces_dir = tmp_path / "pieces"
    lengths = [video_duration(profile.ffprobe, p) for p in sorted(pieces_dir.glob("*.mov"))]
    expected_total = sum(lengths) - 0.3 * (len(lengths) - 1)
    assert abs(total - expected_total) <= frame + 1e-6
    assert abs(layouts[-1].out_start + lengths[-1] - 0.3 - (total - 0.3)) <= frame + 1e-6
    streams = {s["codec_type"]: float(s["duration"]) for s in probe(profile.ffprobe, joined)["streams"]}
    assert abs(streams["video"] - streams["audio"]) <= 0.05   # 音と映像の長さもそろう（AAC の切れ目1つ分まで）


def test_video_duration_falls_back(monkeypatch):
    from app.pipeline import ffmpeg
    info = {"streams": [{"codec_type": "video", "duration": "N/A", "nb_frames": "90"}], "format": {"duration": "3.5"}}
    monkeypatch.setattr(ffmpeg, "probe", lambda *a: info)
    assert ffmpeg.video_duration("ffprobe", Path("x")) == 3.0          # コマ数 ÷ 30
    info["streams"][0]["nb_frames"] = "N/A"
    assert ffmpeg.video_duration("ffprobe", Path("x")) == 3.5          # 全体の長さ


# --- 6. 短すぎるカット -----------------------------------------------------------------

def test_short_cut_never_goes_past_the_material():
    from app.pipeline.fit import MIN_KEEP, Cut, fit_scene
    # 素材の最後ぎりぎりのカット: 後ろに伸ばせないので前に広げる
    r = fit_scene([Cut("a", 9.5, 10.0, 10.0)], target=0.1)
    c = r.cuts[0]
    assert c.end <= 10.0 and c.end - c.start >= MIN_KEEP - 1e-3


def test_all_cuts_too_short_keeps_one():
    from app.pipeline.fit import Cut, fit_scene
    r = fit_scene([Cut("a", 0.0, 0.4, 0.4), Cut("b", 0.0, 0.3, 0.3)], target=0.2)
    assert len(r.cuts) == 1 and r.notes  # 落ちずに1つ残し、知らせる


# --- 7. 文字起こし ------------------------------------------------------------------

def test_no_speech_segments_are_dropped(monkeypatch):
    from app.pipeline import transcribe as tr
    w = lambda s, e, t: SimpleNamespace(start=s, end=e, word=t)  # noqa: E731
    segs = [SimpleNamespace(no_speech_prob=0.1, words=[w(0, 1, "軽い")]),
            SimpleNamespace(no_speech_prob=0.9, words=[w(5, 6, "ご視聴ありがとうございました")])]
    seen = {}

    class FakeModel:
        def transcribe(self, path, **kw):
            seen.update(kw)
            return iter(segs), None
    monkeypatch.setitem(tr._MODELS, "fake", FakeModel())
    words = tr._faster_whisper("fake")(Path("x.wav"), "")
    assert [x.text for x in words] == ["軽い"]
    assert seen["hallucination_silence_threshold"] == 1.0 and seen["condition_on_previous_text"] is False


# --- 8. テロップのファイルの場所 ---------------------------------------------------------

def test_filter_path_is_relative_with_slashes(monkeypatch, tmp_path):
    from app import platform as pf
    assert pf.ffmpeg_rel_path(tmp_path / "work" / "t.ass", tmp_path / "work") == "t.ass"
    monkeypatch.setattr(os.path, "relpath", lambda a, b: r"..\..\assets\fonts")   # Windows の区切り
    assert pf.ffmpeg_rel_path(Path("x"), Path("y")) == "../../assets/fonts"

    def other_drive(a, b):
        raise ValueError("path is on mount 'D:', start on mount 'C:'")
    monkeypatch.setattr(os.path, "relpath", other_drive)
    assert pf.ffmpeg_rel_path(Path("x"), Path("y")).startswith("'")       # エスケープする書き方に戻す


# --- 9. Claude の返事の読み取り -----------------------------------------------------------

@pytest.mark.parametrize("text", [
    '{"a": 1}',
    '結果です。\n```json\n{"a": 1}\n```',
    '前置き {これは JSON ではありません} のあと {"a": 1} です',
    '{"a": 1} 以上です。',
])
def test_extract_json_object(text):
    from app.claude.runner import extract_json_object
    assert extract_json_object(text) == {"a": 1}


def test_extract_json_object_none():
    from app.claude.runner import extract_json_object
    assert extract_json_object("JSON はありません") is None
    assert extract_json_object("[1, 2]") is None


# --- 10. project.json の控え -------------------------------------------------------------

def test_broken_project_is_restored_from_backup(tmp_path):
    from app.project import BACKUP_FILE, BROKEN_FILE, RESTORED_NOTICE, create_project, load_project, save_project
    project, folder = create_project(tmp_path, "TEST-1")
    project.title = "1回目"
    save_project(folder, project)
    project.title = "2回目"
    save_project(folder, project)                 # 控えには「1回目」が入る
    assert (folder / BACKUP_FILE).exists()
    (folder / "project.json").write_text('{"schema_version": 2, "id": "壊れ', encoding="utf-8")
    restored = load_project(folder)
    assert restored.title == "1回目" and restored.notice == RESTORED_NOTICE
    assert (folder / BROKEN_FILE).exists()        # 壊れたファイルは消さずに残す
    assert load_project(folder).title == "1回目"  # 書き戻してあるので、次からは普通に読める


# --- 5. 二重実行 --------------------------------------------------------------------

def test_second_job_is_refused_while_one_runs(monkeypatch):
    from app.ui import components
    notes, started = [], []
    monkeypatch.setattr(components.ui, "notify", lambda msg, **kw: notes.append(msg))

    async def slow(*a, **kw):
        started.append(1)
        await asyncio.sleep(0.05)
        return "done"
    monkeypatch.setattr(components, "_run_job", slow)

    async def both():
        return await asyncio.gather(components.run_job("a", None), components.run_job("b", None))
    first, second = asyncio.run(both())
    assert (first, second) == ("done", None) and started == [1]
    assert notes == [components.BUSY_MESSAGE]
    assert not components._busy
