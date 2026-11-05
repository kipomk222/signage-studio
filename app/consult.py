"""Claude への相談・指示（チャット）。

- Claude は動画の企画・素材・編集・テロップ・色を見たうえで返事をし、必要なら「変更案」を出す
- 変更案は、利用者が「反映する」を押したときだけ project.json に入れる
- 会話は動画ごとに projects/<id>/chat.json に保存し、出した指示は作業場所の履歴にも残す
"""

from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from app.claude.calls import WRITE_TIMEOUT, price_text, product_text
from app.claude.runner import load_prompt
from app.claude.schemas import ConsultChange, ConsultOut
from app.claude.usage import usage_scope
from app.claude.workspace import WORKSPACE, Workspace
from app.errors import UserError
from app.pipeline.colors import normalize_color
from app.pipeline.fonts import FONTS
from app.project import Price, Project, Scene, Stage, Telop, TelopStyle, load_project, save_project

CHAT_FILE = "chat.json"
HISTORY_TURNS = 10
MAX_MESSAGE = 4000
PAGE_LABELS = {"plan": "企画", "material": "素材", "review": "確認", "export": "書き出し"}
NEXT_LABELS = {"rebuild": "作り直し", "preview": "プレビュー", "none": ""}


class ChatEntry(BaseModel):
    role: Literal["user", "assistant"]
    text: str
    at: datetime = Field(default_factory=datetime.now)
    changes: list[ConsultChange] = Field(default_factory=list)
    remember: str = ""
    next_step: Literal["rebuild", "preview", "none"] = "none"
    status: Literal["", "pending", "applied", "declined"] = ""  # 変更案の扱い

    @property
    def has_proposal(self) -> bool:
        return bool(self.changes or self.remember)


# --- 会話の保存 -------------------------------------------------------------------

def chat_path(folder: Path) -> Path:
    return Path(folder) / CHAT_FILE


def load_chat(folder: Path) -> list[ChatEntry]:
    p = chat_path(folder)
    if not p.exists():
        return []
    try:
        return [ChatEntry.model_validate(x) for x in json.loads(p.read_text(encoding="utf-8"))]
    except (json.JSONDecodeError, ValueError):
        return []  # 壊れていたら新しく始める（動画の中身には影響しない）


def save_chat(folder: Path, chat: list[ChatEntry]) -> None:
    p = chat_path(folder)
    part = p.with_name(p.name + ".part")
    part.write_text(json.dumps([c.model_dump(mode="json") for c in chat], ensure_ascii=False, indent=1),
                    encoding="utf-8")
    os.replace(part, p)


# --- Claude に見せる、いまの動画の内容 ---------------------------------------------

def _clip_say(folder: Path, project: Project, clip: str, a: float, b: float) -> str:
    from app.workflow import spoken_text
    try:
        text = spoken_text(folder, project, clip, a, b)
    except (OSError, ValueError):
        text = ""
    return f"「{text[:80]}」" if text else "（話し声なし）"


