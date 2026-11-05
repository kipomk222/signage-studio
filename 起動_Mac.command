#!/bin/bash
# サイネージ動画メーカーを起動します（ダブルクリックで開きます）
cd "$(dirname "$0")"
export PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:$PATH"
if ! command -v uv >/dev/null 2>&1; then
  echo "起動に必要な「uv」が見つかりません。"
  echo "ターミナルで「brew install uv」を実行してから、もう一度ダブルクリックしてください。"
  read -r -p "Enter キーで閉じます"
  exit 1
fi
echo "サイネージ動画メーカーを起動しています。初回は準備に数分かかります…"
uv sync -q && uv run python -m app.ui.main
status=$?
if [ $status -ne 0 ]; then
  echo ""
  echo "起動できませんでした。もう一度ダブルクリックしてください。"
  echo "専用ウィンドウが開かない場合は、ターミナルで「uv run python -m app.ui.main --browser」を実行するとブラウザで開きます。"
  read -r -p "Enter キーで閉じます"
fi
