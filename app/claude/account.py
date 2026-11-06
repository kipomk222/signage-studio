"""Claude へのサインイン（Claude Code のアカウント）の確認と操作。

ツールは Claude Code を通して Claude を使うので、Claude Code にサインインしていれば、
そのアカウントのプラン（サブスクリプション）の利用分で動く。API キーは使わない。
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass

from app.errors import UserError

USAGE_PAGE = "https://claude.ai/settings/usage"
INSTALL_PAGE = "https://docs.claude.com/ja/docs/claude-code/setup"


@dataclass
class Status:
    installed: bool
    version: str = ""
    logged_in: bool = False
    method: str = ""  # サブスクリプション／API など（表示用）
    detail: str = ""

    def text(self) -> str:
        if not self.installed:
            return "Claude Code が入っていません"
        if not self.logged_in:
            return "サインインしていません"
        return f"サインイン済み（{self.method}）"


METHOD_LABELS = {"oauth_token": "Claude のプラン（サブスクリプション）", "claudeai": "Claude のプラン（サブスクリプション）",
                 "api_key": "API キー（従量課金）", "console": "API キー（従量課金）"}


def _claude() -> list[str] | None:
    from app.platform import claude_command
    return claude_command()


def status() -> Status:
    path = _claude()
    if not path:
        return Status(installed=False)
    try:
        enc = {"text": True, "encoding": "utf-8", "errors": "replace"}
        ver = subprocess.run([*path, "--version"], capture_output=True, timeout=20, **enc).stdout.strip()
        out = subprocess.run([*path, "auth", "status", "--json"], capture_output=True, timeout=20, **enc)
        data = json.loads(out.stdout or "{}")
    except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError) as exc:
        return Status(installed=True, detail=str(exc))
    method = str(data.get("authMethod") or "")
    return Status(installed=True, version=ver.split(" ")[0], logged_in=bool(data.get("loggedIn")),
                  method=METHOD_LABELS.get(method, method or "不明"))


def login() -> subprocess.Popen:
    """サインインを始める（ブラウザでサインイン画面が開く）。終わったかは status() で確かめる。"""
    path = _claude()
    if not path:
        raise UserError("Claude Code が入っていません。", "先に Claude Code をインストールしてください。")
    return subprocess.Popen([*path, "auth", "login", "--claudeai"], stdin=subprocess.DEVNULL,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def logout() -> None:
    path = _claude()
    if path:
        subprocess.run([*path, "auth", "logout"], capture_output=True, timeout=30)


def connection_test() -> str:
    """実際に1回呼んでみる。返事の文を返す。"""
    from pydantic import BaseModel

    from app.claude.runner import ClaudeCode

    class Pong(BaseModel):
        reply: str

    return ClaudeCode().ask("接続テストです。「つながりました」とだけ reply に入れて返してください。", Pong,
                            timeout=120, kind="test").reply
