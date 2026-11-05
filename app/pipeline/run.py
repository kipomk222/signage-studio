"""素材フォルダから完成動画（テロップなし）までをコマンド1つで通す。

使い方:
    uv run python -m app.pipeline.run <素材フォルダ> --model <型番> [--out store,youtube]

元の素材は変更しない。プロジェクトフォルダの「素材」にコピーしてから処理する。
"""

from __future__ import annotations

import argparse
import shutil
import sys
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from pathlib import Path

from app.errors import UserError
from app.pipeline import bgm as bgm_step
from app.pipeline.concat import FADE, concat_clips
from app.pipeline.export import TARGETS, ExportResult, export_video
from app.pipeline.inventory import ClipInfo, scan_folder
from app.pipeline.normalize import normalize_clip
from app.pipeline.silence import STRENGTH, TIGHTEST_MIN_SILENCE, CutResult, jumpcut
from app.platform import PlatformProfile, get_profile
from app.project import Clip, Stage, create_project, save_project

ROOT = Path(__file__).resolve().parents[2]
MIN_USABLE = 2 * FADE + 0.2  # これより短く残ったクリップはつなぎ目に収まらないので使わない

Progress = Callable[[str], None]


@dataclass
class RunReport:
    project_dir: Path
    clips: list[ClipInfo]
    cuts: list[CutResult]
    joined_seconds: float
    exports: list[ExportResult]
    bgm: Path | None
    notes: list[str] = field(default_factory=list)

    def text(self) -> str:
        lines = ["■ 素材"]
        for c in self.clips:
            mark = "除外" if c.excluded else "使用"
            why = f"（{'、'.join(c.exclude_reasons)}）" if c.excluded else ""
            lines.append(f"  [{mark}] {c.path.name}  {c.duration:.1f}秒{why}")
        lines.append("■ 無音カット")
        for r in self.cuts:
            tag = "  ※切りすぎ注意" if r.over_cut else ("  ※話し声なし・そのまま使用" if r.no_speech else "")
            lines.append(f"  {r.src.name}: {r.original:.1f}秒 → {r.kept:.1f}秒{tag}")
        lines.append(f"■ つないだ長さ: {self.joined_seconds:.1f}秒")
        lines.append(f"■ BGM: {self.bgm.name if self.bgm else 'なし'}")
        lines.append("■ 書き出し")
        lines += [f"  {e.summary()}" for e in self.exports]
        if self.notes:
            lines.append("■ お知らせ")
            lines += [f"  ・{n}" for n in self.notes]
        return "\n".join(lines)


def copy_into_project(src_folder: Path, dst_folder: Path, clips: list[ClipInfo]) -> None:
    """元の素材はそのままに、プロジェクトの「素材」へコピーする。"""
    for c in clips:
        target = dst_folder / c.path.name
        if not target.exists():
            shutil.copy2(c.path, target)
        c.path = target


