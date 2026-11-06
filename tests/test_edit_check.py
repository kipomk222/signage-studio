"""編集の設計図の検査（商品重視）とテロップの大きさのテスト。"""

from __future__ import annotations

from app.claude.schemas import AiEdit, AiInsert, AiPiece, covered_ratio, make_edit_check, spoken_seconds
from app.pipeline.fonts import font_size

DUR = {"talk1.mov": 6.0, "talk2.mov": 20.0, "talk3.mov": 8.0, "pen.mov": 10.0, "lift.mov": 8.0}
PHRASES = {
    "talk1.mov": [(0.5, 4.0, "今回はこのパソコンを紹介します")],
    "talk2.mov": [(0.5, 5.0, "軽さは約599g"), (9.0, 14.0, "ペンでも書けます")],   # 間に4秒の間
    "talk3.mov": [(0.3, 6.0, "ぜひ売り場へ")],
    "pen.mov": [], "lift.mov": [],
}
SCENES = {"s1", "s2", "s3"}


def piece(clip, a, b, sid):
    return AiPiece(clip=clip, start=a, end=b, scene_id=sid, reason="r")


def ins(clip, a, b, over, offset):
    return AiInsert(clip=clip, start=a, end=b, over_piece=over, offset=offset, reason="r")


def edit(inserts, pieces=None):
    pieces = pieces or [piece("talk1.mov", 0.3, 4.2, "s1"), piece("talk2.mov", 0.3, 14.2, "s2"),
                        piece("talk3.mov", 0.1, 6.2, "s3")]
    return AiEdit(summary="s", pieces=pieces, inserts=inserts, dropped=[])


def test_spoken_seconds_ignores_long_pauses():
    assert round(spoken_seconds("talk2.mov", 0.3, 14.2, PHRASES), 1) == 9.8   # 4.5+5.0 + 前後の間 0.3
    assert spoken_seconds("talk2.mov", 0.3, 14.2, None) == 14.2 - 0.3          # 文字起こしなし → 長さそのもの


def test_length_uses_spoken_time():
    # 素材の長さの合計は 24 秒だが、話している時間は約 19 秒 → 目標 18 秒でも通る（1.2 倍まで）
    check = make_edit_check(DUR, SCENES, 18, phrases=PHRASES)
    good = edit([ins("lift.mov", 0, 4, 1, 0.5), ins("pen.mov", 1, 6, 1, 8.5)])
    assert check(good) == []
    raw = make_edit_check(DUR, SCENES, 18)  # 長さそのもので数えると、こえてしまう
    assert any("大きくこえて" in p for p in raw(good))


def test_coverage_nudge_only_once():
    check = make_edit_check(DUR, SCENES, 30, phrases=PHRASES)
    bare = edit([])
    first = check(bare)
    assert len(first) == 1 and "商品の映像" in first[0] and "pen.mov" in first[0]
    assert check(bare) == []   # 2回目は差し戻さない（機械的な編集に落ちないように）


def test_no_nudge_when_no_unused_footage_or_covered():
    check = make_edit_check({k: v for k, v in DUR.items() if k != "pen.mov"}, SCENES, 30, phrases=PHRASES)
    assert check(edit([ins("lift.mov", 0, 2, 1, 0)])) == []  # 使える映像が残っていない
    covered = edit([ins("lift.mov", 0, 5, 1, 0), ins("pen.mov", 0, 5, 1, 5)])
    assert round(covered_ratio(covered), 2) == round(10 / 13.9, 2)
    assert make_edit_check(DUR, SCENES, 30, phrases=PHRASES)(covered) == []


def test_missing_scene_nudge():
    check = make_edit_check(DUR, SCENES, 30, phrases=PHRASES)
    e = edit([ins("lift.mov", 0, 5, 1, 0), ins("pen.mov", 0, 5, 1, 5)],
             pieces=[piece("talk1.mov", 0.3, 4.2, "s1"), piece("talk2.mov", 0.3, 14.2, "s2")])
    problems = check(e)
    assert any("s3" in p for p in problems)


def test_hard_errors_still_every_time():
    check = make_edit_check(DUR, SCENES, 30, phrases=PHRASES)
    bad = edit([ins("nope.mov", 0, 3, 1, 0)])
    assert any("nope.mov" in p for p in check(bad))
    assert any("nope.mov" in p for p in check(bad))


def test_telops_a_bit_bigger():
    assert font_size("軽さ599g") == 108
    assert font_size("電池が長持ちで一日中使える") == 94
    assert font_size("マウスとキーボード一台で二台を操作") == 82
    assert font_size("あ" * 30) < 82  # 長いときは画面に収まるよう小さく
