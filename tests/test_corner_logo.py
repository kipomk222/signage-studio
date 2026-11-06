"""動画の左上にずっと出すロゴのテスト。"""

from __future__ import annotations

import subprocess

import pytest
from PIL import Image

from app import workflow as wf
from app.pipeline import cards
from app.pipeline.cards import CORNER_MARGIN, corner_logo_image, find_corner_logo
from app.platform import get_profile
from app.project import Product, Telop, create_project, save_project


def red_logo(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGBA", (400, 100), (230, 0, 18, 255)).save(path)
    return path


def frame_rgb(video, t, x, y, tmp_path):
    out = tmp_path / f"f_{t}.png"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-ss", str(t), "-i", str(video), "-frames:v", "1", str(out)], check=True)
    return Image.open(out).convert("RGB").getpixel((x, y))


def is_red(rgb):
    r, g, b = rgb
    return r > 180 and g < 70 and b < 80


@pytest.fixture
def project_folder(tmp_path):
    project, folder = create_project(tmp_path / "p", "X-1")
    project.product = Product(model_number="X-1", name="テストPC")
    project.title = "テストPC"
    project.telops = [Telop(start=0.5, end=2.5, text="軽い")]
    save_project(folder, project)
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "color=c=0x406080:s=1920x1080:r=30:d=4",
                    "-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo", "-t", "4", "-c:v", "libx264", "-pix_fmt", "yuv420p",
                    "-c:a", "aac", str(folder / "work" / "joined.mp4")], check=True)   # 青っぽい無地の本編
    return project, folder


def test_logo_on_title_and_main_but_not_end_card(project_folder, tmp_path):
    project, folder = project_folder
    red_logo(cards.LOGO_DIR / "logo.png")             # 左上用がなければエンドカード用を使う
    notes: list[str] = []
    video = wf.render_telop_video(get_profile(), project, folder, notes=notes)
    pad = round(280 * 0.06)
    x, y = CORNER_MARGIN + pad + 20, CORNER_MARGIN + pad + 10
    assert is_red(frame_rgb(video, 1.0, x, y, tmp_path))      # 冒頭タイトル
    assert is_red(frame_rgb(video, 5.0, x, y, tmp_path))      # 本編
    assert not is_red(frame_rgb(video, 10.0, x, y, tmp_path))  # エンドカード（真ん中にロゴがある）
    assert not any("左上のロゴ" in n for n in notes)


def test_corner_folder_first_and_switch_off(project_folder, tmp_path):
    project, folder = project_folder
    red_logo(cards.LOGO_DIR / "end.png")
    corner = red_logo(cards.CORNER_LOGO_DIR / "corner.png")
    assert find_corner_logo() == corner
    project.settings.corner_logo.show = False
    video = wf.render_telop_video(get_profile(), project, folder, notes=[])
    assert not is_red(frame_rgb(video, 5.0, CORNER_MARGIN + 30, CORNER_MARGIN + 20, tmp_path))


def test_no_logo_note(project_folder):
    project, folder = project_folder
    notes: list[str] = []
    wf.render_telop_video(get_profile(), project, folder, notes=notes)
    assert any("assets/corner_logo" in n for n in notes)


def test_corner_image_sizes(tmp_path):
    logo = red_logo(tmp_path / "l.png")
    small = Image.open(corner_logo_image(logo, tmp_path / "s.png", "small", plate=False))
    large = Image.open(corner_logo_image(logo, tmp_path / "L.png", "large", plate=True))
    assert small.width == 220 and large.width > 360 and large.getpixel((2, large.height // 2))[3] > 200
