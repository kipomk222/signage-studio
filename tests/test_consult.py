"""Claude への相談・指示（変更案 → 「反映する」で反映）のテスト。偽の Claude Code を使う。"""

from __future__ import annotations

import pytest

from app import consult as cs
from app.claude.schemas import ChangeField, ConsultChange
from app.claude.workspace import Workspace
from app.errors import UserError
from app.project import (
    Clip, EditDrop, EditInsert, EditPiece, EditPlan, Scene, Stage, Telop, create_project, load_project, save_project,
)
from app.workflow import Services
from tests.test_claude import FakeClaude, cc_with


def ch(action: str, index: int = -1, desc: str = "変更", **fields) -> ConsultChange:
    return ConsultChange(action=action, index=index, description=desc,
                         fields=[ChangeField(name=k, value=str(v)) for k, v in fields.items()])


@pytest.fixture
def folder(tmp_path):
    project, folder = create_project(tmp_path / "projects", "ABC-123")
    project.product.name = "テスト掃除機"
    project.plan.scenes = [Scene(id="s1", title="つかみ", script="こんにちは", seconds=10),
                           Scene(id="s2", title="特徴", script="軽いです", seconds=20)]
    project.clips = [Clip(file="a.mp4", duration=20, scene_id="s1"), Clip(file="b.mp4", duration=10, scene_id="s2"),
                     Clip(file="c.mp4", duration=8, silent=True)]
    project.edit = EditPlan(source="ai", pieces=[
        EditPiece(clip="a.mp4", start=0, end=5, scene_id="s1"),
        EditPiece(clip="a.mp4", start=9, end=14, scene_id="s1"),
        EditPiece(clip="b.mp4", start=1, end=8, scene_id="s2"),
    ], inserts=[EditInsert(clip="c.mp4", start=0, end=3, piece=2)],
        dropped=[EditDrop(clip="a.mp4", start=5, end=9, reason="言い直し")])
    project.telops = [Telop(start=1, end=3, text="軽い"), Telop(start=4, end=6, text="静か"),
                      Telop(start=8, end=10, text="強い")]
    project.stage = Stage.REVIEW
    save_project(folder, project)
    return folder


@pytest.fixture
def ws(tmp_path):
    return Workspace(home=tmp_path / "home")


def test_apply_uses_original_numbers(folder):
    project = load_project(folder)
    touched = cs.apply_to(project, [
        ch("telop_delete", 0),
        ch("telop_update", 2, text="**とても**強い", emphasis_color="#e60012"),  # 消す前の 2 番
        ch("telop_add", start=11, end=13, text="おすすめ"),
        ch("drop_restore", 0),
        ch("piece_move", 2, to=0),
        ch("style", text_color="#ffff00", band_color="#000000"),
        ch("bgm", level="small"),
        ch("title", text="軽さで選ぶ"),
    ])
    assert touched == []  # 企画は変えていない
    assert [t.text for t in project.telops] == ["静か", "**とても**強い", "おすすめ"]
    strong = project.telops[1]
    assert strong.emphasis_color == "#E60012" and strong.manual
    pieces = project.edit.pieces
    assert [(p.clip, p.start) for p in pieces] == [("b.mp4", 1), ("a.mp4", 0), ("a.mp4", 5), ("a.mp4", 9)]
    assert project.edit.dropped == []
    assert project.edit.inserts[0].piece == 0  # 差し込みは b.mp4 の部分についていく
    assert project.settings.telop_style.text_color == "#FFFF00"
    assert project.settings.bgm_level == "small" and project.title == "軽さで選ぶ"


def test_scene_changes_mark_plan(folder):
    project = load_project(folder)
    touched = cs.apply_to(project, [
        ch("scene_update", 1, seconds=25, shots="手元アップ\n全体"),
        ch("scene_delete", 0),
        ch("scene_add", 0, title="まとめ", script="ぜひ店頭で", seconds=10),
        ch("plan_settings", price_mode="open", target_seconds=150),
    ])
    assert touched == [Stage.PLAN]
    assert [s.title for s in project.plan.scenes] == ["まとめ", "特徴"]
    assert project.plan.scenes[1].shots == ["手元アップ", "全体"]
    assert project.plan.scenes[0].id not in ("s2",)
    assert project.clips[0].scene_id is None  # 消した場面のクリップは場面なしに
    assert project.plan.price.mode == "open" and project.plan.target_seconds == 150


def test_check_reports_problems(folder):
    project = load_project(folder)
    problems = cs.check_changes(project, [
        ch("telop_update", 9, desc="9番を直す", text="x"),
        ch("style", desc="色", text_color="あか"),
        ch("bgm", desc="音量", volume="1"),
    ])
    assert len(problems) == 1 and "9番を直す" in problems[0]  # 番号の間違いは先に見る
    problems = cs.check_changes(project, [ch("style", desc="色", text_color="あか"), ch("bgm", desc="音量", volume="1")])
    assert len(problems) == 2 and "#RRGGBB" in problems[0] and "volume" in problems[1]
    # 何も変わっていない
    assert load_project(folder).model_dump() == project.model_dump()
    assert cs.check_changes(project, [ch("piece_update", i, enabled="false") for i in range(3)])


