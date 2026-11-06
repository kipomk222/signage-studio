"""段階2: 編集処理（画面なし）のテスト。試験用の動画を作って通しで確かめる。"""

from __future__ import annotations

import hashlib
import re
import subprocess
from pathlib import Path

import pytest

from app.pipeline import bgm as bgm_step
from app.pipeline.concat import xfade_offsets
from app.pipeline.ffmpeg import probe
from app.pipeline.inventory import list_videos, scan_folder
from app.pipeline.normalize import normalize_clip
from app.pipeline.run import edit_folder, main
from app.pipeline.silence import STRENGTH, jumpcut, keep_ranges, parse_silences
from app.platform import get_profile
from app.project import Stage, load_project
from tests.media import SPEECH, make_bgm, make_clip, make_sample_folder


@pytest.fixture(scope="session")
def profile():
    return get_profile()


@pytest.fixture(scope="session")
def samples(tmp_path_factory):
    folder = tmp_path_factory.mktemp("素材")
    clips = make_sample_folder(folder)
    return folder, clips


@pytest.fixture(scope="session")
def bgm_file(tmp_path_factory):
    return make_bgm(tmp_path_factory.mktemp("bgm") / "bgm.m4a")


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _quiet(*_args):
    pass


# --- 純粋な計算 ------------------------------------------------------------

def test_keep_ranges_adds_padding_and_merges():
    silences = [(0.0, 0.5), (2.5, 3.5), (5.0, 6.2), (7.5, 8.0)]
    assert keep_ranges(silences, 8.0, 0.15) == [(0.35, 2.65), (3.35, 5.15), (6.05, 7.65)]
    # 余白で重なる場合はひとつにまとめる
    assert keep_ranges([(1.0, 1.2)], 3.0, 0.15) == [(0.0, 3.0)]
    # 全部無音なら何も残らない
    assert keep_ranges([(0.0, 3.0)], 3.0, 0.15) == []


def test_parse_silences_handles_trailing_silence():
    err = "silence_start: 0\nsilence_end: 0.5 | silence_duration: 0.5\nsilence_start: 7.4\n"
    assert parse_silences(err, 8.0) == [(0.0, 0.5), (7.4, 8.0)]


def test_xfade_offsets():
    assert xfade_offsets([5.0, 4.0, 6.0], 0.3) == [4.7, 8.4]


def test_natural_sort(tmp_path):
    for n in ["10.mp4", "2.mp4", "1.MOV", "memo.txt", ".hidden.mp4"]:
        (tmp_path / n).write_bytes(b"x")
    assert [p.name for p in list_videos(tmp_path)] == ["1.MOV", "2.mp4", "10.mp4"]


# --- 各ステップ ------------------------------------------------------------

def test_inventory_marks_exclusion_candidates(profile, samples):
    folder, _ = samples
    info = {c.path.name: c for c in scan_folder(profile, folder)}
    assert set(info) == {"01_talk.mp4", "02_vertical.mp4", "03_short.mp4",
                         "04_silent.mp4", "05_talk.mp4", "06_copy_of_talk1.mp4"}
    assert not info["01_talk.mp4"].excluded
    assert not info["05_talk.mp4"].excluded
    assert info["02_vertical.mp4"].vertical and not info["02_vertical.mp4"].excluded
    assert "短い" in info["03_short.mp4"].exclude_reasons[0]
    assert info["04_silent.mp4"].silent and not info["04_silent.mp4"].excluded   # 差し込み用に残す
    assert "01_talk.mp4" in info["06_copy_of_talk1.mp4"].exclude_reasons[0]


def test_jumpcut_removes_known_silences(profile, samples, tmp_path):
    _, clips = samples
    clip = next(c for c in scan_folder(profile, clips["talk1"].parent) if c.path.name == "01_talk.mp4")
    norm = normalize_clip(profile, clip, tmp_path / "n.mp4")
    result = jumpcut(profile, norm, tmp_path / "c.mp4", STRENGTH["normal"])
    spoken = sum(b - a for a, b in SPEECH)  # 4.8 秒
    assert result.kept == pytest.approx(spoken + 6 * 0.15, abs=0.15)  # 発話 + 前後の余白
    assert not result.over_cut
    assert float(probe(profile.ffprobe, result.dst)["format"]["duration"]) == pytest.approx(result.kept, abs=0.1)


def test_clip_without_speech_is_kept_whole(profile, tmp_path):
    src = make_clip(tmp_path / "broll.mp4", 4, speech=None)
    clip = scan_folder(profile, tmp_path)[0]
    norm = normalize_clip(profile, clip, tmp_path / "out" / "n.mp4")
    result = jumpcut(profile, norm, tmp_path / "out" / "c.mp4", STRENGTH["normal"])
    assert result.no_speech and result.kept == pytest.approx(4.0, abs=0.1)


def test_clip_without_audio_track_gets_silence(profile, tmp_path):
    make_clip(tmp_path / "noaudio.mp4", 4, audio=False)
    clip = scan_folder(profile, tmp_path)[0]
    assert not clip.has_audio
    norm = normalize_clip(profile, clip, tmp_path / "out" / "n.mp4")
    kinds = {s["codec_type"] for s in probe(profile.ffprobe, norm)["streams"]}
    assert kinds == {"video", "audio"}


