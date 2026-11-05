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

## 素材から完成動画を作る（段階2・画面なし版）

```
uv run python -m app.pipeline.run "D:\素材" --model EPC01A-20BK --out store,youtube
```

- 完成動画とレポートは `projects/<日付>_<型番>/output/` にできます
- 元の素材フォルダは変更しません（プロジェクトの「素材」にコピーして使います）
- 主なオプション: `--strength weak|normal|strong`（無音カットの強さ）、`--level small|medium|large`（BGM 音量）、
  `--fit pad|crop`（縦撮りを黒帯／切り抜き）、`--keep-all`（除外候補も使う）、`--target 150`（目標秒数）

## 型番から商品調査と台本づくりを試す（段階3）

```
uv run python -m app.claude.check P1X9ADBL PC-S1355NAP-YC FMVU59L2BA
```

- Claude Code にログインしている必要があります（API キーは不要）
- 調べた商品情報は `cache/products/` に保存され、2回目からは再利用されます（`--refresh` で調べ直し）
- 結果はプロジェクトとして `projects/` に保存されます

## フォント

`assets/fonts/` の6書体はすべて SIL Open Font License 1.1 で、同梱・商用利用が認められています。
ライセンス文書は `assets/fonts/licenses/` にあります。
