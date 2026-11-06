"""声を別に録る作り方（標準）の処理。

映像:         build_video（素材を見て、場面ごとにカットを並べる）→ render_video
ナレーション:  write_script（完成した映像に合わせて台本）→ 録音用の台本・読み合わせ用の映像
              import_voices（録った声を取り込み、場面に振り分ける）→ set_voice_scene / set_voice_use（手で直す）
              finish_with_voices（映像の長さを声に合わせ、声を重ね、テロップを作る）
仕上げ・書き出しは workflow のもの（render_preview・export_all）をそのまま使う。
"""

from __future__ import annotations

import html
import json
import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path

from app.claude.calls import (
    ClipBrief,
    SceneCuts,
    assign_voices,
    decide_video,
    make_telops,
    merge_manual,
    write_narration,
)
from app.errors import UserError
from app.jobs import Cancelled, Job
from app.pipeline.concat import FADE
from app.pipeline.edl import PieceLayout, map_time, render_plan
from app.pipeline.ffmpeg import duration_of, ffmpeg_base, run
from app.pipeline.fit import LEAD, Cut, fit_scene, target_length
from app.pipeline.fixterms import TermFixer, collect_terms, log_text
from app.pipeline.narration import (
    AUDIO_EXTS,
    edge_trim,
    prepare_voice,
    recording_name,
    scene_from_name,
    script_match,
)
from app.pipeline.normalize import AUDIO_ARGS
from app.pipeline.scenes import assign_scenes
from app.pipeline.telop_render import build_telop_ass, burn_ass
from app.pipeline.transcribe import Word, hint_text, to_phrases, transcribe
from app.project import (
    VOICE_DIR,
    EditDrop,
    EditPiece,
    EditPlan,
    Project,
    Scene,
    Stage,
    Telop,
    VoiceTake,
    load_project,
    save_project,
)
from app.workflow import (
    ClipData,
    EditResult,
    Services,
    _scoped,
    _step,
    make_edit_thumbs,
    prepare_analysis,
)

AUTO_CUT = 4.0          # 機械的に並べるときの1カットの長さ
READ_CHUNK = 34         # 読み合わせ用の字幕の1枚の文字数の目安


# --- 映像 ------------------------------------------------------------------------

def _require_separate(project: Project) -> None:
    if not project.separate_voice:
        raise UserError("この動画は「撮影時に話す」作り方になっています。", "企画の詳細設定で「声は別に録る」を選んでください。")


def narration_scenes(project: Project) -> list[Scene]:
    return [s for s in project.plan.scenes if s.voice == "narration"]


@_scoped
def build_video(folder: Path, services: Services, job: Job | None = None, ai: bool | None = None) -> EditResult:
    """素材を見て、場面ごとにカットを並べた映像を作る（画面の「映像を編集する」）。"""
    project = load_project(folder)
    _require_separate(project)
    if not project.plan.scenes:
        raise UserError("構成がまだありません。", "企画の画面で構成を作ってください。")
    use_ai = project.settings.ai_edit if ai is None else ai
    res = EditResult()
    data = prepare_analysis(folder, services, with_words=True, job=job, span=(0.0, 0.5))
    fixer = TermFixer.for_product(project.product)
    project = load_project(folder)
    for c in project.clips:
        if c.file in data:
            c.transcript, _ = fixer.fix("".join(w.text for w in data[c.file].words), c.file)

    plan: EditPlan | None = None
    if use_ai:
        _step(job, 0.5, "Claude Code が素材を見て、映像を組み立てています（数分かかります）")
        try:
            plan = ai_video_plan(project, data, services, fixer)
        except Cancelled:
            raise
        except UserError:
            res.notes.append("Claude Code による編集がうまくいかなかったため、機械的に並べました。")
    if plan is None:
        plan = auto_video_plan(project, data)
    for c in project.clips:
        if not c.scene_manual:
            first = next((p for p in plan.pieces if p.clip == c.file), None)
            c.scene_id, c.scene_guessed = (first.scene_id if first else None), False
    project.edit = plan
    project.fitted, project.fit_notes = [], []
    project.mark_changed(Stage.VIDEO)
    save_project(folder, project)
    res.source, res.summary = plan.source, plan.summary
    return render_video(folder, services, job=job, result=res, data=data)


