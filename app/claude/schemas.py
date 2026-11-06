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
    voice: Literal["talk", "narration"]   # 顔出しでその場で話す／後から声を録る
    points: str                           # この場面で伝えること
    script: str                           # 話すセリフ（ナレーションの場面は、映像の完成後に作るので空）
    shots: list[str]
    seconds: float


class PlanOut(_Strict):
    scenes: list[SceneOut]


def scenes_budget(target_seconds: int) -> float:
    """台本の場面に使える秒数（冒頭タイトルとエンドカードを除く）。"""
    return target_seconds - TITLE_SECONDS - END_CARD_SECONDS


def make_plan_check(target_seconds: int, separate: bool = False):
    budget = scenes_budget(target_seconds)

    def check(p: PlanOut) -> list[str]:
        problems = []
        total = sum(s.seconds for s in p.scenes)
        if not budget * (1 - PLAN_TOLERANCE) <= total <= budget * (1 + PLAN_TOLERANCE):
            problems.append(f"場面の秒数の合計を {budget:.0f} 秒前後にしてください（今は {total:.0f} 秒）。")
        if len(p.scenes) < (5 if separate else 4):
            problems.append(f"場面は{5 if separate else 4}つ以上にしてください。")
        if not separate:
            for i, s in enumerate(p.scenes, 1):
                if s.seconds <= 0 or not s.script.strip() or not s.shots:
                    problems.append(f"場面{i}に秒数・台本・撮るカットを入れてください。")
            return problems
        if p.scenes and (p.scenes[0].voice != "talk" or p.scenes[-1].voice != "talk"):
            problems.append('最初（あいさつ）と最後（店舗案内）の場面は voice="talk" にしてください。')
        if sum(s.voice == "talk" for s in p.scenes) > 2:
            problems.append('voice="talk" は最初と最後の2場面だけにしてください。')
        for i, s in enumerate(p.scenes, 1):
            if s.seconds <= 0 or not s.shots or not s.points.strip():
                problems.append(f"場面{i}に秒数・伝えること・撮るカットを入れてください。")
            if s.voice == "talk" and not s.script.strip():
                problems.append(f"場面{i}（顔出しで話す場面）にセリフを入れてください。")
            if s.voice == "narration" and len(s.shots) < 2:
                problems.append(f"場面{i}の撮るカットを3〜5個にしてください。")
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
        squash = lambda x: x.replace("-", "").replace(" ", "").lower()
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


COVER_MIN = 0.4     # 説明の部分のうち、商品の映像で覆われていてほしい最低の割合（1回だけ指摘）
PIECE_PAD = 0.3     # 部分ごとに残る前後の間（秒）の見込み

Phrases = dict[str, list[tuple[float, float, str]]]


def spoken_seconds(clip: str, start: float, end: float, phrases: Phrases | None) -> float:
    """その部分で話している秒数の見込み（中の長い間はあとで詰めるため）。文字起こしがなければ長さそのもの。"""
    if phrases is None or clip not in phrases:
        return max(0.0, end - start)
    talk = sum(max(0.0, min(b, end) - max(a, start)) for a, b, _ in phrases[clip])
    return min(end - start, talk + PIECE_PAD) if talk else min(end - start, PIECE_PAD)


def covered_ratio(e: AiEdit) -> float | None:
    """最初と最後を除いた説明の部分のうち、差し込みで覆われた割合。説明の部分がなければ None。"""
    middle = range(1, len(e.pieces) - 1)
    total = sum(e.pieces[i].end - e.pieces[i].start for i in middle)
    if total <= 0:
        return None
    covered = 0.0
    for i in middle:
        length = e.pieces[i].end - e.pieces[i].start
        spans = sorted((max(0.0, x.offset), min(length, x.offset + x.end - x.start))
                       for x in e.inserts if x.over_piece == i)
        reach = 0.0
        for a, b in spans:  # 重なりは1回だけ数える
            a = max(a, reach)
            if b > a:
                covered += b - a
                reach = b
    return covered / total


