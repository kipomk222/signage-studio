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
import shutil
from dataclasses import dataclass, field, replace
from pathlib import Path

from app.claude.calls import (
    make_plan, make_telops, merge_manual, product_from_paste, research_product, youtube_text,
)
from app.claude.runner import ClaudeCode
from app.errors import UserError
from app.jobs import Job, checkpoint
from app.pipeline import bgm as bgm_step
from app.pipeline.cards import END_SECONDS, TITLE_SECONDS, find_logo, make_end_card, make_title_card
from app.pipeline.concat import FADE, concat_clips
from app.pipeline.export import ExportResult, export_video
from app.pipeline.ffmpeg import ffmpeg_base, run
from app.pipeline.fixterms import Fix, TermFixer, collect_terms, log_text
from app.pipeline.inventory import ClipInfo, list_videos, probe_clip, scan_folder
from app.pipeline.normalize import normalize_clip
from app.pipeline.scenes import assign_scenes, order_by_scene
from app.pipeline.silence import STRENGTH, TIGHTEST_MIN_SILENCE, CutResult, jumpcut
from app.pipeline.telop_render import build_telop_ass, burn_ass
from app.pipeline.transcribe import Transcriber, Word, hint_text, to_phrases, transcribe
from app.platform import PlatformProfile, get_profile
from app.project import Clip, Project, Stage, create_project, load_project, save_project

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


def research_product_into(folder: Path, services: Services, refresh: bool = False,
                          job: Job | None = None) -> Project:
    project = load_project(folder)
    _step(job, 0.05, "商品を調べています（数分かかることがあります）")
    product, info = research_product(services.cc(), project.model_number, refresh=refresh)
    return _set_product(folder, product, info)


def product_from_page_into(folder: Path, services: Services, text: str = "",
                           images: list[Path] | None = None, job: Job | None = None) -> Project:
    project = load_project(folder)
    _step(job, 0.05, "商品ページの内容を整理しています")
    product, info = product_from_paste(services.cc(), project.model_number, text=text, images=images)
    return _set_product(folder, product, info)


def make_plan_into(folder: Path, services: Services, job: Job | None = None) -> Project:
    project = load_project(folder)
    if not project.product.name:
        raise UserError("商品情報がまだありません。", "先に商品を調べてください。")
    _step(job, 0.1, "台本と構成を考えています")
    project.plan.scenes = make_plan(services.cc(), project.product, project.plan.target_seconds,
                                    project.plan.price, project.plan.extra_text)
    project.mark_changed(Stage.PLAN)
    save_project(folder, project)
    return project


def confirm_plan(folder: Path) -> Path:
    """企画を確定し、撮影リストを書き出す。"""
    project = load_project(folder)
    if not project.plan.scenes:
        raise UserError("台本がまだありません。", "「台本を作る」を押してください。")
    project.plan.confirmed = True
    project.complete_stage(Stage.PLAN)
    save_project(folder, project)
    return write_shooting_list(project, folder)


def write_shooting_list(project: Project, folder: Path) -> Path:
    """撮影リスト（印刷用・スマホ表示用を兼ねた1枚のページ）。"""
    p, total = project.product, sum(s.seconds for s in project.plan.scenes)
    rows = []
    for i, s in enumerate(project.plan.scenes, 1):
        shots = "".join(f"<li><label><input type='checkbox'> {html.escape(x)}</label></li>" for x in s.shots)
        rows.append(f"""<section class="scene"><h2><span>{i}</span>{html.escape(s.title)}<small>{s.seconds:.0f}秒</small></h2>
<p class="script">{html.escape(s.script)}</p><ul>{shots}</ul></section>""")
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
            file=c.path.name, duration=round(c.duration, 2), vertical=c.vertical,
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

