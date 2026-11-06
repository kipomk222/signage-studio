"""ホーム＋4画面（企画・素材・確認・書き出し）。

どの画面も、値を変えたらすぐ project.json に保存する（閉じても続きから再開できる）。
主なボタンは画面の下に1つ。細かい設定は「詳細設定」に折りたたむ。
"""

from __future__ import annotations

import shutil
import tempfile
import webbrowser
from dataclasses import dataclass, field
from pathlib import Path

from nicegui import app, ui

from app.errors import UserError
from app.pipeline.fonts import FONTS
from app.project import (
    PROJECT_FILE, Price, Project, Scene, Stage, Telop, TelopStyle, list_projects, load_project, save_project,
)
from app.ui.components import guarded, header, run_job, setup_page, show_error, stage_url, stale_notice
from app import workflow as wf


@dataclass
class Config:
    projects: Path = wf.PROJECTS
    services: wf.Services = field(default_factory=wf.Services)
    native: bool = True


CONFIG = Config()


def folder_of(pid: str) -> Path:
    folder = CONFIG.projects / pid
    if not (folder / PROJECT_FILE).exists():
        raise UserError("この動画の作業内容が見つかりません。", "ホーム画面の一覧から選び直してください。")
    return folder


def media_url(path: Path) -> str:
    rel = path.relative_to(CONFIG.projects).as_posix()
    return f"/media/{rel}?t={int(path.stat().st_mtime)}" if path.exists() else ""


def open_path(path: Path) -> None:
    """ファイルやフォルダを、Finder／エクスプローラー／ブラウザで開く。"""
    if path.is_dir():
        CONFIG.services.prof().open_folder(path)
    else:
        webbrowser.open(path.resolve().as_uri())


async def pick_folder() -> Path | None:
    """フォルダを選ぶ画面を出す（専用ウィンドウのとき）。"""
    if not CONFIG.native or app.native.main_window is None:
        return None
    import webview  # 専用ウィンドウのときだけ入っている
    kind = webview.FileDialog.FOLDER if hasattr(webview, "FileDialog") else webview.FOLDER_DIALOG
    chosen = await app.native.main_window.create_file_dialog(kind)
    return Path(chosen[0]) if chosen else None


def load_or_home(pid: str) -> tuple[Project, Path] | None:
    try:
        folder = folder_of(pid)
        return load_project(folder), folder
    except UserError as e:
        header()
        show_error(e)
        ui.button("ホームへ", on_click=lambda: ui.navigate.to("/"))
        return None


def section(title: str, hint: str = ""):
    box = ui.column().classes("section gap-3")
    with box:
        ui.label(title).classes("section-title")
        if hint:
            ui.label(hint).classes("hint")
    return box


# =============================================================================
# ホーム
# =============================================================================

@ui.page("/")
def home_page() -> None:
    setup_page()
    header()
    with ui.column().classes("page gap-5 p-6"):
        problems = startup_problems()
        for what, how in problems:
            with ui.row().classes("warn-box items-start gap-2 w-full"):
                ui.icon("warning", color="orange")
                ui.label(f"{what}{how}")

        with ui.row().classes("section items-center justify-between"):
            with ui.column().classes("gap-1"):
                ui.label("新しい動画を作る").classes("section-title")
                ui.label("型番を入れると、商品を調べて台本と撮影リストを作ります。").classes("hint")
            ui.button("新しく企画する", icon="add", on_click=new_project_dialog).classes("big-button")

        with section("続きから", "途中までの動画は自動で保存されています。"):
            rows = list_projects(CONFIG.projects)
            if not rows:
                ui.label("まだありません。").classes("hint")
            for r in rows:
                with ui.row().classes("w-full items-center gap-4 border-b py-2"):
                    if r.broken:
                        ui.icon("report", color="negative")
                        ui.label(f"{r.id}（作業内容を読み込めません）").classes("text-gray-500")
                        continue
                    ui.label(r.model_number).classes("font-mono font-bold w-44")
                    ui.label(r.product_name or "（商品名はまだありません）").classes("grow")
                    ui.label(r.updated_at.strftime("%m/%d %H:%M") if r.updated_at else "").classes("hint w-24")
                    ui.badge(r.stage_label).props("outline")
                    ui.button("開く", on_click=lambda r=r: open_project(r.id)).props("flat")


def startup_problems() -> list[tuple[str, str]]:
    out = []
    try:
        CONFIG.services.prof()
    except UserError as e:
        out.append((e.what, e.how))
    if CONFIG.services.claude is None and shutil.which("claude") is None:
        out.append(("Claude Code が見つかりません。",
                    "商品調査やテロップ作りに使います。インストールしてログインしてから、もう一度起動してください。"))
    return out


def open_project(pid: str) -> None:
    project = load_project(CONFIG.projects / pid)
    ui.navigate.to(stage_url(pid, project.stage))


def new_project_dialog() -> None:
    with ui.dialog() as dlg, ui.card().classes("w-[440px] gap-3"):
        ui.label("型番を入れてください").classes("text-lg font-bold")
        model = ui.input("型番（例: FMVU59L2BA）").props("autofocus outlined").classes("w-full")

        @guarded
        def create() -> None:
            project, folder = wf.start_project(model.value, CONFIG.projects)
            dlg.close()
            ui.navigate.to(f"/p/{folder.name}/plan?research=1")

        model.on("keydown.enter", create)
        with ui.row().classes("self-end"):
            ui.button("やめる", on_click=dlg.close).props("flat")
            ui.button("はじめる", on_click=create)
    dlg.open()


