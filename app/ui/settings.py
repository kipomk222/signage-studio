"""設定の画面: Claude へのサインイン・使用量・メモリ・スキル・claude.ai のプロジェクト・履歴。"""

from __future__ import annotations

import time
import webbrowser

from nicegui import run, ui

from app.claude import account
from app.claude.usage import KIND_LABELS, summary
from app.claude.workspace import WORKSPACE
from app.errors import UserError
from app.ui.components import guarded, header, setup_page, show_error

CLAUDE_PROJECTS = "https://claude.ai/projects"
LOGIN_WAIT = 300  # サインインを待つ秒数
HISTORY_LABELS = {"consult": "相談", "apply": "反映", "memory": "メモリ", "skill": "スキル", "instructions": "指示書"}


def section(title: str, hint: str = ""):
    box = ui.column().classes("section gap-3")
    with box:
        ui.label(title).classes("section-title")
        if hint:
            ui.label(hint).classes("hint")
    return box


@ui.page("/settings")
async def settings_page() -> None:
    setup_page("設定")
    header()
    ws = WORKSPACE
    with ui.column().classes("page gap-5 p-6"):
        with ui.row().classes("items-center gap-2"):
            ui.button(icon="arrow_back", on_click=lambda: ui.navigate.to("/")).props("flat round")
            ui.label("設定").classes("text-2xl font-bold")

        # --- ① サインイン ---
        with section("① Claude へのサインイン",
                     "このツールは、サインインした Claude のアカウントのプラン（サブスクリプション）の利用分で動きます。"
                     "API キーや追加の料金は要りません。"):
            with ui.row().classes("items-center gap-3"):
                icon = ui.icon("hourglass_empty", size="28px").classes("text-gray-400")
                status_label = ui.label("確かめています…").classes("text-lg font-bold")
            version = ui.label("").classes("hint text-sm")
            with ui.row().classes("gap-3"):
                login_btn = ui.button("Claude にサインイン", icon="login")
                logout_btn = ui.button("サインアウト", icon="logout").props("flat color=grey")
                test_btn = ui.button("接続テスト", icon="wifi_tethering").props("outline")
            waiting = ui.label("").classes("hint")

            async def refresh_status() -> account.Status:
                st = await run.io_bound(account.status)
                status_label.set_text(st.text())
                ok = st.installed and st.logged_in
                icon.name = "check_circle" if ok else "error_outline"
                icon.classes(replace="text-green-600" if ok else "text-orange-500")
                version.set_text(f"Claude Code {st.version}" if st.version else
                                 ("" if st.installed else "Claude Code をインストールすると使えます。"))
                login_btn.set_visibility(st.installed and not st.logged_in)
                logout_btn.set_visibility(ok)
                test_btn.set_visibility(ok)
                if not st.installed:
                    login_btn.set_visibility(True)
                    login_btn.set_text("インストール方法を見る")
                return st

            async def login() -> None:
                st = await run.io_bound(account.status)
                if not st.installed:
                    webbrowser.open(account.INSTALL_PAGE)
                    return
                try:
                    account.login()
                except UserError as e:
                    show_error(e)
                    return
                login_btn.disable()
                waiting.set_text("ブラウザでサインインの画面が開きます。サインインが終わるまでお待ちください…")
                end = time.monotonic() + LOGIN_WAIT
                while time.monotonic() < end:
                    st = await run.io_bound(account.status)
                    if st.logged_in:
                        ui.notify("サインインしました", type="positive", position="top-right")
                        break
                    await _sleep(2)
                else:
                    show_error(UserError("サインインが終わりませんでした。",
                                         "もう一度「Claude にサインイン」を押し、開いた画面でサインインしてください。"))
                waiting.set_text("")
                login_btn.enable()
                await refresh_status()

            def logout() -> None:
                with ui.dialog() as dlg, ui.card().classes("gap-3"):
                    ui.label("サインアウトしますか？").classes("text-lg font-bold")
                    ui.label("サインアウトすると、もう一度サインインするまで商品調査やテロップ作りができません。")

                    async def yes() -> None:
                        dlg.close()
                        await run.io_bound(account.logout)
                        await refresh_status()
                    with ui.row().classes("self-end"):
                        ui.button("やめる", on_click=dlg.close).props("flat")
                        ui.button("サインアウトする", on_click=yes).props("color=negative")
                dlg.open()

            async def test() -> None:
                test_btn.disable()
                waiting.set_text("Claude に話しかけています…")
                try:
                    reply = await run.io_bound(account.connection_test)
                    ui.notify(f"Claude から返事がありました: {reply}", type="positive", position="top-right")
                except UserError as e:
                    show_error(e)
                finally:
                    waiting.set_text("")
                    test_btn.enable()
                    usage_view.refresh()

            login_btn.on_click(login)
            logout_btn.on_click(logout)
            test_btn.on_click(test)

        # --- ② 使用量 ---
        with section("② 使用量",
                     "このツールから Claude を使った量の目安です。プラン全体の残りは claude.ai の使用量のページで確かめられます。"):
            @ui.refreshable
            def usage_view() -> None:
                s = summary()
                with ui.grid(columns="auto 1fr").classes("gap-x-6 gap-y-1"):
                    ui.label("今日").classes("font-bold")
                    ui.label(s["today"].text())
                    ui.label("今月").classes("font-bold")
                    ui.label(s["month"].text())
                kinds = [(k.removeprefix("kind:"), v) for k, v in s.items() if k.startswith("kind:")]
                if kinds:
                    with ui.expansion("今月の内訳（作業ごと）").classes("w-full"):
                        for k, v in sorted(kinds, key=lambda kv: -kv[1].input):
                            with ui.row().classes("gap-4"):
                                ui.label(KIND_LABELS.get(k, k)).classes("w-32")
                                ui.label(v.text()).classes("text-sm")
            usage_view()
            ui.button("claude.ai で使用量を見る", icon="open_in_new",
                      on_click=lambda: webbrowser.open(account.USAGE_PAGE)).props("outline")

        # --- ③ メモリ ---
        with section("③ メモリ（Claude に覚えてもらったこと）",
                     "すべての作業で Claude が守ります。相談で「今後は〜して」と言って「反映する」を押すと、ここに増えます。"):
            @ui.refreshable
            def memory_view() -> None:
                items = ws.memories()
                if not items:
                    ui.label("まだありません。").classes("hint")
                for i, m in enumerate(items):
                    with ui.row().classes("w-full items-center gap-2 no-wrap"):
                        box = ui.input(value=m).props("outlined dense").classes("grow")
                        box.on("blur", lambda _, i=i, box=box: save_one(i, box.value))
                        ui.button(icon="delete", on_click=lambda i=i: forget(i)) \
                            .props("flat round color=grey").tooltip("忘れてもらう")
                with ui.row().classes("w-full items-center gap-2 no-wrap"):
                    new = ui.input(placeholder="例: テロップの強調色は赤（#E60012）にする").props("outlined dense") \
                        .classes("grow")
                    ui.button("覚えてもらう", icon="add", on_click=lambda: add(new.value)).props("flat")

            @guarded
            def save_one(i: int, text: str) -> None:
                if not (text or "").strip():
                    return forget(i)
                if ws.memories()[i] != " ".join(text.split()):
                    ws.update_memory(i, text)
                    ui.notify("直しました", position="top-right")

            def forget(i: int) -> None:
                ws.forget(i)
                memory_view.refresh()

            @guarded
            def add(text: str) -> None:
                ws.remember(text or "")
                memory_view.refresh()
            memory_view()

        # --- ④ claude.ai のプロジェクト ---
        with section("④ claude.ai のプロジェクト",
                     "claude.ai（ブラウザやスマホのアプリ）でも、このツールと同じ指示・好みで相談できるようにします。"):
            with ui.column().classes("gap-1"):
                for n, step in enumerate([
                    "下の「claude.ai を開く」を押し、プロジェクトの一覧で新しいプロジェクトを作ります。",
                    "名前は「サイネージ動画メーカー」にします。",
                    "「コピー」を押し、プロジェクトの指示の欄に貼り付けて保存します。",
                    "メモリを増やしたり直したりしたら、もう一度コピーして貼り直してください。",
                ], 1):
                    ui.label(f"{n}. {step}")
            text = ws.claude_ai_project_text()
            area = ui.textarea(value=text).props("outlined readonly rows=8").classes("w-full font-mono text-sm")

            def copy() -> None:
                area.set_value(ws.claude_ai_project_text())
                ui.clipboard.write(area.value)
                ui.notify("コピーしました。claude.ai のプロジェクトの指示に貼り付けてください。", position="top-right")
            with ui.row().classes("gap-3"):
                ui.button("コピー", icon="content_copy", on_click=copy)
                ui.button("claude.ai を開く", icon="open_in_new",
                          on_click=lambda: webbrowser.open(CLAUDE_PROJECTS)).props("outline")

        # --- ⑤ 履歴 ---
        with section("⑤ 履歴", "相談した内容・反映した変更・覚えたことの記録です（新しい順）。"):
            rows = ws.history(limit=60)
            if not rows:
                ui.label("まだありません。").classes("hint")
            with ui.column().classes("w-full gap-0"):
                for h in rows:
                    with ui.row().classes("w-full gap-3 border-b py-1 no-wrap items-start"):
                        ui.label(h["at"][5:16].replace("T", " ")).classes("hint text-sm w-24 shrink-0")
                        ui.badge(HISTORY_LABELS.get(h["kind"], h["kind"])).props("outline").classes("shrink-0")
                        ui.label(h["text"]).classes("text-sm whitespace-pre-wrap grow")
                        if h.get("project"):
                            ui.label(h["project"]).classes("hint text-xs font-mono shrink-0")

        # --- 詳細設定 ---
        with ui.expansion("詳細設定（指示書とスキル）", icon="tune").classes("section"):
            ui.label("Claude に渡す決まりごとです。ふだんは変えなくて大丈夫です。").classes("hint")
            ui.label("指示書（すべての作業で読みます）").classes("font-bold mt-2")
            inst = ui.textarea(value=ws.instructions()).props("outlined rows=10").classes("w-full font-mono text-sm")

            @guarded
            def save_inst() -> None:
                ws.save_instructions(inst.value or "")
                ws.log("instructions", "指示書を直した")
                ui.notify("保存しました", position="top-right")
            ui.button("指示書を保存", icon="save", on_click=save_inst).props("outline")

            names = ws.skills()
            if names:
                ui.label("スキル（作業ごとのやり方）").classes("font-bold mt-4")
                labels = {"signage-script": "台本づくり", "signage-edit": "編集の判断", "signage-telop": "テロップ"}
                pick = ui.select({n: f"{labels.get(n, n)}（{n}）" for n in names}, value=names[0]) \
                    .props("outlined dense").classes("w-80")
                body = ui.textarea(value=ws.skill(names[0])).props("outlined rows=14") \
                    .classes("w-full font-mono text-sm")
                pick.on_value_change(lambda e: body.set_value(ws.skill(e.value)))

                @guarded
                def save_skill() -> None:
                    ws.save_skill(pick.value, body.value or "")
                    ws.log("skill", f"スキル {pick.value} を直した")
                    ui.notify("保存しました", position="top-right")
                ui.button("スキルを保存", icon="save", on_click=save_skill).props("outline")

    await ui.context.client.connected()
    await refresh_status()


async def _sleep(seconds: float) -> None:
    import asyncio
    await asyncio.sleep(seconds)
