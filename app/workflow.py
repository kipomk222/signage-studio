"""画面の段階ごとの処理。どれもプロジェクトのフォルダを受け取り、結果を project.json に保存する。

企画:   start_project → research_product_into / product_from_page_into → make_plan_into → confirm_plan
素材:   import_material
編集:   build_edit（形式をそろえる → 無音カット → 文字起こし → 場面の振り分け → つなぐ → テロップ）
確認:   render_preview
書き出し: export_all

時間のかかる処理は Job の中で呼ぶと、進み具合が分かり、途中で中止できる。
"""

from __future__ import annotations

import html
import os
import shutil
from dataclasses import dataclass, field, replace
from pathlib import Path

from app.claude.calls import (
    make_plan, make_telops, merge_manual, product_from_paste, research_product, youtube_text,
)
from app.claude.runner import ClaudeCode
from app.errors import UserError
from app.jobs import Cancelled, Job, checkpoint
from app.pipeline import bgm as bgm_step
from app.pipeline.cards import (
    END_SECONDS, TITLE_SECONDS, corner_logo_image, find_corner_logo, find_logo, make_end_card, make_title_card,
)
from app.pipeline.concat import FADE, concat_clips
from app.pipeline.export import ExportResult, export_video
from app.pipeline.ffmpeg import duration_of, ffmpeg_base, run
from app.pipeline.fixterms import Fix, TermFixer, collect_terms, log_text
from app.pipeline.inventory import ClipInfo, list_videos, probe_clip, scan_folder
from app.pipeline.normalize import normalize_clip
from app.pipeline.scenes import assign_scenes, order_by_scene
from app.pipeline.silence import STRENGTH, TIGHTEST_MIN_SILENCE, CutResult
from app.pipeline.telop_render import build_telop_ass, burn_ass
from app.pipeline.transcribe import Transcriber, Word, hint_text, to_phrases, transcribe
from app.platform import PlatformProfile, get_profile
from app.project import (
    Clip, EditDrop, EditInsert, EditPiece, EditPlan, Project, Stage, create_project, load_project, save_project,
)

ROOT = Path(__file__).resolve().parents[1]
PROJECTS = ROOT / "projects"
BGM_DIR = ROOT / "assets" / "bgm"
MIN_USABLE = 2 * FADE + 0.2   # これより短く残ったクリップはつなぎ目に収まらないので使わない
PREVIEW_SECONDS = 15.0


@dataclass
class Services:
    """外の仕組み（Claude Code・文字起こし・FFmpeg）。テストでは差し替える。"""

    claude: ClaudeCode | None = None
    transcriber: Transcriber | None = None
    profile: PlatformProfile | None = None
    logo: Path | None = None

    def cc(self) -> ClaudeCode:
        if self.claude is None:
            self.claude = ClaudeCode()
        return self.claude

    def prof(self) -> PlatformProfile:
        if self.profile is None:
            self.profile = get_profile()
        return self.profile


def _scoped(fn):
    """この処理で使った Claude の量を、その動画（フォルダ）の分として記録する。"""
    import functools
    from app.claude.usage import usage_scope

    @functools.wraps(fn)
    def wrapper(folder, *args, **kwargs):
        with usage_scope(Path(folder).name):
            return fn(folder, *args, **kwargs)
    return wrapper


def _step(job: Job | None, fraction: float, message: str) -> None:
    if job is not None:
        job.step(fraction, message)
    else:
        checkpoint()


# --- 企画 -----------------------------------------------------------------------

def start_project(model_number: str, projects_root: Path = PROJECTS) -> tuple[Project, Path]:
    return create_project(projects_root, model_number)


def _set_product(folder: Path, product, info) -> Project:
    project = load_project(folder)
    if not info.found:
        raise UserError("この型番の商品を特定できませんでした。",
                        "商品ページの文章を貼り付けるか、スクリーンショットを入れてください。")
    project.product = product
    project.mark_changed(Stage.PLAN)
    save_project(folder, project)
    return project


@_scoped
def research_product_into(folder: Path, services: Services, refresh: bool = False,
                          job: Job | None = None) -> Project:
    project = load_project(folder)
    _step(job, 0.05, "商品を調べています（数分かかることがあります）")
    product, info = research_product(services.cc(), project.model_number, refresh=refresh)
    return _set_product(folder, product, info)


