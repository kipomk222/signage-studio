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


EDIT = {  # Claude Code が返す編集の設計図（台本の順: 導入 → バッテリー → まとめ）
    "summary": "台本の順に並べ、言い直しを切りました。",
    "pieces": [{"clip": "02.mp4", "start": 0.2, "end": 7.7, "scene_id": "s1", "reason": "導入"},
               {"clip": "01.mp4", "start": 6.0, "end": 7.7, "scene_id": "s3", "reason": "2回目のテイクの方が自然"},
               {"clip": "03.mp4", "start": 0.3, "end": 7.4, "scene_id": "s4", "reason": "まとめ"}],
    "inserts": [],
    "dropped": [{"clip": "01.mp4", "start": 0.3, "end": 5.2, "reason": "言い直し"}],
}


class FakeClaude:
    def __init__(self, edit=EDIT):
        self.prompts, self.edit = [], edit

    def __call__(self, cmd, stdin, cwd, timeout):
        self.prompts.append(stdin)
        if "編集の設計図" in stdin:
            out = self.edit
        elif "YouTube" in stdin:
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
    make_clip(src / "03.mp4", 7.5, speech=SPEECH)  # 01.mp4 と同じだと「重複」で除外される
    fake = FakeClaude()
    services = wf.Services(claude=ClaudeCode(claude_path="claude", runner=fake), profile=get_profile(),
                           transcriber=ScriptedTranscriber([
                               "バッテリーは動画再生で約", "あ、すみません", "バッテリーは動画再生で約14.5時間使えます",
                               "こちらは富士通の", "ノートパソコンです",
                               "ぜひ売り場で", "お試しください", "よろしくお願いします"]))
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

    # まずは機械的な編集（AI なし）: 文字起こしと台本の照合で並べる
    with Job().active() as job:
        res = wf.build_edit(folder, services, job=job, ai=False)
    project = load_project(folder)
    by_file = {c.file: c for c in project.clips}
    assert by_file["01.mp4"].scene_id == "s3"      # バッテリーの話 → バッテリーの場面
    assert by_file["02.mp4"].scene_id == "s1"      # 導入
    assert by_file["03.mp4"].scene_id == "s4"      # 手で選んだ場面は変えない
    assert "14.5時間" in by_file["01.mp4"].transcript
    assert project.edit.source == "auto" and [p.clip for p in project.edit.pieces] == ["02.mp4", "01.mp4", "03.mp4"]
    prompt = fake.prompts[-1]   # テロップの依頼: 台本の順（導入 → バッテリー）に並んだ文字起こし
    assert prompt.index("富士通のノートパソコン") < prompt.index("14.5時間")
    assert project.title == "NOTE U" and project.stage is Stage.REVIEW
    assert res.joined_seconds > 10 and res.source == "auto"

    # Claude Code に編集を考えてもらう: 言い直し（01.mp4 の前半）を切る
    n = len(fake.prompts)
    res = wf.replan_edit(folder, services)
    edit_prompt = next(p for p in fake.prompts[n:] if "編集の設計図" in p)
    assert "01.mp4（8.0秒・話している秒数" in edit_prompt and "あ、すみません" in edit_prompt and "sheet_01.jpg" in edit_prompt
    project = load_project(folder)
    assert res.source == "ai" and project.edit.dropped[0].reason == "言い直し"
    telop_prompt = fake.prompts[-1]
    assert "あ、すみません" not in telop_prompt      # 切った言い直しはテロップの材料にも入らない
    assert telop_prompt.index("ノートパソコン") < telop_prompt.index("14.5時間")
    # 部分の中の間も詰める: 02(6.75) + 01(1.6) + 03(5.45) − つなぎ目 2×0.3
    assert res.joined_seconds == pytest.approx(6.75 + 1.6 + 5.45 - 0.6, abs=0.4)

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