def context_text(folder: Path, project: Project) -> str:
    p, plan, s = project.product, project.plan, project.settings
    out = ["## 商品", product_text(p) if p.name else f"型番: {p.model_number}（商品情報はまだありません）",
           "## 動画の条件",
           f"目標の長さ: {plan.target_seconds}秒 / 価格: {price_text(plan.price)} / "
           f"追加で入れたい情報: {plan.extra_text.strip() or 'なし'}",
           "## 台本の場面"]
    out += [f"{i}. 「{sc.title}」{sc.seconds:.0f}秒: {sc.script}" + (f"（撮るカット: {' / '.join(sc.shots)}）" if sc.shots else "")
            for i, sc in enumerate(plan.scenes)] or ["（まだありません）"]
    if project.clips:
        out.append("## 素材")
        out += [f"- {c.file}（{c.duration:.1f}秒" + ("・話し声なし" if c.silent else "") +
                ("・除外候補: " + c.exclude_reason if c.excluded else "") + "）" for c in project.clips]
    e = project.edit
    if e is not None:
        out.append("## 使う部分（この順につなぐ）")
        out += [f"{i}. {'' if x.enabled else '［使わない］'}{x.clip} {x.start:.1f}-{x.end:.1f}秒 "
                f"{_clip_say(folder, project, x.clip, x.start, x.end)}" for i, x in enumerate(e.pieces)]
        if e.inserts:
            out.append("## 差し込み")
            out += [f"{i}. {'' if x.enabled else '［使わない］'}{x.clip} {x.start:.1f}-{x.end:.1f}秒 → 部分{x.piece}に重ねる"
                    f"（{x.reason}）" for i, x in enumerate(e.inserts)]
        if e.dropped:
            out.append("## 切った部分")
            out += [f"{i}. {x.clip} {x.start:.1f}-{x.end:.1f}秒（{x.reason}）"
                    f"{_clip_say(folder, project, x.clip, x.start, x.end)}" for i, x in enumerate(e.dropped)]
    out.append(f"## 冒頭タイトル\n{project.title or '（まだありません）'}")
    out.append("## テロップ（編集後の動画の秒。冒頭タイトルの分は含まない）")
    out += [f"{i}. {t.start:.1f}-{t.end:.1f}: {t.text.replace(chr(10), '／')}"
            + (f"（フォント {t.font}）" if t.font else "")
            + "".join(f"（{lbl} {c}）" for lbl, c in (("文字", t.text_color), ("縁取り", t.outline_color),
                                                    ("強調", t.emphasis_color)) if c)
            for i, t in enumerate(project.telops)] or ["（まだありません）"]
    st = s.telop_style
    out.append(f"## テロップ全体の色\n文字 {st.text_color} / 縁取り {st.outline_color} / 強調 {st.emphasis_color} / "
               f"帯 {st.band_color or 'なし'}（濃さ {st.band_opacity}）")
    out.append(f"## BGM\n音量 {({'small': '小', 'medium': '中', 'large': '大'})[s.bgm_level]}")
    return "\n".join(out)


def history_text(chat: list[ChatEntry]) -> str:
    lines = []
    for c in chat[-HISTORY_TURNS:]:
        who = "利用者" if c.role == "user" else "あなた"
        line = f"{who}: {c.text}"
        if c.changes:
            state = {"applied": "反映済み", "declined": "反映しなかった", "pending": "まだ反映していない"}.get(c.status, "")
            line += f"\n  （変更案 {len(c.changes)}件: {state}）"
        lines.append(line)
    return "\n".join(lines) or "（はじめての相談です）"


# --- 変更案を当てはめる ---------------------------------------------------------------

class _Problem(ValueError):
    pass


def _fields(ch: ConsultChange, allowed: set[str]) -> dict[str, str]:
    got = {f.name: f.value for f in ch.fields}
    if bad := set(got) - allowed:
        raise _Problem(f"{ch.action} に使えない項目があります: {', '.join(sorted(bad))}")
    return got


def _num(v: str, what: str, lo: float, hi: float) -> float:
    try:
        x = float(str(v).replace("秒", "").replace(",", "").strip())
    except ValueError:
        raise _Problem(f"{what}「{v}」が数字ではありません") from None
    if not lo <= x <= hi:
        raise _Problem(f"{what}「{v}」は {lo:g}〜{hi:g} の範囲にしてください")
    return x


def _bool(v: str, what: str) -> bool:
    low = str(v).strip().lower()
    if low in ("true", "1", "yes", "使う", "はい"):
        return True
    if low in ("false", "0", "no", "使わない", "いいえ"):
        return False
    raise _Problem(f"{what}「{v}」は true か false にしてください")


def _color(v: str, what: str) -> str:
    try:
        return normalize_color(v)
    except UserError:
        raise _Problem(f"{what}の色「{v}」は #RRGGBB で書いてください") from None


def _pick(items: list, ch: ConsultChange, what: str):
    if not 0 <= ch.index < len(items):
        raise _Problem(f"{what}の番号 {ch.index} はありません（0〜{len(items) - 1}）" if items else f"{what}がありません")
    return items[ch.index]


