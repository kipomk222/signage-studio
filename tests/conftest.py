"""テスト全体の準備: 使用量の記録は、本物の記録（cache/usage.jsonl）ではなく一時ファイルに書く。"""

import pytest


@pytest.fixture(autouse=True)
def _usage_log_to_tmp(tmp_path, monkeypatch):
    from app.claude import usage
    monkeypatch.setattr(usage, "LOG", tmp_path / "usage.jsonl")
