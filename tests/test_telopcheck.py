"""テロップと映像の照合のテスト（偽の Claude）。"""

from __future__ import annotations

import pytest

from app import telopcheck as tc
from app.claude.runner import ClaudeCode
from app.claude.schemas import TelopCheckOut, make_telop_fix_check
from app.pipeline.edl import PieceLayout, save_layouts
from app.platform import get_profile
from app.project import Product, Telop, create_project, load_project, save_project
from app.workflow import Services
from tests.media import make_clip
from tests.test_claude import FakeClaude

VISUAL = [(0.0, 4.0, "本体を持ち上げる"), (3.7, 8.0, "キーボードで文字を打つ手元"), (7.7, 12.0, "ペンで画面に書く手元")]


@pytest.fixture
def folder(tmp_path):
    project, folder = create_project(tmp_path, "X-1")
    project.product = Product(model_number="X-1", name="テストPC")
    project.telops = [Telop(start=0.5, end=3.0, text="**軽い**"),
                      Telop(start=4.0, end=7.0, text="ペンで**手書き**もOK"),          # キーボードの映像に乗っている
                      Telop(start=10.0, end=11.8, text="画面がきれい", manual=True)]  # 手で直したもの
    save_project(folder, project)
    make_clip(folder / "work" / "joined.mp4", 12, size="320x180")
    save_layouts(tc.layout_file(folder, project), [PieceLayout(0, "a.mov", [(0, 12)], 0)], 12, VISUAL)
    return folder


def fix(index, action, start, end, text, problem="映像と合わない", shows="キーボード"):
    return {"index": index, "problem": problem, "shows": shows, "action": action, "start": start, "end": end,
            "text": text}


def test_check_fixes_auto_telops_and_flags_manual(folder):
    bad = {"summary": "s", "fixes": [fix(1, "move", 9.0, 11.0, "ペンで**手書き**もOK")]}  # 3つ目と重なる
    good = {"summary": "ペンのテロップがキーボードの映像に乗っていたので、ペンの映像の時刻へ動かしました。",
            "fixes": [fix(1, "move", 7.9, 9.9, "ペンで**手書き**もOK"),
                      fix(2, "rewrite", 10.0, 11.8, "ペンで書ける", shows="ペンの手元")]}
    fake = FakeClaude(bad, good)
    services = Services(claude=ClaudeCode(claude_path="claude", runner=fake), profile=get_profile())
    project = tc.run_check(folder, services)
    prompt = fake.calls[0][1]
    assert "キーボードで文字を打つ手元" in prompt and "（手で直したテロップ）" in prompt and "telops_1.jpg" in prompt
    assert "重なります" in fake.calls[1][1]                                   # 重なる直し方は差し戻す
    assert (folder / "work" / "telop_check" / "telops_1.jpg").exists()
    by = {t.text: t for t in project.telops}
    assert by["ペンで**手書き**もOK"].start == 7.9                             # 自動のテロップは動いた
    assert by["画面がきれい"].start == 10.0 and "ペンで書ける" not in by       # 手で直したものは変えない
    a, b = project.telop_checks
    assert a.applied and a.action == "move" and b.manual and not b.applied
    assert project.telop_check_summary.startswith("ペンのテロップ")

    project = tc.undo_check(folder, 0)                                         # 元に戻す
    assert any(t.text == "ペンで**手書き**もOK" and t.start == 4.0 for t in project.telops)
    assert len(project.telops) == 3 and project.telop_checks[0].undone

    project = tc.apply_suggestion(folder, 1)                                   # おすすめのとおり直す
    assert any(t.text == "ペンで書ける" for t in project.telops)


def test_delete_and_no_problem(folder):
    fake = FakeClaude({"summary": "s", "fixes": [fix(1, "delete", 4.0, 7.0, "")]}, {"summary": "問題なし", "fixes": []})
    services = Services(claude=ClaudeCode(claude_path="claude", runner=fake), profile=get_profile())
    project = tc.run_check(folder, services)
    assert [t.text for t in project.telops] == ["**軽い**", "画面がきれい"]
    tc.undo_check(folder, 0)
    assert len(load_project(folder).telops) == 3
    project = tc.run_check(folder, services)
    assert project.telop_checks == [] and project.telop_check_summary == "問題なし"


def test_check_after_telops_never_stops_the_work(folder):
    fake = FakeClaude({"summary": "s", "fixes": [fix(9, "delete", 0, 1, "")]},
                      {"summary": "s", "fixes": [fix(9, "delete", 0, 1, "")]})
    services = Services(claude=ClaudeCode(claude_path="claude", runner=fake), profile=get_profile())
    notes = tc.check_after_telops(folder, services)
    assert notes and "できませんでした" in notes[0]
    assert len(load_project(folder).telops) == 3


def test_fix_rules():
    telops = [Telop(start=0, end=2, text="a"), Telop(start=3, end=5, text="b")]
    check = make_telop_fix_check(telops, 10)
    out = TelopCheckOut.model_validate({"summary": "", "fixes": [
        fix(0, "rewrite", 0, 2, "とても長いテロップの文言でございますので二十文字を超えます"),
        fix(1, "move", 9.5, 12, "b")]})
    problems = check(out)
    assert any("20 文字" in p for p in problems) and any("0〜10.0秒" in p for p in problems)
