"""Claude Code への4つの依頼: 商品調査・企画・テロップ・YouTube 説明文。"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from app.claude.images import IMAGE_EXTS, prepare_images
from app.claude.runner import ROOT, ClaudeCode, load_prompt
from app.claude.schemas import (
    TELOP_MAX_CHARS,
    TITLE_MAX_CHARS,
    AiEdit,
    AiVideoEdit,
    TelopCheckOut,
    PlanOut,
    ProductInfo,
    TelopsOut,
    YoutubeOut,
    check_product,
    check_youtube,
    make_edit_check,
    make_plan_check,
    make_telop_check,
    scenes_budget,
    tidy_pasted_sources,
)
from app.errors import UserError
from app.project import Price, Product, Scene, Telop, safe_folder_part
from app.fileops import replace_file

CACHE_DIR = ROOT / "cache" / "products"
RESEARCH_TOOLS = ["WebSearch", "WebFetch"]
RESEARCH_TIMEOUT = 420
PASTE_MAX_CHARS = 60_000
WRITE_TIMEOUT = 240
EDIT_TIMEOUT = 900


# --- 文章に直す ---------------------------------------------------------------

def price_text(price: Price) -> str:
    if price.mode == "open":
        return "「オープン価格」と表示する"
    if price.mode == "amount" and price.amount:
        return f"「{price.amount:,}円（税込）」と表示する"
    return "価格は表示しない（価格に触れない）"


def product_text(p: Product) -> str:
    lines = [f"型番: {p.model_number}", f"商品名: {p.name}", f"メーカー: {p.maker}", f"カテゴリ: {p.category}"]
    if p.specs:
        lines.append("主な仕様: " + " / ".join(f"{k}: {v}" for k, v in p.specs.items()))
    if p.features:
        lines.append("特徴:\n" + "\n".join(f"- {f}" for f in p.features))
    return "\n".join(lines)


def scenes_text(scenes: list[Scene]) -> str:
    return "\n".join(f"【{s.title}】{s.script}" for s in scenes) or "（台本なし）"


# --- 商品調査 ----------------------------------------------------------------

def _cache_path(cache_dir: Path, model_number: str) -> Path:
    return Path(cache_dir) / f"{safe_folder_part(model_number)}.json"


def _to_product(model_number: str, info: ProductInfo) -> Product:
    return Product(
        model_number=model_number, name=info.name, maker=info.maker, category=info.category,
        specs={s.label: s.value for s in info.specs}, features=info.features,
        sources=info.sources, confirmed=False,
    )


def research_product(cc: ClaudeCode, model_number: str, cache_dir: Path = CACHE_DIR,
                     refresh: bool = False) -> tuple[Product, ProductInfo]:
    """型番から商品情報を調べる。一度調べた型番は保存したものを使う（refresh=True で調べ直す）。"""
    model_number = model_number.strip()
    path = _cache_path(cache_dir, model_number)
    if path.exists() and not refresh:
        info = ProductInfo.model_validate(json.loads(path.read_text(encoding="utf-8"))["info"])
        return _to_product(model_number, info), info

    info = cc.ask(load_prompt("research", model_number=model_number), ProductInfo,
                  tools=RESEARCH_TOOLS, timeout=RESEARCH_TIMEOUT, check=check_product, kind="research")
    _save_cache(path, model_number, info, "web")
    return _to_product(model_number, info), info


def _save_cache(path: Path, model_number: str, info: ProductInfo, method: str) -> None:
    if not info.found:  # 見つからなかった結果は保存しない（次回また調べる）
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"model_number": model_number, "saved_at": datetime.now().isoformat(timespec="seconds"),
               "method": method, "info": info.model_dump()}
    fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    replace_file(tmp, path)


def product_from_paste(cc: ClaudeCode, model_number: str, text: str = "", images: list[Path] | None = None,
                       cache_dir: Path = CACHE_DIR) -> tuple[Product, ProductInfo]:
    """商品ページから貼り付けた文章・スクリーンショットから商品情報を整理する（Web は使わない）。"""
    model_number = model_number.strip()
    text = text.strip()
    images = [Path(p) for p in images or []]
    if not text and not images:
        raise UserError("商品ページの内容が入っていません。",
                        "商品ページの文章を貼り付けるか、スクリーンショットを入れてください。")
    if bad := [p.name for p in images if p.suffix.lower() not in IMAGE_EXTS]:
        raise UserError(f"「{'、'.join(bad)}」は画像として読めません。", "PNG か JPEG の画像を入れてください。")
    if len(text) > PASTE_MAX_CHARS:
        text = text[:PASTE_MAX_CHARS]

    with tempfile.TemporaryDirectory() as tmp:
        tiles = prepare_images(images, Path(tmp)) if images else []
        parts = []
        if text:
            parts.append("## 貼り付けた文章\n<<<\n" + text + "\n>>>")
        if tiles:
            names = "\n".join(f"- {t.name}" for t in tiles)
            parts.append("## スクリーンショット\n作業フォルダにある次の画像を、上から順にすべて Read で開いて読んでください"
                         "（1枚の縦長の画面を、少し重ねて分割したものです）。\n" + names)
        prompt = load_prompt("extract", model_number=model_number, source="\n\n".join(parts))
        info = cc.ask(prompt, ProductInfo, tools=["Read"] if tiles else [], files=tiles,
                      timeout=RESEARCH_TIMEOUT if tiles else WRITE_TIMEOUT,
                      check=lambda p: check_product(p, need_urls=False), kind="extract")
    info = tidy_pasted_sources(info)
    _save_cache(_cache_path(cache_dir, model_number), model_number, info, "paste")
    return _to_product(model_number, info), info


# --- 企画 ---------------------------------------------------------------------

def make_plan(cc: ClaudeCode, product: Product, target_seconds: int, price: Price,
              extra_text: str = "", separate: bool = True) -> list[Scene]:
    """構成を作る。separate=True（声は別録り）なら、説明の場面は「伝えること」と「撮るカット」だけ。"""
    budget = scenes_budget(target_seconds)
    prompt = load_prompt("plan" if separate else "plan_live", product=product_text(product),
                         budget_seconds=f"{budget:.0f}", price=price_text(price), extra=extra_text.strip() or "なし")
    out = cc.ask(prompt, PlanOut, timeout=WRITE_TIMEOUT, check=make_plan_check(target_seconds, separate), kind="plan")
    return [Scene(id=f"s{i}", title=s.title, script=s.script.strip() if (s.voice == "talk" or not separate) else "",
                  shots=s.shots, seconds=s.seconds, voice=s.voice if separate else "talk", points=s.points.strip())
            for i, s in enumerate(out.scenes, 1)]


# --- テロップ -------------------------------------------------------------------

def visual_text(visual: list[tuple[float, float, str]] | None) -> str:
    return "\n".join(f"{a:.1f}-{b:.1f}: {desc}" for a, b, desc in visual or []) or "（分かりません）"


def make_telops(cc: ClaudeCode, product: Product, scenes: list[Scene], transcript: list[dict],
                duration: float, price: Price, extra_text: str = "",
                visual: list[tuple[float, float, str]] | None = None) -> tuple[str, list[Telop]]:
    """冒頭タイトルと要点テロップを作る。

    transcript: [{"start": 秒, "end": 秒, "text": "..."}]（編集後の動画の時刻）
    visual: [(始まり, 終わり, 映っているもの)]（同じ時刻。テロップを映像に合わせるため）
    """
    lines = "\n".join(f"{t['start']:.1f}-{t['end']:.1f}: {t['text']}" for t in transcript) or "（なし）"
    prompt = load_prompt("telop", product=product_text(product), script=scenes_text(scenes), visual=visual_text(visual),
                         transcript=lines, duration=f"{duration:.1f}", price=price_text(price),
                         extra=extra_text.strip() or "なし", max_chars=str(TELOP_MAX_CHARS),
                         title_max=str(TITLE_MAX_CHARS))
    out = cc.ask(prompt, TelopsOut, timeout=WRITE_TIMEOUT, check=make_telop_check(duration, product.model_number), kind="telop")
    telops = [Telop(start=t.start, end=t.end, text=t.text) for t in sorted(out.telops, key=lambda t: t.start)]
    return out.title.strip(), telops


def merge_manual(new: list[Telop], old: list[Telop]) -> list[Telop]:
    """作り直したテロップに、手で直したテロップを残す（重なる自動テロップは外す）。"""
    manual = [t for t in old if t.manual]
    kept = [t for t in new if not any(t.start < m.end and m.start < t.end for m in manual)]
    return sorted(kept + manual, key=lambda t: t.start)


# --- YouTube 説明文 -------------------------------------------------------------

def youtube_text(cc: ClaudeCode, product: Product, telops: list[Telop], price: Price) -> YoutubeOut:
    lines = "\n".join(f"{t.start:.0f}秒: {t.text.replace(chr(10), ' ')}" for t in telops) or "（なし）"
    prompt = load_prompt("youtube", product=product_text(product), telops=lines, price=price_text(price))
    return cc.ask(prompt, YoutubeOut, timeout=WRITE_TIMEOUT, check=check_youtube, kind="youtube")



# --- 編集の判断 -------------------------------------------------------------------

@dataclass
class ClipBrief:
    """Claude Code に渡す、クリップ1本分の中身。"""

    file: str
    duration: float
    phrases: list[tuple[float, float, str]]   # (始まり, 終わり, 話した内容)
    sheet: Path | None = None


def clips_text(clips: list[ClipBrief]) -> str:
    out = []
    for c in clips:
        if not c.phrases:
            head = f"## {c.file}（{c.duration:.1f}秒）"
            out.append(head + "\n（話し声なし）")
            continue
        talk = sum(b - a for a, b, _ in c.phrases)
        head = f"## {c.file}（{c.duration:.1f}秒・話している秒数 約{talk:.1f}秒）"
        lines = "\n".join(f"{a:.2f}-{b:.2f}: {t}" for a, b, t in c.phrases)
        out.append(f"{head}\n{lines}")
    return "\n\n".join(out)


def decide_edit(cc: ClaudeCode, product: Product, scenes: list[Scene], budget: float,
                clips: list[ClipBrief]) -> AiEdit:
    """素材の中身（話した内容・映像の見本）から、編集の設計図を作ってもらう。"""
    scene_lines = "\n".join(f"- {s.id}「{s.title}」{s.seconds:.0f}秒: {s.script}" for s in scenes) or \
        "（台本なし。scene_id は空文字にする）"
    sheets = [c.sheet for c in clips if c.sheet and c.sheet.exists()]
    prompt = load_prompt("edit", product=product_text(product), scenes=scene_lines, budget=f"{budget:.0f}",
                         clips=clips_text(clips),
                         sheets="\n".join(f"- {p.name}（{p.stem.removeprefix('sheet_')}）" for p in sheets) or "（なし）")
    check = make_edit_check({c.file: c.duration for c in clips}, {s.id for s in scenes}, budget,
                            phrases={c.file: c.phrases for c in clips})
    return cc.ask(prompt, AiEdit, tools=["Read"] if sheets else [], files=sheets, timeout=EDIT_TIMEOUT,
                  check=check, kind="edit")


# --- 映像を先に作る（声は別録り）------------------------------------------------------

def plan_scenes_text(scenes: list[Scene]) -> str:
    out = []
    for s in scenes:
        kind = "talk（顔出しでその場で話す）" if s.voice == "talk" else "narration（映像だけ・声はあとで録る）"
        line = f"- {s.id}「{s.title}」{kind} 予定 {s.seconds:.0f}秒"
        if s.points:
            line += f"\n  伝えること: {s.points}"
        if s.voice == "talk" and s.script:
            line += f"\n  話すセリフ: {s.script}"
        if s.shots:
            line += "\n  撮るカット: " + " / ".join(s.shots)
        out.append(line)
    return "\n".join(out) or "（構成なし）"


def decide_video(cc: ClaudeCode, product: Product, scenes: list[Scene], clips: list[ClipBrief]) -> AiVideoEdit:
    """映像だけを先に組み立てる設計図を作ってもらう（声は別録り）。"""
    from app.claude.schemas import make_video_check
    sheets = [c.sheet for c in clips if c.sheet and c.sheet.exists()]
    prompt = load_prompt("edit_video", product=product_text(product), scenes=plan_scenes_text(scenes),
                         clips=clips_text(clips),
                         sheets="\n".join(f"- {p.name}（{p.stem.removeprefix('sheet_')}）" for p in sheets) or "（なし）")
    check = make_video_check({c.file: c.duration for c in clips}, [(s.id, s.voice, s.seconds) for s in scenes])
    return cc.ask(prompt, AiVideoEdit, tools=["Read"] if sheets else [], files=sheets, timeout=EDIT_TIMEOUT,
                  check=check, kind="edit")


@dataclass
class SceneCuts:
    """台本を作るときに渡す、完成した映像の1場面分。"""

    scene: Scene
    seconds: float
    cuts: list[tuple[float, float, str]]   # (場面の始まりからの秒, 終わり, 映っているもの)


def write_narration(cc: ClaudeCode, product: Product, scenes: list[SceneCuts], price: Price,
                    extra_text: str = "") -> dict[str, str]:
    """完成した映像の場面ごとの長さとカットに合わせて、ナレーションの台本を作る。{場面ID: 台本}"""
    from app.claude.schemas import NarrationOut, make_narration_check
    blocks = []
    for sc in scenes:
        cuts = "\n".join(f"  {a:.1f}〜{b:.1f}秒: {shows or '（説明なし）'}" for a, b, shows in sc.cuts)
        blocks.append(f"## {sc.scene.id}「{sc.scene.title}」映像 {sc.seconds:.1f}秒\n"
                      f"伝えること: {sc.scene.points or '（なし）'}\n{cuts}")
    prompt = load_prompt("narration", product=product_text(product), price=price_text(price),
                         extra=extra_text.strip() or "なし", scenes="\n\n".join(blocks))
    out = cc.ask(prompt, NarrationOut, timeout=WRITE_TIMEOUT, kind="narration",
                 check=make_narration_check({sc.scene.id: sc.seconds for sc in scenes}))
    return {x.scene_id: x.script.strip() for x in out.scenes}


def assign_voices(cc: ClaudeCode, scenes: list[Scene], files: list[tuple[str, float, str]]) -> dict[str, tuple[str, str]]:
    """録った声のファイルを、中身で場面に振り分けてもらう。files: (ファイル名, 秒, 文字起こし)。
    戻り値 {ファイル名: (場面ID または "", 理由)}"""
    from app.claude.schemas import AssignOut, make_assign_check
    scene_lines = "\n".join(f"- {s.id}「{s.title}」: {s.script or s.points}" for s in scenes)
    file_lines = "\n".join(f"- {name}（{sec:.1f}秒）: {text or '（聞き取れず）'}" for name, sec, text in files)
    out = cc.ask(load_prompt("assign", scenes=scene_lines, files=file_lines), AssignOut, timeout=WRITE_TIMEOUT,
                 kind="assign", check=make_assign_check({f for f, _, _ in files}, {s.id for s in scenes}))
    return {a.file: (a.scene_id, a.reason) for a in out.assignments}


# --- テロップと映像の照合 ---------------------------------------------------------------

def check_telops_call(cc: ClaudeCode, product: Product, telops: list[Telop], visual: list[tuple[float, float, str]],
                      duration: float, sheets: list[Path]) -> TelopCheckOut:
    """テロップと、その時刻に映っている映像が食い違っていないか確かめ、直し方を返してもらう。"""
    from app.claude.schemas import make_telop_fix_check
    lines = "\n".join(f"{i}. {t.start:.1f}-{t.end:.1f}: {t.text.replace(chr(10), '／')}"
                      + ("（手で直したテロップ）" if t.manual else "") for i, t in enumerate(telops))
    prompt = load_prompt("telop_check", product=product_text(product), telops=lines, visual=visual_text(visual),
                         duration=f"{duration:.1f}", max_chars=str(TELOP_MAX_CHARS),
                         sheets="\n".join(f"- {p.name}" for p in sheets) or "（なし）")
    return cc.ask(prompt, TelopCheckOut, tools=["Read"] if sheets else [], files=sheets, timeout=EDIT_TIMEOUT,
                  check=make_telop_fix_check(telops, duration), kind="telop_check")