def make_edit_check(durations: dict[str, float], scene_ids: set[str], budget: float,
                    phrases: Phrases | None = None):
    """形・時刻の誤りは毎回差し戻す。商品の映像の使い方と場面の抜けは、1回目だけ指摘する。"""
    nudged = {"done": False}

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
        total = sum(spoken_seconds(p.clip, p.start, p.end, phrases) for p in e.pieces)
        if total > budget * EDIT_OVER:
            problems.append(f"話している時間の合計が約 {total:.0f} 秒で、目標（{budget:.0f} 秒）を大きくこえています。"
                            "言い直しや余計な話を切って短くしてください（場面は丸ごと落とさない）。")
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
        if not nudged["done"]:
            nudged["done"] = True
            problems += _soft_problems(e, durations, scene_ids)
        return problems

    return check


def _soft_problems(e: AiEdit, durations: dict[str, float], scene_ids: set[str]) -> list[str]:
    out = []
    used_scenes = {p.scene_id for p in e.pieces}
    if missing := sorted(scene_ids - used_scenes):
        out.append(f"台本の場面 {', '.join(missing)} が入っていません。素材がある場面は、短くしてでも入れてください"
                   "（素材がないときだけ省き、summary にそう書く）。")
    used = {p.clip for p in e.pieces} | {x.clip for x in e.inserts} | {d.clip for d in e.dropped}
    unused = sorted(set(durations) - used)
    ratio = covered_ratio(e)
    if ratio is not None and ratio < COVER_MIN and unused:
        out.append(f"説明の部分のうち、商品の映像で覆われているのは約 {ratio * 100:.0f}% です。主役は商品なので、"
                   f"まだ使っていない映像（{', '.join(unused[:12])}）から、話している内容に合うものを差し込みで重ねてください"
                   "（合う映像がない説明はそのままでよい）。")
    return out


# --- 映像を先に作る（声は別録り）----------------------------------------------------

class AiCut(_Strict):
    clip: str
    start: float
    end: float
    scene_id: str
    talk: bool       # 顔出しでその場で話しているカット（音を使う）
    shows: str       # 映っているもの（例「ペンで画面に書く手元」）
    reason: str


class AiVideoEdit(_Strict):
    summary: str
    cuts: list[AiCut]      # 並べる順
    spares: list[AiCut]    # 予備のカット（声が長くて映像が足りないときに足す）
    dropped: list[AiDrop]  # 使わなかった話している部分（言い直しなど）


CUT_MIN, CUT_MAX = 1.0, 8.0
SCENE_FIT = (0.6, 1.5)   # 説明の場面の映像の長さ ÷ 予定の秒数（1回だけ指摘）


def make_video_check(durations: dict[str, float], scenes: list[tuple[str, str, float]]):
    """scenes: (場面ID, "talk"/"narration", 予定の秒数) を並べる順に。"""
    order = {sid: i for i, (sid, _, _) in enumerate(scenes)}
    voice = {sid: v for sid, v, _ in scenes}
    nudged = {"done": False}

    def cut_problems(label: str, i: int, c: AiCut) -> list[str]:
        out = []
        d = durations.get(c.clip)
        if d is None:
            return [f"{label}[{i}] の clip「{c.clip}」はありません。一覧のファイル名を使ってください。"]
        if not 0 <= c.start < c.end <= d + 0.05:
            out.append(f"{label}[{i}] の時刻 {c.start}〜{c.end} が「{c.clip}」の長さ（{d:.1f}秒）の範囲外です。")
        if c.scene_id not in order:
            out.append(f"{label}[{i}] の scene_id「{c.scene_id}」は構成にありません。")
        elif c.talk and voice[c.scene_id] != "talk":
            out.append(f"{label}[{i}] は話しているカットですが、場面 {c.scene_id} は声をあとで録る場面です。映像だけのカットにしてください。")
        elif not c.talk and not CUT_MIN <= c.end - c.start <= CUT_MAX:
            out.append(f"{label}[{i}] の長さは {CUT_MIN:.0f}〜{CUT_MAX:.0f} 秒にしてください。")
        return out

    def check(e: AiVideoEdit) -> list[str]:
        problems: list[str] = []
        if not e.cuts:
            return ["cuts が空です。並べるカットを選んでください。"]
        for i, c in enumerate(e.cuts):
            problems += cut_problems("cuts", i, c)
        for i, c in enumerate(e.spares):
            problems += cut_problems("spares", i, c)
        seq = [order.get(c.scene_id, -1) for c in e.cuts]
        if seq != sorted(seq):
            problems.append("cuts は構成の場面の順に並べてください。")
        if problems or nudged["done"]:
            return problems
        nudged["done"] = True
        soft = []
        have = {c.scene_id for c in e.cuts}
        if missing := [sid for sid, _, _ in scenes if sid not in have]:
            soft.append(f"場面 {', '.join(missing)} にカットがありません。合う素材があれば入れてください"
                        "（素材がないときだけ省き、summary にそう書く）。")
        for sid, v, sec in scenes:
            if v != "narration" or sid not in have or sec <= 0:
                continue
            total = sum(c.end - c.start for c in e.cuts if c.scene_id == sid)
            lo, hi = SCENE_FIT
            if not lo * sec <= total <= hi * sec:
                soft.append(f"場面 {sid} の映像が {total:.0f} 秒です。予定の {sec:.0f} 秒に近づけてください"
                            "（カットの数や長さで調整）。")
        if not e.spares:
            soft.append("spares（予備のカット）がありません。声が長くなったときに足せるよう、使わなかった映像から選んでください。")
        return soft

    return check


