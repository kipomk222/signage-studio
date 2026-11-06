"""声を別に録る作り方の通しテスト: 映像 → 台本 → 録った声の振り分け → 尺合わせ → 書き出し。"""

from __future__ import annotations

import pytest

from app import voiceflow as vf
from app import workflow as wf
from app.claude.runner import ClaudeCode
from app.errors import UserError
from app.pipeline.fit import FADE, LEAD, TAIL
from app.pipeline.narration import recording_name, scene_from_name, script_match
from app.platform import get_profile
from app.project import Product, Scene, Stage, load_project, save_project
from tests.media import ScriptedTranscriber, make_clip, make_voice
from tests.test_claude import FakeClaude

SCENES = [
    Scene(id="s1", title="あいさつ", voice="talk", script="今回はこのパソコンを紹介します", points="紹介", seconds=6,
          shots=["正面で話す"]),
    Scene(id="s2", title="軽さ", points="約908gで軽い", seconds=5, shots=["持ち上げる（約3秒）", "横から（約3秒）"]),
    Scene(id="s3", title="画面", points="きれいな画面", seconds=5, shots=["画面アップ（約3秒）"]),
    Scene(id="s4", title="店舗案内", voice="talk", script="ぜひ売り場へお越しください", points="案内", seconds=4,
          shots=["正面で話す"]),
]
S2 = "本体はとても軽くて片手でも楽に持ち上げられます"
S3 = "画面はとても明るく細かい文字までくっきり見えます"


def cut(clip, a, b, sid, talk=False, shows="映像"):
    return {"clip": clip, "start": a, "end": b, "scene_id": sid, "talk": talk, "shows": shows, "reason": "r"}


VIDEO = {"summary": "映像を組み立てました", "dropped": [],
         "cuts": [cut("01_hello.mp4", 0.3, 7.6, "s1", True), cut("02.mp4", 0, 3, "s2", shows="持ち上げる"),
                  cut("03.mp4", 0, 3, "s2", shows="横から"), cut("04.mp4", 0, 3, "s3", shows="画面アップ"),
                  cut("05.mp4", 0, 2.5, "s3", shows="画面の文字"), cut("06_bye.mp4", 0.2, 3.2, "s4", True)],
         "spares": [cut("03.mp4", 3.2, 6, "s2"), cut("05.mp4", 3, 6, "s3")]}
NARRATION = {"scenes": [{"scene_id": "s2", "script": S2}, {"scene_id": "s3", "script": S3}]}
ASSIGN = {"assignments": [{"file": "新規録音.m4a", "scene_id": "s2", "reason": "軽さの話"},
                          {"file": "新規録音 2.m4a", "scene_id": "s2", "reason": "軽さの話（言い直し）"}]}
TELOPS = {"title": "軽いパソコン", "telops": [{"start": 1.0, "end": 3.0, "text": "**軽い**"}]}


@pytest.fixture
def project_folder(tmp_path):
    src = tmp_path / "素材"
    make_clip(src / "01_hello.mp4", 8, speech=[(0.5, 2.5), (3.5, 5.0), (6.2, 7.5)])
    for i in range(2, 6):   # 話していない映像（中身が違うように長さを少しずつ変える）
        make_clip(src / f"0{i}.mp4", 6 + i * 0.1, speech=None)
    make_clip(src / "06_bye.mp4", 4, speech=[(0.4, 3.0)])
    voices = tmp_path / "声"
    make_voice(voices / "03_画面.m4a", 7.0)          # 名前で分かる。映像より長い → 伸ばす
    make_voice(voices / "新規録音 2.m4a", 4.0)        # 録り直し（台本と違う）
    make_voice(voices / "新規録音.m4a", 4.0)          # 中身で振り分け。映像より短い → 詰める
    fake = FakeClaude(VIDEO, NARRATION, ASSIGN, TELOPS)
    services = wf.Services(claude=ClaudeCode(claude_path="claude", runner=fake), profile=get_profile(),
                           transcriber=ScriptedTranscriber([
                               "今回は", "このパソコンを", "紹介します", "ぜひ売り場へお越しください",
                               S3, "えーと軽くてですね", S2]))
    project, folder = wf.start_project("X-1", tmp_path / "projects")
    project.product = Product(model_number="X-1", name="テストPC", category="パソコン")
    project.plan.scenes = SCENES
    save_project(folder, project)
    wf.confirm_plan(folder)
    wf.import_material(folder, src, services)
    return folder, voices, services, fake


