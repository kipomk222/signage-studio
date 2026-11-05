"""段階3の確認用: 型番から商品調査 → 企画まで通し、結果を表示してプロジェクトに保存する。

使い方:
    uv run python -m app.claude.check P1X9ADBL PC-S1355NAP-YC FMVU59L2BA [--target 150] [--refresh]
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from app.claude.calls import make_plan, research_product
from app.claude.runner import ClaudeCode, ClaudeError
from app.project import Price, create_project, save_project

ROOT = Path(__file__).resolve().parents[2]


def show_product(p, info) -> None:
    print(f"  商品名: {p.name}\n  メーカー: {p.maker} / カテゴリ: {p.category}")
    for k, v in p.specs.items():
        print(f"  ・{k}: {v}")
    print("  特徴:")
    for f in p.features:
        print(f"    - {f}")
    print("  参照: " + ", ".join(p.sources))
    if info.note:
        print(f"  メモ: {info.note}")


def show_plan(scenes) -> None:
    total = sum(s.seconds for s in scenes)
    print(f"  台本（場面 {len(scenes)} 個・合計 {total:.0f} 秒）")
    for s in scenes:
        print(f"  【{s.title}】{s.seconds:.0f}秒")
        print(f"    セリフ: {s.script}")
        print(f"    撮るカット: {' / '.join(s.shots)}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="型番から商品調査と企画を試します。")
    ap.add_argument("models", nargs="+", help="型番（複数可）")
    ap.add_argument("--target", type=int, default=150, help="目標の長さ（秒）")
    ap.add_argument("--refresh", action="store_true", help="保存済みの調査結果を使わず調べ直す")
    ap.add_argument("--projects", type=Path, default=ROOT / "projects")
    args = ap.parse_args(argv)

    cc = ClaudeCode()
    ok = 0
    for model in args.models:
        print(f"\n===== {model} =====")
        try:
            print("商品を調べています（1〜5分ほどかかります）")
            product, info = research_product(cc, model, refresh=args.refresh)
            if not info.found:
                print(f"  商品を特定できませんでした。\n  メモ: {info.note}")
                continue
            show_product(product, info)
            print("台本と構成を作っています")
            scenes = make_plan(cc, product, args.target, Price())
            show_plan(scenes)
        except ClaudeError as e:
            print(f"  {e.what}\n  {e.how}")
            continue
        project, folder = create_project(args.projects, model)
        project.product = product
        project.plan.target_seconds = args.target
        project.plan.scenes = scenes
        save_project(folder, project)
        print(f"  保存しました: {folder}")
        ok += 1
    print(f"\n{len(args.models)} 件中 {ok} 件で、商品情報と台本を作れました。")
    return 0 if ok == len(args.models) else 1


if __name__ == "__main__":
    sys.exit(main())
