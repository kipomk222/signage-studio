"""画面の共通部品: 上部の進み具合、時間のかかる処理のダイアログ（中止つき）、エラー表示。"""

from __future__ import annotations

import logging
import traceback
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

from nicegui import run, ui

from app.errors import UserError
from app.jobs import Cancelled, Job
from app.project import STAGE_ORDER, Project, Stage

ROOT = Path(__file__).resolve().parents[2]
LOG = ROOT / "logs" / "エラー記録.txt"

PAGE_KEY = {Stage.PLAN: "plan", Stage.MATERIAL: "material", Stage.VIDEO: "video", Stage.NARRATION: "narration",
            Stage.REVIEW: "review", Stage.EXPORT: "export"}


def steps(project: Project) -> list[Stage]:
    """上部に並べる段階（声を別に録る動画は 映像・ナレーション も）。"""
    return [s for s in project.flow if s is not Stage.DONE]

CSS = """
body { font-size: 16px; background: #f5f6f8; }
.page { max-width: 1100px; margin: 0 auto; width: 100%; }
.section { background: white; border-radius: 14px; padding: 20px 24px; box-shadow: 0 1px 3px rgba(0,0,0,.08); width: 100%; }
.section-title { font-size: 1.15rem; font-weight: 700; margin-bottom: 4px; }
.hint { color: #6b7280; font-size: 0.92rem; }
.step-chip { padding: 4px 10px; border-radius: 999px; font-weight: 600; font-size: .88rem; white-space: nowrap; }
.step-done { background: #e6f4ea; color: #1e7b34; cursor: pointer; }
.step-now { background: #1f3a93; color: white; }
.step-todo { background: #e5e7eb; color: #9ca3af; }
.big-button { font-size: 1.1rem !important; padding: 10px 28px !important; }
.warn-box { background: #fff7e6; border: 1px solid #f5c26b; border-radius: 10px; padding: 10px 14px; }
"""


def setup_page(title: str = "") -> None:
    ui.add_css(CSS)
    ui.page_title(f"{title}｜サイネージ動画メーカー" if title else "サイネージ動画メーカー")
    ui.colors(primary="#1f3a93")
    ui.button.default_props("no-caps")  # 英字を勝手に大文字にしない


def stage_url(pid: str, stage: Stage) -> str:
    return f"/p/{pid}/{PAGE_KEY.get(stage, 'export')}"


def header(project: Project | None = None, current: Stage | None = None,
           folder: Path | None = None, page: str = "") -> None:
    """上部の帯。動画の画面（folder あり）では「Claude に相談」の欄も付ける。"""
    holder: dict = {}
    with ui.header().classes("bg-white text-gray-900 shadow-sm"):
        with ui.row().classes("page items-center justify-between py-1"):
            with ui.row().classes("items-center gap-3 cursor-pointer").on("click", lambda: ui.navigate.to("/")):
                ui.icon("movie", size="28px").classes("text-primary")
                ui.label("サイネージ動画メーカー").classes("text-xl font-bold")
            if project is not None:
                with ui.row().classes("items-center gap-2"):
                    flow = steps(project)
                    for i, stage in enumerate(flow, 1):
                        reached = stage.index <= project.stage.index
                        cls = "step-now" if stage is current else ("step-done" if reached else "step-todo")
                        chip = ui.label(f"{i} {stage.label}").classes(f"step-chip {cls}")
                        if reached and stage is not current:
                            chip.on("click", lambda s=stage: ui.navigate.to(stage_url(project.id, s)))
                        if i < len(flow):
                            ui.icon("chevron_right").classes("text-gray-400")
                ui.label(project.model_number).classes("font-mono text-gray-600")
            with ui.row().classes("items-center gap-1"):
                if project is not None and folder is not None:
                    ui.button("Claude に相談", icon="forum", on_click=lambda: holder["drawer"].toggle()) \
                        .props("outline dense")
                ui.button(icon="settings", on_click=lambda: ui.navigate.to("/settings")) \
                    .props("flat round color=grey").tooltip("設定（Claude のサインイン・使用量・メモリ）")
    if project is not None and folder is not None:
        from app.ui.chat import chat_drawer
        holder["drawer"] = chat_drawer(folder, project.id, page)


def stale_notice(project: Project, stage: Stage) -> None:
    if stage in project.stale:
        with ui.row().classes("warn-box items-center gap-2 w-full"):
            ui.icon("info", color="orange")
            ui.label("前の画面で内容を直したため、この画面の内容は作り直しが必要です。")


def show_error(e: UserError) -> None:
    with ui.dialog() as dlg, ui.card().classes("w-[480px] gap-3"):
        with ui.row().classes("items-center gap-2"):
            ui.icon("error_outline", color="negative", size="32px")
            ui.label(e.what).classes("text-lg font-bold")
        ui.label(e.how)
        ui.button("閉じる", on_click=dlg.close).classes("self-end")
    dlg.open()


def log_unexpected(exc: BaseException) -> None:
    LOG.parent.mkdir(exist_ok=True)
    with LOG.open("a", encoding="utf-8") as f:
        f.write(f"\n===== {datetime.now():%Y-%m-%d %H:%M:%S} =====\n")
        f.write("".join(traceback.format_exception(exc)))
    logging.exception("unexpected error", exc_info=exc)


UNEXPECTED = UserError("思わぬ問題が起きました。",
                       "もう一度お試しください。続くときは logs フォルダの「エラー記録.txt」を確認してください。")


async def run_job(title: str, fn: Callable, *args, **kwargs):
    """時間のかかる処理を、進み具合と「中止」ボタンのあるダイアログで動かす。失敗したら None。"""
    job = Job()
    with ui.dialog().props("persistent") as dlg, ui.card().classes("w-[560px] gap-3"):
        ui.label(title).classes("text-lg font-bold")
        msg = ui.label("準備しています")
        bar = ui.linear_progress(value=0, show_value=False).props("rounded size=14px")
        remain = ui.label("").classes("hint")

        def cancel() -> None:
            job.cancel()
            stop.disable()
            msg.set_text("中止しています…")

        stop = ui.button("中止", on_click=cancel, icon="stop").props("outline color=grey").classes("self-end")
    dlg.open()

    def tick() -> None:
        if not job.cancelled:
            msg.set_text(job.message or "準備しています")
        bar.set_value(job.fraction)
        remain.set_text(job.remaining_text())

    timer = ui.timer(0.5, tick)

    def work():
        with job.active():
            return fn(*args, job=job, **kwargs)

    try:
        return await run.io_bound(work)
    except Cancelled:
        ui.notify("中止しました。", type="info", position="top-right")
    except UserError as e:
        show_error(e)
    except Exception as e:  # noqa: BLE001 — 画面を止めずに知らせる
        log_unexpected(e)
        show_error(UNEXPECTED)
    finally:
        timer.cancel()
        dlg.close()
    return None


def guarded(fn: Callable) -> Callable:
    """ボタンの処理で UserError が起きたら、ダイアログで知らせる（時間のかからない処理用）。"""
    async def wrapper(*args, **kwargs):
        try:
            result = fn(*args, **kwargs)
            if hasattr(result, "__await__"):
                result = await result
            return result
        except UserError as e:
            show_error(e)
        except Exception as e:  # noqa: BLE001
            log_unexpected(e)
            show_error(UNEXPECTED)
        return None
    return wrapper


def stage_reached(project: Project, stage: Stage) -> bool:
    return STAGE_ORDER.index(project.stage) >= STAGE_ORDER.index(stage)