# =============================================================================
# 1. 企画
# =============================================================================

def specs_to_text(specs: dict[str, str]) -> str:
    return "\n".join(f"{k}: {v}" for k, v in specs.items())


def text_to_specs(text: str) -> dict[str, str]:
    out = {}
    for line in text.splitlines():
        if ":" in line or "：" in line:
            k, _, v = line.replace("：", ":").partition(":")
            if k.strip():
                out[k.strip()] = v.strip()
    return out


@ui.page("/p/{pid}/plan")
async def plan_page(pid: str, research: int = 0) -> None:
    setup_page("企画")
    loaded = load_or_home(pid)
    if not loaded:
        return
    project, folder = loaded
    header(project, Stage.PLAN, folder, "plan")

    def persist(changed_plan: bool = True) -> None:
        if changed_plan:
            project.mark_changed(Stage.PLAN)
        save_project(folder, project)

    with ui.column().classes("page gap-5 p-6"):
        # --- 商品 ---
        with section("① 商品", "調べた内容は直せます。間違いがないか確認してください。"):
            product_area = ui.column().classes("w-full gap-3")

        @ui.refreshable
        def product_view() -> None:
            p = project.product
            with ui.row().classes("w-full gap-3"):
                ui.input("商品名", value=p.name).bind_value(p, "name").on("blur", lambda: persist()) \
                    .props("outlined").classes("grow")
                ui.input("メーカー", value=p.maker).bind_value(p, "maker").on("blur", lambda: persist()) \
                    .props("outlined").classes("w-48")
            specs = ui.textarea("主な仕様（1行に1つ「項目: 内容」）", value=specs_to_text(p.specs)) \
                .props("outlined autogrow").classes("w-full")
            specs.on("blur", lambda: (setattr(p, "specs", text_to_specs(specs.value)), persist()))
            feats = ui.textarea("お客様にとっての良さ（1行に1つ）", value="\n".join(p.features)) \
                .props("outlined autogrow").classes("w-full")
            feats.on("blur", lambda: (setattr(p, "features", [x.strip() for x in feats.value.splitlines()
                                                                if x.strip()]), persist()))
            if p.sources:
                ui.label("参照: " + "、".join(p.sources[:3])).classes("hint")

        async def do_research(refresh: bool = False) -> None:
            nonlocal project
            got = await run_job("商品を調べています", wf.research_product_into, folder, CONFIG.services,
                                refresh=refresh)
            if got:
                project = got
                refresh_all()

        with product_area:
            product_view()
            with ui.row().classes("gap-2"):
                ui.button("Webで調べる" if not project.product.name else "調べ直す", icon="travel_explore",
                          on_click=lambda: do_research(refresh=bool(project.product.name))).props("outline")
            with ui.expansion("Webで見つからないとき：商品ページを貼る", icon="content_paste").classes("w-full"):
                ui.label("PCのブラウザで商品ページを開き、全選択してコピーしたものを貼ってください。"
                         "スマホの縦長スクリーンショットも使えます（縮小していない元の画像）。").classes("hint")
                paste = ui.textarea("商品ページの文章").props("outlined").classes("w-full")
                shots: list[Path] = []
                shot_label = ui.label("").classes("hint")

                async def got_images(e) -> None:
                    tmp = Path(tempfile.mkdtemp())
                    for f in e.files:
                        dst = tmp / f.name
                        await f.save(dst)
                        shots.append(dst)
                    shot_label.set_text(f"画像 {len(shots)} 枚")

                ui.upload(label="スクリーンショットを入れる", multiple=True, auto_upload=True,
                          on_multi_upload=got_images).props("accept=image/* flat bordered color=grey-2 text-color=grey-9") \
                    .classes("w-full")

                async def from_page() -> None:
                    nonlocal project
                    got = await run_job("商品ページを整理しています", wf.product_from_page_into, folder,
                                        CONFIG.services, text=paste.value or "", images=list(shots))
                    if got:
                        project = got
                        refresh_all()

                ui.button("これで整理する", on_click=from_page).props("outline")

        # --- 条件 ---
        with section("② 動画の条件"):
            with ui.row().classes("w-full gap-4 items-end"):
                minutes = ui.number("目標の長さ（秒）", value=project.plan.target_seconds, min=30, max=600, step=10) \
                    .props("outlined").classes("w-48")
                minutes.on("blur", lambda: (setattr(project.plan, "target_seconds", int(minutes.value or 150)),
                                            persist(), scenes_view.refresh()))
                price_mode = ui.select({"hidden": "価格は出さない", "open": "オープン価格", "amount": "金額を出す"},
                                       value=project.plan.price.mode, label="価格").props("outlined").classes("w-48")
                amount = ui.number("金額（円・税込）", value=project.plan.price.amount, min=0, step=100,
                                   format="%.0f").props("outlined").classes("w-48")
                amount.bind_visibility_from(price_mode, "value", value="amount")

                def save_price() -> None:
                    project.plan.price = Price(mode=price_mode.value,
                                               amount=int(amount.value) if amount.value else None)
                    persist()

                price_mode.on_value_change(save_price)
                amount.on("blur", save_price)
            extra = ui.textarea("追加で入れたい情報（例: 展示品あり、店頭限定カラー）", value=project.plan.extra_text) \
                .props("outlined autogrow").classes("w-full")
            extra.on("blur", lambda: (setattr(project.plan, "extra_text", extra.value or ""), persist()))

        # --- 構成 ---
        separate = project.separate_voice
        with section("③ 構成と撮るカット" if separate else "③ 台本と構成",
                     "説明の場面は映像だけを撮り、声は映像ができてから台本を作って録ります。"
                     "あいさつと店舗案内は顔を出してその場で話します。自由に直せます。" if separate
                     else "場面ごとのセリフと、撮るカットです。自由に直せます。"):
            scenes_box = ui.column().classes("w-full gap-3")

        @ui.refreshable
        def scenes_view() -> None:
            scenes = project.plan.scenes
            budget = project.plan.target_seconds - 8
            total = sum(s.seconds for s in scenes)
            if scenes:
                color = "text-green-700" if abs(total - budget) <= budget * 0.15 else "text-orange-600"
                ui.label(f"場面の合計 {total:.0f}秒 ／ 目安 {budget}秒（タイトル3秒・エンドカード5秒は別）") \
                    .classes(f"font-bold {color}")
            for i, s in enumerate(scenes):
                with ui.card().classes("w-full gap-2").props("flat bordered"):
                    with ui.row().classes("w-full items-center gap-3"):
                        ui.label(str(i + 1)).classes("bg-primary text-white rounded-full w-8 h-8 flex "
                                                     "items-center justify-center font-bold")
                        ui.input("場面", value=s.title).bind_value(s, "title").on("blur", lambda: persist()) \
                            .props("outlined dense").classes("grow")
                        sec = ui.number("秒", value=s.seconds, min=1, step=1).props("outlined dense").classes("w-24")
                        sec.on("blur", lambda sec=sec, s=s: (setattr(s, "seconds", float(sec.value or 1)),
                                                             persist(), scenes_view.refresh()))
                        ui.button(icon="arrow_upward", on_click=lambda i=i: move_scene(i, -1)).props("flat dense")
                        ui.button(icon="delete", on_click=lambda i=i: delete_scene(i)).props("flat dense color=grey")
                    if separate:
                        with ui.row().classes("w-full items-center gap-3"):
                            ui.toggle({"talk": "顔を出して話す", "narration": "映像だけ（声はあとで録る）"}, value=s.voice,
                                      on_change=lambda e, s=s: (setattr(s, "voice", e.value), persist(),
                                                                scenes_view.refresh())).props("dense no-caps")
                        ui.textarea("伝えること", value=s.points).bind_value(s, "points") \
                            .on("blur", lambda: persist()).props("outlined autogrow").classes("w-full")
                    if not separate or s.voice == "talk":
                        ui.textarea("話すセリフ", value=s.script).bind_value(s, "script").on("blur", lambda: persist()) \
                            .props("outlined autogrow").classes("w-full")
                    elif s.script:
                        ui.label(f"ナレーション（映像の完成後に作った台本）: {s.script}").classes("hint text-sm")
                    shots = ui.textarea("撮るカット（並べる順に1行に1つ）", value="\n".join(s.shots)) \
                        .props("outlined autogrow").classes("w-full")
                    shots.on("blur", lambda shots=shots, s=s: (
                        setattr(s, "shots", [x.strip() for x in (shots.value or "").splitlines() if x.strip()]),
                        persist()))
            ui.button("場面を足す", icon="add", on_click=add_scene).props("flat")

        def move_scene(i: int, d: int) -> None:
            sc = project.plan.scenes
            if 0 <= i + d < len(sc):
                sc[i], sc[i + d] = sc[i + d], sc[i]
                persist()
                scenes_view.refresh()

        def delete_scene(i: int) -> None:
            project.plan.scenes.pop(i)
            persist()
            scenes_view.refresh()

        def add_scene() -> None:
            n = max([int(s.id[1:]) for s in project.plan.scenes if s.id[1:].isdigit()] + [0]) + 1
            project.plan.scenes.append(Scene(id=f"s{n}", title="新しい場面", seconds=10))
            persist()
            scenes_view.refresh()

        async def make_script() -> None:
            nonlocal project
            save_project(folder, project)
            got = await run_job("構成と撮るカットを考えています" if separate else "台本と構成を考えています",
                                wf.make_plan_into, folder, CONFIG.services)
            if got:
                project = got
                refresh_all()

        with scenes_box:
            scenes_view()
            word = "構成" if separate else "台本"
            ui.button(f"{word}を作る" if not project.plan.scenes else f"{word}を作り直す", icon="auto_awesome",
                      on_click=make_script).props("outline")

        with ui.expansion("詳細設定", icon="tune").classes("section"):
            def set_mode(e) -> None:
                project.plan.voice_mode = e.value
                for sc in project.plan.scenes:
                    if e.value == "live":
                        sc.voice = "talk"
                persist()
                refresh_all()
            ui.select({"separate": "声は別に録る（映像を先に完成させ、台本を作ってから録る）",
                       "live": "撮影のときに話す（これまでの方式）"}, value=project.plan.voice_mode,
                      label="声の録り方", on_change=set_mode).props("outlined").classes("w-[520px]")

        def refresh_all() -> None:
            ui.navigate.to(f"/p/{pid}/plan")

        @guarded
        def confirm() -> None:
            save_project(folder, project)
            sheet = wf.confirm_plan(folder)
            with ui.dialog() as dlg, ui.card().classes("w-[480px] gap-3"):
                ui.label("撮影リストができました").classes("text-lg font-bold")
                ui.label("印刷するか、スマホに送って撮影しながら見てください。").classes("hint")
                with ui.row().classes("gap-2"):
                    ui.button("撮影リストを開く", icon="print", on_click=lambda: open_path(sheet)).props("outline")
                    ui.button("フォルダを開く", icon="folder_open",
                              on_click=lambda: open_path(sheet.parent)).props("flat")
                ui.button("撮影が終わったら：素材を入れる", icon="arrow_forward",
                          on_click=lambda: ui.navigate.to(f"/p/{pid}/material")).classes("self-end")
            dlg.open()

        with ui.row().classes("w-full justify-end gap-3"):
            ui.button("素材の画面へ（企画を飛ばす）", on_click=lambda: ui.navigate.to(f"/p/{pid}/material")) \
                .props("flat color=grey")
            ui.button("企画を確定して撮影リストを出す", icon="check", on_click=confirm).classes("big-button")

    if research and not project.product.name:
        await do_research()


