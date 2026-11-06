"""編集の設計図（EditPlan）を動画にする。

- 部分（piece）ごとに、クリップの start〜end を切り出す。その中の長い間は無音カットで詰める
- 部分を上から順につなぐ（つなぎ目は短いクロスフェード）
- 差し込み（insert）は、つないだ動画の上に別クリップの映像を重ねる（声はそのまま）
- 元のクリップの時刻 → 完成動画の時刻 の対応（Layout）を返し、テロップの時刻合わせに使う
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from app.jobs import checkpoint
from app.pipeline.concat import FADE, concat_clips
from app.pipeline.ffmpeg import ffmpeg_base, run
from app.pipeline.normalize import AUDIO_ARGS
from app.pipeline.silence import MIN_PIECE, Range, render_ranges
from app.platform import PlatformProfile
from app.project import EditPiece, EditPlan

MIN_PIECE_SECONDS = 2 * FADE + 0.2   # つなぎ目に収まらないほど短い部分は使わない


def intersect(piece: tuple[float, float], keeps: list[Range]) -> list[Range]:
    """部分の範囲のうち、話している（無音カットで残す）ところだけ。"""
    a, b = piece
    out = [(max(a, s), min(b, e)) for s, e in keeps if min(b, e) - max(a, s) >= MIN_PIECE]
    return [(round(s, 3), round(e, 3)) for s, e in out] or [(a, b)]


@dataclass
class PieceLayout:
    index: int            # plan.pieces の何番目か
    clip: str
    ranges: list[Range]   # 元のクリップの、残した範囲
    out_start: float      # 完成（つないだ）動画での始まり
    speed: float = 1.0
    hold: float = 0.0
    scene_id: str | None = None

    @property
    def length(self) -> float:
        return sum(e - s for s, e in self.ranges) * self.speed + self.hold

    def to_output(self, t: float) -> float | None:
        """元のクリップの時刻 t が、つないだ動画の何秒目か（切った所なら None）。"""
        pos = self.out_start
        for s, e in self.ranges:
            if s <= t <= e:
                return round(pos + (t - s) * self.speed, 3)
            pos += (e - s) * self.speed
        return None


def cut_length(p: EditPiece) -> float:
    """話していない映像のカットの、つないだ動画での長さ（ゆっくり再生・止めた絵を含む）。"""
    return (p.end - p.start) * p.speed + p.hold


def render_cut(profile: PlatformProfile, src: Path, dst: Path, p: EditPiece) -> None:
    """話していない映像のカット: 元の音は消し、必要ならゆっくり再生・最後の絵を止めて伸ばす。"""
    length = cut_length(p)
    v = f"[0:v]trim=start={p.start}:end={p.end},setpts=(PTS-STARTPTS)*{p.speed:.4f}"
    if p.hold > 0:
        v += f",tpad=stop_mode=clone:stop_duration={p.hold:.3f}"
    v += ",fps=30[v]"
    a = f"anullsrc=r=48000:cl=stereo,atrim=0:{length:.3f}[a]"
    run([*ffmpeg_base(profile.ffmpeg), "-i", str(src), "-filter_complex", f"{v};{a}",
         "-map", "[v]", "-map", "[a]", "-t", f"{length:.3f}", *profile.video_args("work"), *AUDIO_ARGS, str(dst)],
        what=f"「{src.name}」のカットを切り出せませんでした。")


def map_time(layouts: list[PieceLayout], clip: str, t: float) -> float | None:
    for lay in layouts:
        if lay.clip == clip:
            out = lay.to_output(t)
            if out is not None:
                return out
    return None


def render_plan(profile: PlatformProfile, plan: EditPlan, norm: dict[str, Path], keeps: dict[str, list[Range]],
                work: Path, progress=None) -> tuple[Path, list[PieceLayout], float]:
    """設計図の部分をつなぐ。戻り値: (つないだ動画, 時刻の対応, 長さ)。"""
    pieces_dir = work / "pieces"
    pieces_dir.mkdir(parents=True, exist_ok=True)
    for old in pieces_dir.glob("*.mp4"):
        old.unlink()
    files, layouts, cursor = [], [], 0.0
    used = [(i, p) for i, p in enumerate(plan.pieces) if p.enabled]
    for k, (i, p) in enumerate(used):
        checkpoint()
        if progress:
            progress(k / max(1, len(used)), f"部分を切り出しています（{k + 1}/{len(used)}）")
        dst = pieces_dir / f"{len(files):03d}.mp4"
        if p.mute:  # 話していない映像: 範囲そのまま（無音カットしない）・音は消す
            ranges = [(p.start, p.end)]
            length = cut_length(p)
            if length < MIN_PIECE_SECONDS:
                continue
            render_cut(profile, norm[p.clip], dst, p)
        else:
            ranges = intersect((p.start, p.end), keeps.get(p.clip, []))
            length = sum(e - s for s, e in ranges)
            if length < MIN_PIECE_SECONDS:
                continue
            render_ranges(profile, norm[p.clip], dst, ranges)
        layouts.append(PieceLayout(i, p.clip, ranges, round(cursor, 3),
                                   speed=p.speed if p.mute else 1.0, hold=p.hold if p.mute else 0.0,
                                   scene_id=p.scene_id))
        files.append(dst)
        cursor += length - FADE
    if not files:
        raise ValueError("no usable pieces")
    joined = work / "joined_base.mp4"
    total = concat_clips(profile, files, joined)
    return joined, layouts, total


@dataclass
class PlacedInsert:
    clip: str
    src_start: float
    src_end: float
    at: float  # つないだ動画での始まり

    @property
    def length(self) -> float:
        return self.src_end - self.src_start


def place_inserts(plan: EditPlan, layouts: list[PieceLayout], total: float) -> list[PlacedInsert]:
    """差し込みを、つないだ動画のどこに重ねるか決める（重なりや、はみ出しは除く）。"""
    by_index = {lay.index: lay for lay in layouts}
    placed: list[PlacedInsert] = []
    for ins in plan.inserts:
        lay = by_index.get(ins.piece)
        if not ins.enabled or lay is None:
            continue
        piece: EditPiece = plan.pieces[ins.piece]
        at = lay.to_output(min(piece.end, piece.start + ins.offset))
        if at is None:  # 重ねる位置が詰めた間に当たったら、部分の始まりから
            at = lay.out_start
        length = min(ins.end - ins.start, total - at - 0.05)
        if length < 0.8:
            continue
        if any(at < p.at + p.length and p.at < at + length for p in placed):
            continue
        placed.append(PlacedInsert(ins.clip, ins.start, round(ins.start + length, 3), round(at, 3)))
    return sorted(placed, key=lambda p: p.at)


def apply_inserts(profile: PlatformProfile, base: Path, placed: list[PlacedInsert], norm: dict[str, Path],
                  dst: Path) -> Path:
    """差し込みの映像を重ねる。音は元のまま。"""
    if not placed:
        dst.write_bytes(base.read_bytes())
        return dst
    args = [*ffmpeg_base(profile.ffmpeg), "-i", str(base)]
    for p in placed:
        args += ["-i", str(norm[p.clip])]
    graph, prev = [], "[0:v]"
    for k, p in enumerate(placed, 1):
        graph.append(f"[{k}:v]trim=start={p.src_start}:end={p.src_end},setpts=PTS-STARTPTS+{p.at}/TB[b{k}]")
        out = f"[v{k}]"
        graph.append(f"{prev}[b{k}]overlay=enable='between(t,{p.at},{p.at + p.length:.3f})':eof_action=pass{out}")
        prev = out
    run([*args, "-filter_complex", ";".join(graph), "-map", prev, "-map", "0:a",
         *profile.video_args("work"), *AUDIO_ARGS, str(dst)],
        what="差し込みの映像を重ねられませんでした。")
    return dst


# --- つないだ結果の記録（テロップの時刻合わせ・テロップと映像の照合に使う）-------------------

Visual = tuple[float, float, str]   # (始まり, 終わり, 映っているもの)


def save_layouts(path: Path, layouts: list[PieceLayout], total: float, visual: list[Visual] | None = None) -> None:
    data = {"total": total,
            "layouts": [{"index": x.index, "clip": x.clip, "ranges": x.ranges, "out_start": x.out_start,
                         "speed": x.speed, "hold": x.hold, "scene_id": x.scene_id} for x in layouts],
            "visual": [list(v) for v in visual or []]}
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def load_layouts(path: Path) -> tuple[list[PieceLayout], float, list[Visual]]:
    d = json.loads(path.read_text(encoding="utf-8"))
    layouts = [PieceLayout(x["index"], x["clip"], [tuple(r) for r in x["ranges"]], x["out_start"], x["speed"],
                           x["hold"], x["scene_id"]) for x in d["layouts"]]
    return layouts, d["total"], [(a, b, t) for a, b, t in d.get("visual", [])]


def visual_of(layouts: list[PieceLayout], pieces: list[EditPiece], placed: list["PlacedInsert"] | None = None,
              inserts_desc: dict[tuple[str, float], str] | None = None) -> list[Visual]:
    """つないだ動画で、どの時刻に何が映っているか（カットの説明から）。差し込みは上に重なる。"""
    out: list[Visual] = []
    for lay in layouts:
        p = pieces[lay.index]
        desc = p.shows or (p.reason if p.mute else "スタッフが正面で話している")
        out.append((round(lay.out_start, 2), round(lay.out_start + lay.length, 2), desc or "（説明なし）"))
    for ins in placed or []:
        desc = (inserts_desc or {}).get((ins.clip, round(ins.src_start, 2)), "")
        out.append((round(ins.at, 2), round(ins.at + ins.length, 2), f"（差し込み）{desc or ins.clip}"))
    return sorted(out)
