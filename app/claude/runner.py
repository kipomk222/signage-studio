"""Claude Code をヘッドレス（claude -p）で呼び出す。

- 文章は標準入力で渡す（長い文字起こしでもコマンドの長さ制限に掛からない）
- 結果は --json-schema で決めた形の JSON として受け取り、Pydantic で検証する
- 形式が崩れていたら理由を添えて1回だけ再依頼し、それでも駄目なら NeedsManualInput
- ファイル編集・コマンド実行は常に不可。お手元の設定・プラグインも読み込まない
"""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TypeVar

from pydantic import BaseModel, ValidationError

from app.errors import UserError

ROOT = Path(__file__).resolve().parents[2]
PROMPTS = ROOT / "prompts"

M = TypeVar("M", bound=BaseModel)


class ClaudeError(UserError):
    """Claude Code の呼び出しで起きた問題。detail は調査用。"""

    def __init__(self, what: str, how: str, detail: str = "") -> None:
        super().__init__(what, how)
        self.detail = detail


class NeedsManualInput(ClaudeError):
    """再依頼しても正しい形で返らなかった。画面で手入力してもらう。"""


NOT_FOUND = ("Claude Code が見つかりません。",
             "Claude Code をインストールしてから、もう一度起動してください。")
NOT_LOGGED_IN = ("Claude Code にログインしていません。",
                 "ターミナルで claude を起動してログインしてから、もう一度お試しください。")
LIMIT = ("Claude の利用上限に達しました。",
         "しばらく時間をおいてから、もう一度お試しください。")
TIMEOUT = ("Claude Code の応答に時間がかかりすぎたため中止しました。",
           "もう一度お試しください。")
FAILED = ("Claude Code からうまく結果を受け取れませんでした。",
          "もう一度お試しください。続く場合は手入力で進めてください。")


@dataclass
class ProcResult:
    returncode: int
    stdout: str
    stderr: str


# テストでは本物の代わりにこれを差し替える
Runner = Callable[[list[str], str, Path, float], ProcResult]


def subprocess_runner(cmd: list[str], stdin: str, cwd: Path, timeout: float) -> ProcResult:
    r = subprocess.run(cmd, input=stdin, capture_output=True, text=True, encoding="utf-8",
                       errors="replace", cwd=cwd, timeout=timeout, check=False)
    return ProcResult(r.returncode, r.stdout, r.stderr)


def load_prompt(name: str, **values: str) -> str:
    """prompts/<name>.md を読み、{{キー}} を差し込む。"""
    text = (PROMPTS / f"{name}.md").read_text(encoding="utf-8")
    for key, value in values.items():
        text = text.replace("{{" + key + "}}", value)
    return text


def build_command(claude: str, schema: dict, tools: list[str]) -> list[str]:
    return [
        claude, "-p",
        "--output-format", "json",
        "--json-schema", json.dumps(schema, ensure_ascii=False, separators=(",", ":")),
        "--tools", ",".join(tools),          # 空 = ツールを一切使わせない
        "--allowedTools", ",".join(tools),   # 許可したツールは確認なしで使える
        "--no-session-persistence",
        "--strict-mcp-config",                # お手元の MCP サーバーは読み込まない
        "--setting-sources", "",             # お手元の設定・CLAUDE.md は読み込まない
    ]


def _classify(text: str) -> tuple[str, str] | None:
    low = text.lower()
    if any(k in low for k in ("not logged in", "please run /login", "invalid api key",
                              "authentication", "unauthorized", "oauth")):
        return NOT_LOGGED_IN
    if any(k in low for k in ("usage limit", "rate limit", "limit reached", "quota")):
        return LIMIT
    return None


class ClaudeCode:
    def __init__(self, claude_path: str | None = None, runner: Runner = subprocess_runner,
                 workdir: Path | None = None) -> None:
        self.claude_path = claude_path
        self.runner = runner
        self.workdir = workdir

    def _claude(self) -> str:
        path = self.claude_path or shutil.which("claude")
        if not path:
            raise ClaudeError(*NOT_FOUND)
        return path

    def _once(self, prompt: str, schema: dict, tools: list[str], timeout: float,
              files: list[Path] | None = None) -> dict:
        cmd = build_command(self._claude(), schema, tools)
        with tempfile.TemporaryDirectory(dir=self.workdir) as tmp:  # 作業フォルダは毎回空の一時フォルダ
            for f in files or []:  # 読ませたいファイルだけを置く
                shutil.copy2(f, Path(tmp) / Path(f).name)
            try:
                proc = self.runner(cmd, prompt, Path(tmp), timeout)
            except FileNotFoundError as exc:
                raise ClaudeError(*NOT_FOUND) from exc
            except subprocess.TimeoutExpired as exc:
                raise ClaudeError(*TIMEOUT) from exc
        try:
            payload = json.loads(proc.stdout)
        except json.JSONDecodeError:
            reason = _classify(proc.stdout + proc.stderr)
            raise ClaudeError(*(reason or FAILED), detail=(proc.stdout + proc.stderr)[-2000:])
        if payload.get("is_error") or proc.returncode != 0:
            text = json.dumps(payload, ensure_ascii=False) + proc.stderr
            raise ClaudeError(*(_classify(text) or FAILED), detail=text[-2000:])
        data = payload.get("structured_output")
        if data is None:  # 念のため本文の JSON も見る
            try:
                data = json.loads(payload.get("result") or "")
            except json.JSONDecodeError:
                data = None
        if not isinstance(data, dict):
            raise _FormatProblem("JSON オブジェクトが返されませんでした。")
        return data

    def ask(self, prompt: str, model: type[M], tools: list[str] | None = None, timeout: float = 180,
            check: Callable[[M], list[str]] | None = None, files: list[Path] | None = None) -> M:
        """依頼して、決めた形で受け取る。形式やルールに合わなければ1回だけ再依頼する。"""
        schema = model.model_json_schema()
        tools = tools or []
        problems: list[str] = []
        for attempt in range(2):
            text = prompt
            if problems:
                text += ("\n\n# 前回の出力の問題（必ず直してください）\n" + "\n".join(f"- {p}" for p in problems))
            try:
                result = model.model_validate(self._once(text, schema, tools, timeout, files))
                problems = check(result) if check else []
            except _FormatProblem as exc:
                problems = [str(exc)]
                continue
            except ValidationError as exc:
                problems = [f"{'.'.join(map(str, e['loc']))}: {e['msg']}" for e in exc.errors()][:10]
                continue
            if not problems:
                return result
        raise NeedsManualInput(
            "Claude Code の結果が決まった形になりませんでした。",
            "画面で内容を入力するか、もう一度お試しください。",
            detail="\n".join(problems),
        )


class _FormatProblem(Exception):
    pass
