# signage-studio

店頭サイネージ動画の自動編集ツール。仕様と開発ルールは [CLAUDE.md](CLAUDE.md) を参照。

## はじめて使う PC での準備

1. Claude Code をインストールしてログイン
2. uv と FFmpeg を入れる
   - Mac: `brew install uv ffmpeg`
   - Windows: `winget install astral-sh.uv` と `winget install ffmpeg`
3. このリポジトリを取り込み、フォルダで `uv sync` を実行
4. `assets/bgm/` にヨドバシカメラの歌、`assets/logo/` にロゴ PNG（背景透過）を置く（Git には含めない）

## 動作確認

```
uv run pytest
```

すべて passed になれば準備完了です。

## フォント

`assets/fonts/` の6書体はすべて SIL Open Font License 1.1 で、同梱・商用利用が認められています。
ライセンス文書は `assets/fonts/licenses/` にあります。