def ai_video_plan(project: Project, data: dict[str, ClipData], services: Services, fixer: TermFixer) -> EditPlan:
    briefs = []
    for c in project.clips:
        if c.excluded or c.file not in data:
            continue
        d = data[c.file]
        phrases = [(ph.start, ph.end, fixer.fix(ph.text)[0]) for ph in to_phrases(d.words)]
        briefs.append(ClipBrief(c.file, round(d.duration, 2), phrases, d.sheet))
    e = decide_video(services.cc(), project.product, project.plan.scenes, briefs)

    def piece(x) -> EditPiece:
        return EditPiece(clip=x.clip, start=round(x.start, 2), end=round(x.end, 2), scene_id=x.scene_id,
                         reason=x.reason, mute=not x.talk, shows=x.shows)
    return EditPlan(source="ai", summary=e.summary, pieces=[piece(x) for x in e.cuts],
                    spares=[piece(x) for x in e.spares],
                    dropped=[EditDrop(clip=d.clip, start=round(d.start, 2), end=round(d.end, 2), reason=d.reason)
                             for d in e.dropped])


def auto_video_plan(project: Project, data: dict[str, ClipData]) -> EditPlan:
    """機械的に並べる: 話しているクリップを顔出しの場面に、残りの映像を説明の場面に、予定の秒数ずつ。"""
    scenes = project.plan.scenes
    talk = [s for s in scenes if s.voice == "talk"]
    use = [c for c in project.clips if not c.excluded and c.file in data]
    speaking = [c for c in use if data[c.file].keeps and not data[c.file].no_speech]
    talk_pick: dict[str, tuple[float, str]] = {}
    if talk and speaking:
        for c, a in zip(speaking, assign_scenes([c.transcript for c in speaking], talk)):
            if a.scene_id and not a.guessed and a.score > talk_pick.get(a.scene_id, (0.0, ""))[0]:
                talk_pick[a.scene_id] = (a.score, c.file)
    talk_files = {f for _, f in talk_pick.values()}
    broll = [c for c in use if c.file not in talk_files]
    pieces: list[EditPiece] = []
    queue = list(broll)
    for s in scenes:
        if s.voice == "talk":
            if s.id in talk_pick:
                f = talk_pick[s.id][1]
                keeps = data[f].keeps
                pieces.append(EditPiece(clip=f, start=keeps[0][0], end=keeps[-1][1], scene_id=s.id,
                                        reason="台本に近いことを話している"))
            continue
        total = 0.0
        while queue and total < max(s.seconds, AUTO_CUT):
            c = queue.pop(0)
            d = data[c.file].duration
            length = min(AUTO_CUT, d)
            mid = d / 2
            pieces.append(EditPiece(clip=c.file, start=round(max(0, mid - length / 2), 2),
                                    end=round(min(d, mid + length / 2), 2), scene_id=s.id, mute=True,
                                    reason="素材の順に並べた"))
            total += length - FADE
    spares = [EditPiece(clip=c.file, start=0, end=round(min(AUTO_CUT, data[c.file].duration), 2),
                        scene_id=None, mute=True, reason="使わなかった映像") for c in queue]
    return EditPlan(source="auto", summary="話しているクリップを顔出しの場面に、残りの映像を素材の順に説明の場面へ並べました。",
                    pieces=pieces, spares=spares)


def _layout_path(folder: Path, name: str) -> Path:
    return folder / "work" / f"{name}_layout.json"


def _save_layouts(path: Path, layouts: list[PieceLayout], total: float) -> None:
    data = {"total": total, "layouts": [{"index": x.index, "clip": x.clip, "ranges": x.ranges, "out_start": x.out_start,
                                         "speed": x.speed, "hold": x.hold, "scene_id": x.scene_id} for x in layouts]}
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def _load_layouts(path: Path) -> tuple[list[PieceLayout], float]:
    if not path.exists():
        raise UserError("まだ映像ができていません。", "映像の画面で「映像を編集する」を押してください。")
    d = json.loads(path.read_text(encoding="utf-8"))
    return [PieceLayout(x["index"], x["clip"], [tuple(r) for r in x["ranges"]], x["out_start"], x["speed"], x["hold"],
                        x["scene_id"]) for x in d["layouts"]], d["total"]


