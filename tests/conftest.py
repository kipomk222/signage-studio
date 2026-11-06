"""テスト全体の準備。

- 使用量の記録は、本物の記録（cache/usage.jsonl）ではなく一時ファイルに書く
- BGM・ロゴは、お手元の assets/bgm・assets/logo・assets/corner_logo にあっても使わない（テストの結果が PC によって変わらないように）
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


@pytest.fixture(autouse=True)
def _no_local_logo(tmp_path, monkeypatch):
    from app.pipeline import cards
    for name in ("LOGO_DIR", "CORNER_LOGO_DIR"):
        empty = tmp_path / f"{name.lower()}_empty"
        empty.mkdir()
        monkeypatch.setattr(cards, name, empty)