# --- ナレーションの台本 --------------------------------------------------------------

class NarrationLine(_Strict):
    scene_id: str
    script: str


class NarrationOut(_Strict):
    scenes: list[NarrationLine]


CHARS_PER_SEC = (4.0, 6.5)   # 読む速さの目安（1秒あたりの文字数）
VOICE_MARGIN = 0.8           # 声の前後に空ける間（秒）


def script_chars(text: str) -> int:
    return len([c for c in text if not c.isspace() and c not in "、。，．・「」『』！？!?…ー―-"])


def make_narration_check(seconds: dict[str, float]):
    nudged = {"done": False}

    def check(o: NarrationOut) -> list[str]:
        got = {x.scene_id: x for x in o.scenes}
        problems = [f"場面 {sid} の台本がありません。" for sid in seconds if sid not in got or not got[sid].script.strip()]
        problems += [f"scene_id「{x.scene_id}」は台本を作る場面ではありません。" for x in o.scenes if x.scene_id not in seconds]
        if problems or nudged["done"]:
            return problems
        nudged["done"] = True
        lo, hi = CHARS_PER_SEC
        for sid, sec in seconds.items():
            n, room = script_chars(got[sid].script), max(1.0, sec - VOICE_MARGIN)
            if not lo * room <= n <= hi * room:
                problems.append(f"場面 {sid} の台本が {n} 文字です。映像 {sec:.1f} 秒に合うよう、"
                                f"{lo * room:.0f}〜{hi * room:.0f} 文字にしてください（1秒あたり5〜6文字が目安）。")
        return problems

    return check


# --- 録った声の振り分け --------------------------------------------------------------

class VoiceAssign(_Strict):
    file: str
    scene_id: str    # どの場面の声か（どれにも当たらなければ空文字）
    reason: str


class AssignOut(_Strict):
    assignments: list[VoiceAssign]


def make_assign_check(files: set[str], scene_ids: set[str]):
    def check(o: AssignOut) -> list[str]:
        problems = []
        got = {a.file for a in o.assignments}
        problems += [f"ファイル「{f}」の振り分けがありません。" for f in sorted(files - got)]
        problems += [f"ファイル「{a.file}」は一覧にありません。" for a in o.assignments if a.file not in files]
        problems += [f"「{a.file}」の scene_id「{a.scene_id}」は声を録る場面ではありません。"
                     for a in o.assignments if a.scene_id and a.scene_id not in scene_ids]
        return problems

    return check


# --- 相談 ---------------------------------------------------------------------

ConsultAction = Literal[
    "scene_update", "scene_add", "scene_delete", "plan_settings",
    "piece_update", "piece_move", "insert_update", "drop_restore",
    "telop_update", "telop_add", "telop_delete", "title", "style", "bgm", "voice_assign",
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