@_scoped
def product_from_page_into(folder: Path, services: Services, text: str = "",
                           images: list[Path] | None = None, job: Job | None = None) -> Project:
    project = load_project(folder)
    _step(job, 0.05, "商品ページの内容を整理しています")
    product, info = product_from_paste(services.cc(), project.model_number, text=text, images=images)
    return _set_product(folder, product, info)


@_scoped
def make_plan_into(folder: Path, services: Services, job: Job | None = None) -> Project:
    project = load_project(folder)
    if not project.product.name:
        raise UserError("商品情報がまだありません。", "先に商品を調べてください。")
    _step(job, 0.1, "構成と撮るカットを考えています" if project.separate_voice else "台本と構成を考えています")
    project.plan.scenes = make_plan(services.cc(), project.product, project.plan.target_seconds,
                                    project.plan.price, project.plan.extra_text, separate=project.separate_voice)
    project.mark_changed(Stage.PLAN)
    save_project(folder, project)
    return project


def confirm_plan(folder: Path) -> Path:
    """企画を確定し、撮影リストを書き出す。"""
    project = load_project(folder)
    if not project.plan.scenes:
        raise UserError("構成がまだありません。", "「構成を作る」を押してください。")
    project.plan.confirmed = True
    project.complete_stage(Stage.PLAN)
    save_project(folder, project)
    return write_shooting_list(project, folder)


def write_shooting_list(project: Project, folder: Path) -> Path:
    """撮影リスト（印刷用・スマホ表示用を兼ねた1枚のページ）。"""
    p, total = project.product, sum(s.seconds for s in project.plan.scenes)
    rows = []
    separate = project.separate_voice
    for i, s in enumerate(project.plan.scenes, 1):
        shots = "".join(f"<li><label><input type='checkbox'> {html.escape(x)}</label></li>" for x in s.shots)
        if separate and s.voice == "narration":
            how = '<p class="how quiet">映像だけ撮る（話さない・声はあとで録る）</p>'
            body = f'<p class="points">伝えること: {html.escape(s.points)}</p>' if s.points else ""
        else:
            how = '<p class="how talk">顔を出して、その場で話す</p>' if separate else ""
            body = f'<p class="script">{html.escape(s.script)}</p>'
        rows.append(f"""<section class="scene"><h2><span>{i}</span>{html.escape(s.title)}<small>{s.seconds:.0f}秒</small></h2>
{how}{body}<ul>{shots}</ul></section>""")
    page = f"""<!doctype html><html lang="ja"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>撮影リスト {html.escape(p.model_number)}</title>
<style>
body{{font-family:"BIZ UDPGothic","Hiragino Sans","Yu Gothic",sans-serif;margin:0;padding:16px;color:#1a1a1a;background:#fff;line-height:1.6}}
header{{border-bottom:3px solid #1a1a1a;margin-bottom:12px}} h1{{font-size:22px;margin:0}} .meta{{color:#555;font-size:14px}}
.scene{{border:1px solid #ccc;border-radius:10px;padding:10px 14px;margin:12px 0;break-inside:avoid}}
.scene h2{{font-size:18px;margin:0 0 6px;display:flex;gap:10px;align-items:center}}
.scene h2 span{{background:#1a1a1a;color:#fff;border-radius:50%;width:28px;height:28px;display:inline-flex;align-items:center;justify-content:center;font-size:15px}}
.scene h2 small{{margin-left:auto;font-weight:normal;color:#555}}
.script{{background:#f4f4f4;border-radius:6px;padding:8px 10px;margin:6px 0;font-size:16px}}
.points{{color:#333;margin:4px 0;font-size:15px}} .how{{display:inline-block;border-radius:999px;padding:2px 10px;margin:2px 0;font-size:13px;font-weight:bold}}
.quiet{{background:#e8f0fe;color:#1a4fb4}} .talk{{background:#fde8e8;color:#b41a1a}}
ul{{list-style:none;padding:0;margin:0}} li{{padding:4px 0;font-size:15px}} input{{transform:scale(1.4);margin-right:8px}}
@media print{{body{{padding:0}} .scene{{border-color:#999}}}}
</style></head><body><header><h1>撮影リスト　{html.escape(p.name or p.model_number)}</h1>
<div class="meta">型番 {html.escape(p.model_number)} ／ 場面 {len(project.plan.scenes)} ／ 合計 約{total:.0f}秒（タイトルとエンドカードは別）</div></header>
{''.join(rows)}</body></html>"""
    out = folder / "output" / "撮影リスト.html"
    out.write_text(page, encoding="utf-8")
    return out


