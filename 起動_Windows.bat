@echo off
chcp 65001 > nul
rem サイネージ動画メーカーを起動します（ダブルクリックで開きます）
cd /d "%~dp0"
where uv > nul 2>&1
if errorlevel 1 (
  echo 起動に必要な「uv」が見つかりません。
  echo コマンドプロンプトで「winget install astral-sh.uv」を実行し、ウィンドウを開き直してから、もう一度ダブルクリックしてください。
  pause
  exit /b 1
)
echo サイネージ動画メーカーを起動しています。初回は準備に数分かかります…
uv sync -q
uv run python -m app.ui.main
if errorlevel 1 (
  echo.
  echo 起動できませんでした。もう一度ダブルクリックしてください。
  echo 専用ウィンドウが開かない場合は「uv run python -m app.ui.main --browser」でブラウザで開きます。
  pause
)