@_scoped
def render_video(folder: Path, services: Services, job: Job | None = None, result: EditResult | None = None,
                 data: dict[str, ClipData] | None = None) -> EditResult:
    """設計図どおりに映像をつなぐ（声はまだ。顔出しの場面の声だけ入る）。"""
    profile, project = services.prof(), load_project(folder)
    if project.edit is None or not any(p.enabled for p in project.edit.pieces):
        raise UserError("使うカットがありません。", "「使う」にしたカットを1つ以上にしてください。")
    res = result or EditResult(source=project.edit.source, summary=project.edit.summary)
    data = data or prepare_analysis(folder, services, with_words=True, job=job, span=(0.0, 0.3))
    if missing := [p.clip for p in project.edit.pieces if p.enabled and p.clip not in data]:
        raise UserError(f"「{missing[0]}」が素材の一覧にありません。", "そのカットを「使わない」にしてください。")
    work = folder / "work"
    try:
        base, layouts, total = render_plan(
            profile, project.edit, {f: d.norm for f, d in data.items()}, {f: d.keeps for f, d in data.items()},
            work, progress=lambda f, m: _step(job, 0.6 + 0.35 * f, m))
    except ValueError:
        raise UserError("つなげられるカットが残りませんでした。", "使うカットを増やして、もう一度お試しください。") from None
    os.replace(base, work / "video.mp4")
    _save_layouts(_layout_path(folder, "video"), layouts, total)
    res.joined_seconds = total
    project.complete_stage(Stage.MATERIAL)
    project.mark_changed(Stage.VIDEO)
    save_project(folder, project)
    make_edit_thumbs(folder, services)
    _step(job, 1.0, "映像ができました")
    return res


def scene_spans(layouts: list[PieceLayout]) -> dict[str, tuple[float, float]]:
    """場面ごとの、つないだ動画での (始まり, 終わり)。"""
    spans: dict[str, tuple[float, float]] = {}
    for lay in layouts:
        if not lay.scene_id:
            continue
        a, b = lay.out_start, lay.out_start + lay.length
        if lay.scene_id in spans:
            a, b = min(a, spans[lay.scene_id][0]), max(b, spans[lay.scene_id][1])
        spans[lay.scene_id] = (round(a, 3), round(b, 3))
    return spans


# --- ナレーションの台本 ---------------------------------------------------------------

@_scoped
def write_script(folder: Path, services: Services, job: Job | None = None) -> Project:
    """完成した映像の場面ごとの長さとカットに合わせて、ナレーションの台本を作る。"""
    project = load_project(folder)
    _require_separate(project)
    layouts, _ = _load_layouts(_layout_path(folder, "video"))
    spans = scene_spans(layouts)
    items = []
    for s in narration_scenes(project):
        if s.id not in spans:
            continue
        a, b = spans[s.id]
        cuts = [(round(lay.out_start - a, 1), round(lay.out_start + lay.length - a, 1),
                 project.edit.pieces[lay.index].shows)  # type: ignore[union-attr]
                for lay in layouts if lay.scene_id == s.id]
        items.append(SceneCuts(s, round(b - a, 2), cuts))
    if not items:
        raise UserError("台本を作る場面がありません。", "映像の画面で、説明の場面にカットを入れてください。")
    _step(job, 0.2, "Claude Code が映像に合わせて台本を書いています")
    scripts = write_narration(services.cc(), project.product, items, project.plan.price, project.plan.extra_text)
    for s in project.plan.scenes:
        if s.id in scripts:
            s.script = scripts[s.id]
    project.complete_stage(Stage.VIDEO)
    project.mark_changed(Stage.NARRATION)
    save_project(folder, project)
    write_recording_script(project, folder, spans)
    _step(job, 0.6, "読み合わせ用の映像を作っています")
    render_readalong(folder, services)
    _step(job, 1.0, "台本ができました")
    return project


