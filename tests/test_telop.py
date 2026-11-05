"""段階4: 型番補正・フォント選択・テロップ描画・タイトル/エンドカードのテスト。"""

from __future__ import annotations

from pathlib import Path

import pytest
from PIL import Image

from app.pipeline import cards, fonts
from app.pipeline.ffmpeg import probe
from app.pipeline.fixterms import TermFixer, collect_terms, kanji_number, log_text
from app.pipeline.telop_render import ass_time, build_telop_ass, burn_ass
from app.pipeline.transcribe import Word, hint_text, to_phrases
from app.platform import get_profile
from app.project import Product, Telop
from tests.media import make_clip

FMV = Product(
    model_number="FMVU59L2BA", name="富士通 NOTE Uシリーズ 14型 FMVU59L2BA", maker="富士通", category="パソコン",
    specs={"CPU": "インテル Core Ultra 5 プロセッサー 226V（8コア）", "無線LAN": "Wi-Fi 7",
           "OS": "Windows 11 Home", "NPU": "インテル AI Boost（最大40TOPS）"},
    features=["NPU搭載のCopilot+ PCで、AI機能も使えます。"],
)


@pytest.fixture(scope="module")
def profile():
    return get_profile()


# --- 型番補正 --------------------------------------------------------------------

@pytest.mark.parametrize("heard, fixed", [
    ("こちらはエフエムブイユー五九エルツービーエーです", "こちらはFMVU59L2BAです"),
    ("fmvu 59 l2ba", "FMVU59L2BA"),
    ("ＦＭＶＵ５９Ｌ２ＢＡ", "FMVU59L2BA"),
    ("CPUはコアウルトラ5です", "CPUはCore Ultra 5です"),
    ("コアウルトラ五", "Core Ultra 5"),
    ("ワイファイ7に対応", "Wi-Fi 7に対応"),
    ("ウィンドウズ11ホーム", "Windows 11 Home"),
    ("コパイロットプラスPC", "Copilot+PC"),
    ("エーアイブースト搭載", "AI Boost搭載"),
])
def test_fixes_typical_misrecognitions(heard, fixed):
    assert TermFixer.for_product(FMV).fix(heard)[0] == fixed


@pytest.mark.parametrize("text", ["ノートパソコンのUシリーズ", "ブイヤベースが好き", "コアの数は8", "WindowsXP"])
def test_leaves_ordinary_words_alone(text):
    out, fixes = TermFixer.for_product(FMV).fix(text)
    assert out == text and fixes == []


def test_fix_log():
    _, fixes = TermFixer.for_product(FMV).fix("ワイファイ7", "3.2秒")
    assert log_text(fixes) == "3.2秒\tワイファイ7 → Wi-Fi 7\n"
    assert "ありません" in log_text([])


def test_terms_and_hint():
    terms = collect_terms(FMV)
    assert terms[0] == "Windows 11 Home" and "FMVU59L2BA" in terms
    assert "NOTE" in terms and "富士通" not in terms
    assert hint_text(["FMVU59L2BA", "Wi-Fi 7", "FMVU59L2BA"]) == "FMVU59L2BA、Wi-Fi 7。"
    assert kanji_number(355) == "三百五十五" and kanji_number(1088) == "千八十八"


def test_phrases_break_on_gaps_and_punctuation():
    words = [Word(0, 0.3, "こちらは"), Word(0.3, 0.6, "富士通です。"), Word(0.7, 1.0, "重さは"),
             Word(2.0, 2.4, "約908g")]
    assert [p.text for p in to_phrases(words)] == ["こちらは富士通です。", "重さは", "約908g"]


# --- フォント --------------------------------------------------------------------

@pytest.mark.parametrize("text, role, category, expected", [
    ("軽さで選ぶ", "title", "パソコン", "dela"),
    ("FMVU59L2BA", "title", "パソコン", "biz_ud"),           # 型番入りの見出しに Dela は使わない
    ("P1X9ADBL 登場", "title", "パソコン", "biz_ud"),
    ("ご飯が早い", "title", "キッチン家電", "rounded"),
    ("FMVU59L2BA", "subtitle", "パソコン", "biz_ud"),
    ("持ち歩きがラク！", "telop", "パソコン", "noto_black"),     # 10文字まで
    ("タッチ対応の画面で操作も直感的に", "telop", "パソコン", "noto_bold"),  # 11〜20文字
    ("約908gの軽さ", "telop", "パソコン", "biz_ud"),          # 数字を含む
    ("持ち歩きがラクで毎日のお出かけにもカフェでの作業にも", "telop", "パソコン", "biz_ud"),  # 長め
    ("Core Ultra 5 226V搭載", "telop", "パソコン", "biz_ud"),   # 型番らしい語
    ("ご飯がすぐ炊ける", "telop", "キッチン家電", "rounded"),
    ("ご飯が25分で炊ける", "telop", "キッチン家電", "biz_ud"),
    ("EPC01A-20BK", "telop", "キッチン家電", "biz_ud"),
])
def test_font_rules(text, role, category, expected):
    assert fonts.choose_font(text, role, category, "FMVU59L2BA") == expected


