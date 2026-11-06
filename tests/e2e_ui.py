"""画面を通しで自動操作する確認（手動で実行）。

    1. uv run python tests/ui_server.py <保存先> 8765
    2. python tests/e2e_ui.py <作業フォルダ（demo/素材 と shots/ を置く）> [URL]
"""
import sys, re
from playwright.sync_api import sync_playwright, expect
S = sys.argv[1]; URL = sys.argv[2] if len(sys.argv) > 2 else "http://127.0.0.1:8765"
SH = f"{S}/shots"
def shot(page, name, full=True):
    page.screenshot(path=f"{SH}/{name}.png", full_page=full); print("shot", name, flush=True)
def wait_job_done(page, timeout=600):
    page.wait_for_timeout(800)
    page.locator(".q-dialog .q-linear-progress").first.wait_for(state="detached", timeout=timeout * 1000)
with sync_playwright() as p:
    b = p.chromium.launch(); page = b.new_page(viewport={"width": 1320, "height": 900})
    page.on("console", lambda m: print("console:", m.text) if m.type == "error" else None)
    page.goto(URL); page.wait_for_timeout(1500); shot(page, "01_home")
    page.get_by_role("button", name="新しく企画する").click()
    page.get_by_label("型番（例: FMVU59L2BA）").fill("FMVU59L2BA"); page.wait_for_timeout(300)
    page.get_by_role("button", name="はじめる").click()
    page.wait_for_url(re.compile(r".*/plan.*")); wait_job_done(page, 120); page.wait_for_timeout(1500)
    expect(page.get_by_label("商品名")).to_have_value(re.compile("NOTE U"), timeout=60000)
    shot(page, "02_plan_product")
    page.get_by_role("button", name="台本を作る").click()
    page.wait_for_timeout(1500); shot(page, "03_progress", full=False)
    wait_job_done(page, 300); page.wait_for_timeout(2500)
    expect(page.get_by_text("場面の合計")).to_be_visible(timeout=60000)
    shot(page, "04_plan_script")
    page.get_by_role("button", name="企画を確定して撮影リストを出す").click()
    page.get_by_text("撮影リストができました").wait_for(); shot(page, "05_confirmed", full=False)
    page.get_by_role("button", name="撮影が終わったら：素材を入れる").click()
    page.wait_for_url(re.compile(r".*/material")); page.wait_for_timeout(1500)
    page.get_by_label("フォルダの場所").fill(f"{S}/demo/素材"); page.get_by_label("フォルダの場所").press("Enter")
    wait_job_done(page, 300); page.wait_for_timeout(2000)
    expect(page.get_by_text(re.compile("本中 .*本を使います"))).to_be_visible(timeout=60000)
    shot(page, "06_material")
    page.get_by_role("button", name="編集を始める").click()
    page.wait_for_timeout(4000); shot(page, "06b_editing", full=False)
    page.get_by_role("button", name="中止").click()
    page.get_by_text("中止しました。").wait_for(timeout=30000); print("cancel ok", page.url, flush=True)
    page.wait_for_timeout(1000)
    page.get_by_role("button", name="編集を始める").click()
    page.wait_for_url(re.compile(r".*/review"), timeout=900000); page.wait_for_timeout(2500)
    shot(page, "07_review")
    page.get_by_role("button", name="プレビューを作る").click(); wait_job_done(page, 600); page.wait_for_timeout(2500)
    page.locator("video").first.evaluate("v => { v.currentTime = 4.5; }"); page.wait_for_timeout(1500)
    shot(page, "08_preview", full=False)
    emph = page.get_by_label("強調（**で囲んだ言葉）"); emph.fill("#E60012"); emph.press("Tab"); page.wait_for_timeout(800)
    page.get_by_role("button", name="書き出しへ進む").click()
    page.wait_for_url(re.compile(r".*/export")); page.wait_for_timeout(1500)
    page.get_by_text("YouTube用（概要欄の説明文も作ります）").click()
    page.get_by_role("button", name="書き出す").click(); wait_job_done(page, 900); page.wait_for_timeout(2500)
    expect(page.get_by_text("できた動画")).to_be_visible(timeout=60000)
    shot(page, "09_export")
    page.goto(URL); page.wait_for_timeout(1500); shot(page, "10_home_resume")
    page.get_by_role("button", name="開く").first.click(); page.wait_for_url(re.compile(r".*/export")); print("resume ok:", page.url)
    b.close()
