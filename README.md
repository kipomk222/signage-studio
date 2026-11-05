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

## 素材から完成動画を作る（画面なし版）

```
uv run python -m app.pipeline.run "D:\素材" --model EPC01A-20BK --out store,youtube
```

- 文字起こし → 型番の補正 → 要点テロップ → 冒頭タイトル・エンドカードまで自動で入ります
  - 商品情報は「保存済み → Web で調査」の順に探します。`--paste ページ.txt` / `--image スクショ.jpg` で商品ページからも作れます
  - `--price open`（オープン価格）や `--price 248780`、`--extra "展示品あり"` でテロップに反映できます
  - `--no-telop` でテロップなし（段階2と同じ）
  - 文字起こしは初回だけモデルのダウンロード（約1.5GB）があり、数分かかります
  - ロゴは `assets/logo/` に PNG を置くとエンドカードに使われます（ないときは仮の無地）
- 完成動画とレポートは `projects/<日付>_<型番>/output/` にできます（型番の修正は「字幕修正ログ.txt」）
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
- Web で商品を特定できないときは、商品ページの文章かスクリーンショットから整理できます
  - 文章: PC のブラウザで商品ページを開き、全選択してコピー → テキストファイルに保存して `--paste ページ.txt`
  - 画像: スマホの縦長スクショ（元の画像のまま）を PC に移して `--image スクショ.jpg`（LINE などで送って縮小された画像は読めません）

## フォント

`assets/fonts/` の6書体はすべて SIL Open Font License 1.1 で、同梱・商用利用が認められています。
ライセンス文書は `assets/fonts/licenses/` にあります。
