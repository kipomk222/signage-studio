"""声を別に録る動画の画面: 3. 映像 と 4. ナレーション。"""

from __future__ import annotations

import tempfile
from pathlib import Path

from nicegui import ui

from app import voiceflow as vf
from app import workflow as wf
from app.claude.schemas import CHARS_PER_SEC, VOICE_MARGIN, script_chars
from app.pipeline.fit import target_length
from app.pipeline.narration import recording_name
from app.project import Stage, load_project, save_project
from app.ui.components import header, run_job, setup_page, stale_notice
from app.ui.pages import CONFIG, load_or_home, media_url, open_path, pick_folder, section


def _thumb(folder: Path, clip: str, t: float, width: str = "w-36") -> None:
    path = wf.edit_thumb(folder, clip, t)
    if path.exists():
        ui.image(media_url(path)).classes(f"{width} rounded shrink-0")
    else:
        ui.element("div").classes(f"{width} h-20 bg-gray-200 rounded shrink-0")


def _spans(folder: Path) -> dict[str, tuple[float, float]]:
    try:
        layouts, _ = vf._load_layouts(vf._layout_path(folder, "video"))
    except Exception:  # noqa: BLE001  まだ映像がない
        return {}
    return vf.scene_spans(layouts)


# =============================================================================
# 3. 映像
# =============================================================================

