"""ステップ4: 場面の振り分け。

クリップごとの文字起こしを台本の各場面のセリフと比べ、いちばん近い場面に割り当てる。
話していないクリップ（実演など）や判断できないクリップは、直前のクリップと同じ場面とみなす
（撮影の流れで、説明のすぐ後に撮ることが多いため）。手で選んだ場面は変えない。
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

from app.project import Scene

MIN_SCORE = 0.12  # これより似ていなければ「判断できない」


def _bigrams(text: str) -> set[str]:
    t = unicodedata.normalize("NFKC", text).lower()
    t = re.sub(r"[\s、。！？!?「」『』（）()・,.\-ー]", "", t)
    return {t[i:i + 2] for i in range(len(t) - 1)}


def similarity(spoken: str, script: str) -> float:
    """話した内容のうち、台本のセリフと重なる割合（0〜1）。"""
    a, b = _bigrams(spoken), _bigrams(script)
    if not a or not b:
        return 0.0
    return len(a & b) / len(a)


@dataclass
class Assignment:
    scene_id: str | None
    score: float
    guessed: bool  # 直前のクリップから推測した


def assign_scenes(transcripts: list[str], scenes: list[Scene],
                  manual: list[str | None] | None = None) -> list[Assignment]:
    """transcripts: クリップごとの文字起こし（撮影順）。manual: 手で選んだ場面（なければ None）。"""
    manual = manual or [None] * len(transcripts)
    if not scenes:
        return [Assignment(m, 0.0, False) for m in manual]
    out: list[Assignment] = []
    previous = scenes[0].id
    for text, fixed in zip(transcripts, manual):
        if fixed:
            out.append(Assignment(fixed, 1.0, False))
            previous = fixed
            continue
        scores = [(similarity(text, f"{s.title} {s.script}"), s.id) for s in scenes]
        best, sid = max(scores)
        if best >= MIN_SCORE:
            out.append(Assignment(sid, round(best, 3), False))
            previous = sid
        else:
            out.append(Assignment(previous, round(best, 3), True))
    return out


def order_by_scene(items: list, scene_of, scenes: list[Scene]) -> list:
    """台本の場面順に並べる（同じ場面の中では撮影順のまま）。"""
    rank = {s.id: i for i, s in enumerate(scenes)}
    return [x for _, _, x in sorted(
        ((rank.get(scene_of(x), len(rank)), i, x) for i, x in enumerate(items)), key=lambda t: (t[0], t[1]))]
