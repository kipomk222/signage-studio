"""カラーコード（#RRGGBB）の読み取りと、字幕（ASS）用の色の書き方への変換。"""

from __future__ import annotations

import re

from app.errors import UserError

_HEX6 = re.compile(r"^#?([0-9a-fA-F]{6})$")
_HEX3 = re.compile(r"^#?([0-9a-fA-F]{3})$")


def normalize_color(code: str) -> str:
    """「#ffd400」「FFD400」「#fd0」などを「#FFD400」にそろえる。空文字はそのまま（指定なし）。"""
    code = (code or "").strip().replace("＃", "#")
    if not code:
        return ""
    if m := _HEX6.match(code):
        return "#" + m.group(1).upper()
    if m := _HEX3.match(code):
        return "#" + "".join(c * 2 for c in m.group(1)).upper()
    raise UserError(f"色の指定「{code}」が読み取れません。",
                    "「#FFD400」のように、# と6けたの英数字（0〜9、A〜F）で入れてください。")


def to_ass(code: str, opacity: float = 1.0) -> str:
    """#RRGGBB → &HAABBGGRR（ASS は青・緑・赤の順で、透明度は 00 が不透明）。"""
    c = normalize_color(code)
    if not c:
        raise ValueError("empty color")
    r, g, b = c[1:3], c[3:5], c[5:7]
    alpha = round((1 - max(0.0, min(1.0, opacity))) * 255)
    return f"&H{alpha:02X}{b}{g}{r}"


def to_ass_tag(code: str) -> str:
    """文字列の途中で色を変えるときの書き方（\\c&HBBGGRR&）。"""
    c = normalize_color(code)
    return f"&H{c[5:7]}{c[3:5]}{c[1:3]}&"
