"""尺合わせ: 録った声の長さに、説明の場面の映像の長さを合わせる（計算だけ。動画は触らない）。

場面の長さ ＝ 前の間 LEAD ＋ 声の長さ ＋ 後の間 TAIL。
- 長くする: 各カットを、素材に残っている前後の部分で伸ばす → 予備のカットを足す →
  ゆっくり再生（最大 MAX_SPEED 倍）→ 最後の絵を止めて伸ばす
- 短くする: 各カットを両端から均等に詰める（1カット MIN_CUT 秒は残す）→ 最後のカットを外す → 最後のカットをさらに詰める
カットどうしは FADE 秒重なってつながるので、場面の長さは「カットの長さの合計 − FADE × カットの数」で数える。
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace

from app.pipeline.concat import FADE

LEAD, TAIL = 0.3, 0.5
MIN_CUT = 1.5
MIN_KEEP = 2 * FADE + 0.2   # これより短いカットは、つなぎ目のクロスフェードに収まらない
MAX_SPEED = 1.25
EPS = 0.02


@dataclass
class Cut:
    clip: str
    start: float
    end: float
    src_len: float          # 素材のクリップ全体の長さ（伸ばせる上限）
    speed: float = 1.0
    hold: float = 0.0
    spare: bool = False     # 予備のカットから足したもの
    ref: int = -1           # 呼び出し側のカットの番号（外したカットがあっても対応が崩れないように）

    @property
    def length(self) -> float:
        return (self.end - self.start) * self.speed + self.hold


@dataclass
class FitResult:
    cuts: list[Cut]
    target: float
    length: float
    notes: list[str] = field(default_factory=list)


def scene_length(cuts: list[Cut], fade: float = FADE) -> float:
    return sum(c.length for c in cuts) - fade * len(cuts)


def target_length(voice_seconds: float) -> float:
    return LEAD + voice_seconds + TAIL


def fit_scene(cuts: list[Cut], target: float, spares: list[Cut] | None = None, fade: float = FADE) -> FitResult:
    if not cuts:
        raise ValueError("no cuts")
    originals = [replace(c) for c in cuts]
    cuts = [replace(c, speed=1.0, hold=0.0) for c in cuts]
    spares = [replace(s, spare=True) for s in (spares or [])]
    notes: list[str] = []
    delta = target - scene_length(cuts, fade)
    if delta > EPS:
        delta = _extend_handles(cuts, delta)
        while delta > EPS and spares:
            sp = spares.pop(0)
            need = delta + fade
            if sp.end - sp.start > need >= MIN_CUT:  # 必要な分だけ（真ん中あたり）を使う
                mid = (sp.start + sp.end) / 2
                sp.start, sp.end = round(mid - need / 2, 3), round(mid + need / 2, 3)
            cuts.append(sp)
            notes.append("映像が足りなかったため、予備のカットを足しました。")
            delta = target - scene_length(cuts, fade)
        if delta < -EPS:   # 予備のカットで長くなりすぎたら、詰め直す
            delta = _shrink_even(cuts, delta)
        if delta > EPS:
            raw = sum(c.end - c.start for c in cuts)
            factor = min(MAX_SPEED, 1 + delta / raw)
            for c in cuts:
                c.speed = round(factor, 4)
            delta = target - scene_length(cuts, fade)
            notes.append("映像が足りなかったため、少しゆっくり再生しました。")
        if delta > EPS:
            cuts[-1].hold = round(delta, 3)
            notes.append(f"映像が足りなかったため、最後の絵を {delta:.1f} 秒止めて伸ばしました。撮り足すと自然になります。")
    elif delta < -EPS:
        delta = _shrink_even(cuts, delta)
        while delta < -EPS and len(cuts) > 1:
            cuts.pop()
            notes.append("声が短かったため、場面の最後のカットを外しました。")
            delta = target - scene_length(cuts, fade)
            if delta > EPS:
                delta = _extend_handles(cuts, delta)
            elif delta < -EPS:
                delta = _shrink_even(cuts, delta)
        if delta < -EPS:
            last = cuts[-1]
            last.end = round(max(min(last.end, last.start + MIN_KEEP), last.end + delta), 3)  # 詰めるだけ（伸ばさない）
    cuts = _protect_short(cuts, originals, notes)
    return FitResult(cuts, round(target, 3), round(scene_length(cuts, fade), 3), notes)


def _protect_short(cuts: list[Cut], originals: list[Cut], notes: list[str]) -> list[Cut]:
    """短すぎるカットを MIN_KEEP まで広げる（素材の長さは超えない。後ろが足りなければ前へ）。広げられなければ外す。"""
    out = []
    for c in cuts:
        need = MIN_KEEP - c.hold
        if (c.end - c.start) * c.speed >= need - 1e-6:
            out.append(c)
            continue
        raw = need / c.speed
        c.end = round(min(c.src_len, c.start + raw), 3)
        c.start = round(max(0.0, min(c.start, c.end - raw)), 3)
        if (c.end - c.start) * c.speed + c.hold >= MIN_KEEP - 1e-3:
            out.append(c)
        else:
            notes.append("素材が短すぎるカットを外しました。")
    if not out:  # 全部外れたときは、元のカットを1つ残す（場面が消えないように）
        keep = max(originals, key=lambda c: c.end - c.start)
        out = [replace(keep, speed=1.0, hold=0.0)]
        notes.append("声の長さに合わせきれませんでした。場面の長さを確かめてください。")
    return out


def _extend_handles(cuts: list[Cut], delta: float) -> float:
    """素材に残っている前後の部分を使って伸ばす（後ろを先に）。戻り値は残りの不足分。"""
    rooms = [(c.src_len - c.end, c.start) for c in cuts]
    total = sum(a + b for a, b in rooms)
    if total <= EPS:
        return delta
    use = min(delta, total)
    for c, (after, before) in zip(cuts, rooms):
        share = use * (after + before) / total
        add_end = min(after, share)
        c.end = round(c.end + add_end, 3)
        c.start = round(c.start - min(before, share - add_end), 3)
    return delta - use


def _shrink_even(cuts: list[Cut], delta: float) -> float:
    """各カットを両端から均等に詰める（MIN_CUT は残す）。delta は負。戻り値は残りの超過分（負）。"""
    rooms = [max(0.0, (c.end - c.start) - MIN_CUT) for c in cuts]
    total = sum(rooms)
    if total <= EPS:
        return delta
    use = min(-delta, total)
    for c, room in zip(cuts, rooms):
        cut = use * room / total
        c.start = round(c.start + cut / 2, 3)
        c.end = round(c.end - cut / 2, 3)
    return delta + use
