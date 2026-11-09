"""テロップと映像の照合: テロップが出ている間の画面を Claude Code に見せ、食い違いを直してもらう。

- テロップごとに、出ている間の画面（始め・中ほど・終わり）を切り出して見本画像にする
- 映像の内容（どの時刻に何が映っているか。つないだときの記録）と一緒に渡す
- 自動で作ったテロップは直す。手で直したテロップは変えずに知らせるだけ。直したものは元に戻せる
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from app.claude.calls import check_telops_call
from app.errors import UserError
from app.jobs import Cancelled
from app.pipeline.edl import load_layouts
from app.pipeline.ffmpeg import duration_of, ffmpeg_base, run
from app.pipeline.fonts import FONTS_DIR, plain_text
from app.project import Project, TelopCheck, load_project, save_project

TILE_W, TILE_H = 384, 216
PER_SHEET = 6     # 1枚の見本画像に並べるテロップの数
LABEL_H = 34


def layout_file(folder: Path, project: Project) -> Path:
    return folder / "work" / ("final_layout.json" if project.separate_voice else "edit_layout.json")


def frame_times(start: float, end: float, duration: float) -> list[float]:
    a, b = min(start + 0.3, end), max(end - 0.3, start)
    return [round(min(max(0.0, t), duration - 0.05), 2) for t in (a, (start + end) / 2, b)]


def telop_sheets(profile, video: Path, telops, dst_dir: Path) -> list[Path]:
    """テロップごとに、出ている間の画面3枚を並べた見本画像。"""
    duration = duration_of(profile.ffprobe, video)
    font = ImageFont.truetype(str(FONTS_DIR / "BIZUDPGothic-Bold.ttf"), 22)
    dst_dir.mkdir(parents=True, exist_ok=True)
    for old in dst_dir.glob("telops_*.jpg"):
        old.unlink()
    sheets = []
    for k in range(0, len(telops), PER_SHEET):
        group = list(enumerate(telops))[k:k + PER_SHEET]
        sheet = Image.new("RGB", (3 * TILE_W, len(group) * (TILE_H + LABEL_H)), (20, 20, 20))
        draw = ImageDraw.Draw(sheet)
        with tempfile.TemporaryDirectory() as tmp:
            for row, (i, t) in enumerate(group):
                y = row * (TILE_H + LABEL_H)
                draw.text((8, y + 5), f"#{i}  {t.start:.1f}〜{t.end:.1f}秒「{plain_text(t.text).replace(chr(10), ' ')}」",
                          font=font, fill=(255, 220, 0))
                for j, at in enumerate(frame_times(t.start, t.end, duration)):
                    frame = Path(tmp) / f"{i}_{j}.jpg"
                    run([*ffmpeg_base(profile.ffmpeg), "-ss", f"{at}", "-i", str(video), "-frames:v", "1",
                         "-vf", f"scale={TILE_W}:{TILE_H}", str(frame)], what="テロップの見本を作れませんでした。", output=frame)
                    if frame.exists():
                        sheet.paste(Image.open(frame).convert("RGB"), (j * TILE_W, y + LABEL_H))
        dst = dst_dir / f"telops_{k // PER_SHEET + 1}.jpg"
        sheet.save(dst, quality=85)
        sheets.append(dst)
    return sheets


def run_check(folder: Path, services, job=None) -> Project:
    """照合して、食い違っていたテロップを直す（手で直したものは知らせるだけ）。"""
    folder = Path(folder)
    project, profile = load_project(folder), services.prof()
    video = folder / "work" / "joined.mp4"
    if not project.telops:
        project.telop_checks, project.telop_check_summary = [], "テロップがないので、確かめていません。"
        save_project(folder, project)
        return project
    if not video.exists():
        raise UserError("まだ動画ができていません。", "先に編集（声を別に録るときは「声に合わせて仕上げる」）をしてください。")
    path = layout_file(folder, project)
    visual = load_layouts(path)[2] if path.exists() else []
    if job is not None:
        job.step(0.2, "テロップが出ている間の画面を切り出しています")
    sheets = telop_sheets(profile, video, project.telops, folder / "work" / "telop_check")
    if job is not None:
        job.step(0.4, "Claude Code がテロップと映像を見比べています")
    out = check_telops_call(services.cc(), project.product, project.telops, visual,
                            duration_of(profile.ffprobe, video), sheets)
    checks, gone = [], set()
    for f in out.fixes:
        t = project.telops[f.index]
        c = TelopCheck(text=t.text, start=t.start, end=t.end, problem=f.problem, shows=f.shows, action=f.action,
                       before=t.model_copy())
        if t.manual:
            c.manual = True
            c.after = t.model_copy(update={"start": f.start, "end": f.end, "text": f.text})  # おすすめの直し方
        elif f.action == "delete":
            gone.add(f.index)
            c.applied = True
        else:
            t.start, t.end = round(f.start, 2), round(f.end, 2)
            if f.action == "rewrite":
                t.text = f.text.strip()
            c.after, c.applied = t.model_copy(), True
        checks.append(c)
    project.telops = sorted((t for i, t in enumerate(project.telops) if i not in gone), key=lambda t: t.start)
    project.telop_checks, project.telop_check_summary = checks, out.summary.strip()
    save_project(folder, project)
    return project


def undo_check(folder: Path, index: int) -> Project:
    """直したテロップを、照合の前の状態に戻す。"""
    project = load_project(folder)
    c = project.telop_checks[index]
    if not c.applied or c.undone:
        return project
    if c.after is not None:
        project.telops = [t for t in project.telops
                          if not (t.text == c.after.text and abs(t.start - c.after.start) < 0.01)]
    project.telops = sorted(project.telops + [c.before], key=lambda t: t.start)
    c.undone = True
    save_project(folder, project)
    return project


def apply_suggestion(folder: Path, index: int) -> Project:
    """手で直したテロップについて、知らせただけの直し方を当てはめる。"""
    project = load_project(folder)
    c = project.telop_checks[index]
    if not c.manual or c.applied:
        return project
    if c.action == "delete":
        project.telops = [t for t in project.telops
                          if not (t.text == c.before.text and abs(t.start - c.before.start) < 0.01)]
    elif c.after is not None:
        for t in project.telops:
            if t.text == c.before.text and abs(t.start - c.before.start) < 0.01:
                t.start, t.end, t.text = c.after.start, c.after.end, c.after.text
    project.telops.sort(key=lambda t: t.start)
    c.applied = True
    save_project(folder, project)
    return project


def check_after_telops(folder: Path, services) -> list[str]:
    """テロップを作ったあとに照合する。うまくいかなくても止めず、お知らせを返す。"""
    try:
        project = run_check(folder, services)
    except Cancelled:
        raise
    except UserError as e:
        return [f"テロップと映像の照らし合わせができませんでした（{e.what}）。仕上げの画面のボタンでやり直せます。"]
    fixed = sum(1 for c in project.telop_checks if c.applied)
    flagged = sum(1 for c in project.telop_checks if c.manual)
    notes = []
    if fixed:
        notes.append(f"テロップと映像の食い違いを {fixed} か所直しました（仕上げの画面で確かめて、元に戻せます）。")
    if flagged:
        notes.append(f"手で直したテロップに、映像との食い違いの可能性が {flagged} か所あります（仕上げの画面で確かめてください）。")
    return notes
