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