# =============================================================================
# 2. 素材
# =============================================================================

@ui.page("/p/{pid}/material")
async def material_page(pid: str) -> None:
    setup_page("素材")
    loaded = load_or_home(pid)
    if not loaded:
        return
    project, folder = loaded
    header(project, Stage.MATERIAL, folder, "material")

    def persist() -> None:
        project.mark_changed(Stage.MATERIAL)
        save_project(folder, project)

    async def import_from(source) -> None:
        nonlocal project
        got = await run_job("素材を取り込んでいます", wf.import_material, folder, source, CONFIG.services)
        if got is not None:
            project = load_project(folder)
            clips_view.refresh()

    with ui.column().classes("page gap-5 p-6"):
        stale_notice(project, Stage.MATERIAL)
        with section("① 素材を入れる", "スマホで撮った動画を入れます。元のファイルは変更しません。"):
            with ui.row().classes("gap-3 items-center"):
                async def choose() -> None:
                    path = await pick_folder()
                    if path is None and not CONFIG.native:
                        path_input.set_visibility(True)
                        return
                    if path:
                        await import_from(path)

                ui.button("フォルダを選ぶ", icon="folder_open", on_click=choose).classes("big-button")
                ui.label("または、下に動画ファイルをドラッグしてください").classes("hint")
            path_input = ui.input("フォルダの場所", placeholder=r"例: D:\素材").props("outlined").classes("w-full")
            path_input.set_visibility(not CONFIG.native)
            path_input.on("keydown.enter", lambda: import_from(Path(path_input.value.strip().strip('"'))))

            async def dropped(e) -> None:
                tmp = Path(tempfile.mkdtemp())
                files = []
                for f in e.files:
                    dst = tmp / f.name
                    await f.save(dst)
                    files.append(dst)
                await import_from(files)

            ui.upload(label="動画ファイルをここにドラッグ", multiple=True, auto_upload=True,
                      on_multi_upload=dropped).props("accept=video/* flat bordered color=grey-2 text-color=grey-9") \
                .classes("w-full")

        with section("② クリップの確認", "場面は自動で振り分けます。違うときは選び直してください。"):
            @ui.refreshable
            def clips_view() -> None:
                if not project.clips:
                    ui.label("まだ素材がありません。").classes("hint")
                    return
                scenes = {"": "自動"} | {s.id: f"{i}. {s.title}" for i, s in enumerate(project.plan.scenes, 1)}
                used = sum(1 for c in project.clips if not c.excluded)
                ui.label(f"{len(project.clips)}本中 {used}本を使います").classes("font-bold")
                with ui.grid(columns=3).classes("w-full gap-4"):
                    for c in project.clips:
                        with ui.card().classes("gap-2" + (" opacity-50" if c.excluded else "")).props("flat bordered"):
                            thumb = wf.thumb_path(folder, c.file)
                            if thumb.exists():
                                ui.image(media_url(thumb)).classes("w-full rounded")
                            ui.label(c.file).classes("font-mono text-sm truncate w-full")
                            with ui.row().classes("gap-2 items-center"):
                                ui.label(f"{c.duration:.1f}秒").classes("hint")
                                if c.vertical:
                                    ui.badge("縦撮り").props("outline color=orange")
                                if c.silent:
                                    ui.badge("話し声なし（差し込み用）").props("outline color=teal")
                                if c.kept_seconds:
                                    ui.label(f"→ {c.kept_seconds:.1f}秒").classes("hint")
                            if c.exclude_reason:
                                ui.label(f"注意: {c.exclude_reason}").classes("text-orange-700 text-sm")
                            if scenes and project.plan.scenes:
                                current = c.scene_id if c.scene_manual else ""
                                label = "場面" + ("（自動: " + scenes.get(c.scene_id or "", "未定") + "）"
                                                  if c.scene_id and not c.scene_manual else "")
                                sel = ui.select(scenes, value=current, label=label).props("outlined dense") \
                                    .classes("w-full")
                                sel.on_value_change(lambda e, c=c: (
                                    setattr(c, "scene_id", e.value or None), setattr(c, "scene_manual", bool(e.value)),
                                    persist()))
                            sw = ui.switch("使う", value=not c.excluded)
                            sw.on_value_change(lambda e, c=c: (setattr(c, "excluded", not e.value), persist(),
                                                               clips_view.refresh()))

            clips_view()

        with ui.expansion("詳細設定", icon="tune").classes("section"):
            with ui.row().classes("gap-4"):
                ui.select({"weak": "弱（間を残す）", "normal": "標準", "strong": "強（テンポ重視）"},
                          value=project.settings.silence_strength, label="話していない部分を詰める強さ",
                          on_change=lambda e: (setattr(project.settings, "silence_strength", e.value), persist())) \
                    .props("outlined").classes("w-64")
                ui.select({"pad": "黒い帯を付ける", "crop": "切り抜いて画面いっぱいに"}, value=project.settings.fit,
                          label="縦撮りの動画",
                          on_change=lambda e: (setattr(project.settings, "fit", e.value), persist())) \
                    .props("outlined").classes("w-64")
            telop_sw = ui.switch("テロップを入れる", value=bool(project.product.name))
            telop_sw.set_visibility(not project.separate_voice)  # 声を別に録るときは、仕上げで作る
            ui.switch("編集の判断を Claude Code にまかせる（言い直しを切る・並べる・差し込む）",
                      value=project.settings.ai_edit,
                      on_change=lambda e: (setattr(project.settings, "ai_edit", e.value), persist()))

        separate = project.separate_voice

        async def start_edit() -> None:
            save_project(folder, project)
            done = await run_job("映像を組み立てています" if separate else "編集しています", wf.build_edit, folder,
                                 CONFIG.services, with_telops=telop_sw.value)
            if done is not None:
                for n in done.notes:
                    ui.notify(n, type="warning", timeout=8000, position="top-right")
                ui.navigate.to(f"/p/{pid}/{'video' if separate else 'review'}")

        with ui.row().classes("w-full justify-end"):
            ui.button("映像を編集する" if separate else "編集を始める", icon="content_cut", on_click=start_edit) \
                .classes("big-button")


