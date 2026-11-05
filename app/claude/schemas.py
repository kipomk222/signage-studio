"""Claude Code から受け取る JSON の形と、形以外のルール（文字数・合計秒数など）。"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict

TELOP_MAX_CHARS = 20
TELOP_MAX_LINES = 2
TITLE_SECONDS, END_CARD_SECONDS = 3, 5  # 冒頭タイトルとエンドカードは台本の外
PLAN_TOLERANCE = 0.15
PASTED = "貼り付け"  # 商品ページから貼り付けた内容が根拠のとき

Category = Literal["パソコン", "キッチン家電", "生活家電", "AV機器", "カメラ", "スマホ・タブレット", "その他"]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


# --- 商品調査 -------------------------------------------------------------

class Spec(_Strict):
    label: str
    value: str


class ProductInfo(_Strict):
    found: bool          # 型番の商品を特定できたか
    name: str
    maker: str
    category: Category
    specs: list[Spec]
    features: list[str]  # お客様にとっての良さ（3〜5点）
    sources: list[str]   # 参照した URL
    note: str            # 不確かな点など


def check_product(p: ProductInfo, need_urls: bool = True) -> list[str]:
    if not p.found:
        return []
    problems = []
    if not 3 <= len(p.features) <= 5:
        problems.append(f"features は3〜5点にしてください（今は{len(p.features)}点）。")
    if len(p.specs) < 3:
        problems.append("specs は主な仕様を3つ以上入れてください。")
    if need_urls and (not p.sources or not all(s.startswith(("http://", "https://")) for s in p.sources)):
        problems.append("sources には実際に参照したページの URL を入れてください。")
    if not p.name.strip() or not p.maker.strip():
        problems.append("name と maker を入れてください。")
    return problems


def tidy_pasted_sources(p: ProductInfo) -> ProductInfo:
    """貼り付けが根拠のときは、URL として正しいものだけ残し、なければ「貼り付け」とする。"""
    urls = [s for s in p.sources if s.startswith(("http://", "https://"))]
    return p.model_copy(update={"sources": urls or [PASTED]})


# --- 企画 -----------------------------------------------------------------

class SceneOut(_Strict):
    title: str
    script: str
    shots: list[str]
    seconds: float


class PlanOut(_Strict):
    scenes: list[SceneOut]


def scenes_budget(target_seconds: int) -> float:
    """台本の場面に使える秒数（冒頭タイトルとエンドカードを除く）。"""
    return target_seconds - TITLE_SECONDS - END_CARD_SECONDS


def make_plan_check(target_seconds: int):
    budget = scenes_budget(target_seconds)

    def check(p: PlanOut) -> list[str]:
        problems = []
        total = sum(s.seconds for s in p.scenes)
        if not budget * (1 - PLAN_TOLERANCE) <= total <= budget * (1 + PLAN_TOLERANCE):
            problems.append(f"場面の秒数の合計を {budget:.0f} 秒前後にしてください（今は {total:.0f} 秒）。")
        if len(p.scenes) < 4:
            problems.append("場面は4つ以上にしてください。")
        for i, s in enumerate(p.scenes, 1):
            if s.seconds <= 0 or not s.script.strip() or not s.shots:
                problems.append(f"場面{i}に秒数・台本・撮るカットを入れてください。")
        return problems

    return check


# --- テロップ ---------------------------------------------------------------

class TelopOut(_Strict):
    start: float
    end: float
    text: str  # 2行にする場合は改行（\n）で区切る


class TelopsOut(_Strict):
    title: str  # 冒頭タイトル（型番を含まない短い見出し）
    telops: list[TelopOut]


TITLE_MAX_CHARS = 14


def telop_length(text: str) -> int:
    return len(text.replace("**", "").replace("\n", ""))


def make_telop_check(duration: float, model_number: str = ""):
    def check(t: TelopsOut) -> list[str]:
        problems = []
        if not t.title.strip() or telop_length(t.title) > TITLE_MAX_CHARS:
            problems.append(f"title は {TITLE_MAX_CHARS} 文字以内の見出しにしてください。")
        squash = lambda x: x.replace("-", "").replace(" ", "").lower()  # noqa: E731
        if model_number and squash(model_number) in squash(t.title):
            problems.append(f"title には型番（{model_number}）を入れないでください。")
        if not t.telops:
            return problems + ["テロップを1つ以上作ってください。"]
        prev_end = 0.0
        for i, x in enumerate(sorted(t.telops, key=lambda x: x.start), 1):
            lines = x.text.split("\n")
            if x.text.count("**") % 2:
                problems.append(f"{i}つ目「{x.text}」の ** が閉じていません。")
            if telop_length(x.text) > TELOP_MAX_CHARS:
                problems.append(f"{i}つ目「{x.text}」が {TELOP_MAX_CHARS} 文字を超えています。")
            if len(lines) > TELOP_MAX_LINES:
                problems.append(f"{i}つ目「{x.text}」は {TELOP_MAX_LINES} 行以内にしてください。")
            if not 0 <= x.start < x.end <= duration + 0.01:
                problems.append(f"{i}つ目の時刻（{x.start}〜{x.end}秒）が動画の範囲（0〜{duration:.1f}秒）外です。")
            if x.start < prev_end - 0.01:
                problems.append(f"{i}つ目が前のテロップと重なっています。")
            prev_end = max(prev_end, x.end)
        return problems

    return check


# --- YouTube 説明文 ---------------------------------------------------------

class YoutubeOut(_Strict):
    title: str
    description: str


def check_youtube(y: YoutubeOut) -> list[str]:
    problems = []
    if len(y.title) > 100:
        problems.append("title は100文字以内にしてください。")
    if len(y.description) > 4500:
        problems.append("description は4500文字以内にしてください。")
    return problems