def write_recording_script(project: Project, folder: Path, spans: dict[str, tuple[float, float]]) -> Path:
    """録音用の台本（印刷用・スマホ表示用を兼ねた1枚のページ）。"""
    rows = []
    for i, s in enumerate(project.plan.scenes, 1):
        if s.voice != "narration" or not s.script:
            continue
        sec = spans[s.id][1] - spans[s.id][0] if s.id in spans else s.seconds
        rows.append(f"""<section class="scene"><h2><span>{i}</span>{html.escape(s.title)}<small>約{max(1.0, sec - 0.8):.0f}秒で読む</small></h2>
<p class="file">ファイル名のおすすめ: <b>{html.escape(recording_name(i, s))}</b></p><p class="script">{html.escape(s.script)}</p></section>""")
    p = project.product
    page = f"""<!doctype html><html lang="ja"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>録音用の台本 {html.escape(p.model_number)}</title>
<style>
body{{font-family:"BIZ UDPGothic","Hiragino Sans","Yu Gothic",sans-serif;margin:0;padding:16px;color:#1a1a1a;background:#fff;line-height:1.7}}
header{{border-bottom:3px solid #1a1a1a;margin-bottom:12px}} h1{{font-size:22px;margin:0}} .meta{{color:#555;font-size:14px}}
.scene{{border:1px solid #ccc;border-radius:10px;padding:10px 14px;margin:12px 0;break-inside:avoid}}
.scene h2{{font-size:18px;margin:0 0 6px;display:flex;gap:10px;align-items:center}}
.scene h2 span{{background:#1a1a1a;color:#fff;border-radius:50%;width:28px;height:28px;display:inline-flex;align-items:center;justify-content:center;font-size:15px}}
.scene h2 small{{margin-left:auto;font-weight:normal;color:#555}}
.file{{color:#555;font-size:13px;margin:2px 0}} .script{{font-size:20px;margin:6px 0}}
@media print{{body{{padding:0}} .scene{{border-color:#999}}}}
</style></head><body><header><h1>録音用の台本　{html.escape(p.name or p.model_number)}</h1>
<div class="meta">場面ごとに1つのファイルで録ってください。ファイル名は変えなくても、中身を聞き取って振り分けます。<br>
前後に少し間を空けて、ゆっくりはっきり読んでください。</div></header>
{''.join(rows)}</body></html>"""
    out = folder / "output" / "録音用の台本.html"
    out.write_text(page, encoding="utf-8")
    return out


