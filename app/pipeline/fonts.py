"""ステップ9: テロップのフォントと文字の大きさを選ぶ。

ルール（CLAUDE.md）:
- 冒頭タイトル（型番を含まない短い見出し） → Dela Gothic One
- 短い決めゼリフ（10文字程度まで） → Noto Sans JP Black
- 通常（11〜20文字程度） → Noto Sans JP Bold
- 長め、数字・型番を含む → BIZ UDPGothic Bold
- キッチン・生活家電の動画 → 全体を M PLUS Rounded 1c に寄せる
- 型番を含む文字列には Dela Gothic One を使わない（0 と O が見分けにくい）
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
FONTS_DIR = ROOT / "assets" / "fonts"


@dataclass(frozen=True)
class FontSpec:
    label: str     # 画面に出す名前
    family: str    # ASS で指定する名前
    bold: bool
    file: str
    # 字幕の描画は「行の高さ」で大きさを決めるため、行の高さが大きいフォントほど文字が小さく出る。
    # 行の高さ ÷ 文字の大きさ（usWinAscent+Descent ÷ unitsPerEm）を掛けて、どのフォントでも同じ大きさにそろえる
    line_ratio: float = 1.0


FONTS: dict[str, FontSpec] = {
    "dela": FontSpec("Dela Gothic One（見出し）", "Dela Gothic One", False, "DelaGothicOne-Regular.ttf", 1.448),
    "noto_black": FontSpec("Noto Sans JP 極太", "Noto Sans JP Black", False, "NotoSansJP-Black.otf", 1.448),
    "zen_black": FontSpec("Zen Kaku Gothic New 極太", "Zen Kaku Gothic New Black", False, "ZenKakuGothicNew-Black.ttf",
                          1.448),
    "noto_bold": FontSpec("Noto Sans JP 太字", "Noto Sans JP", True, "NotoSansJP-Bold.otf", 1.448),
    "biz_ud": FontSpec("BIZ UDPゴシック（数字・型番向け）", "BIZ UDPGothic", True, "BIZUDPGothic-Bold.ttf"),
    "rounded": FontSpec("M PLUS Rounded 1c（丸ゴシック）", "Rounded Mplus 1c ExtraBold", False,
                        "MPLUSRounded1c-ExtraBold.ttf", 1.395),
}
SOFT_CATEGORIES = {"キッチン家電", "生活家電"}
SHORT, NORMAL = 10, 20

# 英字と数字が混ざった語（型番らしいもの）
MODEL_LIKE = re.compile(r"(?=[A-Za-z0-9\-]*[A-Za-z])(?=[A-Za-z0-9\-]*\d)[A-Za-z0-9][A-Za-z0-9\-]{2,}")
DIGITS = re.compile(r"\d")


def char_count(text: str) -> int:
    return len(text.replace("\n", ""))


def has_model_number(text: str, model_number: str = "") -> bool:
    t = unicodedata.normalize("NFKC", text)
    if model_number and model_number.replace("-", "").lower() in t.replace("-", "").lower():
        return True
    return bool(MODEL_LIKE.search(t))


def choose_font(text: str, role: str = "telop", category: str = "", model_number: str = "") -> str:
    """role: "title"（冒頭タイトル）／"subtitle"（タイトルの下の型番）／"telop"。"""
    model = has_model_number(text, model_number)
    if role == "title":
        if model:
            return "biz_ud"
        return "rounded" if category in SOFT_CATEGORIES else "dela"
    if model or role == "subtitle" or DIGITS.search(unicodedata.normalize("NFKC", text)):
        return "biz_ud"  # 数字・型番は見分けやすい UD 書体
    if category in SOFT_CATEGORIES:
        return "rounded"
    n = char_count(text)
    if n <= SHORT:
        return "noto_black"
    if n <= NORMAL:
        return "noto_bold"
    return "biz_ud"


def visual_width(line: str) -> float:
    """文字の幅（全角＝1、半角英数字＝約0.6）。"""
    return sum(0.6 if unicodedata.east_asian_width(c) in "NaH" else 1.0 for c in line)


def font_size(text: str, max_width_px: int = 1700, role: str = "telop") -> int:
    """長いほど小さく。1080p の画面に収まる大きさにする。"""
    longest = max((visual_width(l) for l in text.split("\n")), default=1) or 1
    if role == "title":
        base = 120 if longest <= 8 else 104 if longest <= 12 else 88
    elif role == "subtitle":
        base = 56
    else:
        base = 96 if longest <= 10 else 84 if longest <= 14 else 72
    return int(min(base, max_width_px / longest))


def ass_size(font_id: str, size: int) -> int:
    """font_size() の大きさを、そのフォントで字幕に指定する値に直す。"""
    return round(size * FONTS[font_id].line_ratio)


def font_choices() -> list[tuple[str, str]]:
    """確認画面のプルダウン用（id, 表示名）。"""
    return [(k, v.label) for k, v in FONTS.items()]
