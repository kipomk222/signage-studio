"""「Claude に相談」の欄（画面の右側）。

日本語で相談・指示を送ると、Claude が返事と「変更案」を出す。変更案は「反映する」を押したときだけ入る。
"""

from __future__ import annotations

from pathlib import Path

from nicegui import run, ui

from app import consult as cs
from app.claude.usage import summary
from app.errors import UserError
from app.jobs import Cancelled, Job
from app.ui.components import log_unexpected, run_job, show_error, UNEXPECTED

OPEN: set[str] = set()  # 相談の欄を開いている動画（画面を作り直しても開いたままにする）
EXAMPLES = ["この動画をもっと良くするには？", "強調の色を赤にして", "2枚目のテロップを短くして",
            "言い直している部分が残っていないか見て"]


def chat_drawer(folder: Path, pid: str, page: str):
    from app.ui import pages
    drawer = ui.right_drawer(value=pid in OPEN, fixed=False).props("width=440 bordered") \
        .classes("bg-white p-0")
    drawer.on_value_change(lambda e: OPEN.add(pid) if e.value else OPEN.discard(pid))
    state = {"job": None}

    with drawer, ui.column().classes("w-full h-full gap-0 no-wrap"):
        with ui.column().classes("w-full gap-1 p-4 border-b"):
            with ui.row().classes("w-full items-center justify-between"):
                ui.label("Claude に相談").classes("text-lg font-bold")
                ui.button(icon="close", on_click=drawer.hide).props("flat round dense color=grey")
            ui.label("日本語で相談や指示ができます。動画は「反映する」を押したときだけ変わります。").classes("hint text-sm")
            used = ui.label("").classes("hint text-xs")

        scroll = ui.scroll_area().classes("w-full grow")
        with ui.column().classes("w-full gap-2 p-3 border-t"):
            thinking = ui.row().classes("items-center gap-2")
            with thinking:
                ui.spinner(size="sm")
                ui.label("Claude が考えています…").classes("hint")
                ui.button("中止", on_click=lambda: state["job"] and state["job"].cancel()) \
                    .props("flat dense color=grey")
            thinking.set_visibility(False)
            box = ui.textarea(placeholder="例: 強調の色を赤にして（Ctrl＋Enter で送る）") \
                .props("outlined autogrow rows=2").classes("w-full")
            with ui.row().classes("w-full justify-between items-center"):
                ui.label("覚えてほしいことは「今後は〜して」と書くと、メモリに残せます。").classes("hint text-xs")
                send_btn = ui.button("送る", icon="send")

    def refresh_used() -> None:
        try:
            used.set_text("この動画で使った Claude: " + summary(pid)["project"].text())
        except OSError:
            used.set_text("")

    @ui.refreshable
    def messages() -> None:
        chat = cs.load_chat(folder)
        with ui.column().classes("w-full gap-3 p-3"):
            if not chat:
                ui.label("たとえば、こんなふうに話しかけてください。").classes("hint text-sm")
                for ex in EXAMPLES:
                    ui.button(ex, on_click=lambda ex=ex: box.set_value(ex)) \
                        .props("flat dense no-caps align=left").classes("text-left")
            for i, c in enumerate(chat):
                if c.role == "user":
                    ui.chat_message(c.text, name="あなた", sent=True, stamp=c.at.strftime("%m/%d %H:%M")) \
                        .classes("w-full")
                    continue
                ui.chat_message(c.text, name="Claude", stamp=c.at.strftime("%m/%d %H:%M")) \
                    .props("bg-color=grey-2").classes("w-full whitespace-pre-wrap")
                if c.has_proposal:
                    proposal(i, c)
        ui.timer(0.1, lambda: scroll.scroll_to(percent=1.0), once=True)

    def proposal(i: int, c: cs.ChatEntry) -> None:
        color = {"pending": "border-amber-400 bg-amber-50", "applied": "border-green-400 bg-green-50"} \
            .get(c.status, "border-gray-200 bg-gray-50")
        with ui.column().classes(f"w-full gap-1 p-3 rounded-lg border {color}"):
            ui.label("変更案").classes("font-bold text-sm")
            for ch in c.changes:
                with ui.row().classes("items-start gap-1 no-wrap"):
                    ui.icon("edit", size="16px").classes("text-gray-500 mt-1")
                    ui.label(ch.description).classes("text-sm")
            if c.remember:
                with ui.row().classes("items-start gap-1 no-wrap"):
                    ui.icon("bookmark", size="16px").classes("text-gray-500 mt-1")
                    ui.label(f"これから覚えること: {c.remember}").classes("text-sm")
            if c.status == "pending":
                with ui.row().classes("gap-2 mt-1"):
                    ui.button("反映する", icon="check", on_click=lambda i=i: apply(i)).props("dense")
                    ui.button("反映しない", on_click=lambda i=i: decline(i)).props("flat dense color=grey")
            elif c.status == "applied":
                ui.label("反映しました").classes("text-sm text-green-700")
                next_buttons(c.next_step)
            elif c.status == "declined":
                ui.label("反映しませんでした").classes("hint text-sm")

    def next_buttons(step: str) -> None:
        from app import workflow as wf
        if step == "rebuild":
            async def rebuild() -> None:
                if await run_job("直した内容で作り直しています", wf.render_edit, folder, pages.CONFIG.services) is not None:
                    ui.navigate.to(f"/p/{pid}/review")
            ui.button("作り直す", icon="refresh", on_click=rebuild).props("outline dense")
        elif step == "preview":
            async def preview() -> None:
                if await run_job("プレビューを作っています", wf.render_preview, folder, pages.CONFIG.services) is not None:
                    ui.navigate.to(f"/p/{pid}/review")
            ui.button("プレビューを作る", icon="play_circle", on_click=preview).props("outline dense")

    def apply(i: int) -> None:
        try:
            _, entry = cs.apply_proposal(folder, i)
        except UserError as e:
            show_error(e)
            messages.refresh()
            return
        ui.notify("反映しました", type="positive", position="top-right")
        if entry.remember:
            ui.notify(f"覚えました: {entry.remember}", type="info", position="top-right")
        ui.navigate.reload()  # 画面の内容を新しくする（相談の欄は開いたまま）

    def decline(i: int) -> None:
        cs.decline_proposal(folder, i)
        messages.refresh()

    async def send() -> None:
        text = (box.value or "").strip()
        if not text or state["job"] is not None:
            return
        job = Job()
        state["job"] = job
        thinking.set_visibility(True)
        send_btn.disable()

        def work():
            with job.active():
                return cs.consult(folder, pages.CONFIG.services, text, page=page)

        try:
            await run.io_bound(work)
            box.set_value("")
        except Cancelled:
            ui.notify("中止しました。", type="info", position="top-right")
        except UserError as e:
            show_error(e)
        except Exception as e:  # noqa: BLE001
            log_unexpected(e)
            show_error(UNEXPECTED)
        finally:
            state["job"] = None
            thinking.set_visibility(False)
            send_btn.enable()
        messages.refresh()
        refresh_used()

    send_btn.on_click(send)
    box.on("keydown.ctrl.enter", send)
    box.on("keydown.meta.enter", send)
    with scroll:
        messages()
    refresh_used()
    return drawer
