"""Claude の使用量の記録（呼び出しごとに、使った量を cache/usage.jsonl に書く）。

サブスクリプションでは料金ではなく「プランの利用上限」を消費する。ここでは目安として、
読み込んだ量・書いた量（トークン）と、API 料金に換算した参考額を記録する。
"""

from __future__ import annotations

import json
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
LOG = ROOT / "cache" / "usage.jsonl"
_local = threading.local()
_lock = threading.Lock()

KIND_LABELS = {"research": "商品調査", "extract": "商品ページの整理", "plan": "構成づくり", "edit": "編集の判断",
               "narration": "ナレーション台本", "assign": "声の振り分け", "telop_check": "テロップと映像の照合",
               "telop": "テロップ", "youtube": "YouTube説明文", "consult": "相談", "test": "接続テスト"}


@contextmanager
def usage_scope(project_id: str) -> Iterator[None]:
    """この中で呼んだ Claude の使用量を、その動画の分として記録する。"""
    prev = getattr(_local, "project", "")
    _local.project = project_id
    try:
        yield
    finally:
        _local.project = prev


def record(kind: str, payload: dict, log: Path | None = None) -> None:
    u = payload.get("usage") or {}
    entry = {
        "at": datetime.now().isoformat(timespec="seconds"),
        "project": getattr(_local, "project", ""),
        "kind": kind,
        "input": int(u.get("input_tokens") or 0) + int(u.get("cache_creation_input_tokens") or 0)
                 + int(u.get("cache_read_input_tokens") or 0),
        "output": int(u.get("output_tokens") or 0),
        "cost": float(payload.get("total_cost_usd") or 0),
        "seconds": round(float(payload.get("duration_ms") or 0) / 1000, 1),
    }
    path = log or LOG
    with _lock:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")


@dataclass
class Total:
    calls: int = 0
    input: int = 0
    output: int = 0
    cost: float = 0.0

    def add(self, e: dict) -> None:
        self.calls += 1
        self.input += e.get("input", 0)
        self.output += e.get("output", 0)
        self.cost += e.get("cost", 0.0)

    def text(self) -> str:
        return (f"{self.calls}回・読み込み {self.input / 1000:,.0f}千・書き出し {self.output / 1000:,.1f}千トークン"
                f"（API料金に換算すると約{self.cost:,.2f}ドル）")


def entries(log: Path | None = None) -> list[dict]:
    path = log or LOG
    if not path.exists():
        return []
    return [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x.strip()]


def summary(project_id: str = "", now: datetime | None = None, log: Path | None = None) -> dict[str, Total]:
    """今日・今月・この動画の合計と、作業の種類ごとの今月の合計。"""
    now = now or datetime.now()
    out = {"today": Total(), "month": Total(), "project": Total()}
    by_kind: dict[str, Total] = {}
    for e in entries(log):
        at = datetime.fromisoformat(e["at"])
        if at.date() == now.date():
            out["today"].add(e)
        if (at.year, at.month) == (now.year, now.month):
            out["month"].add(e)
            by_kind.setdefault(e["kind"], Total()).add(e)
        if project_id and e.get("project") == project_id:
            out["project"].add(e)
    out.update({f"kind:{k}": v for k, v in by_kind.items()})
    return out