def test_dela_never_used_with_model_number():
    for text in ["FMVU59L2BA", "新しいFMVU59L2BA", "fmvu59l2ba"]:
        for role in ("title", "telop", "subtitle"):
            assert fonts.choose_font(text, role, "パソコン", "FMVU59L2BA") != "dela"


def test_longer_text_gets_smaller_and_fits():
    sizes = [fonts.font_size(t) for t in ["軽い", "持ち歩きがラクです！", "約908gで毎日の持ち歩きがラク", "あ" * 20]]
    assert sizes == sorted(sizes, reverse=True)
    assert all(fonts.visual_width("あ" * 20) * fonts.font_size("あ" * 20) <= 1700 for _ in [0])


def test_all_font_files_exist():
    for f in fonts.FONTS.values():
        assert (fonts.FONTS_DIR / f.file).exists()


# --- ASS と焼き込み -----------------------------------------------------------------

def test_ass_file(tmp_path):
    telops = [Telop(start=3.2, end=6.5, text="約908gの軽さ"),
              Telop(start=0.5, end=3.0, text="FMV\nNOTE U", font="dela"),       # 手動指定は優先
              Telop(start=7, end=9, text="{危険}な\\記号")]
    path = build_telop_ass(telops, tmp_path / "t.ass", "パソコン", "FMVU59L2BA")
    text = path.read_text(encoding="utf-8-sig")
    assert "PlayResX: 1920" in text and "Style: dela,Dela Gothic One" in text
    events = [l for l in text.splitlines() if l.startswith("Dialogue")]
    assert events[0].startswith("Dialogue: 0,0:00:00.50,0:00:03.00,dela") and "FMV\\NNOTE U" in events[0]
    assert "｛危険｝な＼記号" in events[2]
    assert ass_time(3725.456) == "1:02:05.46"


def _bright_pixels(png: Path, box: tuple[int, int, int, int]) -> int:
    im = Image.open(png).convert("L").crop(box)
    return sum(im.histogram()[241:])


def test_burn_and_cards(profile, tmp_path):
    src = make_clip(tmp_path / "src.mp4", 4, size="1920x1080", speech=[(0.5, 3.5)])
    # 背景を暗くしてから焼き込み、白い文字が下部に出ることを確かめる
    dark = tmp_path / "dark.mp4"
    import subprocess
    subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-i", str(src), "-vf", "eq=brightness=-0.9",
                    "-c:a", "copy", str(dark)], check=True)
    ass = build_telop_ass([Telop(start=0.5, end=3.5, text="約908gの軽さ")], tmp_path / "t.ass")
    out = burn_ass(profile, dark, ass, tmp_path / "burned.mp4")
    for name, src_video in (("before", dark), ("after", out)):
        subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-ss", "2", "-i", str(src_video), "-frames:v", "1",
                        str(tmp_path / f"{name}.png")], check=True)
    lower = (300, 850, 1620, 1000)
    assert _bright_pixels(tmp_path / "after.png", lower) > _bright_pixels(tmp_path / "before.png", lower) + 2000

    title = cards.make_title_card(profile, out, "軽さで選ぶ FMV", "FMVU59L2BA", tmp_path / "title.mp4")
    end = cards.make_end_card(profile, tmp_path / "end.mp4")
    logo = tmp_path / "logo.png"
    Image.new("RGBA", (800, 200), (220, 0, 30, 255)).save(logo)
    end_logo = cards.make_end_card(profile, tmp_path / "end_logo.mp4", logo)
    for video, seconds in ((title, 3.0), (end, 5.0), (end_logo, 5.0)):
        info = probe(profile.ffprobe, video)
        v = next(s for s in info["streams"] if s["codec_type"] == "video")
        assert (v["width"], v["height"]) == (1920, 1080)
        assert float(info["format"]["duration"]) == pytest.approx(seconds, abs=0.1)
        assert any(s["codec_type"] == "audio" for s in info["streams"])
    subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-ss", "2", "-i", str(end_logo), "-frames:v", "1",
                    str(tmp_path / "end.png")], check=True)
    assert Image.open(tmp_path / "end.png").convert("RGB").getpixel((960, 540))[0] > 180  # 中央にロゴ


def test_find_logo(tmp_path):
    assert cards.find_logo(tmp_path) is None
    (tmp_path / "b.png").write_bytes(b"x")
    (tmp_path / "a.txt").write_bytes(b"x")
    assert cards.find_logo(tmp_path).name == "b.png"


def test_font_line_ratios_match_font_files():
    """フォントを差し替えても、文字の大きさがそろうことを確かめる。"""
    from fontTools.ttLib import TTFont
    for f in fonts.FONTS.values():
        t = TTFont(fonts.FONTS_DIR / f.file, lazy=True)
        real = (t["OS/2"].usWinAscent + t["OS/2"].usWinDescent) / t["head"].unitsPerEm
        assert f.line_ratio == pytest.approx(real, abs=0.002), f.file
    assert fonts.ass_size("noto_bold", 100) == 145 and fonts.ass_size("biz_ud", 100) == 100
