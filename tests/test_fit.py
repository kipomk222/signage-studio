"""尺合わせの計算のテスト。"""

from __future__ import annotations

import pytest

from app.pipeline.fit import FADE, MAX_SPEED, MIN_CUT, Cut, fit_scene, scene_length, target_length


def cuts(*spec):
    return [Cut(clip=f"c{i}.mov", start=a, end=b, src_len=n) for i, (a, b, n) in enumerate(spec)]


def test_target_includes_gaps():
    assert target_length(10) == pytest.approx(10.8)


def test_extend_with_handles():
    cs = cuts((2, 5, 10), (1, 4, 6))            # 3+3 - 2*0.3 = 5.4秒、伸ばせる余り 5+2 / 2+1
    r = fit_scene(cs, 9.0)
    assert r.length == pytest.approx(9.0, abs=0.03)
    assert all(c.speed == 1 and c.hold == 0 for c in r.cuts) and not r.notes
    assert all(0 <= c.start < c.end <= c.src_len for c in r.cuts)


def test_extend_adds_spare_then_slows_then_holds():
    cs = cuts((0, 3, 3), (0, 3, 3))             # 余りなし
    spare = Cut(clip="sp.mov", start=1, end=5, src_len=6)
    r = fit_scene(cs, 8.0, [spare])
    assert r.length == pytest.approx(8.0, abs=0.03) and r.cuts[-1].spare
    assert any("予備" in n for n in r.notes)

    r = fit_scene(cuts((0, 3, 3), (0, 3, 3)), 6.5)  # 予備なし → ゆっくり再生（上限内）
    assert r.length == pytest.approx(6.5, abs=0.03)
    assert 1 < r.cuts[0].speed <= MAX_SPEED and r.cuts[-1].hold == 0

    r = fit_scene(cuts((0, 3, 3), (0, 3, 3)), 12.0)  # ゆっくりでも足りない → 最後の絵を止める
    assert r.cuts[0].speed == MAX_SPEED and r.cuts[-1].hold > 0
    assert r.length == pytest.approx(12.0, abs=0.03)
    assert any("止めて" in n for n in r.notes)


def test_shrink_evenly_keeps_min_cut():
    cs = cuts((0, 5, 5), (0, 4, 4), (0, 3, 3))  # 12 - 0.9 = 11.1秒
    r = fit_scene(cs, 7.0)
    assert r.length == pytest.approx(7.0, abs=0.03)
    assert all(c.end - c.start >= MIN_CUT - 0.01 for c in r.cuts)
    assert len(r.cuts) == 3


def test_shrink_drops_last_cuts_when_needed():
    cs = cuts((0, 2, 5), (0, 2, 5), (0, 2, 5), (0, 2, 5))  # 6.8秒
    r = fit_scene(cs, 3.0)
    assert r.length == pytest.approx(3.0, abs=0.03)
    assert len(r.cuts) < 4 and any("外しました" in n for n in r.notes)


def test_single_cut_trimmed():
    r = fit_scene(cuts((0, 6, 6)), 2.0)
    assert r.length == pytest.approx(2.0, abs=0.03) and len(r.cuts) == 1


def test_input_not_modified():
    cs = cuts((2, 5, 10))
    fit_scene(cs, 9.0)
    assert (cs[0].start, cs[0].end) == (2, 5)
    assert scene_length(cs) == pytest.approx(3 - FADE)
