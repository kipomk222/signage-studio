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
from dataclasses import dataclass, field, replace
from pathlib import Path

from app.claude.calls import make_telops, merge_manual
from app.claude.runner import ClaudeCode
from app.errors import UserError
from app.pipeline import bgm as bgm_step
from app.pipeline.cards import END_SECONDS, TITLE_SECONDS, find_logo, make_end_card, make_title_card
from app.pipeline.fixterms import Fix, TermFixer, collect_terms, log_text
from app.pipeline.telop_render import build_telop_ass, burn_ass
from app.pipeline.transcribe import Transcriber, hint_text, save_words, to_phrases, transcribe
from app.pipeline.concat import FADE, concat_clips
from app.pipeline.export import TARGETS, ExportResult, export_video
from app.pipeline.inventory import ClipInfo, scan_folder
from app.pipeline.normalize import normalize_clip
from app.pipeline.silence import STRENGTH, TIGHTEST_MIN_SILENCE, CutResult, jumpcut
from app.platform import PlatformProfile, get_profile
from app.project import Clip, Price, Product, Scene, Stage, Telop, create_project, save_project

ROOT = Path(__file__).resolve().parents[2]
MIN_USABLE = 2 * FADE + 0.2  # これより短く残ったクリップはつなぎ目に収まらないので使わない

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
    telop: TelopInputs | None = None,
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
    n = 8 if telop else 6
    step = iter(range(1, n + 1))
    say = lambda msg: progress(f"[{next(step)}/{n}] {msg}")  # noqa: E731

    say("素材を調べています")
    clips = scan_folder(profile, src_folder)
    if not clips:
        raise UserError("素材フォルダに動画が見つかりません。", "スマホで撮った動画ファイルを入れてください。")
    copy_into_project(src_folder, folder / "素材", clips)
    use = clips if keep_all else [c for c in clips if not c.excluded]
    if not use:
        raise UserError("使える動画がありませんでした。", "除外の理由を確認して、素材を入れ直してください。")

    say(f"形式をそろえています（{len(use)}本）")
    normalized = [normalize_clip(profile, c, work / "norm" / f"{i:03d}.mp4", fit=fit)  # type: ignore[arg-type]
                  for i, c in enumerate(use, 1)]

    say("話していない部分を詰めています")
    params = STRENGTH[strength]
    cuts = [jumpcut(profile, n, work / "cut" / n.name, params) for n in normalized]
    estimate = sum(r.kept for r in cuts) - FADE * (len(cuts) - 1)
    target = project.plan.target_seconds - (TITLE_SECONDS + END_SECONDS if telop else 0)
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

    say("クリップをつないでいます")
    joined = work / "joined.mp4"
    joined_seconds = concat_clips(profile, pieces, joined)
    main_video, title, telops, fixes = joined, "", [], []

    if telop:
        product = telop.product
        project.product = product
        project.plan.scenes = telop.scenes
        project.plan.price = telop.price
        project.plan.extra_text = telop.extra_text

        say("話した内容を文字にしています（初回は準備に数分かかります）")
        words = transcribe(profile, joined, hint_text(collect_terms(product)[:20] + [product.name]),
                           telop.transcriber)
        save_words(words, work / "words.json")
        fixer = TermFixer.for_product(product)
        transcript = []
        for ph in to_phrases(words):
            text, fx = fixer.fix(ph.text, f"{ph.start:.1f}秒")
            fixes += fx
            transcript.append({"start": ph.start, "end": ph.end, "text": text})
        (out / "字幕修正ログ.txt").write_text(log_text(fixes), encoding="utf-8")

        say("テロップを作っています")
        title, new = make_telops(telop.claude or ClaudeCode(), product, telop.scenes, transcript,
                                 joined_seconds, telop.price, telop.extra_text)
        telops = merge_manual(new, project.telops)
        ass = build_telop_ass(telops, work / "telops.ass", product.category, product.model_number)
        with_telops = burn_ass(profile, joined, ass, work / "telops.mp4")
        logo = telop.logo or find_logo()
        if logo is None:
            notes.append("ロゴが見つからなかったため、エンドカードは仮の無地にしました。assets/logo にロゴを入れると使われます。")
        title_card = make_title_card(profile, joined, title, product.model_number, work / "title.mp4",
                                     product.category, product.model_number)
        end_card = make_end_card(profile, work / "end.mp4", logo)
        main_video = work / "with_cards.mp4"
        concat_clips(profile, [title_card, with_telops, end_card], main_video)
        project.title, project.telops = title, telops

    say("BGM を重ねています")
    if bgm_path is None:
        found = bgm_step.find_bgm_files(ROOT / "assets" / "bgm")
        bgm_path = found[0] if found else None
    if bgm_path is None:
        notes.append("BGM が見つからなかったため、BGM なしで書き出しました。assets/bgm に曲を入れると使われます。")
    mixed = bgm_step.mix_bgm(profile, main_video, bgm_path, work / "mixed.mp4", level=level)

    say("書き出しています")
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
    project.complete_stage(Stage.PLAN)
    project.complete_stage(Stage.MATERIAL)
    save_project(folder, project)

    report = RunReport(folder, clips, cuts, joined_seconds, exports, bgm_path, notes, title, telops, fixes)
    (out / "レポート.txt").write_text(report.text(), encoding="utf-8")
    return report


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
    return TelopInputs(product=product, price=parse_price(args.price), extra_text=args.extra, claude=cc)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="素材フォルダから完成動画を作ります。")
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
    ap.add_argument("--no-telop", action="store_true", help="テロップ・タイトル・エンドカードを付けない")
    ap.add_argument("--paste", type=Path, help="商品ページから写した文章のファイル")
    ap.add_argument("--image", type=Path, nargs="+", help="商品ページのスクリーンショット")
    ap.add_argument("--price", default="hidden", help="価格: hidden（表示しない）/ open（オープン価格）/ 金額（例 248780）")
    ap.add_argument("--extra", default="", help="追加で入れたい情報（例「展示品あり」）")
    args = ap.parse_args(argv)

    outputs = [o.strip() for o in args.out.split(",") if o.strip()]
    if unknown := [o for o in outputs if o not in TARGETS]:
        print(f"書き出し先「{'、'.join(unknown)}」は使えません。store か youtube を指定してください。")
        return 2
    try:
        if not args.folder.is_dir():  # 商品情報を調べる前に確かめる
            raise UserError("素材フォルダが見つかりません。", "フォルダの場所を確認してください。")
        telop = None if args.no_telop else prepare_telop(args)
        report = edit_folder(args.folder, args.model, projects_root=args.projects, bgm_path=args.bgm,
                             outputs=outputs, strength=args.strength, level=args.level,
                             fit=args.fit, keep_all=args.keep_all, target_seconds=args.target, telop=telop)
    except UserError as e:
        print(f"\n{e.what}\n{e.how}")
        return 1
    print("\n" + report.text())
    print(f"\n完成した動画: {report.project_dir / 'output'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