def edit_folder(
    src_folder: Path,
    model_number: str,
    projects_root: Path = ROOT / "projects",
    bgm_path: Path | None = None,
    outputs: list[str] | None = None,
    strength: str = "normal",
    level: str = "medium",
    fit: str = "pad",
    keep_all: bool = False,
    target_seconds: int | None = None,
    profile: PlatformProfile | None = None,
    progress: Progress = print,
) -> RunReport:
    outputs = outputs or ["store"]
    profile = profile or get_profile()
    src_folder = Path(src_folder)
    if not src_folder.is_dir():
        raise UserError("素材フォルダが見つかりません。", "フォルダの場所を確認してください。")

    project, folder = create_project(projects_root, model_number)
    if target_seconds:
        project.plan.target_seconds = target_seconds
    work, out = folder / "work", folder / "output"
    notes: list[str] = []

    progress("[1/6] 素材を調べています")
    clips = scan_folder(profile, src_folder)
    if not clips:
        raise UserError("素材フォルダに動画が見つかりません。", "スマホで撮った動画ファイルを入れてください。")
    copy_into_project(src_folder, folder / "素材", clips)
    use = clips if keep_all else [c for c in clips if not c.excluded]
    if not use:
        raise UserError("使える動画がありませんでした。", "除外の理由を確認して、素材を入れ直してください。")

    progress(f"[2/6] 形式をそろえています（{len(use)}本）")
    normalized = [normalize_clip(profile, c, work / "norm" / f"{i:03d}.mp4", fit=fit)  # type: ignore[arg-type]
                  for i, c in enumerate(use, 1)]

    progress("[3/6] 話していない部分を詰めています")
    params = STRENGTH[strength]
    cuts = [jumpcut(profile, n, work / "cut" / n.name, params) for n in normalized]
    estimate = sum(r.kept for r in cuts) - FADE * (len(cuts) - 1)
    target = project.plan.target_seconds
    if estimate > target and params.min_silence > TIGHTEST_MIN_SILENCE:
        tight = replace(params, min_silence=TIGHTEST_MIN_SILENCE)
        cuts = [jumpcut(profile, n, work / "cut" / n.name, tight) for n in normalized]
        notes.append(f"目標の長さ（{target}秒）を超えたため、無音カットを強めました。")
    for c, r in zip(use, cuts):
        r.src = c.path  # レポートは元のファイル名で表示する
        if r.over_cut:
            notes.append(f"「{c.path.name}」は半分以下まで短くなりました。切りすぎていないか確認してください。")

    pieces = [r.dst for r in cuts if r.kept >= MIN_USABLE]
    for c, r in zip(use, cuts):
        if r.kept < MIN_USABLE:
            notes.append(f"「{c.path.name}」は残った部分が短すぎるため使いませんでした。")
    if not pieces:
        raise UserError("つなげられる部分が残りませんでした。", "無音カットの強さを「弱」にして、もう一度お試しください。")

    progress("[4/6] クリップをつないでいます")
    joined_seconds = concat_clips(profile, pieces, work / "joined.mp4")

    progress("[5/6] BGM を重ねています")
    if bgm_path is None:
        found = bgm_step.find_bgm_files(ROOT / "assets" / "bgm")
        bgm_path = found[0] if found else None
    if bgm_path is None:
        notes.append("BGM が見つからなかったため、BGM なしで書き出しました。assets/bgm に曲を入れると使われます。")
    mixed = bgm_step.mix_bgm(profile, work / "joined.mp4", bgm_path, work / "mixed.mp4", level=level)

    progress("[6/6] 書き出しています")
    exports = [export_video(profile, mixed, out, folder.name, key) for key in outputs]

    # プロジェクトに記録
    cut_by_src = {r.src: r for r in cuts}
    project.clips = [
        Clip(
            file=c.path.name,
            excluded=(c.excluded and not keep_all),
            exclude_reason="、".join(c.exclude_reasons),
            cut_ranges=cut_by_src[c.path].ranges if c.path in cut_by_src else [],
        )
        for c in clips
    ]
    project.settings.bgm_file = bgm_path.name if bgm_path else ""
    project.settings.bgm_level = level  # type: ignore[assignment]
    project.settings.silence_strength = strength  # type: ignore[assignment]
    project.settings.outputs = outputs  # type: ignore[assignment]
    project.complete_stage(Stage.MATERIAL)
    save_project(folder, project)

    report = RunReport(folder, clips, cuts, joined_seconds, exports, bgm_path, notes)
    (out / "レポート.txt").write_text(report.text(), encoding="utf-8")
    return report


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="素材フォルダから完成動画（テロップなし）を作ります。")
    ap.add_argument("folder", type=Path, help="スマホで撮った動画が入ったフォルダ")
    ap.add_argument("--model", required=True, help="型番（プロジェクト名に使います）")
    ap.add_argument("--bgm", type=Path, help="BGM ファイル（省略時は assets/bgm の最初の曲）")
    ap.add_argument("--out", default="store", help="store（店頭用）, youtube をカンマ区切りで")
    ap.add_argument("--strength", choices=list(STRENGTH), default="normal", help="無音カットの強さ")
    ap.add_argument("--level", choices=list(bgm_step.LEVELS), default="medium", help="BGM の音量")
    ap.add_argument("--fit", choices=["pad", "crop"], default="pad", help="縦撮りを 黒帯(pad)／切り抜き(crop)")
    ap.add_argument("--keep-all", action="store_true", help="除外候補も使う")
    ap.add_argument("--target", type=int, help="目標の長さ（秒）。省略時は150秒")
    ap.add_argument("--projects", type=Path, default=ROOT / "projects", help="プロジェクトの保存先")
    args = ap.parse_args(argv)

    outputs = [o.strip() for o in args.out.split(",") if o.strip()]
    if unknown := [o for o in outputs if o not in TARGETS]:
        print(f"書き出し先「{'、'.join(unknown)}」は使えません。store か youtube を指定してください。")
        return 2
    try:
        report = edit_folder(args.folder, args.model, projects_root=args.projects, bgm_path=args.bgm,
                             outputs=outputs, strength=args.strength, level=args.level,
                             fit=args.fit, keep_all=args.keep_all, target_seconds=args.target)
    except UserError as e:
        print(f"\n{e.what}\n{e.how}")
        return 1
    print("\n" + report.text())
    print(f"\n完成した動画: {report.project_dir / 'output'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