@ui.page("/p/{pid}/video")
async def video_page(pid: str) -> None:
    setup_page("映像")
    loaded = load_or_home(pid)
    if not loaded:
        return
    project, folder = loaded
    header(project, Stage.VIDEO, folder, "video")
    edit = project.edit

    def persist() -> None:
        save_project(folder, project)
        redo.props("color=orange")
        redo.set_text("直した内容で作り直す")

    with ui.column().classes("page gap-5 p-6"):
        stale_notice(project, Stage.VIDEO)
        with section("① できた映像", "声はまだ入っていません（あいさつと店舗案内の声だけ入っています）。"):
            video = folder / "work" / "video.mp4"
            if video.exists():
                ui.video(media_url(video)).classes("w-full rounded")
            else:
                ui.label("まだ映像がありません。素材の画面で「映像を編集する」を押してください。").classes("hint")

        with section("② カットの並び", "場面ごとに、上から順につなぎます。理由を確かめて、違うところは直してください。"):
            if edit is None:
                ui.label("まだ編集していません。").classes("hint")
            else:
                with ui.row().classes("items-center gap-2"):
                    ui.badge("Claude Code が編集" if edit.source == "ai" else "機械的に並べた") \
                        .props("color=primary" if edit.source == "ai" else "color=grey")
                    ui.label(edit.summary).classes("text-sm")
                spans = _spans(folder)

                @ui.refreshable
                def cuts_view() -> None:
                    for s in project.plan.scenes:
                        idx = [i for i, p in enumerate(edit.pieces) if p.scene_id == s.id]
                        span = spans.get(s.id)
                        with ui.row().classes("w-full items-center gap-2 mt-3"):
                            ui.label(s.title).classes("font-bold text-lg")
                            ui.badge("顔出しで話す" if s.voice == "talk" else "映像だけ（声はあとで）") \
                                .props("outline color=" + ("red" if s.voice == "talk" else "primary"))
                            if span:
                                ui.label(f"{span[1] - span[0]:.1f}秒（予定 {s.seconds:.0f}秒）").classes("hint")
                        if not idx:
                            ui.label("この場面のカットはありません。").classes("hint text-sm")
                        for i in idx:
                            p = edit.pieces[i]
                            with ui.row().classes("w-full items-center gap-3 no-wrap border-b py-1" +
                                                  ("" if p.enabled else " opacity-50")):
                                _thumb(folder, p.clip, p.start)
                                with ui.column().classes("grow gap-0"):
                                    ui.label(p.shows or ("話しているところ" if not p.mute else "")).classes("font-bold")
                                    ui.label(f"{p.clip}　{p.start:.1f}〜{p.end:.1f}秒（{p.end - p.start:.1f}秒）") \
                                        .classes("font-mono text-sm")
                                    if p.reason:
                                        ui.label(f"理由: {p.reason}").classes("hint text-sm")
                                ui.switch("使う", value=p.enabled, on_change=lambda e, p=p: (
                                    setattr(p, "enabled", e.value), persist(), cuts_view.refresh()))
                                with ui.row().classes("gap-0 shrink-0"):
                                    ui.button(icon="arrow_upward", on_click=lambda i=i: (
                                        wf.move_piece(project, i, -1), persist(), cuts_view.refresh())).props("flat dense")
                                    ui.button(icon="arrow_downward", on_click=lambda i=i: (
                                        wf.move_piece(project, i, 1), persist(), cuts_view.refresh())).props("flat dense")
                    if edit.spares:
                        with ui.expansion(f"予備のカット（{len(edit.spares)}）: 声が長いときに足します",
                                          icon="inventory_2").classes("w-full mt-2"):
                            names = {s.id: s.title for s in project.plan.scenes}
                            for sp in edit.spares:
                                with ui.row().classes("w-full items-center gap-3 no-wrap py-1"):
                                    _thumb(folder, sp.clip, sp.start, "w-28")
                                    ui.label(f"{names.get(sp.scene_id or '', '場面なし')}: {sp.shows or sp.clip}"
                                             f"（{sp.clip} {sp.start:.1f}〜{sp.end:.1f}秒）").classes("text-sm")
                    if edit.dropped:
                        with ui.expansion(f"切った部分（{len(edit.dropped)}か所）", icon="content_cut").classes("w-full"):
                            for k, d in enumerate(edit.dropped):
                                with ui.row().classes("w-full items-center gap-3 no-wrap border-b py-2"):
                                    _thumb(folder, d.clip, d.start, "w-28")
                                    ui.label(f"{d.clip}　{d.start:.1f}〜{d.end:.1f}秒　理由: {d.reason}") \
                                        .classes("text-sm grow")
                                    ui.button("戻す", icon="undo", on_click=lambda k=k: (
                                        wf.restore_drop(project, k), persist(), cuts_view.refresh())).props("flat")

                cuts_view()

            async def rerender() -> None:
                if await run_job("直した内容で作り直しています", vf.render_video, folder, CONFIG.services) is not None:
                    ui.navigate.to(f"/p/{pid}/video")

            async def ai_again() -> None:
                got = await run_job("Claude Code が映像を組み立て直しています", vf.build_video, folder,
                                    CONFIG.services, ai=True)
                if got is not None:
                    for n in got.notes:
                        ui.notify(n, type="warning", timeout=8000, position="top-right")
                    ui.navigate.to(f"/p/{pid}/video")

            with ui.row().classes("gap-3"):
                redo = ui.button("この内容で作り直す", icon="refresh", on_click=rerender).props("outline")
                ui.button("Claude Code に組み立て直してもらう", icon="auto_awesome", on_click=ai_again).props("flat")

        async def make_script() -> None:
            save_project(folder, project)
            if await run_job("映像に合わせて台本を書いています", vf.write_script, folder, CONFIG.services) is not None:
                ui.navigate.to(f"/p/{pid}/narration")

        with ui.row().classes("w-full justify-end"):
            ui.button("この映像で台本を作る", icon="edit_note", on_click=make_script).classes("big-button")


# =============================================================================
# 4. ナレーション
# =============================================================================

ASSIGNED = {"name": "ファイル名から", "claude": "Claude が中身から", "manual": "手で選んだ", "": ""}


