"""サイネージ動画メーカーを起動する。

    uv run python -m app.ui.main            # 専用ウィンドウで開く
    uv run python -m app.ui.main --browser  # ブラウザのタブで開く（専用ウィンドウが開けないとき）
"""

from __future__ import annotations

import argparse
from pathlib import Path

from nicegui import ui

from app import workflow as wf
from app.ui import pages, settings, voicepages  # noqa: F401  画面を登録する


def start(native: bool = True, port: int | None = None, projects: Path | None = None,
          services: wf.Services | None = None, show: bool = True) -> None:
    pages.CONFIG.native = native
    if projects is not None:
        pages.CONFIG.projects = Path(projects)
    if services is not None:
        pages.CONFIG.services = services
    pages.register_media()
    ui.run(title="サイネージ動画メーカー", native=native, window_size=(1320, 900) if native else None,
           port=port, reload=False, language="ja", show=show, show_welcome_message=False,
           favicon="🎬", storage_secret=None)


def main() -> None:
    ap = argparse.ArgumentParser(description="サイネージ動画メーカー")
    ap.add_argument("--browser", action="store_true", help="専用ウィンドウではなくブラウザで開く")
    ap.add_argument("--port", type=int, default=None)
    args = ap.parse_args()
    start(native=not args.browser, port=args.port)


if __name__ in {"__main__", "__mp_main__"}:
    main()