# =============================================================================
# 3. 確認
# =============================================================================

FONT_OPTIONS = {"": "自動"} | {k: v.label for k, v in FONTS.items()}


ACTION_LABELS = {"move": "時刻を動かした", "rewrite": "文言を直した", "delete": "消した"}


def shown(text: str) -> str:
    """画面に出る文字だけ（強調の印 ** と改行を外す）。"""
    return text.replace("**", "").replace("\n", " ")


def telop_check_section(project: Project, folder: Path, pid: str) -> None:
    """Claude Code がテロップと画面を見比べた結果（食い違いと、どう直したか）。"""
    from app import telopcheck as tc
    with section("テロップと映像の照らし合わせ",
                 "テロップの内容と、そのとき画面に映っているものが食い違っていないか、Claude Code が画面を見て確かめます。"):
        if project.telop_check_summary:
            ui.label(project.telop_check_summary).classes("text-sm")
        if project.telops and not project.telop_checks and project.telop_check_summary:
            ui.label("食い違いは見つかりませんでした。").classes("text-sm text-green-700")
        for i, c in enumerate(project.telop_checks):
            with ui.card().classes("w-full gap-1" + (" opacity-60" if c.undone else "")).props("flat bordered"):
                with ui.row().classes("w-full items-center gap-2"):
                    if c.manual:
                        ui.badge("手で直したテロップ（知らせるだけ）").props("color=orange")
                    elif c.undone:
                        ui.badge("元に戻した").props("color=grey")
                    else:
                        ui.badge(ACTION_LABELS[c.action]).props("color=primary")
                    ui.label(f"「{shown(c.text)}」 {c.start:.1f}〜{c.end:.1f}秒").classes("font-bold")
                ui.label(f"食い違い: {c.problem}").classes("text-sm")
                if c.shows:
                    ui.label(f"そのとき映っているもの: {c.shows}").classes("hint text-sm")
                if c.after is not None and c.action != "delete":
                    head = "おすすめの直し方" if c.manual else "直したあと"
                    ui.label(f"{head}: 「{shown(c.after.text)}」 {c.after.start:.1f}〜{c.after.end:.1f}秒") \
                        .classes("text-sm text-primary")
                with ui.row().classes("gap-2"):
                    if c.applied and not c.undone and not c.manual:
                        ui.button("元に戻す", icon="undo", on_click=lambda i=i: (
                            tc.undo_check(folder, i), ui.navigate.to(f"/p/{pid}/review"))).props("flat dense")
                    if c.manual and not c.applied:
                        ui.button("おすすめのとおり直す", icon="check", on_click=lambda i=i: (
                            tc.apply_suggestion(folder, i), ui.navigate.to(f"/p/{pid}/review"))).props("flat dense")

        async def recheck() -> None:
            save_project(folder, project)
            if await run_job("テロップと映像を照らし合わせています", tc.run_check, folder, CONFIG.services) is not None:
                ui.navigate.to(f"/p/{pid}/review")

        ui.button("テロップと映像を照らし合わせる", icon="fact_check", on_click=recheck).props("outline")