def test_consult_then_apply(folder, ws):
    bad = {"reply": "直します", "changes": [{"action": "telop_update", "index": 7, "description": "7番",
                                           "fields": [{"name": "text", "value": "x"}]}],
           "remember": "", "next_step": "preview"}
    good = {"reply": "強調色を赤にします。よろしければ『反映する』を押してください。",
            "changes": [{"action": "style", "index": -1, "description": "強調色を赤（#E60012）にします",
                         "fields": [{"name": "emphasis_color", "value": "#E60012"}]}],
            "remember": "強調色は赤（#E60012）にする", "next_step": "preview"}
    fake = FakeClaude(bad, good)
    services = Services(claude=cc_with(fake))
    reply = cs.consult(folder, services, "強調は赤がいい。今後もそうして", page="review", workspace=ws)
    assert len(fake.calls) == 2 and "前回の出力の問題" in fake.calls[1][1]
    assert "## テロップ" in fake.calls[0][1] and "「仕上げ」の画面" in fake.calls[0][1]
    assert reply.status == "pending" and reply.next_step == "preview"
    chat = cs.load_chat(folder)
    assert [c.role for c in chat] == ["user", "assistant"]
    # まだ何も変わらない
    assert load_project(folder).settings.telop_style.emphasis_color == "#FFD400"
    assert ws.memories() == []

    project, entry = cs.apply_proposal(folder, 1, workspace=ws)
    assert project.settings.telop_style.emphasis_color == "#E60012"
    assert load_project(folder).settings.telop_style.emphasis_color == "#E60012"
    assert ws.memories() == ["強調色は赤（#E60012）にする"]
    assert cs.load_chat(folder)[1].status == "applied"
    assert [h["kind"] for h in ws.history()][:2] == ["apply", "memory"]
    with pytest.raises(UserError):
        cs.apply_proposal(folder, 1, workspace=ws)  # 2回は反映しない


def test_new_message_declines_old_proposal(folder, ws):
    proposal = {"reply": "BGMを小さくします", "next_step": "preview", "remember": "",
                "changes": [{"action": "bgm", "index": -1, "description": "BGMを小に",
                             "fields": [{"name": "level", "value": "small"}]}]}
    chat_only = {"reply": "2分半くらいがおすすめです。", "changes": [], "remember": "", "next_step": "none"}
    services = Services(claude=cc_with(FakeClaude(proposal, chat_only)))
    cs.consult(folder, services, "BGMうるさい", workspace=ws)
    reply = cs.consult(folder, services, "長さはどれくらいがいい？", workspace=ws)
    chat = cs.load_chat(folder)
    assert chat[1].status == "declined" and reply.status == "" and len(chat) == 4
    with pytest.raises(UserError):
        cs.apply_proposal(folder, 1, workspace=ws)


def test_stale_proposal_is_refused(folder, ws):
    proposal = {"reply": "3番目を消します", "next_step": "preview", "remember": "",
                "changes": [{"action": "telop_delete", "index": 2, "description": "3番目のテロップを消す",
                             "fields": []}]}
    services = Services(claude=cc_with(FakeClaude(proposal)))
    cs.consult(folder, services, "最後のテロップいらない", workspace=ws)
    project = load_project(folder)
    project.telops = project.telops[:1]  # そのあと画面で消した
    save_project(folder, project)
    with pytest.raises(UserError) as e:
        cs.apply_proposal(folder, 1, workspace=ws)
    assert "合わなくなりました" in e.value.what


def test_plan_change_marks_later_stages_stale(folder, ws):
    proposal = {"reply": "場面を直します", "next_step": "none", "remember": "",
                "changes": [{"action": "scene_update", "index": 0, "description": "つかみを短く",
                             "fields": [{"name": "seconds", "value": "6"}]}]}
    services = Services(claude=cc_with(FakeClaude(proposal)))
    cs.consult(folder, services, "つかみを短く", workspace=ws)
    project, _ = cs.apply_proposal(folder, 1, workspace=ws)
    assert Stage.MATERIAL in project.stale and Stage.REVIEW in project.stale


def test_voice_and_narration_changes(folder):
    from app.project import VoiceTake
    project = load_project(folder)
    project.plan.voice_mode = "separate"
    project.plan.scenes[0].voice, project.plan.scenes[1].voice = "talk", "narration"
    project.voices = [VoiceTake(file="a.m4a", scene_id="s2", use=True, duration=4),
                      VoiceTake(file="b.m4a", scene_id=None, use=False, duration=5)]
    touched = cs.apply_to(project, [ch("voice_assign", 1, scene="1"),
                                    ch("scene_update", 1, script="とても軽いです", points="軽さ")])
    assert Stage.NARRATION in touched and Stage.PLAN in touched
    a, b = project.voices
    assert b.scene_id == "s2" and b.use and not a.use        # 同じ場面で使うのは1つ
    assert project.plan.scenes[1].script == "とても軽いです" and project.plan.scenes[1].points == "軽さ"
    assert cs.check_changes(project, [ch("voice_assign", 0, scene="0")])  # 顔出しの場面には割り当てない
    assert "録った声" in cs.context_text(folder, project) and "映像だけ" in cs.context_text(folder, project)


def test_empty_message(folder, ws):
    with pytest.raises(UserError):
        cs.consult(folder, Services(claude=cc_with(FakeClaude())), "  ", workspace=ws)
