"""ファイルの置き換え（書きかけの一時ファイル → 本物の名前）。

Windows では、画面で再生中・読み込み中のファイルは名前の置き換えができないことがある。
少し待ってやり直し、それでも駄目なら中身を上書きコピーする（再生中でも上書きはできる）。
"""

from __future__ import annotations

import os
import shutil
import time
from pathlib import Path

ATTEMPTS = 10
WAIT = 0.15


def replace_file(src: Path | str, dst: Path | str) -> None:
    src, dst = Path(src), Path(dst)
    for i in range(ATTEMPTS):
        try:
            os.replace(src, dst)
            return
        except PermissionError:
            if i < ATTEMPTS - 1:
                time.sleep(WAIT)
    shutil.copyfile(src, dst)
    try:
        src.unlink()
    except OSError:
        pass


def same_file(a: Path | str, b: Path | str) -> bool:
    """同じファイルとみなせるか（大きさと更新日時。copy2 は更新日時も写すので、取り込んだ物とも一致する）。"""
    try:
        sa, sb = Path(a).stat(), Path(b).stat()
    except OSError:
        return False
    return sa.st_size == sb.st_size and int(sa.st_mtime) == int(sb.st_mtime)


def copy_in(src: Path | str, folder: Path | str) -> Path:
    """取り込み: src を folder にコピーして、コピー先を返す（元のファイルは変えない）。

    - 同じファイルが既にあれば、コピーせずにそれを返す
    - 同じ名前で中身が違えば「名前_2」「名前_3」… と空いている名前にする
    - 仮の名前（.part）に書いてから本当の名前にする（途中で止まっても、壊れたファイルが残らない）
    """
    src, folder = Path(src), Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    name, n = src.name, 2
    while (folder / name).exists() and not same_file(folder / name, src):
        name = f"{src.stem}_{n}{src.suffix}"
        n += 1
    dst = folder / name
    if not dst.exists():
        part = dst.with_name(dst.name + ".part")
        shutil.copy2(src, part)
        replace_file(part, dst)
    return dst
