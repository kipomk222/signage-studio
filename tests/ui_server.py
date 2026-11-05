"""画面の確認用サーバー（ブラウザで開く。文字起こしは代用品、Claude Code は本物）。

    uv run python tests/ui_server.py <プロジェクトの保存先> <ポート>
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import workflow as wf  # noqa: E402
from app.ui.main import start  # noqa: E402
from tests.media import ScriptedTranscriber  # noqa: E402

SCRIPT = ["こちらは富士通のエフエムブイノートユーシリーズです", "重さは約九百八グラムととても軽いんです",
          "バッテリーは動画再生で約十四点五時間持ちます", "CPUはコアウルトラ五を搭載しています",
          "十四型のタッチ対応の画面で操作も直感的です", "コパイロットプラスPCなのでAIも快適に使えます"]

if __name__ in {"__main__", "__mp_main__"}:
    start(native=False, port=int(sys.argv[2]), projects=Path(sys.argv[1]),
          services=wf.Services(transcriber=ScriptedTranscriber(SCRIPT)), show=False)