def fit_section(project: Project, pid: str) -> None:
    """声に合わせて映像の長さを調整した結果（声を別に録る動画）。"""
    with section("② 声に合わせた調整", "映像の長さを、録った声に合わせました。声や台本を直すときはナレーションの画面へ。"):
        if not project.fitted:
            ui.label("まだ声に合わせていません。ナレーションの画面で「声に合わせて仕上げる」を押してください。").classes("hint")
        for n in project.fit_notes:
            ui.label(f"・{n}").classes("text-sm")
        if project.fitted and not project.fit_notes:
            ui.label("カットの長さを少し伸ばす・詰めるだけで、ぴったり合いました。").classes("text-sm")
        ui.button("ナレーションの画面へ", icon="mic", on_click=lambda: ui.navigate.to(f"/p/{pid}/narration")) \
            .props("flat")


@ui.page("/p/{pid}/review")
async def review_page(pid: str) -> None:
    setup_page("仕上げ")
    loaded = load_or_home(pid)
    if not loaded:
        return
    project, folder = loaded
    header(project, Stage.REVIEW, folder, "review")
    style = project.settings.telop_style

    def persist() -> None:
        save_project(folder, project)

    with ui.column().classes("page gap-5 p-6"):
        stale_notice(project, Stage.REVIEW)

        # --- プレビュー ---
        with section("① プレビュー（冒頭15秒）", "テロップ・BGM の感じを確かめます。直したら作り直してください。"):
            preview = folder / "work" / "preview.mp4"
            player = ui.video(media_url(preview) if preview.exists() else "").classes("w-full rounded")
            player.set_visibility(preview.exists())

            async def make_preview() -> None:
                persist()
                out = await run_job("プレビューを作っています", wf.render_preview, folder, CONFIG.services)
                if out:
                    player.set_source(media_url(out))
                    player.set_visibility(True)

            ui.button("プレビューを作る", icon="play_circle", on_click=make_preview).props("outline")

        # --- 編集の内容 ---
        if project.separate_voice:
            fit_section(project, pid)
        else:
            edit_section(project, folder, pid)

        # --- 色 ---
        with section("③ テロップの色", "カラーコード（#RRGGBB）で入れるか、色見本から選びます。"):
            with ui.row().classes("gap-4 items-end"):
                def color_box(label: str, attr: str) -> None:
                    def changed(e) -> None:
                        try:
                            setattr(project.settings, "telop_style",
                                    TelopStyle.model_validate({**project.settings.telop_style.model_dump(),
                                                               attr: e.value or ""}))
                            persist()
                        except UserError as err:
                            show_error(err)
                    ui.color_input(label, value=getattr(style, attr), on_change=changed) \
                        .props("outlined").classes("w-60")
                color_box("文字", "text_color")
                color_box("縁取り", "outline_color")
                color_box("強調（**で囲んだ言葉）", "emphasis_color")
                color_box("帯（空なら帯なし）", "band_color")
                ui.number("帯の濃さ（0〜1）", value=style.band_opacity, min=0, max=1, step=0.1,
                          on_change=lambda e: (setattr(project.settings.telop_style, "band_opacity",
                                                       float(e.value or 0)), persist())) \
                    .props("outlined").classes("w-40")

        # --- タイトルとテロップ ---
        with section("④ 冒頭タイトルとテロップ",
                     "文言・時刻・フォント・色を直せます。**ことば** と囲むと強調色になります。直したテロップは作り直しても残ります。"):
            title = ui.input("冒頭タイトル", value=project.title).props("outlined").classes("w-full")
            title.on("blur", lambda: (setattr(project, "title", title.value or ""), persist()))

            @ui.refreshable
            def telops_view() -> None:
                if not project.telops:
                    ui.label("テロップはありません（テロップなしで編集しました）。").classes("hint")
                for i, t in enumerate(project.telops):
                    with ui.card().classes("w-full gap-2").props("flat bordered"):
                        with ui.row().classes("w-full items-center gap-2"):
                            s = ui.number("開始（秒）", value=t.start, step=0.1, format="%.1f") \
                                .props("outlined dense").classes("w-28")
                            e = ui.number("終了（秒）", value=t.end, step=0.1, format="%.1f") \
                                .props("outlined dense").classes("w-28")
                            txt = ui.textarea("テロップ", value=t.text).props("outlined dense autogrow rows=1") \
                                .classes("grow")
                            font = ui.select(FONT_OPTIONS, value=t.font if t.font in FONTS else "", label="フォント") \
                                .props("outlined dense").classes("w-56")
                            ui.button(icon="delete", on_click=lambda i=i: remove(i)).props("flat dense color=grey")

                            def edited(_=None, t=t, s=s, e=e, txt=txt, font=font) -> None:
                                t.start, t.end = float(s.value or 0), float(e.value or 0)
                                t.text, t.font, t.manual = txt.value or "", font.value or "", True
                                persist()

                            for el in (s, e, txt):
                                el.on("blur", edited)
                            font.on_value_change(edited)
                        with ui.expansion("このテロップだけ色を変える").classes("w-full text-sm"):
                            with ui.row().classes("gap-3"):
                                for label, attr in (("文字", "text_color"), ("縁取り", "outline_color"),
                                                    ("強調", "emphasis_color")):
                                    def changed(ev, t=t, attr=attr) -> None:
                                        try:
                                            setattr(t, attr, ev.value or "")
                                            t.manual = True
                                            persist()
                                        except UserError as err:
                                            show_error(err)
                                    ui.color_input(f"{label}（空なら全体の色）", value=getattr(t, attr),
                                                   on_change=changed).props("outlined dense").classes("w-56")
                ui.button("テロップを足す", icon="add", on_click=add).props("flat")

            def remove(i: int) -> None:
                project.telops.pop(i)
                persist()
                telops_view.refresh()

            def add() -> None:
                last = project.telops[-1].end if project.telops else 0.0
                project.telops.append(Telop(start=round(last + 0.5, 1), end=round(last + 3.5, 1),
                                            text="新しいテロップ", manual=True))
                persist()
                telops_view.refresh()

            telops_view()

        # --- テロップと映像の照らし合わせ ---
        telop_check_section(project, folder, pid)

        # --- BGM ---
        with section("⑤ BGM"):
            with ui.row().classes("gap-4"):
                choices = wf.bgm_choices()
                if choices:
                    ui.select(choices, value=project.settings.bgm_file if project.settings.bgm_file in choices
                              else choices[0], label="曲",
                              on_change=lambda e: (setattr(project.settings, "bgm_file", e.value),
                                                   [p.unlink() for p in (folder / "work").glob("bgm.*")], persist())) \
                        .props("outlined").classes("w-72")
                else:
                    ui.label("assets/bgm フォルダに曲を入れると使えます。").classes("hint")
                ui.toggle({"small": "小", "medium": "中", "large": "大"}, value=project.settings.bgm_level,
                          on_change=lambda e: (setattr(project.settings, "bgm_level", e.value), persist()))

        with ui.row().classes("w-full justify-end"):
            ui.button("書き出しへ進む", icon="arrow_forward",
                      on_click=lambda: (persist(), ui.navigate.to(f"/p/{pid}/export"))).classes("big-button")


