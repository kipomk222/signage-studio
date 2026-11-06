"""ツール専用の Claude の作業場所（claude_home/）。

- CLAUDE.md: このツールでの Claude の役割（指示書）
- memory.md: 利用者の好み（メモリ）。相談で「覚えておいて」と言われたことを1行ずつ
- .claude/skills/<名前>/SKILL.md: 作業ごとのやり方（スキル）
- history/: 相談の履歴と出した指示（PCごと。Git には入れない）

Claude Code を呼び出すたびに、指示書・メモリ・その作業のスキルを読ませる。
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from app.errors import UserError

ROOT = Path(__file__).resolve().parents[2]
HOME = ROOT / "claude_home"

# 呼び出しの種類 → 読ませるスキル
SKILL_FOR = {
    "plan": "signage-script",
    "edit": "signage-edit",
    "telop": "signage-telop",
    "narration": "signage-narration",
    "consult": None,  # 相談はすべてのスキルを読む
}
MEMORY_HEADER = "# メモリ（利用者の好み）\n\n相談のときに「覚えておいて」と言われたことを、1行に1つ書く。すべての作業で守る。\n"


@dataclass
class Workspace:
    home: Path = HOME

    # --- 指示書 ---------------------------------------------------------------
    @property
    def instructions_path(self) -> Path:
        return self.home / "CLAUDE.md"

    def instructions(self) -> str:
        p = self.instructions_path
        return p.read_text(encoding="utf-8") if p.exists() else ""

    def save_instructions(self, text: str) -> None:
        _write(self.instructions_path, text)

    # --- メモリ ---------------------------------------------------------------
    @property
    def memory_path(self) -> Path:
        return self.home / "memory.md"

    def memories(self) -> list[str]:
        p = self.memory_path
        if not p.exists():
            return []
        return [m.group(1).strip() for line in p.read_text(encoding="utf-8").splitlines()
                if (m := re.match(r"^\s*-\s+(.+)$", line))]

    def _save_memories(self, items: list[str]) -> None:
        _write(self.memory_path, MEMORY_HEADER + "\n" + "".join(f"- {x}\n" for x in items))

    def remember(self, text: str) -> bool:
        """好みを1つ覚える。同じものがあれば足さない。"""
        text = " ".join(text.split())
        if not text:
            raise UserError("覚える内容が空です。", "覚えておいてほしいことを書いてください。")
        items = self.memories()
        if text in items:
            return False
        self._save_memories(items + [text])
        self.log("memory", f"覚えた: {text}")
        return True

    def forget(self, index: int) -> str:
        items = self.memories()
        removed = items.pop(index)
        self._save_memories(items)
        self.log("memory", f"忘れた: {removed}")
        return removed

    def update_memory(self, index: int, text: str) -> None:
        items = self.memories()
        items[index] = " ".join(text.split())
        self._save_memories(items)

    # --- スキル ---------------------------------------------------------------
    @property
    def skills_dir(self) -> Path:
        return self.home / ".claude" / "skills"

    def skills(self) -> list[str]:
        d = self.skills_dir
        return sorted(p.parent.name for p in d.glob("*/SKILL.md")) if d.exists() else []

    def skill(self, name: str) -> str:
        p = self.skills_dir / name / "SKILL.md"
        return p.read_text(encoding="utf-8") if p.exists() else ""

    def skill_body(self, name: str) -> str:
        """先頭の説明部分（--- で囲んだところ）を除いた本文。"""
        return re.sub(r"^---\n.*?\n---\n", "", self.skill(name), flags=re.DOTALL).strip()

    def save_skill(self, name: str, text: str) -> None:
        if not re.fullmatch(r"[a-z0-9][a-z0-9\-]{1,40}", name):
            raise UserError("スキルの名前が使えません。", "半角の小文字・数字・ハイフンで名前を付けてください。")
        _write(self.skills_dir / name / "SKILL.md", text)

    # --- Claude に読ませる文章 ---------------------------------------------------
    def system_text(self, kind: str) -> str:
        """呼び出しの種類に合わせた、指示書＋メモリ＋スキル。"""
        parts = [self.instructions().strip()]
        mem = self.memories()
        parts.append("# 利用者の好み（必ず守る）\n" + ("\n".join(f"- {m}" for m in mem) if mem else "（まだありません）"))
        if kind not in SKILL_FOR:
            names: list[str] = []                 # 商品調査など: スキルなし
        elif SKILL_FOR[kind] is None:
            names = self.skills()                 # 相談: すべてのスキル
        else:
            names = [SKILL_FOR[kind]]             # その作業のスキルだけ
        for n in names:
            if body := self.skill_body(n):
                parts.append(f"# スキル: {n}\n{body}")
        return "\n\n".join(p for p in parts if p)

    def claude_ai_project_text(self) -> str:
        """claude.ai のプロジェクトの「指示」に貼る文章。"""
        parts = [self.instructions().strip(),
                 "# 利用者の好み（メモリ）\n" + ("\n".join(f"- {m}" for m in self.memories()) or "（まだありません）")]
        for n in self.skills():
            parts.append(f"# {n}\n{self.skill_body(n)}")
        parts.append("# このプロジェクトでの使い方\n"
                     "- サイネージ動画の企画・台本・テロップの相談に使う。\n"
                     "- 新しく覚えた好みは、ツールの設定画面のメモリにも足すと、ツールでも使われる。")
        return "\n\n".join(parts)

    # --- 履歴 -----------------------------------------------------------------
    @property
    def history_path(self) -> Path:
        return self.home / "history" / "history.jsonl"

    def log(self, kind: str, text: str, project: str = "") -> None:
        self.history_path.parent.mkdir(parents=True, exist_ok=True)
        entry = {"at": datetime.now().isoformat(timespec="seconds"), "kind": kind, "project": project, "text": text}
        with self.history_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")

    def history(self, limit: int = 100) -> list[dict]:
        p = self.history_path
        if not p.exists():
            return []
        lines = p.read_text(encoding="utf-8").splitlines()[-limit:]
        return [json.loads(x) for x in reversed(lines) if x.strip()]


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    part = path.with_name(path.name + ".part")
    part.write_text(text, encoding="utf-8")
    os.replace(part, path)


WORKSPACE = Workspace()
