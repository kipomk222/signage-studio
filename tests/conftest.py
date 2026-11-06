"""テスト全体の準備。

- 使用量の記録は、本物の記録（cache/usage.jsonl）ではなく一時ファイルに書く
- BGM は、お手元の assets/bgm に曲があっても使わない（テストの結果が PC によって変わらないように）
"""

import pytest


@pytest.fixture(autouse=True)
def _usage_log_to_tmp(tmp_path, monkeypatch):
    from app.claude import usage
    monkeypatch.setattr(usage, "LOG", tmp_path / "usage.jsonl")


@pytest.fixture(autouse=True)
def _no_local_bgm(tmp_path, monkeypatch):
    from app import workflow
    empty = tmp_path / "bgm_empty"
    empty.mkdir()
    monkeypatch.setattr(workflow, "BGM_DIR", empty)
