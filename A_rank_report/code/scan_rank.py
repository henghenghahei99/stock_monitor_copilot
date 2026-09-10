#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
一键流程: 策略扫描 -> (A股自动补行业) -> 行业加权分排行。

把整个"筛选 -> 补行业 -> 行业分统计"流程合成一步:
  1) 用 run_strategy 扫描(默认 A股上涨趋势)
  2) A股自动确保行业数据(cn_industry.json 缺失/过期时自动从东方财富拉取)
  3) 按行业聚合加权分(默认 8->8, 7->6, 6->4, 6分以下不计), 按平均分排行
输出: 命中结果CSV + 行业排行CSV, 控制台打印排行表。

用法:
  python scan_rank.py --market cn --strategies uptrend
  python scan_rank.py --market cn --strategies uptrend --min-volume 30000000 --workers 6
  python scan_rank.py --market us --strategies pullback --col pullback --score-map "8:8,7:6,6:4"
  python scan_rank.py --market hk --strategies rsi --top 20 --delay 0.2
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import find_overbought_stocks as fos  # noqa: E402
import industry_score  # noqa: E402
import run_strategy  # noqa: E402


def ensure_cn_industry() -> None:
    """确保 data/cn_industry.json 存在且未过期, 否则自动从东财拉取。"""
    import fetch_cn_industry as fci  # noqa: PLC0415
    if (os.path.exists(fci.CACHE_FILE)
            and (time.time() - os.path.getmtime(fci.CACHE_FILE)) < fci.CACHE_TTL_DAYS * 86400):
        return
    print("[行业] 本地A股行业缓存缺失/过期, 从东方财富拉取...")
    ind = fci.fetch_all()
    if not ind:
        print("[行业] 拉取失败, 继续(行业列可能为空)。", file=sys.stderr)
        return
    with open(fci.CACHE_FILE, "w", encoding="utf-8") as f:
        json.dump(ind, f, ensure_ascii=False)
    print(f"[行业] 已保存 {len(ind)} 只 -> {fci.CACHE_FILE}")


STRATEGY_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "strategies")


def load_meta_strategy(strategies_arg: str) -> dict | None:
    """若 --strategies 指向 strategies/xxx.json 且 type==sector_rank, 返回其配置。

    例如 a_rank: {"market":"cn","base":"uptrend","score_map":"8:8,7:6,6:4",
                "top":10, "sort":"平均分"} —— 底层扫描 base 策略, 再做板块加权排名。
    """
    sid = strategies_arg.split(",")[0].strip()
    path = os.path.join(STRATEGY_DIR, f"{sid}.json")
    if not os.path.exists(path):
        return None
    try:
        with open(path, encoding="utf-8") as f:
            cfg = json.load(f)
    except Exception:  # noqa: BLE001
        return None
    return cfg if cfg.get("type") == "sector_rank" else None