# --- 素材 -----------------------------------------------------------------------

def thumb_path(folder: Path, clip_file: str) -> Path:
    return folder / "work" / "thumbs" / f"{Path(clip_file).stem}.jpg"


def import_material(folder: Path, source: Path | list[Path], services: Services,
                    job: Job | None = None) -> list[ClipInfo]:
    """素材をプロジェクトの「素材」フォルダにコピーし、調べて一覧にする（元のファイルは変えない）。"""
    profile = services.prof()
    dst = folder / "素材"
    files = list_videos(source) if isinstance(source, Path) and source.is_dir() else [Path(p) for p in source]  # type: ignore[union-attr]
    if not files:
        raise UserError("動画が見つかりませんでした。", "スマホで撮った動画ファイルが入ったフォルダを選んでください。")
    for i, f in enumerate(files, 1):
        _step(job, 0.4 * i / len(files), f"素材をコピーしています（{i}/{len(files)}）")
        target = dst / f.name
        if not target.exists():
            shutil.copy2(f, target)

    _step(job, 0.45, "素材を調べています")
    infos = scan_folder(profile, dst)
    project = load_project(folder)
    old = {c.file: c for c in project.clips}
    clips: list[Clip] = []
    for i, c in enumerate(infos, 1):
        _step(job, 0.45 + 0.55 * i / len(infos), f"見本の画像を作っています（{i}/{len(infos)}）")
        prev = old.get(c.path.name)
        clips.append(Clip(
            file=c.path.name, duration=round(c.duration, 2), vertical=c.vertical, silent=c.silent,
            excluded=prev.excluded if prev else c.excluded,
            exclude_reason="、".join(c.exclude_reasons),
            scene_id=prev.scene_id if prev else None, scene_manual=prev.scene_manual if prev else False,
        ))
        _make_thumb(profile, c, thumb_path(folder, c.path.name))
    project.clips = clips
    project.mark_changed(Stage.MATERIAL)
    save_project(folder, project)
    return infos