def apply_to(project: Project, changes: list[ConsultChange], folder: Path | None = None) -> list[Stage]:
    """変更案を project に当てはめる（保存はしない）。番号はすべて「変える前」の番号として読む。

    戻り値は、直したことで「やり直しが必要」になる段階（Stage.PLAN なら企画を直した）。
    当てはめられない変更があれば _Problem（理由つき）。
    """
    problems: list[str] = []
    touched: set[Stage] = set()
    plan, edit = project.plan, project.edit
    clip_len = {c.file: c.duration for c in project.clips}

    # 1) 番号を、変える前の「もの」に置き換える（消したり足したりしても取り違えない）
    targets: list[tuple[ConsultChange, object]] = []
    for ch in changes:
        try:
            a = ch.action
            if a in ("scene_update", "scene_delete"):
                obj = _pick(plan.scenes, ch, "場面")
            elif a in ("piece_update", "piece_move", "insert_update", "drop_restore"):
                if edit is None:
                    raise _Problem("まだ編集していないので、使う部分・差し込み・切った部分は変えられません")
                lst, what = {"piece_update": (edit.pieces, "使う部分"), "piece_move": (edit.pieces, "使う部分"),
                             "insert_update": (edit.inserts, "差し込み"), "drop_restore": (edit.dropped, "切った部分")}[a]
                obj = _pick(lst, ch, what)
            elif a in ("telop_update", "telop_delete"):
                obj = _pick(project.telops, ch, "テロップ")
            else:
                obj = None
            targets.append((ch, obj))
        except _Problem as exc:
            problems.append(f"{ch.description or ch.action}: {exc}")
    if problems:
        raise _Problem("\n".join(problems))

    owners = [(ins, edit.pieces[ins.piece]) for ins in edit.inserts
              if 0 <= ins.piece < len(edit.pieces)] if edit else []
    moves, restores, deletes, adds = [], [], [], []

    # 2) 中身を直す
    for ch, obj in targets:
        try:
            a = ch.action
            if a == "scene_update":
                f = _fields(ch, {"title", "script", "seconds", "shots"})
                _scene_fields(obj, f)  # type: ignore[arg-type]
                touched.add(Stage.PLAN)
            elif a == "scene_add":
                f = _fields(ch, {"title", "script", "seconds", "shots"})
                if not f.get("title", "").strip():
                    raise _Problem("足す場面の title がありません")
                used = {s.id for s in plan.scenes}
                new_id = next(f"s{n}" for n in range(1, 1000) if f"s{n}" not in used)
                sc = Scene(id=new_id, title="")
                _scene_fields(sc, f)
                adds.append(("scene", ch.index, sc))
                touched.add(Stage.PLAN)
            elif a == "plan_settings":
                f = _fields(ch, {"target_seconds", "price_mode", "price_amount", "extra_text"})
                if "target_seconds" in f:
                    plan.target_seconds = int(_num(f["target_seconds"], "目標の長さ", 20, 900))
                price = plan.price.model_copy()
                if "price_mode" in f:
                    if f["price_mode"] not in ("hidden", "open", "amount"):
                        raise _Problem("price_mode は hidden / open / amount のどれかにしてください")
                    price.mode = f["price_mode"]  # type: ignore[assignment]
                if "price_amount" in f:
                    price.amount = int(_num(f["price_amount"], "価格", 1, 100_000_000))
                if price.mode == "amount" and not price.amount:
                    raise _Problem("金額を表示するときは price_amount も入れてください")
                plan.price = Price.model_validate(price.model_dump())
                if "extra_text" in f:
                    plan.extra_text = f["extra_text"].strip()
                touched.add(Stage.PLAN)
            elif a == "piece_update":
                f = _fields(ch, {"enabled", "start", "end"})
                length = clip_len.get(obj.clip, 1e9)  # type: ignore[union-attr]
                start = _num(f["start"], "始まり", 0, length) if "start" in f else obj.start  # type: ignore[union-attr]
                end = _num(f["end"], "終わり", 0, length) if "end" in f else obj.end  # type: ignore[union-attr]
                if end - start < 0.5:
                    raise _Problem("使う部分は 0.5 秒以上にしてください")
                obj.start, obj.end = round(start, 2), round(end, 2)  # type: ignore[union-attr]
                if "enabled" in f:
                    obj.enabled = _bool(f["enabled"], "enabled")  # type: ignore[union-attr]
            elif a == "piece_move":
                f = _fields(ch, {"to"})
                if "to" not in f:
                    raise _Problem("移す先（to）がありません")
                moves.append((obj, int(_num(f["to"], "移す先", 0, max(len(edit.pieces) - 1, 0)))))  # type: ignore[union-attr]
            elif a == "insert_update":
                f = _fields(ch, {"enabled"})
                if "enabled" in f:
                    obj.enabled = _bool(f["enabled"], "enabled")  # type: ignore[union-attr]
            elif a == "drop_restore":
                _fields(ch, set())
                restores.append(obj)
            elif a == "telop_update":
                f = _fields(ch, {"start", "end", "text", "font", "text_color", "outline_color", "emphasis_color"})
                _telop_fields(obj, f)  # type: ignore[arg-type]
            elif a == "telop_add":
                f = _fields(ch, {"start", "end", "text", "font", "text_color", "outline_color", "emphasis_color"})
                if not f.get("text", "").strip() or "start" not in f or "end" not in f:
                    raise _Problem("足すテロップには start・end・text が要ります")
                t = Telop(start=0, end=0, text="")
                _telop_fields(t, f)
                adds.append(("telop", -1, t))
            elif a in ("scene_delete", "telop_delete"):
                _fields(ch, set())
                deletes.append(obj)
                if a == "scene_delete":
                    touched.add(Stage.PLAN)
            elif a == "title":
                f = _fields(ch, {"text"})
                project.title = f.get("text", "").strip()
            elif a == "style":
                f = _fields(ch, {"text_color", "outline_color", "emphasis_color", "band_color", "band_opacity"})
                data = project.settings.telop_style.model_dump()
                for k, v in f.items():
                    data[k] = _num(v, "帯の濃さ", 0, 1) if k == "band_opacity" else _color(v, k)
                try:
                    project.settings.telop_style = TelopStyle.model_validate(data)
                except (UserError, ValueError):
                    raise _Problem("文字・縁取り・強調の色は空にできません") from None
            elif a == "bgm":
                f = _fields(ch, {"level"})
                if f.get("level") not in ("small", "medium", "large"):
                    raise _Problem("BGM の level は small / medium / large のどれかにしてください")
                project.settings.bgm_level = f["level"]  # type: ignore[assignment]
        except _Problem as exc:
            problems.append(f"{ch.description or ch.action}: {exc}")
    if problems:
        raise _Problem("\n".join(problems))

    # 3) 並びを変える（切った部分を戻す → 順番の入れ替え → 消す → 足す）
    if edit is not None:
        from app.workflow import restore_drop
        for d in restores:
            restore_drop(project, next(i for i, x in enumerate(edit.dropped) if x is d))
        for piece, to in moves:
            i = next(i for i, x in enumerate(edit.pieces) if x is piece)
            edit.pieces.insert(min(to, len(edit.pieces) - 1), edit.pieces.pop(i))
        for ins, owner in owners:  # 差し込みの重ね先は、同じ部分についていく
            ins.piece = next(i for i, x in enumerate(edit.pieces) if x is owner)
    for obj in deletes:
        lst = plan.scenes if isinstance(obj, Scene) else project.telops
        lst[:] = [x for x in lst if x is not obj]
        if isinstance(obj, Scene):
            for c in project.clips:
                if c.scene_id == obj.id:
                    c.scene_id, c.scene_manual = None, False
    for kind, at, obj in adds:
        if kind == "scene":
            plan.scenes.insert(at if 0 <= at <= len(plan.scenes) else len(plan.scenes), obj)
        else:
            project.telops.append(obj)
    project.telops.sort(key=lambda t: t.start)
    if edit is not None and not any(p.enabled for p in edit.pieces):
        raise _Problem("使う部分がすべて「使わない」になります。1つ以上は残してください")
    return sorted(touched, key=lambda s: s.index)


