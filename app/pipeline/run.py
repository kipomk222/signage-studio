"""素材フォルダから完成動画までをコマンド1つで通す。

使い方:
    uv run python -m app.pipeline.run <素材フォルダ> --model <型番> [--out store,youtube]
    （商品情報は保存済み → Web で調査 の順に探す。--paste / --image で商品ページから。--no-telop でテロップなし）

元の素材は変更しない。プロジェクトフォルダの「素材」にコピーしてから処理する。
"""

from __future__ import annotations

import argparse
import shutil
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from app.claude.runner import ClaudeCode
from app.errors import UserError
from app.pipeline import bgm as bgm_step
from app.pipeline.export import TARGETS, ExportResult
from app.pipeline.fixterms import Fix
from app.pipeline.inventory import ClipInfo
from app.pipeline.silence import STRENGTH, CutResult
from app.pipeline.transcribe import Transcriber
from app.platform import PlatformProfile, get_profile
from app.project import Price, Product, Scene, Telop, TelopStyle, create_project, load_project, save_project
from app.workflow import Services, build_edit, export_all, import_material, render_telop_video  # noqa: F401

ROOT = Path(__file__).resolve().parents[2]

Progress = Callable[[str], None]


@dataclass
class TelopInputs:
    """テロップを入れるときに必要なもの。"""

    product: Product
    scenes: list[Scene] = field(default_factory=list)
    price: Price = field(default_factory=Price)
    extra_text: str = ""
    claude: ClaudeCode | None = None
    transcriber: Transcriber | None = None
    logo: Path | None = None
    style: TelopStyle | None = None  # テロップの色（省略時は白文字・黒縁・黄色の強調）


@dataclass
class RunReport:
    project_dir: Path
    clips: list[ClipInfo]
    cuts: list[CutResult]
    joined_seconds: float
    exports: list[ExportResult]
    bgm: Path | None
    notes: list[str] = field(default_factory=list)
    title: str = ""
    telops: list[Telop] = field(default_factory=list)
    fixes: list[Fix] = field(default_factory=list)
    edit_summary: str = ""

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
        if self.edit_summary:
            lines.append(f"■ 編集の考え方: {self.edit_summary}")
        if self.telops:
            lines.append(f"■ 冒頭タイトル: {self.title}")
            lines.append(f"■ テロップ（{len(self.telops)}枚）")
            lines += [f"  {t.start:5.1f}〜{t.end:5.1f}秒  {t.text.replace(chr(10), ' / ')}" for t in self.telops]
            lines.append(f"■ 型番・固有名詞の修正: {len(self.fixes)}か所（字幕修正ログ.txt）")
        lines.append(f"■ BGM: {self.bgm.name if self.bgm else 'なし'}")
        lines.append("■ 書き出し")
        lines += [f"  {e.summary()}" for e in self.exports]
        if self.notes:
            lines.append("■ お知らせ")
            lines += [f"  ・{n}" for n in self.notes]
        return "\n".join(lines)


