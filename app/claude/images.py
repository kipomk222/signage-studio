"""商品ページのスクリーンショットを、Claude が読める大きさに切り分ける。

スマホの「スクロールして全体を撮る」スクショは極端に縦長で、そのまま渡すと縮小されて
文字がつぶれる。横幅をそろえ、少し重ねながら縦に分割する。
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageOps

from app.errors import UserError

MIN_WIDTH = 600       # これより細い画像は、送るときに縮小されて文字が読めない可能性が高い
TILE_WIDTH = 1000
TILE_HEIGHT = 1400
OVERLAP = 120         # 行が切れ目で分断されても読めるように重ねる
MAX_TILES = 30
IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".heic", ".bmp"}

TOO_SMALL = ("画像が小さすぎて、文字が読めません。",
             "チャットやLINEで送った画像は縮小されるため、スマホで撮った元の画像をPCに移して入れてください。")
TOO_LONG = ("画像が長すぎます。",
            "商品情報と仕様の部分だけを撮り直すか、画像を何枚かに分けて入れてください。")
UNREADABLE = ("画像を開けませんでした。", "PNG か JPEG の画像を入れてください。")


def split_image(src: Path, out_dir: Path, prefix: str) -> list[Path]:
    try:
        im = Image.open(src)
        im = ImageOps.exif_transpose(im).convert("RGB")
    except Exception as exc:
        raise UserError(*UNREADABLE) from exc
    w, h = im.size
    if w < MIN_WIDTH:
        raise UserError(*TOO_SMALL)
    if w > TILE_WIDTH:
        im = im.resize((TILE_WIDTH, round(h * TILE_WIDTH / w)), Image.LANCZOS)
        w, h = im.size

    tiles: list[Path] = []
    top = 0
    while True:
        bottom = min(h, top + TILE_HEIGHT)
        path = out_dir / f"{prefix}_{len(tiles) + 1:02d}.png"
        im.crop((0, top, w, bottom)).save(path, optimize=True)
        tiles.append(path)
        if bottom >= h:
            break
        top = bottom - OVERLAP
        if len(tiles) >= MAX_TILES:
            raise UserError(*TOO_LONG)
    return tiles


def prepare_images(paths: list[Path], out_dir: Path) -> list[Path]:
    """複数の画像を、ページの上から順に読める分割画像のリストにする。"""
    out_dir.mkdir(parents=True, exist_ok=True)
    tiles: list[Path] = []
    for i, p in enumerate(paths, 1):
        tiles += split_image(Path(p), out_dir, f"page{i}")
        if len(tiles) > MAX_TILES:
            raise UserError(*TOO_LONG)
    return tiles