def edit_section(project: Project, folder: Path, pid: str) -> None:
    """Claude Code（または機械的な編集）が決めた、使う部分・差し込み・切った部分の一覧。"""
    edit = project.edit
    dirty = {"changed": False}

    def persist() -> None:
        dirty["changed"] = True
        save_project(folder, project)
        redo.props("color=orange")
        redo.set_text("直した内容で作り直す")

    def thumb(clip: str, t: float, width: str = "w-40") -> None:
        path = wf.edit_thumb(folder, clip, t)
        if path.exists():
            ui.image(media_url(path)).classes(f"{width} rounded shrink-0")
        else:
            ui.element("div").classes(f"{width} h-20 bg-gray-200 rounded shrink-0")

    def say(clip: str, a: float, b: float) -> str:
        text = wf.spoken_text(folder, project, clip, a, b)
        return f"「{text}」" if text else "（話し声なし）"

    scene_names = {s.id: s.title for s in project.plan.scenes}

    with section("② 編集の内容", "使う部分を上から順につなぎます。理由を確かめて、違うところは直してください。"):
        if edit is None:
            ui.label("まだ編集していません。素材の画面で「編集を始める」を押してください。").classes("hint")
            return
        with ui.row().classes("items-center gap-2"):
            if edit.source == "ai":
                ui.badge("Claude Code が編集").props("color=primary")
            else:
                ui.badge("機械的に編集（無音カットと台本の順）").props("color=grey")
            ui.label(edit.summary).classes("text-sm")

        @ui.refreshable
        def pieces_view() -> None:
            for i, p in enumerate(edit.pieces):
                with ui.card().classes("w-full gap-2" + ("" if p.enabled else " opacity-50")).props("flat bordered"):
                    with ui.row().classes("w-full items-start gap-3 no-wrap"):
                        ui.label(str(i + 1)).classes("bg-primary text-white rounded-full w-8 h-8 flex items-center "
                                                     "justify-center font-bold shrink-0")
                        thumb(p.clip, p.start)
                        with ui.column().classes("grow gap-1"):
                            with ui.row().classes("items-center gap-2"):
                                if p.scene_id in scene_names:
                                    ui.badge(scene_names[p.scene_id]).props("outline")
                                ui.label(f"{p.clip}　{p.start:.1f}〜{p.end:.1f}秒（{p.end - p.start:.1f}秒）") \
                                    .classes("font-mono text-sm")
                            ui.label(say(p.clip, p.start, p.end)).classes("text-sm")
                            if p.reason:
                                ui.label(f"理由: {p.reason}").classes("hint text-sm")
                        with ui.column().classes("items-end gap-1 shrink-0"):
                            ui.switch("使う", value=p.enabled,
                                      on_change=lambda e, p=p: (setattr(p, "enabled", e.value), persist(),
                                                                pieces_view.refresh()))
                            with ui.row().classes("gap-0"):
                                ui.button(icon="arrow_upward", on_click=lambda i=i: (
                                    wf.move_piece(project, i, -1), persist(), pieces_view.refresh())) \
                                    .props("flat dense").tooltip("前へ")
                                ui.button(icon="arrow_downward", on_click=lambda i=i: (
                                    wf.move_piece(project, i, 1), persist(), pieces_view.refresh())) \
                                    .props("flat dense").tooltip("後ろへ")
                    for ins in [x for x in edit.inserts if x.piece == i]:
                        with ui.row().classes("w-full items-center gap-3 no-wrap pl-12" +
                                              ("" if ins.enabled else " opacity-50")):
                            ui.icon("subdirectory_arrow_right", color="primary")
                            thumb(ins.clip, ins.start, "w-28")
                            with ui.column().classes("grow gap-0"):
                                ui.label(f"差し込み: {ins.clip} {ins.start:.1f}〜{ins.end:.1f}秒を、"
                                         f"この部分の {ins.offset:.1f}秒後から重ねる（声はそのまま）").classes("text-sm")
                                if ins.reason:
                                    ui.label(f"理由: {ins.reason}").classes("hint text-sm")
                            ui.switch("使う", value=ins.enabled,
                                      on_change=lambda e, ins=ins: (setattr(ins, "enabled", e.value), persist(),
                                                                    pieces_view.refresh()))
            if edit.dropped:
                with ui.expansion(f"切った部分（{len(edit.dropped)}か所）", icon="content_cut").classes("w-full"):
                    for k, d in enumerate(edit.dropped):
                        with ui.row().classes("w-full items-center gap-3 no-wrap border-b py-2"):
                            thumb(d.clip, d.start, "w-28")
                            with ui.column().classes("grow gap-0"):
                                ui.label(f"{d.clip}　{d.start:.1f}〜{d.end:.1f}秒　{say(d.clip, d.start, d.end)}") \
                                    .classes("text-sm")
                                ui.label(f"理由: {d.reason}").classes("hint text-sm")
                            ui.button("戻す", icon="undo", on_click=lambda k=k: (
                                wf.restore_drop(project, k), persist(), pieces_view.refresh())).props("flat")

        pieces_view()

        async def rerender() -> None:
            got = await run_job("直した内容で作り直しています", wf.render_edit, folder, CONFIG.services)
            if got is not None:
                ui.navigate.to(f"/p/{pid}/review")

        async def ai_again() -> None:
            got = await run_job("Claude Code が編集を考え直しています", wf.replan_edit, folder, CONFIG.services)
            if got is not None:
                for n in got.notes:
                    ui.notify(n, type="warning", timeout=8000, position="top-right")
                ui.navigate.to(f"/p/{pid}/review")

        with ui.row().classes("gap-3"):
            redo = ui.button("この内容で作り直す", icon="refresh", on_click=rerender).props("outline")
            ui.button("Claude Code に編集し直してもらう", icon="auto_awesome", on_click=ai_again).props("flat")
        ui.label("作り直すと、テロップも新しい編集に合わせて作り直します（手で直したテロップは残ります）。") \
            .classes("hint text-sm")


