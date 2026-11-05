"""ツール専用の作業場所（指示書・メモリ・スキル・履歴）と、使用量の記録のテスト。"""

from __future__ import annotations

import json
from datetime import datetime, timedelta

import pytest

from app.claude import usage
from app.claude.runner import ClaudeCode, ProcResult
from app.claude.workspace import HOME, Workspace
from app.errors import UserError
from pydantic import BaseModel


@pytest.fixture
def ws(tmp_path):
    w = Workspace(home=tmp_path / "home")
    w.save_instructions("# 指示書\nあなたはサイネージ動画の担当です。")
    w.save_skill("signage-telop", "---\nname: signage-telop\ndescription: テロップ\n---\n\n要点だけ書く。")
    w.save_skill("signage-edit", "---\nname: signage-edit\ndescription: 編集\n---\n\n言い直しを切る。")
    return w


def test_memory_add_edit_forget(ws):
    assert ws.remember("強調色は赤")
    assert not ws.remember("  強調色は赤 ")  # 同じものは足さない
    ws.remember("タイトルは短く")
    ws.update_memory(1, "タイトルは10文字まで")
    assert ws.memories() == ["強調色は赤", "タイトルは10文字まで"]
    assert ws.forget(0) == "強調色は赤"
    assert ws.memories() == ["タイトルは10文字まで"]
    with pytest.raises(UserError):
        ws.remember("  ")
    assert [h["kind"] for h in ws.history()] == ["memory", "memory", "memory"]


def test_system_text_reads_only_needed_skill(ws):
    ws.remember("強調色は赤")
    text = ws.system_text("telop")
    assert "指示書" in text and "- 強調色は赤" in text and "要点だけ書く" in text
    assert "言い直しを切る" not in text and "description:" not in text
    assert "言い直しを切る" in ws.system_text("consult") and "要点だけ書く" in ws.system_text("consult")
    assert "要点だけ書く" not in ws.system_text("research")


def test_skill_name_rules(ws):
    with pytest.raises(UserError):
        ws.save_skill("../悪い名前", "x")
    assert ws.skills() == ["signage-edit", "signage-telop"]


def test_claude_ai_project_text(ws):
    ws.remember("強調色は赤")
    text = ws.claude_ai_project_text()
    assert "指示書" in text and "強調色は赤" in text and "# signage-edit" in text


def test_shipped_workspace_has_skills():
    shipped = Workspace(home=HOME)
    assert {"signage-script", "signage-edit", "signage-telop"} <= set(shipped.skills())
    for name in shipped.skills():
        head = shipped.skill(name).split("---")[1]
        assert f"name: {name}" in head and "description:" in head
    assert shipped.instructions().strip()


def test_runner_passes_workspace_and_records_usage(ws, tmp_path):
    class Out(BaseModel):
        reply: str

    seen = {}

    def runner(cmd, stdin, cwd, timeout):
        f = cmd[cmd.index("--append-system-prompt-file") + 1]
        seen["system"] = open(f, encoding="utf-8").read()
        return ProcResult(0, json.dumps({"is_error": False, "structured_output": {"reply": "ok"},
                                         "usage": {"input_tokens": 100, "cache_read_input_tokens": 900,
                                                   "output_tokens": 50},
                                         "total_cost_usd": 0.01, "duration_ms": 2500}), "")

    cc = ClaudeCode(claude_path="claude", runner=runner, workspace=ws)
    with usage.usage_scope("2026-11-06_ABC"):
        assert cc.ask("こんにちは", Out, kind="telop").reply == "ok"
    assert "要点だけ書く" in seen["system"]
    e = usage.entries()[-1]
    assert e["project"] == "2026-11-06_ABC" and e["kind"] == "telop"
    assert e["input"] == 1000 and e["output"] == 50 and e["seconds"] == 2.5


def test_usage_summary(tmp_path):
    log = tmp_path / "u.jsonl"
    now = datetime(2026, 11, 6, 12, 0)
    rows = [(now, "p1", "telop"), (now - timedelta(days=2), "p1", "edit"), (now - timedelta(days=40), "p2", "plan")]
    log.write_text("".join(json.dumps({"at": t.isoformat(), "project": p, "kind": k, "input": 1000, "output": 100,
                                       "cost": 0.1, "seconds": 3}) + "\n" for t, p, k in rows), encoding="utf-8")
    s = usage.summary("p1", log=log, now=now)
    assert s["today"].calls == 1 and s["month"].calls == 2 and s["project"].calls == 2
    assert s["kind:edit"].calls == 1
    assert "2回" in s["project"].text()