def _chunks(text: str, limit: int = READ_CHUNK) -> list[str]:
    """読み合わせ用の字幕に分ける（句点で区切り、長ければ読点で）。"""
    out: list[str] = []
    for sent in [x + "。" for x in text.replace("\n", "").split("。") if x.strip()]:
        while len(sent) > limit:
            cut = max(sent.rfind("、", 0, limit), limit // 2)
            out.append(sent[:cut + 1])
            sent = sent[cut + 1:]
        if sent.strip("。"):
            out.append(sent)
    return out


def _wrap2(text: str, width: int = 18) -> str:
    return text if len(text) <= width else text[:width] + "\n" + text[width:]


def render_readalong(folder: Path, services: Services) -> Path:
    """完成した映像に、台本を字幕で流した「読み合わせ用の映像」（録音の練習用）。"""
    profile, project = services.prof(), load_project(folder)
    layouts, _ = _load_layouts(_layout_path(folder, "video"))
    spans = scene_spans(layouts)
    subs: list[Telop] = []
    for s in narration_scenes(project):
        if s.id not in spans or not s.script:
            continue
        a, b = spans[s.id]
        a, b = a + LEAD, b - 0.5
        parts = _chunks(s.script)
        total = sum(len(x) for x in parts) or 1
        t = a
        for x in parts:
            d = (b - a) * len(x) / total
            subs.append(Telop(start=round(t, 2), end=round(t + d - 0.05, 2), text=_wrap2(x), font="biz_ud"))
            t += d
    work = folder / "work"
    ass = build_telop_ass(subs, work / "readalong.ass", project.product.category, project.product.model_number,
                          project.settings.telop_style)
    out = folder / "output" / "読み合わせ用の映像.mp4"
    burn_ass(profile, work / "video.mp4", ass, out)
    return out


# --- 録った声 --------------------------------------------------------------------

def _voice_wav(folder: Path, file: str) -> Path:
    return folder / "work" / "voice" / f"{Path(file).stem}.wav"


def _voice_words(folder: Path, file: str) -> Path:
    return folder / "work" / "voice" / f"{Path(file).stem}_words.json"


def list_audio(source: Path | list[Path]) -> list[Path]:
    paths = source if isinstance(source, list) else [source]
    out: list[Path] = []
    for p in map(Path, paths):
        if p.is_dir():
            out += sorted(x for x in p.rglob("*") if x.is_file() and x.suffix.lower() in AUDIO_EXTS
                          and not x.name.startswith("."))
        elif p.is_file() and p.suffix.lower() in AUDIO_EXTS:
            out.append(p)
    return out


@_scoped
def import_voices(folder: Path, source: Path | list[Path], services: Services, job: Job | None = None) -> list[VoiceTake]:
    """録った声のファイルを取り込み（コピー）、聞き取って場面に振り分ける。元のファイルは変えない。"""
    profile, project = services.prof(), load_project(folder)
    _require_separate(project)
    files = list_audio(source)
    if not files:
        raise UserError("声のファイルが見つかりませんでした。", "録音したファイル（m4a・mp3・wav など）か、それが入ったフォルダを入れてください。")
    vdir = folder / VOICE_DIR
    vdir.mkdir(exist_ok=True)
    hint = hint_text(collect_terms(project.product)[:20] + [project.product.name])
    fixer = TermFixer.for_product(project.product)
    known = {v.file for v in project.voices}
    for i, src in enumerate(files, 1):
        _step(job, 0.05 + 0.6 * (i - 1) / len(files), f"声を聞き取っています（{i}/{len(files)}）")
        name = src.name
        n = 2
        while name in known or (vdir / name).exists() and not _same_file(vdir / name, src):
            name = f"{src.stem}_{n}{src.suffix}"
            n += 1
        dst = vdir / name
        if not dst.exists():
            shutil.copy2(src, dst)
        info = edge_trim(profile, dst)
        wav = prepare_voice(profile, dst, _voice_wav(folder, name), info)
        words = transcribe(profile, wav, hint, services.transcriber)
        _voice_words(folder, name).write_text(json.dumps([vars(w) for w in words], ensure_ascii=False), encoding="utf-8")
        text, _ = fixer.fix("".join(w.text for w in words), name)
        take = VoiceTake(file=name, duration=duration_of(profile.ffprobe, wav), trim_start=info.trim_start,
                         trim_end=info.trim_end, transcript=text)
        project.voices = [v for v in project.voices if v.file != name] + [take]
        known.add(name)
    save_project(folder, project)
    _step(job, 0.7, "どの場面の声か、振り分けています")
    project = assign_voices_into(folder, services)
    _step(job, 1.0, "声を取り込みました")
    return project.voices


def _same_file(a: Path, b: Path) -> bool:
    try:
        return a.stat().st_size == b.stat().st_size and a.read_bytes()[:65536] == b.read_bytes()[:65536]
    except OSError:
        return False


def assign_voices_into(folder: Path, services: Services) -> Project:
    """場面が決まっていない声を振り分ける: ファイル名 → 分からなければ Claude が中身で。録り直しは台本に近い方を使う。"""
    project = load_project(folder)
    scenes = narration_scenes(project)
    by_id = {s.id: s for s in scenes}
    todo = []
    for v in project.voices:
        if v.assigned_by == "manual" and v.scene_id in by_id:
            continue
        s = scene_from_name(v.file, project.plan.scenes)
        if s is not None:
            v.scene_id, v.assigned_by, v.reason = s.id, "name", "ファイル名から判断"
        else:
            todo.append(v)
    if todo and scenes:
        got = assign_voices(services.cc(), scenes, [(v.file, v.duration, v.transcript) for v in todo])
        for v in todo:
            sid, reason = got.get(v.file, ("", ""))
            v.scene_id = sid or None
            v.assigned_by, v.reason = "claude", reason or "どの場面にも当たらない"
    for s in scenes:
        takes = [v for v in project.voices if v.scene_id == s.id]
        if not takes or any(v.assigned_by == "manual" for v in takes):
            continue
        best = max(takes, key=lambda v: (script_match(v.transcript, s.script), project.voices.index(v)))
        for v in takes:
            v.use = v is best
            if v is not best:
                v.reason = (v.reason + "／" if v.reason else "") + "録り直しの別テイク（台本との一致が低い方）"
    for v in project.voices:
        if v.scene_id is None:
            v.use = False
    project.mark_changed(Stage.NARRATION)
    save_project(folder, project)
    return project


def set_voice_scene(folder: Path, file: str, scene_id: str | None) -> Project:
    """声の場面を手で決める（同じ場面のほかの声は「使わない」に）。"""
    project = load_project(folder)
    for v in project.voices:
        if v.file == file:
            v.scene_id, v.assigned_by, v.reason, v.use = scene_id, "manual", "手で選んだ", scene_id is not None
        elif scene_id and v.scene_id == scene_id:
            v.use = False
    project.mark_changed(Stage.NARRATION)
    save_project(folder, project)
    return project


def set_voice_use(folder: Path, file: str) -> Project:
    """同じ場面の録り直しのうち、このファイルを使う。"""
    project = load_project(folder)
    target = next(v for v in project.voices if v.file == file)
    for v in project.voices:
        if v.scene_id == target.scene_id:
            v.use = v is target
            if v.use:
                v.assigned_by = "manual"
    project.mark_changed(Stage.NARRATION)
    save_project(folder, project)
    return project


# --- 声に合わせて仕上げる -------------------------------------------------------------

@dataclass
class FinishResult:
    seconds: float = 0.0
    notes: list[str] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)   # 声がない場面の名前