def _scene_fields(sc: Scene, f: dict[str, str]) -> None:
    if "title" in f:
        sc.title = f["title"].strip()
    if "script" in f:
        sc.script = f["script"].strip()
    if "seconds" in f:
        sc.seconds = _num(f["seconds"], "場面の秒数", 1, 600)
    if "shots" in f:
        sc.shots = [x.strip(" ・-") for x in f["shots"].replace("／", "\n").splitlines() if x.strip(" ・-")]


def _telop_fields(t: Telop, f: dict[str, str]) -> None:
    if "start" in f:
        t.start = round(_num(f["start"], "テロップの開始", 0, 3600), 2)
    if "end" in f:
        t.end = round(_num(f["end"], "テロップの終了", 0, 3600), 2)
    if t.end - t.start < 0.5:
        raise _Problem("テロップは 0.5 秒以上出してください")
    if "text" in f:
        if not f["text"].strip():
            raise _Problem("テロップの文言が空です")
        t.text = f["text"].strip()
    if "font" in f:
        if f["font"] and f["font"] not in FONTS:
            raise _Problem(f"フォント「{f['font']}」はありません（{', '.join(FONTS)} か空）")
        t.font = f["font"]
    for k in ("text_color", "outline_color", "emphasis_color"):
        if k in f:
            setattr(t, k, _color(f[k], k))
    t.manual = True  # 作り直しても消さない