def _band_rms(profile, path: Path, start: float, end: float, freq: int) -> float:
    """指定区間・周波数帯の音の大きさ（dB）。"""
    out = subprocess.run(
        [profile.ffmpeg, "-hide_banner", "-nostdin", "-ss", str(start), "-t", str(end - start), "-i", str(path),
         "-vn", "-af", f"bandpass=f={freq}:width_type=h:w=30,astats=metadata=0:measure_overall=RMS_level",
         "-f", "null", "-"],
        capture_output=True, text=True, check=True,
    ).stderr
    return float(re.findall(r"RMS level dB:\s*(-?[\d.]+)", out)[-1])


def test_bgm_ducks_under_speech(profile, tmp_path):
    """話している間は BGM が下がる。声（220/330Hz）から離れた 1500Hz の BGM で測る。"""
    make_clip(tmp_path / "talk.mp4", 8, speech=SPEECH)
    tone = make_bgm(tmp_path / "bgm" / "tone.m4a", freqs=(1500,))
    clip = scan_folder(profile, tmp_path)[0]
    norm = normalize_clip(profile, clip, tmp_path / "out" / "n.mp4")
    mixed = bgm_step.mix_bgm(profile, norm, tone, tmp_path / "out" / "m.mp4", level="medium")
    during_speech = _band_rms(profile, mixed, 4.0, 4.8, 1500)
    after_release = _band_rms(profile, mixed, 6.0, 6.2, 1500)  # 無音区間 5.0〜6.2 の終わり
    assert after_release - during_speech > 6


# --- 通し ------------------------------------------------------------------

def test_edit_folder_end_to_end(profile, samples, bgm_file, tmp_path):
    """完了条件: 素材フォルダから、テロップなしの完成動画がコマンド1つで出る。"""
    folder, clips = samples
    before = {p.name: _sha(p) for p in folder.iterdir()}

    report = edit_folder(folder, "EPC01A-20BK", projects_root=tmp_path, bgm_path=bgm_file,
                         outputs=["store", "youtube"], profile=profile, progress=_quiet)

    assert {p.name: _sha(p) for p in folder.iterdir()} == before  # 元の素材は変えない
    assert len(report.cuts) == 4  # 01, 02(縦), 04(話し声なし), 05 を使用
    assert next(r for r in report.cuts if r.src.name == "04_silent.mp4").no_speech
    assert 20 < report.joined_seconds < 25
    for e in report.exports:
        streams = probe(profile.ffprobe, e.path)["streams"]
        v = next(s for s in streams if s["codec_type"] == "video")
        a = next(s for s in streams if s["codec_type"] == "audio")
        assert (v["width"], v["height"], v["r_frame_rate"]) == (1920, 1080, "30/1")
        assert a["sample_rate"] == "48000"
        assert e.lufs == pytest.approx(e.target.lufs, abs=1.0)
        assert e.true_peak <= e.target.true_peak + 0.5
        assert e.duration == pytest.approx(report.joined_seconds, abs=0.2)

    project = load_project(report.project_dir)
    assert project.stage is Stage.DONE
    assert [c.file for c in project.clips if not c.excluded] == ["01_talk.mp4", "02_vertical.mp4", "04_silent.mp4",
                                                                 "05_talk.mp4"]
    assert all(c.cut_ranges for c in project.clips if not c.excluded)
    assert project.settings.bgm_file == "bgm.m4a"
    assert (report.project_dir / "素材" / "01_talk.mp4").exists()
    assert "店頭用" in (report.project_dir / "output" / "レポート.txt").read_text(encoding="utf-8")


def test_tightens_cuts_when_over_target(profile, samples, tmp_path):
    folder, _ = samples
    report = edit_folder(folder, "TIGHT-1", projects_root=tmp_path, bgm_path=None,
                         target_seconds=5, profile=profile, progress=_quiet)
    assert any("強めました" in n for n in report.notes)
    assert any("BGM なし" in n for n in report.notes)


def test_cli_reports_friendly_errors(tmp_path, capsys):
    assert main([str(tmp_path / "ない"), "--model", "X-1", "--projects", str(tmp_path)]) == 1
    assert "素材フォルダが見つかりません" in capsys.readouterr().out
    assert main([str(tmp_path), "--model", "X-1", "--out", "tv"]) == 2