@dataclass
class EditResult:
    cuts: list[CutResult] = field(default_factory=list)
    joined_seconds: float = 0.0
    fixes: list[Fix] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def build_edit(folder: Path, services: Services, with_telops: bool = True, job: Job | None = None) -> EditResult:
    profile = services.prof()
    project = load_project(folder)
    work, out = folder / "work", folder / "output"
    src = folder / "素材"
    use = [c for c in project.clips if not c.excluded]
    if not use:
        raise UserError("使える動画がありませんでした。", "除外にしたクリップを戻すか、素材を入れ直してください。")
    with_telops = with_telops and bool(project.product.name)
    res = EditResult()
    n = len(use)

    # 形式をそろえる
    for d in ("norm", "cut"):
        shutil.rmtree(work / d, ignore_errors=True)
    normalized = []
    for i, c in enumerate(use, 1):
        _step(job, 0.25 * i / n, f"形式をそろえています（{i}/{n}）")
        info = probe_clip(profile, src / c.file)
        normalized.append(normalize_clip(profile, info, work / "norm" / f"{i:03d}.mp4",
                                         fit=project.settings.fit))

    # 無音カット（目標の長さを超えたら少し強める）
    params = STRENGTH[project.settings.silence_strength]
    target = project.plan.target_seconds - (TITLE_SECONDS + END_SECONDS if with_telops else 0)

    def cut_all(p) -> list[CutResult]:
        out_cuts = []
        for i, nv in enumerate(normalized, 1):
            _step(job, 0.25 + 0.2 * i / n, f"話していない部分を詰めています（{i}/{n}）")
            out_cuts.append(jumpcut(profile, nv, work / "cut" / nv.name, p))
        return out_cuts

    cuts = cut_all(params)
    if sum(r.kept for r in cuts) - FADE * (n - 1) > target and params.min_silence > TIGHTEST_MIN_SILENCE:
        cuts = cut_all(replace(params, min_silence=TIGHTEST_MIN_SILENCE))
        res.notes.append(f"目標の長さ（{target:.0f}秒）を超えたため、無音カットを強めました。")
    for c, r in zip(use, cuts):
        r.src = src / c.file
        c.cut_ranges, c.kept_seconds = r.ranges, round(r.kept, 2)
        if r.over_cut:
            res.notes.append(f"「{c.file}」は半分以下まで短くなりました。切りすぎていないか確認してください。")

    # 文字起こし（クリップごと）と型番の補正
    words_by_clip: list[list[Word]] = [[] for _ in use]
    fixer = TermFixer.for_product(project.product) if with_telops else None
    if with_telops:
        hint = hint_text(collect_terms(project.product)[:20] + [project.product.name])
        for i, (c, r) in enumerate(zip(use, cuts), 1):
            _step(job, 0.45 + 0.25 * i / n, f"話した内容を文字にしています（{i}/{n}）"
                  + ("　初回は準備に数分かかります" if i == 1 else ""))
            words_by_clip[i - 1] = [] if r.no_speech else transcribe(profile, r.dst, hint, services.transcriber)
            text, fx = fixer.fix("".join(w.text for w in words_by_clip[i - 1]), c.file)  # type: ignore[union-attr]
            c.transcript = text
            res.fixes += fx

    # 場面の振り分けと並べ替え
    if project.plan.scenes:
        manual = [c.scene_id if c.scene_manual else None for c in use]
        for c, a in zip(use, assign_scenes([c.transcript for c in use], project.plan.scenes, manual)):
            c.scene_id, c.scene_guessed = a.scene_id, a.guessed
    order = order_by_scene(list(range(n)), lambda i: use[i].scene_id, project.plan.scenes) \
        if project.plan.scenes else list(range(n))
    pieces, piece_words = [], []
    for i in order:
        if cuts[i].kept < MIN_USABLE:
            res.notes.append(f"「{use[i].file}」は残った部分が短すぎるため使いませんでした。")
            continue
        pieces.append(cuts[i].dst)
        piece_words.append((cuts[i].kept, words_by_clip[i]))
    if not pieces:
        raise UserError("つなげられる部分が残りませんでした。", "無音カットの強さを「弱」にして、もう一度お試しください。")

    _step(job, 0.75, "クリップをつないでいます")
    res.joined_seconds = concat_clips(profile, pieces, work / "joined.mp4")
    res.cuts = cuts

    # テロップ
    if with_telops:
        transcript, offset = [], 0.0
        for kept, words in piece_words:
            for ph in to_phrases([Word(w.start + offset, w.end + offset, w.text) for w in words]):
                text, _ = fixer.fix(ph.text, f"{ph.start:.1f}秒")  # type: ignore[union-attr]
                transcript.append({"start": ph.start, "end": ph.end, "text": text})
            offset += kept - FADE
        (out / "字幕修正ログ.txt").write_text(log_text(res.fixes), encoding="utf-8")
        _step(job, 0.85, "テロップを作っています")
        title, new = make_telops(services.cc(), project.product, project.plan.scenes, transcript,
                                 res.joined_seconds, project.plan.price, project.plan.extra_text)
        project.title, project.telops = title, merge_manual(new, project.telops)

    project.complete_stage(Stage.MATERIAL)
    save_project(folder, project)
    _step(job, 1.0, "編集ができました")
    return res


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
    with_telops = burn_ass(profile, src, ass, work / f"{tag}telops.mp4")
    title_card = make_title_card(profile, joined, project.title, p.model_number, work / f"{tag}title.mp4",
                                 p.category, p.model_number, style)
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