def _make_thumb(profile: PlatformProfile, clip: ClipInfo, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    at = min(1.0, max(0.0, clip.duration / 3))
    try:
        run([*ffmpeg_base(profile.ffmpeg), "-ss", f"{at:.2f}", "-i", str(clip.path), "-frames:v", "1",
             "-vf", "scale=320:-2", str(dst)], what="見本の画像を作れませんでした。")
    except UserError:
        pass  # 見本がなくても編集はできる


# --- 編集 -----------------------------------------------------------------------
#
# 1. 下ごしらえ（prepare_analysis）: 形式をそろえる・話している範囲・文字起こし・映像の見本（結果はとっておく）
# 2. 設計図（plan_edit）: Claude Code が「使う・切る・並べる・差し込む」を決める。だめなら機械的に決める
# 3. 仕上げ（render_edit）: 設計図どおりにつなぎ、差し込みを重ね、テロップを作る

@dataclass
class EditResult:
    cuts: list[CutResult] = field(default_factory=list)
    joined_seconds: float = 0.0
    fixes: list[Fix] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    source: str = "auto"
    summary: str = ""


@dataclass
class ClipData:
    file: str
    norm: Path
    duration: float
    keeps: list[tuple[float, float]]      # 話している範囲（無音カットで残す所）
    words: list[Word]
    sheet: Path | None
    no_speech: bool


def _analysis_dir(folder: Path) -> Path:
    return folder / "work" / "analysis"


def _cached_json(path: Path, make):
    import json
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    data = make()
    path.parent.mkdir(parents=True, exist_ok=True)
    part = path.with_name(path.name + ".part")
    part.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    os.replace(part, path)
    return data


def prepare_analysis(folder: Path, services: Services, with_words: bool, tight: bool = False,
                     job: Job | None = None, span: tuple[float, float] = (0.0, 0.5)) -> dict[str, ClipData]:
    """クリップごとの下ごしらえ。一度作った結果はとっておき、作り直しでは使い回す。"""
    from app.pipeline.analysis import contact_sheet
    from app.pipeline.silence import detect_silences, keep_ranges
    profile, project = services.prof(), load_project(folder)
    src, adir = folder / "素材", _analysis_dir(folder)
    use = [c for c in project.clips if not c.excluded]
    if not use:
        raise UserError("使える動画がありませんでした。", "除外にしたクリップを戻すか、素材を入れ直してください。")
    params = STRENGTH[project.settings.silence_strength]
    if tight:
        params = replace(params, min_silence=TIGHTEST_MIN_SILENCE)
    hint = hint_text(collect_terms(project.product)[:20] + [project.product.name]) if with_words else ""
    lo, hi = span
    out: dict[str, ClipData] = {}
    for i, c in enumerate(use, 1):
        frac = lambda k: lo + (hi - lo) * ((i - 1) + k) / len(use)  # noqa: E731
        stem = Path(c.file).stem
        norm = folder / "work" / "norm" / f"{stem}_{project.settings.fit}.mp4"
        if not norm.exists() or norm.stat().st_mtime < (src / c.file).stat().st_mtime:
            _step(job, frac(0.0), f"形式をそろえています（{i}/{len(use)}）")
            part = norm.with_name(norm.stem + ".part.mp4")  # できあがってから名前を変える（途中で止めても壊れない）
            normalize_clip(profile, probe_clip(profile, src / c.file), part, fit=project.settings.fit)
            os.replace(part, norm)
        duration = duration_of(profile.ffprobe, norm)
        tag = f"{params.noise_db}_{params.min_silence}_{params.pad}"
        keeps = [tuple(r) for r in _cached_json(
            adir / f"{stem}_{project.settings.fit}_keeps_{tag}.json",
            lambda: keep_ranges(detect_silences(profile, norm, params, duration), duration, params.pad))]
        words: list[Word] = []
        if with_words and keeps:
            _step(job, frac(0.4), f"話した内容を文字にしています（{i}/{len(use)}）"
                  + ("　初回は準備に数分かかります" if i == 1 else ""))
            words = [Word(**w) for w in _cached_json(
                adir / f"{stem}_{project.settings.fit}_words.json",
                lambda: [vars(w) for w in transcribe(profile, norm, hint, services.transcriber)])]
        sheet = None
        if with_words:
            sheet = adir / f"sheet_{stem}.jpg"
            if not sheet.exists():
                _step(job, frac(0.8), f"映像の見本を作っています（{i}/{len(use)}）")
                contact_sheet(profile, norm, duration, sheet, f"{c.file}（{duration:.1f}秒）")
        out[c.file] = ClipData(c.file, norm, duration, keeps, words, sheet, not keeps)
    return out


def auto_plan(project: Project, data: dict[str, ClipData]) -> EditPlan:
    """機械的な編集: クリップごとに話している範囲を使い、台本の場面順に並べる。"""
    use = [c for c in project.clips if not c.excluded and c.file in data]
    if project.plan.scenes:
        manual = [c.scene_id if c.scene_manual else None for c in use]
        for c, a in zip(use, assign_scenes([c.transcript for c in use], project.plan.scenes, manual)):
            c.scene_id, c.scene_guessed = a.scene_id, a.guessed
        order = order_by_scene(use, lambda c: c.scene_id, project.plan.scenes)
    else:
        order = use
    pieces = []
    for c in order:
        d = data[c.file]
        start, end = (d.keeps[0][0], d.keeps[-1][1]) if d.keeps else (0.0, d.duration)
        pieces.append(EditPiece(clip=c.file, start=start, end=end, scene_id=c.scene_id,
                                reason="話している範囲" if d.keeps else "話し声のない映像"))
    return EditPlan(source="auto", summary="クリップごとに話している範囲を使い、台本の場面順に並べました。",
                    pieces=pieces)


def ai_plan(project: Project, data: dict[str, ClipData], services: Services, fixer: TermFixer) -> EditPlan:
    from app.claude.calls import ClipBrief, decide_edit
    briefs = []
    for c in project.clips:
        if c.excluded or c.file not in data:
            continue
        d = data[c.file]
        phrases = [(ph.start, ph.end, fixer.fix(ph.text)[0]) for ph in to_phrases(d.words)]
        briefs.append(ClipBrief(c.file, round(d.duration, 2), phrases, d.sheet))
    budget = project.plan.target_seconds - TITLE_SECONDS - END_SECONDS
    e = decide_edit(services.cc(), project.product, project.plan.scenes, budget, briefs)
    return EditPlan(
        source="ai", summary=e.summary,
        pieces=[EditPiece(clip=p.clip, start=round(p.start, 2), end=round(p.end, 2),
                          scene_id=p.scene_id or None, reason=p.reason) for p in e.pieces],
        inserts=[EditInsert(clip=i.clip, start=round(i.start, 2), end=round(i.end, 2), piece=i.over_piece,
                            offset=round(i.offset, 2), reason=i.reason) for i in e.inserts],
        dropped=[EditDrop(clip=d.clip, start=round(d.start, 2), end=round(d.end, 2), reason=d.reason)
                 for d in e.dropped],
    )


@_scoped
def build_edit(folder: Path, services: Services, with_telops: bool = True, job: Job | None = None,
               ai: bool | None = None) -> EditResult:
    """素材から、設計図を作って編集する（画面の「編集を始める」）。声を別に録る動画は、映像だけを先に作る。"""
    project = load_project(folder)
    if project.separate_voice:
        from app.voiceflow import build_video
        return build_video(folder, services, job=job, ai=ai)
    with_telops = with_telops and bool(project.product.name)
    use_ai = (project.settings.ai_edit if ai is None else ai) and with_telops
    res = EditResult()
    data = prepare_analysis(folder, services, with_words=with_telops, job=job, span=(0.0, 0.5))

    fixer = TermFixer.for_product(project.product) if with_telops else None
    project = load_project(folder)
    for c in project.clips:
        if c.file in data and fixer:
            text, fx = fixer.fix("".join(w.text for w in data[c.file].words), c.file)
            c.transcript, res.fixes = text, res.fixes + fx

    plan: EditPlan | None = None
    if use_ai:
        _step(job, 0.5, "Claude Code が素材を見て、編集を考えています（数分かかります）")
        try:
            plan = ai_plan(project, data, services, fixer)  # type: ignore[arg-type]
        except Cancelled:
            raise
        except UserError:
            res.notes.append("Claude Code による編集がうまくいかなかったため、機械的に編集しました。")
    if plan is None:
        budget = project.plan.target_seconds - (TITLE_SECONDS + END_SECONDS if with_telops else 0)
        estimate = sum(sum(e - s for s, e in d.keeps) or d.duration for d in data.values()) - FADE * (len(data) - 1)
        if estimate > budget and STRENGTH[project.settings.silence_strength].min_silence > TIGHTEST_MIN_SILENCE:
            data = prepare_analysis(folder, services, with_words=with_telops, tight=True, span=(0.5, 0.55))
            res.notes.append(f"目標の長さ（{budget:.0f}秒）を超えたため、無音カットを強めました。")
        plan = auto_plan(project, data)
    if plan.source == "ai":  # クリップの場面の表示を、Claude Code が選んだ場面にそろえる
        for c in project.clips:
            if not c.scene_manual:
                first = next((p for p in plan.pieces if p.clip == c.file), None)
                c.scene_id, c.scene_guessed = (first.scene_id if first else None), False
    project.edit = plan
    save_project(folder, project)
    res.source, res.summary = plan.source, plan.summary
    return render_edit(folder, services, with_telops=with_telops, job=job, result=res, data=data)


@_scoped
def replan_edit(folder: Path, services: Services, job: Job | None = None) -> EditResult:
    """Claude Code に編集を考え直してもらう（画面の「AIで編集し直す」）。"""
    return build_edit(folder, services, with_telops=True, job=job, ai=True)


@_scoped
def render_edit(folder: Path, services: Services, with_telops: bool = True, job: Job | None = None,
                result: EditResult | None = None, data: dict[str, ClipData] | None = None) -> EditResult:
    """設計図どおりに動画を作り、テロップを付ける（画面で設計図を直したあとの「この内容で作り直す」も）。"""
    from app.pipeline.edl import apply_inserts, map_time, place_inserts, render_plan, save_layouts, visual_of
    profile, project = services.prof(), load_project(folder)
    if project.edit is None or not any(p.enabled for p in project.edit.pieces):
        raise UserError("使う部分がありません。", "「使う」にした部分を1つ以上にしてください。")
    with_telops = with_telops and bool(project.product.name)
    res = result or EditResult(source=project.edit.source, summary=project.edit.summary)
    data = data or prepare_analysis(folder, services, with_words=with_telops, job=job, span=(0.0, 0.3))
    work, out = folder / "work", folder / "output"

    missing = [p.clip for p in project.edit.pieces + project.edit.inserts if p.clip not in data]  # type: ignore[operator]
    if missing:
        raise UserError(f"「{missing[0]}」が素材の一覧にありません。", "そのクリップを「使う」に戻すか、設計図から外してください。")
    norm = {f: d.norm for f, d in data.items()}
    keeps = {f: d.keeps for f, d in data.items()}
    try:
        base, layouts, total = render_plan(
            profile, project.edit, norm, keeps, work,
            progress=lambda f, m: _step(job, 0.65 + 0.15 * f, m))
    except ValueError:
        raise UserError("つなげられる部分が残りませんでした。",
                        "使う部分を増やすか、無音カットの強さを「弱」にして、もう一度お試しください。") from None
    _step(job, 0.8, "差し込みの映像を重ねています")
    placed = place_inserts(project.edit, layouts, total)
    apply_inserts(profile, base, placed, norm, work / "joined.mp4")
    res.joined_seconds = total
    visual = visual_of(layouts, project.edit.pieces, placed,
                       {(x.clip, round(x.start, 2)): x.reason for x in project.edit.inserts})
    save_layouts(work / "edit_layout.json", layouts, total, visual)

    # レポート用: クリップごとに、どれだけ使ったか
    res.cuts = []
    for c in project.clips:
        if c.file in data:
            ranges = [r for lay in layouts if lay.clip == c.file for r in lay.ranges]
            kept = sum(e - s for s, e in ranges)
            res.cuts.append(CutResult(src=folder / "素材" / c.file, dst=data[c.file].norm,
                                      original=data[c.file].duration, kept=kept, ranges=ranges,
                                      no_speech=data[c.file].no_speech))
            c.cut_ranges, c.kept_seconds = ranges, round(kept, 2)

    if with_telops:
        fixer = TermFixer.for_product(project.product)
        timeline = []
        for f, d in data.items():
            for w in d.words:
                at = map_time(layouts, f, (w.start + w.end) / 2)
                if at is not None:
                    half = (w.end - w.start) / 2
                    timeline.append(Word(round(at - half, 3), round(at + half, 3), w.text))
        timeline.sort(key=lambda w: w.start)
        transcript = []
        for ph in to_phrases(timeline):
            text, _ = fixer.fix(ph.text, f"{ph.start:.1f}秒")
            transcript.append({"start": ph.start, "end": ph.end, "text": text})
        (out / "字幕修正ログ.txt").write_text(log_text(res.fixes), encoding="utf-8")
        _step(job, 0.88, "テロップを作っています")
        title, new = make_telops(services.cc(), project.product, project.plan.scenes, transcript,
                                 total, project.plan.price, project.plan.extra_text, visual=visual)
        project.title, project.telops = title, merge_manual(new, project.telops)

    project.complete_stage(Stage.MATERIAL)
    save_project(folder, project)
    if with_telops and project.telops:
        from app.telopcheck import check_after_telops
        _step(job, 0.95, "テロップと映像に食い違いがないか、Claude Code が確かめています")
        res.notes += check_after_telops(folder, services)
    make_edit_thumbs(folder, services)
    _step(job, 1.0, "編集ができました")
    return res


def edit_thumb(folder: Path, clip: str, t: float) -> Path:
    return _analysis_dir(folder) / "thumbs" / f"{Path(clip).stem}_{t:.2f}.jpg"


def make_edit_thumbs(folder: Path, services: Services) -> None:
    """確認画面に出す、部分・差し込み・切った部分の見本画像。"""
    project, profile = load_project(folder), services.prof()
    if project.edit is None:
        return
    items = [(x.clip, x.start) for x in project.edit.pieces + project.edit.inserts + project.edit.dropped]  # type: ignore[operator]
    for clip, t in items:
        dst = edit_thumb(folder, clip, t)
        norm = folder / "work" / "norm" / f"{Path(clip).stem}_{project.settings.fit}.mp4"
        if dst.exists() or not norm.exists():
            continue
        dst.parent.mkdir(parents=True, exist_ok=True)
        try:
            run([*ffmpeg_base(profile.ffmpeg), "-ss", f"{t + 0.3:.2f}", "-i", str(norm), "-frames:v", "1",
                 "-vf", "scale=240:-2", str(dst)], what="見本の画像を作れませんでした。")
        except UserError:
            pass


def spoken_text(folder: Path, project: Project, clip: str, start: float, end: float) -> str:
    """そのクリップの start〜end で話している内容（文字起こしから）。"""
    import json
    path = _analysis_dir(folder) / f"{Path(clip).stem}_{project.settings.fit}_words.json"
    if not path.exists():
        return ""
    words = json.loads(path.read_text(encoding="utf-8"))
    text = "".join(w["text"] for w in words if start - 0.05 <= (w["start"] + w["end"]) / 2 <= end + 0.05).strip()
    return TermFixer.for_product(project.product).fix(text)[0] if text and project.product.name else text


def restore_drop(project: Project, index: int) -> None:
    """切った部分を「戻す」: 同じクリップの部分の中で、元の時間の順になる位置に入れる（なければ最後）。"""
    d = project.edit.dropped.pop(index)  # type: ignore[union-attr]
    pieces = project.edit.pieces  # type: ignore[union-attr]
    same = [i for i, p in enumerate(pieces) if p.clip == d.clip]
    later = [i for i in same if pieces[i].start > d.start]   # 元の時間の順に戻す
    at = later[0] if later else (same[-1] + 1 if same else len(pieces))
    scene = pieces[(later or same)[0]].scene_id if same else None
    pieces.insert(at, EditPiece(clip=d.clip, start=d.start, end=d.end, scene_id=scene, reason=f"戻した（{d.reason}）"))
    for ins in project.edit.inserts:  # type: ignore[union-attr]
        if ins.piece >= at:
            ins.piece += 1


def move_piece(project: Project, index: int, delta: int) -> None:
    """部分の順番を入れ替える（差し込みの重ね先も一緒に動かす）。"""
    pieces = project.edit.pieces  # type: ignore[union-attr]
    j = index + delta
    if not 0 <= j < len(pieces):
        return
    pieces[index], pieces[j] = pieces[j], pieces[index]
    for ins in project.edit.inserts:  # type: ignore[union-attr]
        if ins.piece == index:
            ins.piece = j
        elif ins.piece == j:
            ins.piece = index


# --- 確認・書き出し --------------------------------------------------------------

def find_bgm(folder: Path, project: Project) -> Path | None:
    """使う BGM。コマンドで指定した曲 → 選んだ曲 → assets/bgm の最初の曲 の順。"""
    override = sorted((folder / "work").glob("bgm.*"))
    if override:
        return override[0]
    files = bgm_step.find_bgm_files(BGM_DIR)
    chosen = [f for f in files if f.name == project.settings.bgm_file]
    return (chosen or files or [None])[0]


def bgm_choices() -> list[str]:
    return [f.name for f in bgm_step.find_bgm_files(BGM_DIR)]


def render_telop_video(profile: PlatformProfile, project: Project, folder: Path, logo: Path | None = None,
                       notes: list[str] | None = None, until: float | None = None) -> Path:
    """つないだ動画にテロップを入れ、冒頭タイトルとエンドカードを付ける（until 秒までならプレビュー用）。"""
    work = folder / "work"
    joined = work / "joined.mp4"
    p, style = project.product, project.settings.telop_style
    tag = "preview_" if until else ""
    ass = build_telop_ass(project.telops, work / f"{tag}telops.ass", p.category, p.model_number, style)
    src = joined
    if until:
        src = work / "preview_src.mp4"
        run([*ffmpeg_base(profile.ffmpeg), "-i", str(joined), "-t", f"{until:.2f}", "-c", "copy", str(src)],
            what="プレビューを作れませんでした。")
    corner = None
    cl = project.settings.corner_logo
    if cl.show:
        found = find_corner_logo()
        if found is not None:
            corner = corner_logo_image(found, work / "corner_logo.png", cl.size, cl.plate)
        elif notes is not None and not until:
            notes.append("左上のロゴは、assets/corner_logo（または assets/logo）に画像を入れると出ます。")
    with_telops = burn_ass(profile, src, ass, work / f"{tag}telops.mp4", corner)
    title_card = make_title_card(profile, joined, project.title, p.model_number, work / f"{tag}title.mp4",
                                 p.category, p.model_number, style, corner)
    parts = [title_card, with_telops]
    if not until:
        logo = logo or find_logo()
        if logo is None and notes is not None:
            notes.append("ロゴが見つからなかったため、エンドカードは仮の無地にしました。assets/logo にロゴを入れると使われます。")
        parts.append(make_end_card(profile, work / "end.mp4", logo))
    main_video = work / f"{tag}with_cards.mp4"
    concat_clips(profile, parts, main_video)
    return main_video


def _check_edit(folder: Path) -> Project:
    project = load_project(folder)
    if not (folder / "work" / "joined.mp4").exists():
        raise UserError("まだ編集ができていません。", "素材の画面で「編集を始める」を押してください。")
    return project


def render_preview(folder: Path, services: Services, job: Job | None = None) -> Path:
    """冒頭15秒のプレビュー（タイトル・テロップ・BGM 入り）。"""
    profile, project = services.prof(), _check_edit(folder)
    _step(job, 0.2, "プレビューを作っています")
    video = render_telop_video(profile, project, folder, until=PREVIEW_SECONDS - TITLE_SECONDS + FADE) \
        if project.telops else folder / "work" / "joined.mp4"
    _step(job, 0.7, "BGM を重ねています")
    out = bgm_step.mix_bgm(profile, video, find_bgm(folder, project), folder / "work" / "preview.mp4",
                           level=project.settings.bgm_level)
    _step(job, 1.0, "プレビューができました")
    return out


@_scoped
def export_all(folder: Path, services: Services, outputs: list[str] | None = None,
               job: Job | None = None, notes: list[str] | None = None) -> list[ExportResult]:
    profile, project = services.prof(), _check_edit(folder)
    outputs = outputs or list(project.settings.outputs)
    work, out = folder / "work", folder / "output"
    notes = notes if notes is not None else []
    if project.telops:
        _step(job, 0.1, "テロップと冒頭タイトルを入れています")
        main_video = render_telop_video(profile, project, folder, services.logo, notes)
    else:
        main_video = work / "joined.mp4"
    _step(job, 0.45, "BGM を重ねています")
    bgm = find_bgm(folder, project)
    if bgm is None:
        notes.append("BGM が見つからなかったため、BGM なしで書き出しました。assets/bgm に曲を入れると使われます。")
    mixed = bgm_step.mix_bgm(profile, main_video, bgm, work / "mixed.mp4", level=project.settings.bgm_level)
    exports = []
    for i, key in enumerate(outputs):
        _step(job, 0.55 + 0.35 * i / len(outputs), f"書き出しています（{'店頭用' if key == 'store' else 'YouTube用'}）")
        exports.append(export_video(profile, mixed, out, folder.name, key))
    if "youtube" in outputs and project.telops and project.product.name:
        _step(job, 0.92, "YouTube の説明文を作っています")
        yt = youtube_text(services.cc(), project.product, project.telops, project.plan.price)
        project.youtube_title, project.youtube_description = yt.title, yt.description
        (out / "YouTube説明文.txt").write_text(f"{yt.title}\n\n{yt.description}\n", encoding="utf-8")
    project.settings.outputs = outputs  # type: ignore[assignment]
    project.complete_stage(Stage.REVIEW)
    project.complete_stage(Stage.EXPORT)
    save_project(folder, project)
    _step(job, 1.0, "書き出しが終わりました")
    return exports