def test_separate_voice_flow(project_folder):
    folder, voices, services, fake = project_folder
    assert load_project(folder).separate_voice

    res = wf.build_edit(folder, services)          # 声を別に録る動画は、映像だけを先に作る
    p = load_project(folder)
    assert res.source == "ai" and p.stage is Stage.VIDEO
    assert [x.mute for x in p.edit.pieces] == [False, True, True, True, True, False]
    assert (folder / "work" / "video.mp4").exists() and len(p.edit.spares) == 2
    assert "映像だけを完成" in fake.calls[0][1] or "先に映像だけ" in fake.calls[0][1]

    vf.write_script(folder, services)
    p = load_project(folder)
    assert p.stage is Stage.NARRATION and p.plan.scenes[1].script == S2
    prompt = fake.calls[1][1]
    assert "持ち上げる" in prompt and "映像 5.7秒" in prompt
    sheet = (folder / "output" / "録音用の台本.html").read_text(encoding="utf-8")
    assert S2 in sheet and "02_軽さ" in sheet
    assert (folder / "output" / "読み合わせ用の映像.mp4").exists()

    vf.import_voices(folder, voices, services)
    p = load_project(folder)
    by = {v.file: v for v in p.voices}
    assert by["03_画面.m4a"].scene_id == "s3" and by["03_画面.m4a"].assigned_by == "name"
    assert by["新規録音.m4a"].scene_id == "s2" and by["新規録音.m4a"].assigned_by == "claude"
    assert by["新規録音.m4a"].use and not by["新規録音 2.m4a"].use     # 台本に近い方を使う
    assert abs(by["新規録音.m4a"].duration - 4.0) < 0.5               # 前後の無音を切った
    assert "新規録音.m4a" in fake.calls[2][1] and "03_画面.m4a" not in fake.calls[2][1]
    assert (folder / "声" / "新規録音.m4a").exists() and (voices / "新規録音.m4a").exists()  # 元は残す

    out = vf.finish_with_voices(folder, services)
    p = load_project(folder)
    assert p.stage is Stage.REVIEW and p.title == "軽いパソコン" and p.telops
    layouts, total = vf._load_layouts(vf._layout_path(folder, "final"))
    spans = vf.scene_spans(layouts)
    for sid, file in (("s2", "新規録音.m4a"), ("s3", "03_画面.m4a")):
        want = LEAD + by[file].duration + TAIL
        # 場面の見えている長さ（次の場面と重なるつなぎ目の分を引く）が、声＋前後の間と同じ
        assert spans[sid][1] - spans[sid][0] - FADE == pytest.approx(want, abs=0.15), sid
    assert out.seconds == pytest.approx(total, abs=0.01)
    assert (folder / "work" / "joined.mp4").exists()
    telop_prompt = fake.calls[3][1]
    assert S2[:6] in telop_prompt                                       # 別録りの声の中身からテロップを作る

    exports = wf.export_all(folder, services, outputs=["store"])
    assert exports[0].path.exists() and load_project(folder).stage is Stage.DONE


def test_manual_voice_choice(project_folder):
    folder, voices, services, fake = project_folder
    wf.build_edit(folder, services)
    vf.write_script(folder, services)
    vf.import_voices(folder, voices, services)
    vf.set_voice_use(folder, "新規録音 2.m4a")
    p = load_project(folder)
    by = {v.file: v for v in p.voices}
    assert by["新規録音 2.m4a"].use and not by["新規録音.m4a"].use
    vf.set_voice_scene(folder, "新規録音.m4a", None)
    assert not {v.file: v for v in load_project(folder).voices}["新規録音.m4a"].use


def test_voice_steps_explain_what_is_missing(project_folder):
    folder, voices, services, fake = project_folder
    with pytest.raises(UserError, match="映像"):
        vf.write_script(folder, services)
    with pytest.raises(UserError, match="声のファイル"):
        vf.import_voices(folder, folder / "素材", services)


def test_scene_from_file_name():
    assert scene_from_name("03_画面.m4a", SCENES).id == "s3"
    assert scene_from_name("場面2.m4a", SCENES).id == "s2"
    assert scene_from_name("軽さ 撮り直し.m4a", SCENES).id == "s2"
    assert scene_from_name("新規録音 3.m4a", SCENES) is None     # 録音アプリの番号は場面番号にしない
    assert scene_from_name("01_あいさつ.m4a", SCENES) is None     # 顔出しの場面には声を入れない
    assert recording_name(2, SCENES[1]) == "02_軽さ"
    assert script_match(S2, S2) == 1 and script_match("全然ちがう話", S2) < 0.3
