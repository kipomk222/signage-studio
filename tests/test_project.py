from datetime import date, datetime, timedelta
from pathlib import Path

import pytest

from app import project as pj
from app.project import (
    Clip, Price, ProjectError, Scene, Stage, Telop,
    create_project, list_projects, load_project, safe_folder_part, save_project,
)


def test_create_save_reload_roundtrip(tmp_path):
    """完了条件: テスト用プロジェクトを保存・再読み込みできる。"""
    project, folder = create_project(tmp_path, "EPC01A-20BK", today=date(2026, 11, 5))
    assert folder.name == "2026-11-05_EPC01A-20BK"
    for sub in pj.SUBDIRS:
        assert (folder / sub).is_dir()

    project.product.name = "Re・De Pot 電気圧力鍋 2L"
    project.product.specs = {"容量": "2L"}
    project.plan.scenes = [Scene(id="s1", title="冒頭", script="今回はリデポットをご紹介します", seconds=8)]
    project.plan.price = Price(mode="open")
    project.clips = [Clip(file="IMG_0001.MOV", scene_id="s1", cut_ranges=[(1.2, 3.4)])]
    project.telops = [Telop(start=0.5, end=3.0, text="ご飯が25分で炊ける！", manual=True)]
    project.settings.outputs = ["store", "youtube"]
    save_project(folder, project)

    reloaded = load_project(folder)
    assert reloaded.model_dump() == project.model_dump()
    assert reloaded.plan.target_seconds == 150  # 標準2〜3分


def test_folder_name_is_safe_on_both_os(tmp_path):
    assert safe_folder_part(' AB/C:12*3?"<x>| ') == "AB-C-12-3-x"
    _, f1 = create_project(tmp_path, "XPS 13/9350", today=date(2026, 11, 5))
    _, f2 = create_project(tmp_path, "XPS 13/9350", today=date(2026, 11, 5))
    assert f1.name == "2026-11-05_XPS-13-9350"
    assert f2.name == "2026-11-05_XPS-13-9350_2"


def test_empty_model_number_is_rejected(tmp_path):
    with pytest.raises(ProjectError) as e:
        create_project(tmp_path, "   ")
    assert "型番" in e.value.what


def test_save_failure_keeps_previous_file(tmp_path, monkeypatch):
    """保存の途中で落ちても、元の project.json は壊れない。一時ファイルも残らない。"""
    project, folder = create_project(tmp_path, "ABC-123")
    before = (folder / pj.PROJECT_FILE).read_text(encoding="utf-8")

    def boom(*_args, **_kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(pj.os, "replace", boom)
    project.product.name = "書き換え途中"
    with pytest.raises(ProjectError) as e:
        save_project(folder, project)
    assert e.value.what and e.value.how

    assert (folder / pj.PROJECT_FILE).read_text(encoding="utf-8") == before
    assert list(folder.glob(".project-*.tmp")) == []
    assert load_project(folder).product.name == ""


def test_corrupt_file_gives_friendly_error(tmp_path):
    _, folder = create_project(tmp_path, "ABC-123")
    (folder / pj.PROJECT_FILE).write_text("{壊れた", encoding="utf-8")
    with pytest.raises(ProjectError) as e:
        load_project(folder)
    assert "読み込めませんでした" in e.value.what


def test_list_is_newest_first_and_marks_broken(tmp_path):
    base = datetime(2026, 11, 5, 9, 0)
    for i, model in enumerate(["AAA-1", "BBB-2", "CCC-3"]):
        project, folder = create_project(tmp_path, model, now=base)
        save_project(folder, project, now=base + timedelta(hours=[2, 0, 1][i]))
    _, bad = create_project(tmp_path, "BAD-9")
    (bad / pj.PROJECT_FILE).write_text("not json", encoding="utf-8")
    (tmp_path / "関係ないフォルダ").mkdir()

    rows = list_projects(tmp_path)
    assert [r.model_number for r in rows[:3]] == ["AAA-1", "CCC-3", "BBB-2"]
    assert rows[0].stage_label == "企画"
    assert rows[-1].broken and rows[-1].id.endswith("BAD-9")
    assert len(rows) == 4
    assert list_projects(tmp_path / "まだない") == []


def test_stage_progress_and_stale_marking(tmp_path):
    project, folder = create_project(tmp_path, "ABC-123")
    now = datetime(2026, 11, 5, 10, 0)
    project.complete_stage(Stage.PLAN, now)
    project.complete_stage(Stage.MATERIAL, now)
    project.complete_stage(Stage.REVIEW, now)
    assert project.stage is Stage.EXPORT
    project.telops = [Telop(start=0, end=2, text="手で直した", manual=True)]

    # 企画を直すと、進んでいた 素材・確認・書き出し が「再作成が必要」になる
    marked = project.mark_changed(Stage.PLAN)
    assert marked == [Stage.MATERIAL, Stage.REVIEW, Stage.EXPORT]
    assert project.telops[0].text == "手で直した"  # 手直しは消えない

    # 素材をやり直すと、その印だけ外れる
    project.complete_stage(Stage.MATERIAL, now)
    assert project.stale == [Stage.REVIEW, Stage.EXPORT]
    assert project.stage is Stage.EXPORT  # 進み具合は戻らない

    save_project(folder, project)
    assert load_project(folder).stale == [Stage.REVIEW, Stage.EXPORT]


def test_early_change_does_not_mark_unreached_stages(tmp_path):
    project, _ = create_project(tmp_path, "ABC-123")
    project.complete_stage(Stage.PLAN)
    assert project.mark_changed(Stage.PLAN) == [Stage.MATERIAL]
