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


# --- 編集の判断 -----------------------------------------------------------------

class AiPiece(_Strict):
    clip: str        # ファイル名
    start: float     # 秒（そのクリップの中の時刻）
    end: float
    scene_id: str    # 台本の場面（場面がなければ空文字）
    reason: str


class AiInsert(_Strict):
    clip: str        # 差し込むクリップ
    start: float
    end: float
    over_piece: int  # pieces の何番目（0始まり）に重ねるか
    offset: float    # その部分の始まりから何秒後に重ねるか
    reason: str


class AiDrop(_Strict):
    clip: str
    start: float
    end: float
    reason: str


class AiEdit(_Strict):
    summary: str
    pieces: list[AiPiece]
    inserts: list[AiInsert]
    dropped: list[AiDrop]


EDIT_OVER = 1.2     # 目標の長さをこれ以上こえたらやり直し
INSERT_MIN, INSERT_MAX = 1.0, 8.0


def make_edit_check(durations: dict[str, float], scene_ids: set[str], budget: float):
    def check(e: AiEdit) -> list[str]:
        problems: list[str] = []
        if not e.pieces:
            return ["pieces が空です。使う部分を1つ以上選んでください。"]
        spans: dict[str, list[tuple[float, float]]] = {}
        for i, p in enumerate(e.pieces):
            d = durations.get(p.clip)
            if d is None:
                problems.append(f"pieces[{i}] の clip「{p.clip}」はありません。一覧のファイル名を使ってください。")
                continue
            if not (0 <= p.start < p.end <= d + 0.05):
                problems.append(f"pieces[{i}] の時刻 {p.start}〜{p.end} が「{p.clip}」の長さ（{d:.1f}秒）の範囲外です。")
            elif p.end - p.start < 0.8:
                problems.append(f"pieces[{i}] が短すぎます（0.8秒以上にしてください）。")
            if scene_ids and p.scene_id not in scene_ids:
                problems.append(f"pieces[{i}] の scene_id「{p.scene_id}」は台本にありません。")
            for s, t in spans.get(p.clip, []):
                if p.start < t and s < p.end:
                    problems.append(f"pieces[{i}] が、同じクリップの別の部分と重なっています。")
            spans.setdefault(p.clip, []).append((p.start, p.end))
        total = sum(max(0.0, p.end - p.start) for p in e.pieces)
        if total > budget * EDIT_OVER:
            problems.append(f"使う部分の合計が {total:.0f} 秒で、目標（{budget:.0f} 秒）を大きくこえています。削ってください。")
        for i, ins in enumerate(e.inserts):
            d = durations.get(ins.clip)
            if d is None:
                problems.append(f"inserts[{i}] の clip「{ins.clip}」はありません。")
                continue
            if not (0 <= ins.start < ins.end <= d + 0.05):
                problems.append(f"inserts[{i}] の時刻が「{ins.clip}」の範囲外です。")
            if not (INSERT_MIN <= ins.end - ins.start <= INSERT_MAX):
                problems.append(f"inserts[{i}] の長さは {INSERT_MIN:.0f}〜{INSERT_MAX:.0f} 秒にしてください。")
            if not 0 <= ins.over_piece < len(e.pieces):
                problems.append(f"inserts[{i}] の over_piece が pieces の範囲外です。")
                continue
            piece = e.pieces[ins.over_piece]
            if piece.clip == ins.clip:
                problems.append(f"inserts[{i}] は、重ねる部分と別のクリップにしてください。")
            if not 0 <= ins.offset < max(0.01, piece.end - piece.start):
                problems.append(f"inserts[{i}] の offset が、重ねる部分の長さをこえています。")
        return problems

    return check


# --- 相談 ---------------------------------------------------------------------

ConsultAction = Literal[
    "scene_update", "scene_add", "scene_delete", "plan_settings",
    "piece_update", "piece_move", "insert_update", "drop_restore",
    "telop_update", "telop_add", "telop_delete", "title", "style", "bgm",
]


class ChangeField(_Strict):
    name: str
    value: str


class ConsultChange(_Strict):
    action: ConsultAction
    index: int = -1
    fields: list[ChangeField] = []
    description: str          # 利用者に見せる「何をどう変えるか」


class ConsultOut(_Strict):
    reply: str
    changes: list[ConsultChange] = []
    remember: str = ""        # これからも守る好み（あれば）
    next_step: Literal["rebuild", "preview", "none"] = "none"