def test_edit_folder_with_telops(profile, samples, bgm_file, tmp_path):
    """段階4: 文字起こし → 型番補正 → テロップ → タイトル・エンドカードまで通す。"""
    import json as _json
    from app.claude.runner import ClaudeCode, ProcResult
    from app.pipeline.run import TelopInputs
    from app.project import Price, Product
    from tests.media import ScriptedTranscriber

    product = Product(model_number="FMVU59L2BA", name="富士通 NOTE Uシリーズ", maker="富士通", category="パソコン",
                      specs={"CPU": "Core Ultra 5 226V"})
    prompts = []

    def fake_claude(cmd, stdin, cwd, timeout):
        prompts.append(stdin)
        out = {"title": "軽さで選ぶ NOTE U", "telops": [{"start": 0.3, "end": 2.5, "text": "富士通 NOTE U"},
                                                    {"start": 3.0, "end": 5.5, "text": "約908gの軽さ"}]}
        if "食い違っていないか確かめて" in stdin:   # テロップと映像の照合
            out = {"summary": "食い違いはありません。", "fixes": []}
        return ProcResult(0, _json.dumps({"is_error": False, "structured_output": out}), "")

    folder, _ = samples
    report = edit_folder(
        folder, "FMVU59L2BA", projects_root=tmp_path, bgm_path=bgm_file, profile=profile, progress=_quiet,
        ai_edit=False,
        telop=TelopInputs(product=product, price=Price(mode="open"),
                          claude=ClaudeCode(claude_path="claude", runner=fake_claude),
                          transcriber=ScriptedTranscriber(["こちらはエフエムブイのノートユーです", "コアウルトラ五搭載"])))

    assert "Core Ultra 5" in prompts[0] and "コアウルトラ五" not in prompts[0]   # 補正してから渡す
    assert "NOTE U" in prompts[0] and "オープン価格" in prompts[0]
    assert report.title == "軽さで選ぶ NOTE U" and len(report.telops) == 2
    log = (report.project_dir / "output" / "字幕修正ログ.txt").read_text(encoding="utf-8")
    assert "コアウルトラ五 → Core Ultra 5" in log
    # タイトル3秒 + 本編 + エンドカード5秒（つなぎ目2か所で 0.3 秒ずつ重なる）
    expected = report.joined_seconds + 3 + 5 - 0.6
    assert report.exports[0].duration == pytest.approx(expected, abs=0.3)
    project = load_project(report.project_dir)
    assert project.title == "軽さで選ぶ NOTE U" and project.telops[1].text == "約908gの軽さ"
    assert project.product.model_number == "FMVU59L2BA" and project.plan.price.mode == "open"
    assert any("ロゴが見つからなかった" in n for n in report.notes)
    assert "■ テロップ（2枚）" in report.text()


def test_rerender_changes_colors_without_reediting(profile, samples, bgm_file, tmp_path):
    """色だけ変えて書き出し直す（文字起こし・テロップ作りはやり直さない）。"""
    import json as _json
    from app.claude.runner import ClaudeCode, ProcResult
    from app.errors import UserError
    from app.pipeline.run import TelopInputs, rerender
    from app.project import Product, TelopStyle, save_project
    from tests.media import ScriptedTranscriber

    calls = []

    def fake_claude(cmd, stdin, cwd, timeout):
        calls.append(1)
        out = {"title": "NOTE U", "telops": [{"start": 0.3, "end": 2.5, "text": "約**908g**の軽さ"}]}
        if "食い違っていないか確かめて" in stdin:   # テロップと映像の照合
            out = {"summary": "食い違いはありません。", "fixes": []}
        return ProcResult(0, _json.dumps({"is_error": False, "structured_output": out}), "")

    folder, _ = samples
    report = edit_folder(folder, "FMVU59L2BA", projects_root=tmp_path, bgm_path=bgm_file, profile=profile,
                         progress=_quiet, ai_edit=False,
                         telop=TelopInputs(product=Product(model_number="FMVU59L2BA", name="NOTE U"),
                                           claude=ClaudeCode(claude_path="claude", runner=fake_claude),
                                           transcriber=ScriptedTranscriber(["こんにちは"]),
                                           style=TelopStyle(emphasis_color="#FF0000")))
    pdir = report.project_dir
    assert "\\c&H0000FF&}908g" in (pdir / "work" / "telops.ass").read_text(encoding="utf-8-sig")
    assert load_project(pdir).settings.telop_style.emphasis_color == "#FF0000"

    # 手でテロップを直し、色も変えて作り直す
    project = load_project(pdir)
    project.telops[0].text = "**約908g**で軽い"
    project.telops[0].manual = True
    save_project(pdir, project)
    exports = rerender(pdir, TelopStyle(emphasis_color="#00A0E9", band_color="#000000"),
                       profile=profile, progress=_quiet)
    ass = (pdir / "work" / "telops.ass").read_text(encoding="utf-8-sig")
    assert "\\c&HE9A000&}約908g" in ass and "Style: band_" in ass
    assert len(calls) == 2                                   # テロップ作りと照合の2回だけ（作り直しでは呼ばない）
    assert exports[0].path.exists() and exports[0].duration == pytest.approx(report.exports[0].duration, abs=0.3)
    saved = load_project(pdir)
    assert saved.settings.telop_style.band_color == "#000000" and saved.telops[0].manual

    with pytest.raises(UserError):
        rerender(tmp_path / "ない", profile=profile)


def test_cli_color_errors_come_first(tmp_path, capsys):
    assert main([str(tmp_path), "--model", "X-1", "--emphasis-color", "きいろ"]) == 1
    out = capsys.readouterr().out
    assert "読み取れません" in out and "#FFD400" in out
