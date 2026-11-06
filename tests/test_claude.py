"""段階3: Claude Code 連携のテスト。本物の代わりに、決まった応答を返す偽の Claude Code を使う。"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from app.claude import calls
from app.claude.runner import ClaudeCode, ClaudeError, NeedsManualInput, ProcResult, build_command
from app.claude.schemas import ProductInfo, telop_length
from app.project import Price, Product, Scene


class FakeClaude:
    """呼ばれるたびに用意した応答を順に返し、渡されたコマンドと文章を記録する。"""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls: list[tuple[list[str], str, Path]] = []

    def __call__(self, cmd, stdin, cwd, timeout):
        self.calls.append((cmd, stdin, cwd))
        r = self.responses.pop(0)
        if isinstance(r, Exception):
            raise r
        if isinstance(r, ProcResult):
            return r
        return ProcResult(0, json.dumps({"is_error": False, "subtype": "success",
                                         "structured_output": r, "result": ""}), "")


def cc_with(fake: FakeClaude) -> ClaudeCode:
    return ClaudeCode(claude_path="claude", runner=fake)


PRODUCT = {
    "found": True, "name": "dynabook X9", "maker": "ダイナブック", "category": "パソコン",
    "specs": [{"label": "画面", "value": "13.3型"}, {"label": "CPU", "value": "Core Ultra 7 355"},
              {"label": "メモリ", "value": "16GB"}],
    "features": ["持ち歩きやすい", "AI処理が速い", "長時間バッテリー"],
    "sources": ["https://www.yodobashi.com/product/xxx/"], "note": "",
}


def plan_json(seconds_each: float, n: int = 5) -> dict:
    return {"scenes": [{"title": f"場面{i}", "voice": "talk", "points": "要点", "script": "セリフ", "shots": ["手元アップ"],
                        "seconds": seconds_each} for i in range(n)]}


def separate_plan_json(seconds_each: float, n: int = 5, bad_talk: bool = False) -> dict:
    scenes = []
    for i in range(n):
        talk = i in (0, n - 1) or bad_talk
        scenes.append({"title": f"場面{i}", "voice": "talk" if talk else "narration", "points": f"伝えること{i}",
                       "script": "今回はこの商品を紹介します" if talk else "", "seconds": seconds_each,
                       "shots": ["正面で話す"] if talk else ["持ち上げる（約3秒）", "横から（約3秒）", "手元（約3秒）"]})
    return {"scenes": scenes}


# --- コマンドの組み立て --------------------------------------------------------

def test_command_restricts_tools_and_settings():
    cmd = build_command("claude", {"type": "object"}, [])
    assert cmd[:2] == ["claude", "-p"]
    assert cmd[cmd.index("--tools") + 1] == ""          # ツールなし
    assert cmd[cmd.index("--setting-sources") + 1] == ""  # お手元の設定を読まない
    assert "--strict-mcp-config" in cmd and "--no-session-persistence" in cmd
    research = build_command("claude", {}, calls.RESEARCH_TOOLS)
    assert research[research.index("--tools") + 1] == "WebSearch,WebFetch"
    assert not any(t in " ".join(research) for t in ("Bash", "Edit", "Write"))


def test_prompt_goes_through_stdin_in_empty_temp_dir(tmp_path):
    fake = FakeClaude(PRODUCT)
    calls.research_product(cc_with(fake), "P1X9ADBL", cache_dir=tmp_path)
    cmd, stdin, cwd = fake.calls[0]
    assert "P1X9ADBL" in stdin and "P1X9ADBL" not in " ".join(cmd)
    assert not cwd.exists()  # 終わったら一時フォルダは消える


# --- 商品調査とキャッシュ ----------------------------------------------------------

def test_research_saves_and_reuses_cache(tmp_path):
    fake = FakeClaude(PRODUCT)
    product, info = calls.research_product(cc_with(fake), "P1X9ADBL", cache_dir=tmp_path)
    assert product.specs["CPU"] == "Core Ultra 7 355" and product.category == "パソコン"
    assert (tmp_path / "P1X9ADBL.json").exists()

    again, _ = calls.research_product(cc_with(FakeClaude()), "P1X9ADBL", cache_dir=tmp_path)  # 呼ばれない
    assert again == product

    fake2 = FakeClaude({**PRODUCT, "name": "新しい名前"})
    refreshed, _ = calls.research_product(cc_with(fake2), "P1X9ADBL", cache_dir=tmp_path, refresh=True)
    assert refreshed.name == "新しい名前" and len(fake2.calls) == 1


def test_not_found_is_not_cached(tmp_path):
    missing = {**PRODUCT, "found": False, "specs": [], "features": [], "sources": [], "note": "見つからず"}
    _, info = calls.research_product(cc_with(FakeClaude(missing)), "ZZZ-000", cache_dir=tmp_path)
    assert not info.found
    assert not list(tmp_path.glob("*.json"))


def test_broken_shape_is_retried_once_with_reasons(tmp_path):
    bad = {**PRODUCT, "features": ["ひとつだけ"]}
    fake = FakeClaude(bad, PRODUCT)
    product, _ = calls.research_product(cc_with(fake), "P1X9ADBL", cache_dir=tmp_path)
    assert len(fake.calls) == 2
    assert "前回の出力の問題" in fake.calls[1][1] and "3〜5点" in fake.calls[1][1]


def test_gives_up_after_one_retry(tmp_path):
    bad = {"found": True}  # 項目が足りない
    with pytest.raises(NeedsManualInput) as e:
        calls.research_product(cc_with(FakeClaude(bad, bad)), "P1X9ADBL", cache_dir=tmp_path)
    assert "手入力" in e.value.how or "入力" in e.value.how


# --- エラー -------------------------------------------------------------------

@pytest.mark.parametrize("response, words", [
    (FileNotFoundError(), "見つかりません"),
    (subprocess.TimeoutExpired("claude", 1), "時間がかかりすぎた"),
    (ProcResult(1, json.dumps({"is_error": True, "result": "Not logged in · Please run /login"}), ""), "ログイン"),
    (ProcResult(1, "", "Claude AI usage limit reached"), "利用上限"),
    (ProcResult(0, "壊れた出力", ""), "うまく結果を受け取れません"),
])
def test_errors_are_friendly(tmp_path, response, words):
    with pytest.raises(ClaudeError) as e:
        calls.research_product(cc_with(FakeClaude(response)), "P1X9ADBL", cache_dir=tmp_path)
    assert words in e.value.what and e.value.how


def test_missing_claude_binary(monkeypatch, tmp_path):
    monkeypatch.setattr("app.claude.runner.shutil.which", lambda _n: None)
    with pytest.raises(ClaudeError) as e:
        calls.research_product(ClaudeCode(runner=FakeClaude()), "X", cache_dir=tmp_path)
    assert "インストール" in e.value.how


# --- 企画 -----------------------------------------------------------------------

def product_obj() -> Product:
    info = ProductInfo.model_validate(PRODUCT)
    return calls._to_product("P1X9ADBL", info)


def test_plan_fits_target_and_retries_when_too_long():
    # 目標150秒 → 台本に使えるのは142秒。最初は合計300秒で長すぎ、2回目で収まる
    fake = FakeClaude(plan_json(60), plan_json(28.4))
    scenes = calls.make_plan(cc_with(fake), product_obj(), 150, Price(mode="open"), "展示品あり", separate=False)
    assert len(scenes) == 5 and sum(s.seconds for s in scenes) == pytest.approx(142)
    assert [s.id for s in scenes] == ["s1", "s2", "s3", "s4", "s5"]
    first_prompt = fake.calls[0][1]
    assert "142" in first_prompt and "オープン価格" in first_prompt and "展示品あり" in first_prompt
    assert "合計を 142 秒前後" in fake.calls[1][1]


def test_separate_plan_has_talk_only_at_both_ends():
    fake = FakeClaude(separate_plan_json(28.4, bad_talk=True), separate_plan_json(28.4))
    scenes = calls.make_plan(cc_with(fake), product_obj(), 150, Price(), "")
    assert "voice=\"talk\" は最初と最後" in fake.calls[1][1]
    assert [s.voice for s in scenes] == ["talk", "narration", "narration", "narration", "talk"]
    assert scenes[1].script == "" and scenes[1].points == "伝えること1" and scenes[0].script
    assert "映像だけを撮る" in fake.calls[0][1]


def test_price_text():
    assert "表示しない" in calls.price_text(Price())
    assert "オープン価格" in calls.price_text(Price(mode="open"))
    assert "248,780円" in calls.price_text(Price(mode="amount", amount=248780))


# --- テロップ ---------------------------------------------------------------------

TRANSCRIPT = [{"start": 0.5, "end": 3.0, "text": "今回はダイナブックのエックスナインをご紹介します"},
              {"start": 3.2, "end": 7.0, "text": "重さは約1キロと軽いです"}]


def test_telops_follow_length_rules():
    too_long = {"title": "dynabook X", "telops": [{"start": 0.5, "end": 3.0, "text": "ダイナブックX9は持ち歩きにちょうどいい軽さのノートパソコン"}]}
    good = {"title": "dynabook X", "telops": [{"start": 3.2, "end": 6.5, "text": "約1kgの軽さ"},
                       {"start": 0.5, "end": 3.0, "text": "dynabook X9\n新登場"}]}
    fake = FakeClaude(too_long, good)
    scenes = [Scene(id="s1", title="導入", script="ご紹介します", seconds=5)]
    title, telops = calls.make_telops(cc_with(fake), product_obj(), scenes, TRANSCRIPT, 8.0, Price())
    assert title == "dynabook X"
    assert [t.start for t in telops] == [0.5, 3.2]  # 時刻順に並べ直す
    assert all(telop_length(t.text) <= 20 for t in telops)
    assert "20 文字を超えています" in fake.calls[1][1]
    assert "3.2-7.0: 重さは約1キロと軽いです" in fake.calls[0][1]


def test_telops_out_of_range_or_overlapping_are_rejected():
    bad = {"title": "P1X9ADBL", "telops": [{"start": 0, "end": 4, "text": "A"}, {"start": 3, "end": 9.5, "text": "B"}]}
    with pytest.raises(NeedsManualInput) as e:
        calls.make_telops(cc_with(FakeClaude(bad, bad)), product_obj(), [], TRANSCRIPT, 8.0, Price())
    assert "重なって" in e.value.detail and "範囲" in e.value.detail and "型番" in e.value.detail


def test_manual_telops_survive_regeneration():
    from app.project import Telop
    old = [Telop(start=2, end=4, text="手直し", manual=True), Telop(start=5, end=6, text="自動")]
    new = [Telop(start=1, end=3, text="新A"), Telop(start=4.5, end=6, text="新B")]
    merged = calls.merge_manual(new, old)
    assert [t.text for t in merged] == ["手直し", "新B"]


# --- YouTube ---------------------------------------------------------------------

def test_youtube_text():
    out = calls.youtube_text(cc_with(FakeClaude({"title": "dynabook X9 紹介", "description": "・軽い"})),
                             product_obj(), [], Price())
    assert out.title == "dynabook X9 紹介"


def test_prompts_have_no_unfilled_placeholders(tmp_path):
    video = {"summary": "s", "dropped": [],
             "cuts": [{"clip": "a.mov", "start": 0, "end": 3, "scene_id": "s1", "talk": False, "shows": "手元",
                       "reason": "r"}],
             "spares": [{"clip": "a.mov", "start": 4, "end": 6, "scene_id": "s1", "talk": False, "shows": "全体",
                         "reason": "r"}]}
    fake = FakeClaude(PRODUCT, separate_plan_json(28.4), plan_json(28.4),
                      {"title": "見出し", "telops": [{"start": 0, "end": 2, "text": "A"}]},
                      {"title": "t", "description": "d"}, video,
                      {"scenes": [{"scene_id": "s1", "script": "片手で持てる軽さです"}]},
                      {"assignments": [{"file": "v.m4a", "scene_id": "s1", "reason": "r"}]})
    cc = cc_with(fake)
    p, _ = calls.research_product(cc, "P1X9ADBL", cache_dir=tmp_path)
    calls.make_plan(cc, p, 150, Price())
    calls.make_plan(cc, p, 150, Price(), separate=False)
    calls.make_telops(cc, p, [], TRANSCRIPT, 8.0, Price())
    calls.youtube_text(cc, p, [], Price())
    sc = Scene(id="s1", title="軽さ", points="軽い", seconds=3, shots=["持ち上げる"])
    calls.decide_video(cc, p, [sc], [calls.ClipBrief("a.mov", 8.0, [])])
    calls.write_narration(cc, p, [calls.SceneCuts(sc, 3.0, [(0, 3, "手元")])], Price())
    calls.assign_voices(cc, [sc], [("v.m4a", 2.0, "軽いです")])
    assert len(fake.calls) == 8
    for _cmd, stdin, _cwd in fake.calls:
        assert "{{" not in stdin


# --- 商品ページの貼り付け -------------------------------------------------------

FIXTURES = Path(__file__).parent / "fixtures"


def test_paste_text_without_web(tmp_path):
    pasted = {**PRODUCT, "sources": ["www.yodobashi.com"]}  # 正しくない URL は「貼り付け」に置き換える
    fake = FakeClaude(pasted)
    text = (FIXTURES / "yodobashi_P1X9ADBL.txt").read_text(encoding="utf-8")
    product, info = calls.product_from_paste(cc_with(fake), "P1X9ADBL", text=text, cache_dir=tmp_path)
    cmd, stdin, _ = fake.calls[0]
    assert cmd[cmd.index("--tools") + 1] == ""  # Web もファイルも使わせない
    assert "Core Ultra 7 ※355" in stdin and "別の型番の商品の仕様を混ぜない" in stdin
    assert product.sources == ["貼り付け"]
    saved = json.loads((tmp_path / "P1X9ADBL.json").read_text(encoding="utf-8"))
    assert saved["method"] == "paste"


def test_paste_screenshot_is_split_and_readable(tmp_path):
    from PIL import Image
    shot = tmp_path / "long.jpg"
    Image.new("RGB", (1080, 4000), "white").save(shot)
    fake = FakeClaude(PRODUCT)
    calls.product_from_paste(cc_with(fake), "P1X9ADBL", images=[shot], cache_dir=tmp_path / "c")
    cmd, stdin, cwd = fake.calls[0]
    assert cmd[cmd.index("--tools") + 1] == "Read"
    assert "page1_01.png" in stdin and "page1_03.png" in stdin


def test_too_small_screenshot_is_rejected(tmp_path):
    from PIL import Image
    from app.errors import UserError
    shot = tmp_path / "shrunk.jpg"
    Image.new("RGB", (246, 2576), "white").save(shot)  # LINE などで縮小された縦長スクショ
    with pytest.raises(UserError) as e:
        calls.product_from_paste(cc_with(FakeClaude()), "X", images=[shot], cache_dir=tmp_path)
    assert "小さすぎ" in e.value.what and "元の画像" in e.value.how


def test_split_tiles_overlap():
    from PIL import Image
    from app.claude.images import OVERLAP, TILE_HEIGHT, split_image
    import tempfile
    d = Path(tempfile.mkdtemp())
    Image.new("RGB", (2160, 6000), "white").save(d / "big.png")  # 横2160 → 1000に縮小、高さ約2778
    tiles = split_image(d / "big.png", d, "p")
    sizes = [Image.open(t).size for t in tiles]
    assert all(w == 1000 for w, _ in sizes)
    assert sum(h for _, h in sizes) - OVERLAP * (len(tiles) - 1) == pytest.approx(2778, abs=2)
    assert max(h for _, h in sizes) <= TILE_HEIGHT


def test_paste_needs_something(tmp_path):
    from app.errors import UserError
    with pytest.raises(UserError):
        calls.product_from_paste(cc_with(FakeClaude()), "X", cache_dir=tmp_path)