class _PrintJob:
    """コマンドで動かすときの進み具合の表示（画面の Job の代わり）。"""

    def __init__(self, progress: Progress, label: str) -> None:
        self.progress, self.label, self.last = progress, label, ""

    def step(self, fraction: float, message: str) -> None:
        if message != self.last:
            self.progress(f"[{self.label}] {message}")
            self.last = message


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
    telop: TelopInputs | None = None,
    ai_edit: bool = True,
    profile: PlatformProfile | None = None,
    progress: Progress = print,
) -> RunReport:
    """素材フォルダから完成動画までを一度に作る（画面と同じ処理を順に呼ぶ）。"""
    outputs = outputs or ["store"]
    src_folder = Path(src_folder)
    if not src_folder.is_dir():
        raise UserError("素材フォルダが見つかりません。", "フォルダの場所を確認してください。")
    services = Services(claude=telop.claude if telop else None, transcriber=telop.transcriber if telop else None,
                        profile=profile or get_profile(), logo=telop.logo if telop else None)

    project, folder = create_project(projects_root, model_number)
    project.plan.voice_mode = "live"  # コマンド1つで仕上げる: 撮影時に話した素材から（声の別録りは画面で）
    if target_seconds:
        project.plan.target_seconds = target_seconds
    project.settings.silence_strength = strength  # type: ignore[assignment]
    project.settings.bgm_level = level  # type: ignore[assignment]
    project.settings.fit = fit  # type: ignore[assignment]
    project.settings.outputs = outputs  # type: ignore[assignment]
    project.settings.ai_edit = ai_edit
    if telop:
        project.product, project.plan.scenes = telop.product, telop.scenes
        project.plan.price, project.plan.extra_text = telop.price, telop.extra_text
        if telop.style:
            project.settings.telop_style = telop.style
    if bgm_path is not None:  # 指定された曲をプロジェクトに控える（作り直しでも同じ曲を使う）
        (folder / "work").mkdir(exist_ok=True)
        shutil.copy2(bgm_path, folder / "work" / f"bgm{Path(bgm_path).suffix.lower()}")
        project.settings.bgm_file = Path(bgm_path).name
    save_project(folder, project)

    clips = import_material(folder, src_folder, services, _PrintJob(progress, "素材"))  # type: ignore[arg-type]
    if keep_all:
        project = load_project(folder)
        for c in project.clips:
            c.excluded = False
        save_project(folder, project)
    edit = build_edit(folder, services, with_telops=bool(telop), job=_PrintJob(progress, "編集"))  # type: ignore[arg-type]
    notes = list(edit.notes)
    exports = export_all(folder, services, outputs, job=_PrintJob(progress, "書き出し"), notes=notes)  # type: ignore[arg-type]

    from app.workflow import find_bgm
    project = load_project(folder)
    report = RunReport(folder, clips, edit.cuts, edit.joined_seconds, exports, find_bgm(folder, project), notes,
                       project.title, project.telops, edit.fixes,
                       f"（Claude Code）{edit.summary}" if edit.source == "ai" else "")
    (folder / "output" / "レポート.txt").write_text(report.text(), encoding="utf-8")
    return report


def rerender(project_dir: Path, style: TelopStyle | None = None, outputs: list[str] | None = None,
             profile: PlatformProfile | None = None, progress: Progress = print) -> list[ExportResult]:
    """テロップの色や文言を直したあと、編集し直さずに書き出しだけやり直す。"""
    folder = Path(project_dir)
    project = load_project(folder)
    if not (folder / "work" / "joined.mp4").exists() or not project.telops:
        raise UserError("作り直せる編集結果が見つかりません。",
                        "一度、素材フォルダから完成動画まで作ってから、お試しください。")
    if style:
        project.settings.telop_style = style
        save_project(folder, project)
    return export_all(folder, Services(profile=profile or get_profile()), outputs,
                      job=_PrintJob(progress, "書き出し"))  # type: ignore[arg-type]


def style_from_args(args, base: TelopStyle | None = None) -> TelopStyle | None:
    """コマンドの色の指定を TelopStyle にする。何も指定がなければ None。"""
    given = {k: getattr(args, k) for k in ("text_color", "outline_color", "emphasis_color", "band_color",
                                           "band_opacity") if getattr(args, k) is not None}
    if not given:
        return None
    return TelopStyle.model_validate({**(base or TelopStyle()).model_dump(), **given})


def parse_price(text: str) -> Price:
    if text in ("hidden", ""):
        return Price()
    if text == "open":
        return Price(mode="open")
    digits = text.replace(",", "").replace("円", "")
    if not digits.isdigit():
        raise UserError("価格の指定が読めませんでした。", "hidden、open、または数字（例 248780）で指定してください。")
    return Price(mode="amount", amount=int(digits))


