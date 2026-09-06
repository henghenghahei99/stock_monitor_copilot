#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
上涨趋势打分扫描器: 对每只股票评估多个趋势条件, 满足条件越多排名越靠前。

条件(共8个):
  1 多头排列    收盘价 > MA20 > MA50
  2 站上年线    收盘价 > MA200
  3 MA20上行    MA20 较5日前走高
  4 低点抬高    最近20日最低 > 前20日最低
  5 高点抬高    最近20日最高 > 前20日最高
  6 斜率向上    20日线性回归斜率 > 0
  7 近60日新高  收盘价 ≥ 60日最高价的95%
  8 放量        近5日均量 > 近20日均量

用法示例:
    python find_uptrend.py --list builtin              # 内置约500只
    python find_uptrend.py --list github               # 全量美股(约7000只)
    python find_uptrend.py --min-score 6               # 只看满足>=6个条件
    python find_uptrend.py --tickers AAPL,MSFT,NVDA
    python find_uptrend.py --output trend.csv

依赖:
    pip install pandas
"""

from __future__ import annotations

import argparse
import os
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date
from typing import Optional

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import find_overbought_stocks as fos  # noqa: E402  复用数据拉取/基础过滤
from translations import translate  # noqa: E402  行业/板块中英翻译


def cond_bull_align(close, volume, ma):
    """1 多头排列: 收盘价 > MA20 > MA50"""
    m20, m50 = ma["ma20"], ma["ma50"]
    if pd.isna(m20.iloc[-1]) or pd.isna(m50.iloc[-1]):
        return None
    return bool(close.iloc[-1] > m20.iloc[-1] > m50.iloc[-1])


def cond_above_year(close, volume, ma):
    """2 站上年线: 收盘价 > MA200"""
    m200 = ma["ma200"]
    if pd.isna(m200.iloc[-1]):
        return None
    return bool(close.iloc[-1] > m200.iloc[-1])


def cond_ma20_up(close, volume, ma):
    """3 MA20上行: 当前 MA20 > 5日前的 MA20"""
    m20 = ma["ma20"]
    if pd.isna(m20.iloc[-1]) or pd.isna(m20.iloc[-6]):
        return None
    return bool(m20.iloc[-1] > m20.iloc[-6])


def cond_higher_low(close, volume, ma):
    """4 低点抬高: 最近20日最低 > 前20日最低"""
    if len(close) < 40:
        return None
    return bool(close[-20:].min() > close[-40:-20].min())


def cond_higher_high(close, volume, ma):
    """5 高点抬高: 最近20日最高 > 前20日最高"""
    if len(close) < 40:
        return None
    return bool(close[-20:].max() > close[-40:-20].max())


def cond_slope_up(close, volume, ma):
    """6 斜率向上: 20日线性回归斜率 > 0"""
    if len(close) < 20:
        return None
    x = np.arange(20)
    slope = float(np.polyfit(x, close[-20:], 1)[0])
    return bool(slope > 0)


def cond_near_high(close, volume, ma):
    """7 近60日新高: 收盘价 >= 60日最高价的95%"""
    if len(close) < 60:
        return None
    return bool(close.iloc[-1] >= 0.95 * close[-60:].max())


def cond_volume_up(close, volume, ma):
    """8 放量: 近5日均量 > 近20日均量"""
    if len(volume) < 20:
        return None
    return bool(volume[-5:].mean() > volume[-20:].mean())


# 条件列表: (名称, 计算函数)
CONDITIONS = [
    ("多头排列", cond_bull_align),
    ("站上年线", cond_above_year),
    ("MA20上行", cond_ma20_up),
    ("低点抬高", cond_higher_low),
    ("高点抬高", cond_higher_high),
    ("斜率向上", cond_slope_up),
    ("近60日新高", cond_near_high),
    ("放量", cond_volume_up),
]


def compute_ma(close: pd.Series) -> dict:
    return {f"ma{n}": close.rolling(n).mean() for n in (20, 50, 200)}


def process_one(
    market: str,
    ticker: str,
    rsi_period: int,
    bars: int,
    min_bars: int,
    min_price: float,
    min_volume: float,
    min_shares: float,
    max_stale_days: int,
    keep_shells: bool,
    full_info: dict,
    min_score: int,
) -> Optional[dict]:
    """评估单只股票的上涨趋势, 命中返回结果字典, 否则返回 None。"""
    try:
        info = fos.tencent_quote(market, ticker)
        if info is None:
            return None
        prefixed, name = info
        k = fos.tencent_kline(prefixed, bars)
        if k is None:
            return None
        close, volume = k
        if len(close) < min_bars:
            return None

        last_close = float(close.iloc[-1])
        # 停牌/退市
        if (date.today() - close.index[-1].date()).days > max_stale_days:
            return None
        # 价格
        if last_close < min_price:
            return None
        # 近20日均成交额(市场货币)
        mult = fos.VOLUME_MULT.get(market, 1.0)
        avg_vol = float((close * volume * mult).tail(20).mean()) if len(volume) else 0.0
        if avg_vol < min_volume:
            return None
        # 近20日均成交量(股)
        avg_shares = float(volume.tail(20).mean()) * mult
        if avg_shares < min_shares:
            return None
        # 壳股/权证/优先股 (主要针对美股)
        meta = full_info.get(prefixed) or full_info.get(ticker.upper()) or {}
        full_name = meta.get("full_name") or name
        if not keep_shells and fos.is_shell_name(full_name):
            return None

        # 评估趋势条件
        ma = compute_ma(close)
        satisfied: list[str] = []
        evaluated = 0
        for cname, fn in CONDITIONS:
            try:
                res = fn(close, volume, ma)
            except Exception:  # noqa: BLE001
                res = None
            if res is None:
                continue
            evaluated += 1
            if res:
                satisfied.append(cname)

        score = len(satisfied)
        if score < min_score:
            return None

        chg20 = (
            (last_close / float(close.iloc[-21]) - 1) * 100
            if len(close) > 21 else 0.0
        )
        rsi = float(fos.compute_rsi(close, rsi_period).iloc[-1])
        return {
            "ticker": ticker,
            "name": name,
            "sector": translate(meta.get("sector", "")),
            "industry": translate(meta.get("industry", "")),
            "date": str(close.index[-1].date()),
            "close": round(last_close, 2),
            "score": score,
            "score_max": evaluated,
            "chg20": round(chg20, 1),
            "volume": int(avg_vol),
            "rsi": round(rsi, 1),
            "conditions": ",".join(satisfied),
        }
    except Exception as exc:  # noqa: BLE001
        print(f"[警告] {ticker} 获取失败: {exc}", file=sys.stderr)
    return None


def scan(tickers, args, full_info) -> list[dict]:
    results: list[dict] = []
    total = len(tickers)
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {
            pool.submit(
                process_one, args.market, t, args.period, args.bars, 60,
                args.min_price, args.min_volume, args.min_shares, args.max_stale_days,
                args.keep_shells, full_info, args.min_score,
            ): t
            for t in tickers
        }
        done = 0
        for fut in as_completed(futures):
            r = fut.result()
            done += 1
            if r:
                results.append(r)
            if total > 10:
                print(f"[进度] 已检查 {done}/{total} ...", end="\r", file=sys.stderr)
    if total > 10:
        print(file=sys.stderr)
    return results


def load_tickers(args) -> list[str]:
    if args.tickers:
        return [fos.normalize_code(args.market, t)
                for t in args.tickers.split(",") if t.strip()]
    if args.watchlist:
        with open(args.watchlist, encoding="utf-8") as f:
            return [fos.normalize_code(args.market, ln.split("#")[0].strip())
                    for ln in f if ln.strip()]
    return fos.load_market_list(args.market, args.list, 500) \
        or fos.load_market_list(args.market, "builtin", 500)


def main() -> None:
    p = argparse.ArgumentParser(description="按上涨趋势条件打分并排序的美股/A股/港股扫描器")
    p.add_argument("--market", choices=["us", "cn", "hk"], default="us",
                   help="市场: us=美股, cn=A股, hk=港股(默认 us)")
    p.add_argument("--list", choices=["all", "github", "builtin"],
                   default="builtin", help="股票池来源: all=全量/市值前, github=全美股(仅us), builtin=内置样本(默认 builtin)")
    p.add_argument("--tickers", help="逗号分隔的股票代码")
    p.add_argument("--watchlist", help="从文件读取代码(每行一个)")
    p.add_argument("--min-score", type=int, default=4,
                   help="至少满足几个条件才显示(默认 4, 共8个)")
    p.add_argument("--top", type=int, default=0, help="只显示前 N 名(0=全部)")
    p.add_argument("--period", type=int, default=fos.DEFAULT_RSI_PERIOD,
                   help=f"RSI 周期(默认 {fos.DEFAULT_RSI_PERIOD})")
    p.add_argument("--bars", type=int, default=fos.DEFAULT_BARS,
                   help=f"日K根数(默认 {fos.DEFAULT_BARS})")
    p.add_argument("--workers", type=int, default=fos.DEFAULT_WORKERS,
                   help=f"并发线程数(默认 {fos.DEFAULT_WORKERS})")
    p.add_argument("--min-price", type=float, default=fos.DEFAULT_MIN_PRICE)
    p.add_argument("--min-volume", type=float, default=fos.DEFAULT_MIN_VOLUME)
    p.add_argument("--min-shares", type=float, default=fos.DEFAULT_MIN_SHARES,
                   help=f"近20日均成交量(股)下限(默认 {fos.DEFAULT_MIN_SHARES:,.0f})")
    p.add_argument("--max-stale-days", type=int, default=fos.DEFAULT_MAX_STALE_DAYS)
    p.add_argument("--keep-shells", action="store_true")
    p.add_argument("--output", help="结果保存为 CSV")
    args = p.parse_args()

    tickers = load_tickers(args)
    if not tickers:
        print("没有可用的股票代码。", file=sys.stderr)
        sys.exit(1)

    print(f"[信息] 开始评估 {len(tickers)} 只美股的趋势条件(共{len(CONDITIONS)}个)...")
    full_info = fos.load_full_info()
    results = scan(tickers, args, full_info)

    if not results:
        print(f"没有满足 >= {args.min_score} 个条件的股票。")
        return

    # 排序: 条件数降序 -> 20日涨幅降序
    results.sort(key=lambda x: (x["score"], x["chg20"]), reverse=True)
    if args.top and args.top > 0:
        results = results[: args.top]

    print(f"\n找到 {len(results)} 只, 按满足条件数排序:")
    print(f"{'#':<4}{'代码':<7}{'名称':<12}{'行业':<16}{'收盘':>8}{'得分':>6}"
          f"{'20日%':>8}{'RSI':>6}  满足条件")
    print("-" * 110)
    for i, r in enumerate(results, 1):
        ind = (r["industry"] or "-")[:14]
        print(
            f"{i:<4}{r['ticker']:<7}{(r['name'] or '')[:10]:<12}{ind:<16}"
            f"{r['close']:>8.2f}{r['score']:>4}/{r['score_max']:<2}"
            f"{r['chg20']:>8.1f}{r['rsi']:>6.1f}  {r['conditions']}"
        )

    if args.output:
        pd.DataFrame(results).to_csv(args.output, index=False, encoding="utf-8-sig")
        print(f"\n结果已保存到 {args.output}")


if __name__ == "__main__":
    main()