# =============================================================================
# 4. 書き出し
# =============================================================================

@ui.page("/p/{pid}/export")
async def export_page(pid: str) -> None:
    setup_page("書き出し")
    loaded = load_or_home(pid)
    if not loaded:
        return
    project, folder = loaded
    header(project, Stage.EXPORT, folder, "export")
    out_dir = folder / "output"

    with ui.column().classes("page gap-5 p-6"):
        with section("① 書き出す動画", "店頭のサイネージ用と、YouTube 用では音の大きさの基準が違います。"):
            store = ui.checkbox("店頭用", value="store" in project.settings.outputs)
            youtube = ui.checkbox("YouTube用（概要欄の説明文も作ります）", value="youtube" in project.settings.outputs)

        results = ui.column().classes("w-full gap-5")

        def show_results() -> None:
            results.clear()
            files = sorted(out_dir.glob("*.mp4"))
            if not files:
                return
            with results, section("② できた動画"):
                for f in files:
                    with ui.row().classes("items-center gap-3"):
                        ui.icon("movie", color="primary")
                        ui.label(f.name).classes("font-mono")
                        ui.label(f"{f.stat().st_size / 1_000_000:.1f}MB").classes("hint")
                report = out_dir / "書き出し結果.txt"
                if report.exists():
                    ui.label(report.read_text(encoding="utf-8")).classes("hint whitespace-pre-line")
                ui.button("フォルダを開く", icon="folder_open", on_click=lambda: open_path(out_dir)).props("outline")
                yt = load_project(folder)
                if yt.youtube_description:
                    ui.label("YouTube の説明文").classes("section-title mt-2")
                    text = f"{yt.youtube_title}\n\n{yt.youtube_description}"
                    ui.textarea(value=text).props("outlined readonly autogrow").classes("w-full")
                    ui.button("コピー", icon="content_copy",
                              on_click=lambda: (ui.clipboard.write(text), ui.notify("コピーしました", position="top-right"))).props("flat")

        async def do_export() -> None:
            outputs = [k for k, on in (("store", store.value), ("youtube", youtube.value)) if on]
            if not outputs:
                show_error(UserError("書き出す動画が選ばれていません。", "「店頭用」か「YouTube用」にチェックを入れてください。"))
                return
            notes: list[str] = []
            exports = await run_job("書き出しています", wf.export_all, folder, CONFIG.services, outputs, notes=notes)
            if exports:
                (out_dir / "書き出し結果.txt").write_text("\n".join(e.summary() for e in exports), encoding="utf-8")
                for n in notes:
                    ui.notify(n, type="warning", timeout=8000, position="top-right")
                ui.notify("書き出しが終わりました", type="positive", position="top-right")
                show_results()

        with ui.row().classes("w-full justify-end"):
            ui.button("書き出す", icon="file_download", on_click=do_export).classes("big-button")
        show_results()


def register_media() -> None:
    CONFIG.projects.mkdir(parents=True, exist_ok=True)
    app.add_media_files("/media", CONFIG.projects)

