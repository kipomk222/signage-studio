# signage-studio

店頭サイネージ動画の自動編集ツール。仕様と開発ルールは [CLAUDE.md](CLAUDE.md) を参照。

## はじめて使う PC での準備

1. Claude Code をインストールしてログイン
2. uv と FFmpeg を入れる
   - Mac: `brew install uv ffmpeg`
   - Windows: `winget install astral-sh.uv` と `winget install ffmpeg`
3. このリポジトリを取り込み、フォルダで `uv sync` を実行
4. `assets/bgm/` にヨドバシカメラの歌、`assets/logo/` にロゴ PNG（背景透過）を置く（Git には含めない）

## 起動（ふだんはこれだけ）

- Mac: `起動_Mac.command` をダブルクリック
- Windows: `起動_Windows.bat` をダブルクリック

専用ウィンドウで「サイネージ動画メーカー」が開きます。初回だけ準備に数分かかります。
専用ウィンドウが開かないときは `uv run python -m app.ui.main --browser` でブラウザで開けます。

画面の流れ: ホーム →「新しく企画する」→ ① 企画（型番→商品→台本→撮影リスト）→ ② 素材（フォルダを選ぶ→編集を始める）
→ ③ 確認（プレビュー・テロップ・色・BGM）→ ④ 書き出し（店頭用／YouTube用）。途中で閉じても、ホームの「続きから」で再開できます。

「編集を始める」では、Claude Code が素材の話した内容と映像を見て、言い直しや「えー」を切り、台本の順に並べ、
手元アップなどの映像を説明に差し込みます。確認の画面で、何をどう編集したか（理由つき）を見て直せます。
どの動画の画面でも、右上の「Claude に相談」から日本語で相談・指示ができます（例「強調の色を赤にして」）。
Claude は返事と「変更案」を出し、「反映する」を押したときだけ動画が変わります。「今後は〜して」と言うとメモリに残ります。
右上の歯車（設定）では、Claude へのサインイン・接続テスト・使用量・メモリ・claude.ai のプロジェクト用の指示文・履歴を扱えます。
Mac で初めて開くときに「開発元を確認できない」と出たら、右クリック →「開く」で開いてください。

## 動作確認

```
uv run pytest
```

すべて passed になれば準備完了です。

## コマンドで使う（画面なし版）

```
uv run python -m app.pipeline.run "D:\素材" --model EPC01A-20BK --out store,youtube
```

- 文字起こし → 型番の補正 → 要点テロップ → 冒頭タイトル・エンドカードまで自動で入ります
  - 商品情報は「保存済み → Web で調査」の順に探します。`--paste ページ.txt` / `--image スクショ.jpg` で商品ページからも作れます
  - `--price open`（オープン価格）や `--price 248780`、`--extra "展示品あり"` でテロップに反映できます
  - `--no-telop` でテロップなし（段階2と同じ）
  - `--no-ai-edit` で、編集の判断を Claude Code にまかせず機械的に編集（無音カットと台本の順）
  - 文字起こしは初回だけモデルのダウンロード（約1.5GB）があり、数分かかります
  - ロゴは `assets/logo/` に PNG を置くとエンドカードに使われます（ないときは仮の無地）
- テロップの色はカラーコードで指定できます
  ```
  uv run python -m app.pipeline.run "D:\素材" --model FMVU59L2BA --emphasis-color "#E60012" --outline-color "#FFFFFF" --text-color "#1A1A1A"
  ```
  - `--text-color`（文字）、`--outline-color`（縁取り）、`--emphasis-color`（`**強調**` した言葉）、`--band-color`（文字の後ろの帯）、`--band-opacity`（帯の濃さ 0〜1）
  - 1枚だけ色を変えたいときは、`project.json` のそのテロップに `"text_color": "#00A0E9"` などを書きます
- 色や文言を直したあと、書き出しだけやり直す（文字起こしやテロップ作りはやり直しません）
  ```
  uv run python -m app.pipeline.run --rerender "projects/2026-11-05_FMVU59L2BA" --band-color "#000000"
  ```
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