def check_changes(project: Project, changes: list[ConsultChange]) -> list[str]:
    """実際には変えずに、当てはめられるかだけ確かめる。"""
    try:
        apply_to(project.model_copy(deep=True), changes)
    except _Problem as exc:
        return str(exc).splitlines()
    return []


# --- 相談する・反映する ------------------------------------------------------------

def consult(folder: Path, services, message: str, page: str = "", job=None,
            workspace: Workspace | None = None) -> ChatEntry:
    """相談・指示を送り、Claude の返事（と変更案）を受け取って会話に保存する。"""
    message = message.strip()
    if not message:
        raise UserError("相談の文章が空です。", "聞きたいことや、直してほしいことを書いてから送ってください。")
    message = message[:MAX_MESSAGE]
    ws = workspace or WORKSPACE
    folder = Path(folder)
    project, chat = load_project(folder), load_chat(folder)
    prompt = load_prompt("consult", page=PAGE_LABELS.get(page, "動画"), context=context_text(folder, project),
                         history=history_text(chat), message=message, fonts=", ".join(FONTS))
    with usage_scope(folder.name):
        out = services.cc().ask(prompt, ConsultOut, timeout=WRITE_TIMEOUT, kind="consult",
                                check=lambda o: check_changes(project, o.changes))
    reply = ChatEntry(role="assistant", text=out.reply.strip(), changes=out.changes,
                      remember=" ".join(out.remember.split()), next_step=out.next_step if out.changes else "none")
    if reply.has_proposal:
        reply.status = "pending"
    for c in chat:  # 前の変更案は、新しい相談をしたら「反映しなかった」扱い
        if c.status == "pending":
            c.status = "declined"
    chat += [ChatEntry(role="user", text=message), reply]
    save_chat(folder, chat)
    ws.log("consult", message, folder.name)
    return reply


def apply_proposal(folder: Path, index: int, workspace: Workspace | None = None) -> tuple[Project, ChatEntry]:
    """会話の index 番目の変更案を反映する（「反映する」ボタン）。"""
    ws = workspace or WORKSPACE
    folder = Path(folder)
    chat = load_chat(folder)
    entry = chat[index]
    if entry.status != "pending":
        raise UserError("この変更案はもう使えません。", "もう一度 Claude に相談してください。")
    project = load_project(folder)
    try:
        touched = apply_to(project, entry.changes, folder)
    except _Problem:
        raise UserError("この変更案は、いまの内容に合わなくなりました。",
                        "画面で内容を変えたあとなら、もう一度 Claude に相談してください。") from None
    for stage in touched:
        project.mark_changed(stage)
    if entry.changes:
        save_project(folder, project)
    if entry.remember:
        ws.remember(entry.remember)
    entry.status = "applied"
    save_chat(folder, chat)
    ws.log("apply", "\n".join(c.description for c in entry.changes) or f"覚えた: {entry.remember}", folder.name)
    return project, entry


def decline_proposal(folder: Path, index: int) -> None:
    chat = load_chat(folder)
    if chat[index].status == "pending":
        chat[index].status = "declined"
        save_chat(folder, chat)