def fit_to_voices(project: Project, data: dict[str, ClipData]) -> tuple[list[EditPiece], list[str], list[str]]:
    """説明の場面のカットを、使う声の長さに合わせる。戻り値: (カット, お知らせ, 声がない場面)。"""
    assert project.edit is not None
    voice = {v.scene_id: v for v in project.voices if v.use and v.scene_id}
    notes, missing, out = [], [], []
    for s in project.plan.scenes:
        pieces = [p for p in project.edit.pieces if p.enabled and p.scene_id == s.id]
        if s.voice == "talk" or not pieces:
            out += pieces
            continue
        if s.id not in voice:
            missing.append(s.title)
            out += pieces
            continue
        dur = {f: d.duration for f, d in data.items()}
        cuts = [Cut(p.clip, p.start, p.end, dur.get(p.clip, p.end)) for p in pieces]
        spares = [Cut(p.clip, p.start, p.end, dur.get(p.clip, p.end)) for p in project.edit.spares
                  if p.enabled and p.scene_id == s.id and p.clip in dur]
        r = fit_scene(cuts, target_length(voice[s.id].duration), spares)
        notes += [f"「{s.title}」: {n}" for n in dict.fromkeys(r.notes)]
        shows = {(p.clip): p.shows for p in pieces + project.edit.spares}
        out += [EditPiece(clip=c.clip, start=c.start, end=c.end, scene_id=s.id, mute=True, speed=c.speed, hold=c.hold,
                          shows=shows.get(c.clip, ""), reason="予備のカットから足した" if c.spare else "声に合わせて長さを調整")
                for c in r.cuts]
    return out, notes, missing