def main() -> None:
    p = argparse.ArgumentParser(description="一键: 策略扫描 -> 行业加权分排行(支持 sector_rank 元策略如 a_rank)")
    # --- 扫描参数(透传给 run_strategy.execute) ---
    p.add_argument("--market", choices=["us", "cn", "hk"], default="cn")
    p.add_argument("--list", choices=["all", "github", "builtin"], default="all")
    p.add_argument("--strategies", default="uptrend", help="逗号分隔策略id(默认 uptrend)")
    p.add_argument("--tickers", default=None, help="指定代码(逗号分隔); 默认全量")
    p.add_argument("--limit", type=int, default=0, help="只扫前N只(0=全部)")
    p.add_argument("--bars", type=int, default=fos.DEFAULT_BARS)
    p.add_argument("--workers", type=int, default=6)
    p.add_argument("--min-price", type=float, default=fos.DEFAULT_MIN_PRICE)
    p.add_argument("--min-volume", type=float, default=fos.DEFAULT_MIN_VOLUME)
    p.add_argument("--min-shares", type=float, default=fos.DEFAULT_MIN_SHARES)
    p.add_argument("--max-stale-days", type=int, default=fos.DEFAULT_MAX_STALE_DAYS)
    p.add_argument("--keep-shells", action="store_true")
    p.add_argument("--delay", type=float, default=0.1)
    p.add_argument("--no-cache", action="store_true")
    p.add_argument("--with-intro", action="store_true")
    # --- 行业排行参数 ---
    p.add_argument("--col", default=None, help="策略列名(默认取 --strategies 第一个id)")
    p.add_argument("--score-map", default="8:8,7:6,6:4",
                   help="得分->加权分映射(默认 8:8,7:6,6:4, 未列出不计入)")
    p.add_argument("--sort", choices=["平均分", "得分", "股票数"], default="平均分")
    p.add_argument("--top", type=int, default=10, help="显示前N个行业(0=全部)")
    # --- 输出 ---
    p.add_argument("--results-out", default=None, help="命中结果CSV路径")
    p.add_argument("--rank-out", default=None, help="行业排行CSV路径")
    args = p.parse_args()

    # 元策略(A_rank 等 sector_rank): 用 JSON 里的参数, 扫描底层 base 策略
    meta = load_meta_strategy(args.strategies)
    scan_strategies = args.strategies
    if meta:
        params = meta.get("params", {})
        print(f"[A_rank] 加载元策略 {meta.get('id')} ({meta.get('name')})")
        args.market = params.get("market", args.market)
        scan_strategies = params.get("base", args.strategies)
        args.score_map = params.get("score_map", args.score_map)
        args.sort = params.get("sort", args.sort)
        args.top = params.get("top", args.top)

    # 1) A股确保行业数据
    if args.market == "cn":
        ensure_cn_industry()

    # 2) 扫描
    if args.col is None:
        args.col = scan_strategies.split(",")[0].strip()
    if args.results_out is None:
        args.results_out = os.path.join(fos.OUTPUT_DIR, f"{args.market}_{args.col}_scan_rank.csv")
    run_args = argparse.Namespace(
        delay=args.delay, no_cache=args.no_cache, list_strategies=False,
        strategies=scan_strategies, market=args.market, list=args.list,
        tickers=args.tickers, limit=args.limit, bars=args.bars, workers=args.workers,
        min_price=args.min_price, min_volume=args.min_volume,
        min_shares=args.min_shares, max_stale_days=args.max_stale_days,
        keep_shells=args.keep_shells, with_intro=args.with_intro,
        output=args.results_out,
    )
    results = run_strategy.execute(run_args)
    if not results:
        print("没有命中任何策略的股票, 无法统计行业。")
        return

    # 3) 行业加权分排行(剔除无行业行)
    df = pd.DataFrame(results)
    df = df[df["industry"].fillna("").astype(str).str.strip() != ""]
    if df.empty:
        print("结果缺少行业数据(美股需本地full_info, A股需cn_industry.json)。", file=sys.stderr)
        return
    mapping = industry_score.parse_score_map(args.score_map)
    agg = industry_score.aggregate(df, args.col, mapping)
    if agg.empty:
        print("没有计入的股票(都在6分以下)。")
        return
    # 两步排名: 先按板块总分取前 top 名, 再在池内按 --sort(默认平均分)排序
    show = industry_score.rank_table(agg, args.top, args.sort)
    if args.rank_out is None:
        args.rank_out = os.path.splitext(args.results_out)[0] + "_industry_rank.csv"
    show.to_csv(args.rank_out, index=False, encoding="utf-8-sig")

    print(f"\n计入股票 {int(agg['股票数'].sum())} 只, 共 {len(agg)} 个行业; 前{len(show)}名按{args.sort}:")
    print(show.to_string(index=False))
    print(f"\n结果CSV:     {args.results_out}")
    print(f"行业排行CSV: {args.rank_out}")


if __name__ == "__main__":
    main()