@ui.page("/p/{pid}/narration")
async def narration_page(pid: str) -> None:
    setup_page("ナレーション")
    loaded = load_or_home(pid)
    if not loaded:
        return
    project, folder = loaded
    header(project, Stage.NARRATION, folder, "narration")
    spans = _spans(folder)
    scenes = vf.narration_scenes(project)
    numbers = {s.id: i for i, s in enumerate(project.plan.scenes, 1)}

    def persist() -> None:
        project.mark_changed(Stage.NARRATION)
        save_project(folder, project)

    with ui.column().classes("page gap-5 p-6"):
        stale_notice(project, Stage.NARRATION)

        # --- 台本 ---
        with section("① ナレーションの台本", "完成した映像の長さに合わせた台本です。読みやすいように直してかまいません。"):
            @ui.refreshable
            def script_view() -> None:
                lo, hi = CHARS_PER_SEC
                for s in scenes:
                    sec = spans[s.id][1] - spans[s.id][0] if s.id in spans else s.seconds
                    room = max(1.0, sec - VOICE_MARGIN)
                    with ui.card().classes("w-full gap-1").props("flat bordered"):
                        with ui.row().classes("w-full items-center gap-3"):
                            ui.label(str(numbers[s.id])).classes("bg-primary text-white rounded-full w-8 h-8 flex "
                                                                  "items-center justify-center font-bold")
                            ui.label(s.title).classes("font-bold")
                            ui.label(f"映像 {sec:.1f}秒").classes("hint")
                            count = ui.label("").classes("text-sm ml-auto")
                            ui.label(f"おすすめのファイル名: {recording_name(numbers[s.id], s)}").classes("hint text-xs")
                        box = ui.textarea(value=s.script).props("outlined autogrow").classes("w-full text-lg")

                        def show_count(_=None, box=box, count=count, room=room) -> None:
                            n = script_chars(box.value or "")
                            ok = lo * room <= n <= hi * room
                            count.set_text(f"{n}文字（目安 {lo * room:.0f}〜{hi * room:.0f}）")
                            count.classes(replace="text-sm ml-auto " + ("text-green-700" if ok else "text-orange-600"))
                        show_count()
                        box.on("update:model-value", show_count)
                        box.on("blur", lambda _, s=s, box=box: (setattr(s, "script", (box.value or "").strip()),
                                                                persist()))
            script_view()

            async def again() -> None:
                if await run_job("映像に合わせて台本を書き直しています", vf.write_script, folder, CONFIG.services):
                    ui.navigate.to(f"/p/{pid}/narration")

            def refresh_sheet() -> None:
                current = load_project(folder)
                vf.write_recording_script(current, folder, spans)
                open_path(folder / "output" / "録音用の台本.html")

            async def readalong() -> None:
                if await run_job("読み合わせ用の映像を作っています", vf.render_readalong, folder, CONFIG.services):
                    player.set_source(media_url(folder / "output" / "読み合わせ用の映像.mp4"))
                    player.set_visibility(True)

            with ui.row().classes("gap-3"):
                ui.button("録音用の台本を開く（印刷・スマホ用）", icon="print", on_click=refresh_sheet).props("outline")
                ui.button("読み合わせ用の映像を作り直す", icon="subtitles", on_click=readalong).props("flat")
                ui.button("台本を作り直す", icon="auto_awesome", on_click=again).props("flat")
            ra = folder / "output" / "読み合わせ用の映像.mp4"
            ui.label("読み合わせ用の映像: 台本が字幕で流れます。これを見ながら録ると、長さが合いやすくなります。").classes("hint")
            player = ui.video(media_url(ra) if ra.exists() else "").classes("w-full rounded")
            player.set_visibility(ra.exists())

        # --- 録った声 ---
        with section("② 録った声を入れる", "場面ごとに録ったファイルを入れてください。名前を変えていなくても、中身を聞き取って振り分けます。"
                                       "元のファイルは変更しません。"):
            async def import_from(source) -> None:
                if await run_job("声を取り込んで振り分けています", vf.import_voices, folder, source, CONFIG.services) \
                        is not None:
                    ui.navigate.to(f"/p/{pid}/narration")

            with ui.row().classes("gap-3 items-center"):
                async def choose() -> None:
                    path = await pick_folder()
                    if path is None and not CONFIG.native:
                        path_input.set_visibility(True)
                        return
                    if path:
                        await import_from(path)
                ui.button("フォルダを選ぶ", icon="folder_open", on_click=choose).props("outline")
                ui.label("または、下に声のファイルをドラッグしてください").classes("hint")
            path_input = ui.input("フォルダの場所", placeholder=r"例: D:\声").props("outlined").classes("w-full")
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
            ui.upload(label="声のファイル（m4a・mp3・wav など）をここにドラッグ", multiple=True, auto_upload=True,
                      on_multi_upload=dropped).props("accept=audio/* flat bordered color=grey-2 text-color=grey-9") \
                .classes("w-full")

            choices = {"": "使わない（どの場面でもない）"} | {s.id: f"{numbers[s.id]}. {s.title}" for s in scenes}
            have = {v.scene_id for v in project.voices if v.use and v.scene_id}
            if project.voices:
                missing = [s.title for s in scenes if s.id not in have]
                if missing:
                    with ui.row().classes("warn-box items-center gap-2 w-full"):
                        ui.icon("info", color="orange")
                        ui.label(f"まだ声がない場面: {'、'.join(missing)}（そのままでも仕上げられます。映像だけになります）")
            for v in project.voices:
                with ui.card().classes("w-full gap-1" + ("" if v.use else " opacity-60")).props("flat bordered"):
                    with ui.row().classes("w-full items-center gap-3"):
                        ui.icon("mic", color="primary")
                        ui.label(v.file).classes("font-mono")
                        ui.label(f"{v.duration:.1f}秒").classes("hint")
                        if v.scene_id in spans:
                            need = target_length(v.duration)
                            have_sec = spans[v.scene_id][1] - spans[v.scene_id][0]
                            diff = need - have_sec
                            ui.label("映像より " + (f"{diff:.1f}秒長い（映像を伸ばします）" if diff > 0.2 else
                                                   f"{-diff:.1f}秒短い（映像を詰めます）" if diff < -0.2 else "ほぼ同じ"))\
                                .classes("hint text-sm")
                        if ASSIGNED[v.assigned_by]:
                            ui.badge(ASSIGNED[v.assigned_by]).props("outline")
                        sel = ui.select(choices, value=v.scene_id or "", label="場面").props("outlined dense") \
                            .classes("w-56 ml-auto")
                        sel.on_value_change(lambda e, v=v: (vf.set_voice_scene(folder, v.file, e.value or None),
                                                            ui.navigate.to(f"/p/{pid}/narration")))
                        if v.scene_id and not v.use:
                            ui.button("こちらを使う", on_click=lambda v=v: (vf.set_voice_use(folder, v.file),
                                                                       ui.navigate.to(f"/p/{pid}/narration"))) \
                                .props("flat dense")
                        elif v.use:
                            ui.badge("使う").props("color=green")
                    ui.label(f"聞き取った内容: {v.transcript or '（聞き取れませんでした）'}").classes("text-sm")
                    if v.reason:
                        ui.label(f"理由: {v.reason}").classes("hint text-sm")
                    wav = folder / "work" / "voice" / f"{Path(v.file).stem}.wav"
                    if wav.exists():
                        ui.audio(media_url(wav)).classes("w-full")

        async def finish() -> None:
            done = await run_job("声に合わせて仕上げています", vf.finish_with_voices, folder, CONFIG.services)
            if done is not None:
                for n in done.notes:
                    ui.notify(n, type="info", timeout=8000, position="top-right")
                ui.navigate.to(f"/p/{pid}/review")

        with ui.row().classes("w-full justify-end"):
            ui.button("声に合わせて仕上げる", icon="graphic_eq", on_click=finish).classes("big-button")
