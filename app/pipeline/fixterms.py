"""ステップ7: 型番・固有名詞の補正。

文字起こしでは、英字や数字がカタカナ・漢数字になったり、空白やハイフンが揺れたりする。
商品情報の正しい表記（型番・商品名・仕様に出てくる英数字の語）を基準に直し、修正ログを残す。
例: 「エフエムブイユー五九エルツービーエー」→「FMVU59L2BA」、「コアウルトラ 7」→「Core Ultra 7」
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from app.project import Product

LETTER = {
    "A": "エー|エイ", "B": "ビー", "C": "シー", "D": "ディー|デー", "E": "イー", "F": "エフ",
    "G": "ジー", "H": "エイチ|エッチ", "I": "アイ", "J": "ジェー|ジェイ", "K": "ケー|ケイ",
    "L": "エル", "M": "エム", "N": "エヌ", "O": "オー", "P": "ピー", "Q": "キュー", "R": "アール",
    "S": "エス", "T": "ティー|テー", "U": "ユー", "V": "ブイ|ヴィ|ヴイ", "W": "ダブリュー|ダブル",
    "X": "エックス", "Y": "ワイ", "Z": "ゼット|ズィー",
}
WORD = {
    "core": "コア", "ultra": "ウルトラ", "wi": "ワイ", "fi": "ファイ", "copilot": "コパイロット",
    "windows": "ウィンドウズ|ウインドウズ", "office": "オフィス", "home": "ホーム", "intel": "インテル",
    "arc": "アーク", "note": "ノート", "lavie": "ラヴィ|ラビ|ラヴィエ|ラビエ", "dynabook": "ダイナブック",
    "thunderbolt": "サンダーボルト", "bluetooth": "ブルートゥース|ブルートゥス", "microsoft": "マイクロソフト",
    "personal": "パーソナル", "business": "ビジネス", "pro": "プロ", "air": "エアー|エア", "max": "マックス",
    "plus": "プラス", "mini": "ミニ", "surface": "サーフェス|サーフェイス", "galaxy": "ギャラクシー",
    "iphone": "アイフォン|アイフォーン", "ipad": "アイパッド", "mac": "マック", "macbook": "マックブック",
    "snapdragon": "スナップドラゴン", "ryzen": "ライゼン", "geforce": "ジーフォース", "rtx": "アールティーエックス",
    "oled": "オーレッド|有機EL", "fujitsu": "フジツウ|富士通", "nec": "エヌイーシー", "panasonic": "パナソニック",
    "lenovo": "レノボ", "dell": "デル", "asus": "エイスース|アスース", "acer": "エイサー", "hp": "エイチピー",
    "apple": "アップル", "sony": "ソニー", "sharp": "シャープ", "hitachi": "日立", "toshiba": "東芝",
    "series": "シリーズ", "type": "タイプ", "usb": "ユーエスビー", "boost": "ブースト",
}
# 1語だけだと普通の日本語と区別できない語（「ノートパソコン」の「ノート」など）は、カタカナから直さない
GENERIC = {"note", "home", "core", "office", "air", "pro", "plus", "mini", "type", "series", "max", "arc",
           "ultra", "personal", "business", "mac", "surface", "boost", "type"}
DIGIT_EN = ["ゼロ|オー", "ワン", "ツー", "スリー", "フォー", "ファイブ", "シックス", "セブン", "エイト", "ナイン"]
KANJI_DIGIT = "〇一二三四五六七八九"
FULLWIDTH = str.maketrans(
    "ＡＢＣＤＥＦＧＨＩＪＫＬＭＮＯＰＱＲＳＴＵＶＷＸＹＺａｂｃｄｅｆｇｈｉｊｋｌｍｎｏｐｑｒｓｔｕｖｗｘｙｚ０１２３４５６７８９",
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789",
)
SEP = r"[\s\-‐・、]*"
TOKEN = re.compile(r"[A-Za-z]+|\d+")
CHUNK = re.compile(r"[A-Za-z][A-Za-z0-9]*(?:[ \-][A-Za-z0-9]+)*\+?")


@dataclass
class Fix:
    before: str
    after: str
    where: str  # 何秒の発言か


def kanji_number(n: int) -> str:
    """355 → 三百五十五（1万未満）。"""
    if n == 0:
        return "〇"
    out = ""
    for unit, name in ((1000, "千"), (100, "百"), (10, "十")):
        q, n = divmod(n, unit)
        if q:
            out += ("" if q == 1 else KANJI_DIGIT[q]) + name
    return out + (KANJI_DIGIT[n] if n else "")


def _digit_alt(run: str) -> str:
    alts = {re.escape(run), "".join(KANJI_DIGIT[int(c)] for c in run)}
    if len(run) <= 4 and not run.startswith("0"):
        alts.add(kanji_number(int(run)))
    alts = sorted(alts, key=len, reverse=True)
    if len(run) <= 4:  # 型番の数字は英語で読まれることもある（L2 →「エルツー」）
        alts.append("".join(f"(?:{DIGIT_EN[int(c)]})" for c in run))
    return "(?:" + "|".join(alts) + ")"


def _letters_alt(run: str, readings: bool = True) -> str:
    alts = [re.escape(run)]
    if readings and (w := WORD.get(run.lower())):
        alts.append(w)
    if readings and len(run) <= 5:  # 頭文字の並び（FMV, ADBL など）は1文字ずつ読まれることがある
        alts.append("".join(f"(?:{LETTER[c.upper()]})" for c in run))
    return "(?:" + "|".join(alts) + ")"


def term_pattern(term: str) -> str:
    tokens = TOKEN.findall(term)
    readings = not (len(tokens) == 1 and tokens[0].lower() in GENERIC)
    parts = []
    for tok in tokens:
        parts.append(_digit_alt(tok) if tok.isdigit() else _letters_alt(tok, readings))
    body = SEP.join(parts)
    if term.endswith("+"):
        body += r"(?:\s*\+|\s*プラス)"
    return body


def _prefixes(chunk: str) -> list[str]:
    words = chunk.split(" ")
    return [" ".join(words[:i]) for i in range(len(words), 0, -1)]


def collect_terms(product: Product) -> list[str]:
    """補正の基準にする正しい表記。長いものから並べる（長い一致を優先するため）。"""
    sources = [product.model_number, product.name, product.maker, *product.specs.values(), *product.features]
    terms: set[str] = set()
    if product.model_number:
        terms.add(product.model_number.strip())
    for text in sources:
        for chunk in CHUNK.findall(text.translate(FULLWIDTH)):
            for t in _prefixes(chunk.strip()):
                letters = sum(c.isalpha() for c in t)
                if letters >= 2 and len(t) >= 2:
                    terms.add(t)
    return sorted(terms, key=len, reverse=True)


class TermFixer:
    def __init__(self, terms: list[str]) -> None:
        self.terms = terms
        self.regex = None
        if terms:
            alts = "|".join(f"(?P<t{i}>{term_pattern(t)})" for i, t in enumerate(terms))
            # 英数字どうしが続く途中では区切らない（カタカナから直した直後に「PC」が続くのはよい）
            self.regex = re.compile(
                rf"(?<![A-Za-z0-9](?=[A-Za-z0-9]))(?:{alts})(?!(?<=[A-Za-z0-9])[A-Za-z0-9])", re.IGNORECASE)

    @classmethod
    def for_product(cls, product: Product) -> "TermFixer":
        return cls(collect_terms(product))

    def fix(self, text: str, where: str = "") -> tuple[str, list[Fix]]:
        if not self.regex:
            return text, []
        fixes: list[Fix] = []
        source = text.translate(FULLWIDTH)

        def repl(m: re.Match) -> str:
            idx = int(next(k for k, v in m.groupdict().items() if v is not None)[1:])
            right = self.terms[idx]
            if m.group(0) != right:
                fixes.append(Fix(m.group(0), right, where))
            return right

        return self.regex.sub(repl, source), fixes  # 全角英数字は半角にそろう


def log_text(fixes: list[Fix]) -> str:
    if not fixes:
        return "直したところはありません。\n"
    return "".join(f"{f.where}\t{f.before} → {f.after}\n" for f in fixes)