@_scoped
def finish_with_voices(folder: Path, services: Services, job: Job | None = None) -> FinishResult:
    """映像の長さを声に合わせ、声を重ね、テロップを作る（画面の「声に合わせて仕上げる」）。"""
    profile, project = services.prof(), load_project(folder)
    _require_separate(project)
    if project.edit is None:
        raise UserError("まだ映像ができていません。", "映像の画面で「映像を編集する」を押してください。")
    if not any(v.use and v.scene_id for v in project.voices):
        raise UserError("使える声がありません。", "録った声のファイルを入れて、場面を選んでください。")
    data = prepare_analysis(folder, services, with_words=True, job=job, span=(0.0, 0.2))
    res = FinishResult()
    _step(job, 0.25, "声の長さに合わせて、映像の長さを調整しています")
    pieces, res.notes, res.missing = fit_to_voices(project, data)
    if res.missing:
        res.notes.append(f"声がない場面（{'、'.join(res.missing)}）は、映像のまま入れました。")
    work = folder / "work"
    base, layouts, total = render_plan(
        profile, EditPlan(pieces=pieces), {f: d.norm for f, d in data.items()}, {f: d.keeps for f, d in data.items()},
        work, progress=lambda f, m: _step(job, 0.3 + 0.35 * f, m))
    _save_layouts(_layout_path(folder, "final"), layouts, total)
    spans = scene_spans(layouts)

    _step(job, 0.7, "声を重ねています")
    placed = []  # (声, 置く時刻)
    for v in project.voices:
        if v.use and v.scene_id in spans:
            placed.append((v, round(spans[v.scene_id][0] + LEAD, 3)))
    mix_voices(profile, base, [(_voice_wav(folder, v.file), at) for v, at in placed], work / "joined.mp4")
    res.seconds = total

    _step(job, 0.8, "テロップを作っています")
    timeline: list[Word] = []
    for f, d in data.items():   # 顔出しの場面の声
        for w in d.words:
            at = map_time([x for x in layouts if not pieces[x.index].mute], f, (w.start + w.end) / 2)
            if at is not None:
                half = (w.end - w.start) / 2
                timeline.append(Word(round(at - half, 3), round(at + half, 3), w.text))
    for v, at in placed:        # 別録りの声
        path = _voice_words(folder, v.file)
        if path.exists():
            timeline += [Word(round(w["start"] + at, 3), round(w["end"] + at, 3), w["text"])
                         for w in json.loads(path.read_text(encoding="utf-8"))]
    timeline.sort(key=lambda w: w.start)
    fixer = TermFixer.for_product(project.product)
    transcript, fixes = [], []
    for ph in to_phrases(timeline):
        text, fx = fixer.fix(ph.text, f"{ph.start:.1f}秒")
        transcript.append({"start": ph.start, "end": ph.end, "text": text})
        fixes += fx
    (folder / "output" / "字幕修正ログ.txt").write_text(log_text(fixes), encoding="utf-8")
    title, new = make_telops(services.cc(), project.product, project.plan.scenes, transcript, total,
                             project.plan.price, project.plan.extra_text)
    project = load_project(folder)
    project.title, project.telops = title, merge_manual(new, project.telops)
    project.fitted, project.fit_notes = pieces, res.notes
    project.complete_stage(Stage.NARRATION)
    save_project(folder, project)
    _step(job, 1.0, "声に合わせて仕上げました")
    return res


def mix_voices(profile, video: Path, voices: list[tuple[Path, float]], dst: Path) -> Path:
    """映像の音（顔出しの場面の声）に、別録りの声をそれぞれの時刻に重ねる。"""
    if not voices:
        shutil.copy2(video, dst)
        return dst
    args = [*ffmpeg_base(profile.ffmpeg), "-i", str(video)]
    for wav, _ in voices:
        args += ["-i", str(wav)]
    graph = [f"[{k}:a]adelay={int(at * 1000)}:all=1[d{k}]" for k, (_, at) in enumerate(voices, 1)]
    labels = "[0:a]" + "".join(f"[d{k}]" for k in range(1, len(voices) + 1))
    graph.append(f"{labels}amix=inputs={len(voices) + 1}:duration=first:normalize=0[a]")
    run([*args, "-filter_complex", ";".join(graph), "-map", "0:v", "-map", "[a]", "-c:v", "copy", *AUDIO_ARGS, str(dst)],
        what="声を映像に重ねられませんでした。")
    return dst