def test_inserts_overlay_broll_picture_but_keep_voice(tmp_path):
    """差し込み: 説明の部分の途中から、別クリップ（赤い映像・話し声なし）の映像を重ねる。"""
    import subprocess
    from PIL import Image
    from app.pipeline.edl import PieceLayout, intersect, map_time, place_inserts
    from app.project import EditInsert, EditPiece, EditPlan

    src = tmp_path / "素材"
    make_clip(src / "talk.mp4", 8, speech=SPEECH)
    subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-f", "lavfi", "-i", "color=c=red:s=640x360:r=30:d=6",
                    "-c:v", "libx264", "-pix_fmt", "yuv420p", str(src / "broll.mp4")], check=True)
    edit = {"summary": "差し込みあり",
            "pieces": [{"clip": "talk.mp4", "start": 0.3, "end": 7.7, "scene_id": "s1", "reason": "説明"}],
            "inserts": [{"clip": "broll.mp4", "start": 1.0, "end": 3.0, "over_piece": 0, "offset": 3.4,
                         "reason": "手元アップ"}],
            "dropped": []}
    services = wf.Services(claude=ClaudeCode(claude_path="claude", runner=FakeClaude(edit)), profile=get_profile(),
                           transcriber=ScriptedTranscriber(["説明です"]))
    project, folder = wf.start_project("X-1", tmp_path / "p")
    project.product = Product(model_number="X-1", name="テスト")
    project.plan.scenes = SCENES[:1]
    save_project(folder, project)
    wf.import_material(folder, src, services)
    res = wf.build_edit(folder, services)
    project = load_project(folder)
    assert res.source == "ai" and project.edit.inserts[0].reason == "手元アップ"
    assert project.edit.inserts[0].clip == "broll.mp4"

    # 重ねる位置: talk.mp4 の 3.7秒（0.3 + 3.4）が、つないだ動画の何秒目か
    lay = PieceLayout(0, "talk.mp4", intersect((0.3, 7.7), [(0.35, 2.65), (3.35, 5.15), (6.05, 7.65)]), 0.0)
    at = map_time([lay], "talk.mp4", 3.7)
    assert at == pytest.approx(2.3 + 0.35, abs=0.05)
    placed = place_inserts(project.edit, [lay], 5.5)
    assert placed[0].at == pytest.approx(at, abs=0.05) and placed[0].length == pytest.approx(2.0, abs=0.05)

    joined = folder / "work" / "joined.mp4"
    def color_at(t):
        png = tmp_path / f"f{t}.png"
        subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-ss", str(t), "-i", str(joined), "-frames:v", "1",
                        str(png)], check=True)
        return Image.open(png).convert("RGB").getpixel((100, 100))
    r, g, b = color_at(at + 1.0)
    assert r > 180 and g < 80 and b < 80                       # 差し込み中は赤い映像
    r, g, b = color_at(0.5)
    assert not (r > 180 and g < 80 and b < 80)                 # それ以外は元の映像
    # 音は説明のまま（差し込み中も話し声が聞こえる）
    out = subprocess.run(["ffmpeg", "-hide_banner", "-ss", f"{at + 0.3}", "-t", "1", "-i", str(joined), "-vn",
                          "-af", "volumedetect", "-f", "null", "-"], capture_output=True, text=True).stderr
    import re
    assert float(re.search(r"mean_volume: (-?[\d.]+)", out).group(1)) > -40


def test_restore_and_move_keep_inserts_attached():
    from app.project import EditDrop, EditInsert, EditPiece, EditPlan, Project
    from datetime import datetime
    p = Project(id="x", created_at=datetime.now(), updated_at=datetime.now(), product=Product(model_number="X"))
    p.edit = EditPlan(pieces=[EditPiece(clip="a.mp4", start=0, end=3), EditPiece(clip="b.mp4", start=0, end=3)],
                      inserts=[EditInsert(clip="c.mp4", start=0, end=2, piece=1)],
                      dropped=[EditDrop(clip="a.mp4", start=4, end=6, reason="言い直し")])
    wf.restore_drop(p, 0)
    assert [x.clip for x in p.edit.pieces] == ["a.mp4", "a.mp4", "b.mp4"] and p.edit.inserts[0].piece == 2
    assert p.edit.pieces[1].reason.startswith("戻した") and not p.edit.dropped
    wf.move_piece(p, 2, -1)
    assert [x.clip for x in p.edit.pieces] == ["a.mp4", "b.mp4", "a.mp4"] and p.edit.inserts[0].piece == 1
    # 「えーっと」（b.mp4 の頭）を戻すと、b.mp4 の説明の前に入る
    p.edit.dropped.append(EditDrop(clip="b.mp4", start=0, end=-0.0 + 0.9, reason="えーっと"))
    p.edit.pieces[1].start = 1.2
    wf.restore_drop(p, 0)
    assert [(x.clip, x.start) for x in p.edit.pieces][1:3] == [("b.mp4", 0), ("b.mp4", 1.2)]
    assert p.edit.inserts[0].piece == 2
