"""画面の各ボタンが呼ぶ処理（app/workflow.py）のテスト。"""

from __future__ import annotations

import json

import pytest

from app import workflow as wf
from app.claude.runner import ClaudeCode, ProcResult
from app.errors import UserError
from app.jobs import Job
from app.pipeline.ffmpeg import duration_of
from app.platform import get_profile
from app.project import Product, Scene, Stage, load_project, save_project
from tests.media import SPEECH, SPEECH2, ScriptedTranscriber, make_clip

SCENES = [Scene(id="s1", title="導入", script="こちらは富士通のノートパソコンです", shots=["全体"], seconds=20),
          Scene(id="s2", title="軽さ", script="重さは約908グラムでとても軽いです", shots=["片手で持つ"], seconds=20),
          Scene(id="s3", title="バッテリー", script="動画再生で約14.5時間使えます", shots=["横から"], seconds=20),
          Scene(id="s4", title="まとめ", script="ぜひ売り場でお試しください", shots=["正面"], seconds=20)]


class FakeClaude:
    def __init__(self):
        self.prompts = []

    def __call__(self, cmd, stdin, cwd, timeout):
        self.prompts.append(stdin)
        if "YouTube" in stdin:
            out = {"title": "富士通 NOTE U 紹介", "description": "・軽い"}
        else:
            out = {"title": "NOTE U", "telops": [{"start": 0.3, "end": 2.5, "text": "約**908g**"}]}
        return ProcResult(0, json.dumps({"is_error": False, "structured_output": out}), "")


@pytest.fixture(scope="module")
def setup(tmp_path_factory):
    root = tmp_path_factory.mktemp("projects")
    src = tmp_path_factory.mktemp("素材")
    # 撮影順は バッテリー → 導入 → 軽さ（台本の順と違う）
    make_clip(src / "01.mp4", 8, speech=SPEECH)
    make_clip(src / "02.mp4", 8, speech=SPEECH2)
    make_clip(src / "03.mp4", 8, speech=SPEECH)
    fake = FakeClaude()
    services = wf.Services(claude=ClaudeCode(claude_path="claude", runner=fake), profile=get_profile(),
                           transcriber=ScriptedTranscriber([
                               "バッテリーは動画再生で約14.5時間", "使えますよ",
                               "こちらは富士通のノートパソコン", "です",
                               "重さは約908グラムで", "とても軽いです", "片手で持てます"]))
    project, folder = wf.start_project("FMVU59L2BA", root)
    project.product = Product(model_number="FMVU59L2BA", name="富士通 NOTE U", category="パソコン")
    project.plan.scenes = SCENES
    save_project(folder, project)
    return folder, src, services, fake


def test_confirm_plan_writes_shooting_list(setup):
    folder, *_ = setup
    sheet = wf.confirm_plan(folder)
    html = sheet.read_text(encoding="utf-8")
    assert "撮影リスト" in html and "片手で持つ" in html and "viewport" in html and "@media print" in html
    assert load_project(folder).stage is Stage.MATERIAL


def test_import_edit_preview_export(setup):
    folder, src, services, fake = setup
    progress = []
    job = Job(lambda f, m: progress.append(m))
    with job.active():
        infos = wf.import_material(folder, src, services, job=job)
    assert len(infos) == 3 and all(wf.thumb_path(folder, f"0{i}.mp4").exists() for i in (1, 2, 3))

    project = load_project(folder)
    project.clips[2].scene_id, project.clips[2].scene_manual = "s4", True   # 3本目は手で「まとめ」に
    save_project(folder, project)

    with Job().active() as job:
        res = wf.build_edit(folder, services, job=job)
    project = load_project(folder)
    by_file = {c.file: c for c in project.clips}
    assert by_file["01.mp4"].scene_id == "s3"      # バッテリーの話 → バッテリーの場面
    assert by_file["02.mp4"].scene_id == "s1"      # 導入
    assert by_file["03.mp4"].scene_id == "s4"      # 手で選んだ場面は変えない
    assert "14.5時間" in by_file["01.mp4"].transcript
    # 台本の順（導入 → バッテリー → まとめ）につながる: 文字起こしを渡した順で確かめる
    prompt = fake.prompts[-1]
    assert prompt.index("富士通のノートパソコン") < prompt.index("14.5時間")
    assert project.title == "NOTE U" and project.stage is Stage.REVIEW
    assert res.joined_seconds > 10

    preview = wf.render_preview(folder, services)
    assert duration_of(services.prof().ffprobe, preview) == pytest.approx(15.0, abs=0.6)

    notes = []
    exports = wf.export_all(folder, services, ["store", "youtube"], notes=notes)
    assert [e.target.key for e in exports] == ["store", "youtube"]
    project = load_project(folder)
    assert project.stage is Stage.DONE and project.youtube_title == "富士通 NOTE U 紹介"
    assert (folder / "output" / "YouTube説明文.txt").exists()
    assert any("ロゴ" in n for n in notes)


def test_steps_explain_what_is_missing(tmp_path):
    project, folder = wf.start_project("X-1", tmp_path)
    services = wf.Services(profile=get_profile())
    with pytest.raises(UserError, match="台本"):
        wf.confirm_plan(folder)
    with pytest.raises(UserError, match="商品情報"):
        wf.make_plan_into(folder, services)
    with pytest.raises(UserError, match="編集"):
        wf.render_preview(folder, services)
    (tmp_path / "空").mkdir()
    with pytest.raises(UserError, match="動画が見つかりません"):
        wf.import_material(folder, tmp_path / "空", services)


def test_ui_pages_load():
    """画面のモジュールが読み込め、ページが登録されている。"""
    import importlib
    from nicegui import app as nicegui_app
    importlib.import_module("app.ui.pages")
    paths = {getattr(r, "path", "") for r in nicegui_app.routes}
    assert {"/", "/p/{pid}/plan", "/p/{pid}/material", "/p/{pid}/review", "/p/{pid}/export"} <= paths