def prepare_telop(args) -> TelopInputs:
    """商品情報を用意する（保存済み → 商品ページ → Web の順）。"""
    from app.claude.calls import product_from_paste, research_product

    cc = ClaudeCode()
    if args.paste or args.image:
        print("商品ページの内容から商品情報を整理しています")
        text = args.paste.read_text(encoding="utf-8") if args.paste else ""
        product, info = product_from_paste(cc, args.model, text=text, images=args.image)
    else:
        print("商品情報を確認しています（初めての型番は Web で調べるため数分かかります）")
        product, info = research_product(cc, args.model)
    if not info.found:
        raise UserError("商品情報が見つかりませんでした。",
                        "--paste で商品ページの文章を渡すか、--no-telop でテロップなしにしてください。")
    return TelopInputs(product=product, price=parse_price(args.price), extra_text=args.extra, claude=cc,
                       style=style_from_args(args))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="素材フォルダから完成動画を作ります。")
    ap.add_argument("folder", type=Path, nargs="?", help="スマホで撮った動画が入ったフォルダ")
    ap.add_argument("--model", help="型番（プロジェクト名に使います）")
    ap.add_argument("--rerender", type=Path, metavar="プロジェクトのフォルダ",
                    help="色やテロップを直したあと、書き出しだけやり直す")
    ap.add_argument("--bgm", type=Path, help="BGM ファイル（省略時は assets/bgm の最初の曲）")
    ap.add_argument("--out", default="store", help="store（店頭用）, youtube をカンマ区切りで")
    ap.add_argument("--strength", choices=list(STRENGTH), default="normal", help="無音カットの強さ")
    ap.add_argument("--level", choices=list(bgm_step.LEVELS), default="medium", help="BGM の音量")
    ap.add_argument("--fit", choices=["pad", "crop"], default="pad", help="縦撮りを 黒帯(pad)／切り抜き(crop)")
    ap.add_argument("--keep-all", action="store_true", help="除外候補も使う")
    ap.add_argument("--target", type=int, help="目標の長さ（秒）。省略時は150秒")
    ap.add_argument("--projects", type=Path, default=ROOT / "projects", help="プロジェクトの保存先")
    ap.add_argument("--no-telop", action="store_true", help="テロップ・タイトル・エンドカードを付けない")
    ap.add_argument("--no-ai-edit", action="store_true",
                    help="編集の判断を Claude Code にまかせず、機械的に編集する（無音カットと台本の順）")
    ap.add_argument("--paste", type=Path, help="商品ページから写した文章のファイル")
    ap.add_argument("--image", type=Path, nargs="+", help="商品ページのスクリーンショット")
    ap.add_argument("--price", default="hidden", help="価格: hidden（表示しない）/ open（オープン価格）/ 金額（例 248780）")
    ap.add_argument("--extra", default="", help="追加で入れたい情報（例「展示品あり」）")
    colors = ap.add_argument_group("テロップの色（カラーコード）")
    colors.add_argument("--text-color", help="文字の色（標準 #FFFFFF）")
    colors.add_argument("--outline-color", help="縁取りの色（標準 #000000）")
    colors.add_argument("--emphasis-color", help="**強調** した言葉の色（標準 #FFD400）")
    colors.add_argument("--band-color", help="文字の後ろに敷く帯の色（標準はなし。\"\" で帯を消す）")
    colors.add_argument("--band-opacity", type=float, help="帯の濃さ 0〜1（標準 0.6）")
    args = ap.parse_args(argv)

    outputs = [o.strip() for o in args.out.split(",") if o.strip()]
    if unknown := [o for o in outputs if o not in TARGETS]:
        print(f"書き出し先「{'、'.join(unknown)}」は使えません。store か youtube を指定してください。")
        return 2
    if args.rerender:
        try:
            base = load_project(args.rerender).settings.telop_style
            exports = rerender(args.rerender, style_from_args(args, base), outputs)
        except UserError as e:
            print(f"\n{e.what}\n{e.how}")
            return 1
        print("\n" + "\n".join(e.summary() for e in exports))
        print(f"\n完成した動画: {args.rerender / 'output'}")
        return 0
    if args.folder is None or not args.model:
        print("素材フォルダと --model（型番）を指定してください。")
        return 2
    try:
        style_from_args(args)  # 色の指定の間違いは、時間のかかる処理の前に知らせる
        if not args.folder.is_dir():  # 商品情報を調べる前に確かめる
            raise UserError("素材フォルダが見つかりません。", "フォルダの場所を確認してください。")
        telop = None if args.no_telop else prepare_telop(args)
        report = edit_folder(args.folder, args.model, projects_root=args.projects, bgm_path=args.bgm,
                             outputs=outputs, strength=args.strength, level=args.level,
                             fit=args.fit, keep_all=args.keep_all, target_seconds=args.target, telop=telop,
                             ai_edit=not args.no_ai_edit)
    except UserError as e:
        print(f"\n{e.what}\n{e.how}")
        return 1
    print("\n" + report.text())
    print(f"\n完成した動画: {report.project_dir / 'output'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
