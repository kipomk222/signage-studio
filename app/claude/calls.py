"""Claude Code への4つの依頼: 商品調査・企画・テロップ・YouTube 説明文。"""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime
from pathlib import Path

from app.claude.images import IMAGE_EXTS, prepare_images
from app.claude.runner import ROOT, ClaudeCode, load_prompt
from app.errors import UserError
from app.claude.schemas import (
    TELOP_MAX_CHARS, PlanOut, ProductInfo, TelopsOut, YoutubeOut,
    check_product, check_youtube, tidy_pasted_sources, make_plan_check, make_telop_check, scenes_budget,
)
from app.project import Price, Product, Scene, Telop, safe_folder_part

CACHE_DIR = ROOT / "cache" / "products"
RESEARCH_TOOLS = ["WebSearch", "WebFetch"]
RESEARCH_TIMEOUT = 420
PASTE_MAX_CHARS = 60_000
WRITE_TIMEOUT = 240


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
                  tools=RESEARCH_TOOLS, timeout=RESEARCH_TIMEOUT, check=check_product)
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
    os.replace(tmp, path)


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
                      check=lambda p: check_product(p, need_urls=False))
    info = tidy_pasted_sources(info)
    _save_cache(_cache_path(cache_dir, model_number), model_number, info, "paste")
    return _to_product(model_number, info), info


# --- 企画 ---------------------------------------------------------------------

def make_plan(cc: ClaudeCode, product: Product, target_seconds: int, price: Price,
              extra_text: str = "") -> list[Scene]:
    budget = scenes_budget(target_seconds)
    prompt = load_prompt("plan", product=product_text(product), budget_seconds=f"{budget:.0f}",
                         price=price_text(price), extra=extra_text.strip() or "なし")
    out = cc.ask(prompt, PlanOut, timeout=WRITE_TIMEOUT, check=make_plan_check(target_seconds))
    return [Scene(id=f"s{i}", title=s.title, script=s.script, shots=s.shots, seconds=s.seconds)
            for i, s in enumerate(out.scenes, 1)]


# --- テロップ -------------------------------------------------------------------

def make_telops(cc: ClaudeCode, product: Product, scenes: list[Scene], transcript: list[dict],
                duration: float, price: Price, extra_text: str = "") -> list[Telop]:
    """transcript: [{"start": 秒, "end": 秒, "text": "..."}]（編集後の動画の時刻）"""
    lines = "\n".join(f"{t['start']:.1f}-{t['end']:.1f}: {t['text']}" for t in transcript) or "（なし）"
    prompt = load_prompt("telop", product=product_text(product), script=scenes_text(scenes),
                         transcript=lines, duration=f"{duration:.1f}", price=price_text(price),
                         extra=extra_text.strip() or "なし", max_chars=str(TELOP_MAX_CHARS))
    out = cc.ask(prompt, TelopsOut, timeout=WRITE_TIMEOUT, check=make_telop_check(duration))
    return [Telop(start=t.start, end=t.end, text=t.text) for t in sorted(out.telops, key=lambda t: t.start)]


# --- YouTube 説明文 -------------------------------------------------------------

def youtube_text(cc: ClaudeCode, product: Product, telops: list[Telop], price: Price) -> YoutubeOut:
    lines = "\n".join(f"{t.start:.0f}秒: {t.text.replace(chr(10), ' ')}" for t in telops) or "（なし）"
    prompt = load_prompt("youtube", product=product_text(product), telops=lines, price=price_text(price))
    return cc.ask(prompt, YoutubeOut, timeout=WRITE_TIMEOUT, check=check_youtube)
