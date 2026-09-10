#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""离线重建 A股 历史命中CSV 为 V2 口径(趋势分=7交易日窗口)。

V2 = 与 run_strategy.eval_uptrend_7d 完全相同的 9 个条件:
  站上MA7 / MA7上行 / 低点抬高 / 高点抬高 / 斜率向上 / 近7日新高 / 放量
  / 7窗口持续性(7日为窗逐日前移共7窗, >=5窗涨幅>0 且 >=4窗涨幅>2%)
  / 7窗口量能放大(7窗累计成交量回归斜率>0 且 最新窗量>最老窗量)
满足 >=6 命中; 展示值按比例折算到 /8。

数据源(全程 0 联网):
  - data/kline_cache/<prefixed>_320.json  (行=[date, open, close, high, low, volume], 按数据日 asof 切片)
  - data/quote_cache/cn_<prefixed>.json  ([prefixed, name] → 股票名)
  - data/cn_industry.json                (东财二级行业)
  - data/cn_tickers.txt                  (全量 A股代码)

参与门槛与 scan 一致: 价格 >= 1 元; 近20日均成交额 > 1000万; 近20日均成交量 > 10万股
(成交额/成交量用 VOLUME_MULT['cn']=100 换算)。

用法: python rebuild_history_v2.py 0901 0902 0903 0904 0907 0908
      (每个 MMDD 的 asof 取该日现有 CSV 的 date 列最大值, 即其真实数据日)
输出: 覆盖 A_rank_report/output/cn_uptrend_<MMDD>.csv, 并留 .v1.bak 备份(仅首次)
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date

import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)                       # A_rank_report
REPO = os.path.dirname(ROOT)                       # stock_monitor_copilot
DATA = os.path.join(REPO, "data")
OUT = os.path.join(ROOT, "output")
KLINE = os.path.join(DATA, "kline_cache")
QUOTE = os.path.join(DATA, "quote_cache")

MIN_PRICE = 1.0
MIN_VOLUME = 10_000_000.0
MIN_SHARES = 100_000.0
MULT = 100.0          # A股 volume 单位为手 → 股
MIN_SCORE = 6         # V2 命中门槛 6/9

sys.path.insert(0, HERE)
import find_uptrend as fu  # noqa: E402

COLS = ["ticker", "prefixed", "name", "sector", "industry", "date", "close",
        "chg20", "chg1", "chg2", "chg3", "volume", "score", "passed", "uptrend7"]


def _load_quote_names() -> dict[str, str]:
    names: dict[str, str] = {}
    for fn in os.listdir(QUOTE):
        if not fn.startswith("cn_") or not fn.endswith(".json"):
            continue
        try:
            with open(os.path.join(QUOTE, fn), encoding="utf-8") as f:
                p, n = json.load(f)
            names[str(p)] = str(n)
        except Exception:  # noqa: BLE001
            continue
    return names


def _eval_one(pref: str, asof: date, names: dict[str, str], inds: dict[str, str]):
    """单只: 按 asof 切片 → 门槛过滤 → V2 打分; 命中返回行, 否则 None。"""
    fp = os.path.join(KLINE, f"{pref.replace('.', '_')}_320.json")
    if not os.path.exists(fp):
        return None
    try:
        with open(fp, encoding="utf-8") as f:
            rows = json.load(f)
    except Exception:  # noqa: BLE001
        return None
    if not rows:
        return None
    close = pd.Series([float(r[2]) for r in rows],
                      index=pd.to_datetime([r[0] for r in rows])).astype(float).dropna()
    vol = pd.Series([float(r[5]) for r in rows],
                    index=pd.to_datetime([r[0] for r in rows])).astype(float)
    c = close[close.index.date <= asof]
    v = vol.reindex(c.index).fillna(0.0)
    if len(c) < 60:
        return None
    last = float(c.iloc[-1])
    if last < MIN_PRICE:
        return None
    nv = min(20, len(c))
    avg_vol = float((c * v * MULT).tail(nv).mean())
    avg_shares = float(v.tail(nv).mean()) * MULT
    if avg_vol < MIN_VOLUME or avg_shares < MIN_SHARES:
        return None
    # V2 打分
    ma = fu.compute_ma7(c)
    s = ev = 0
    for _nm, fn in fu.CONDITIONS_7D:
        try:
            r = fn(c, v, ma)
        except Exception:  # noqa: BLE001
            r = None
        if r is None:
            continue
        ev += 1
        if r:
            s += 1
    if ev == 0 or s < MIN_SCORE:
        return None
    n = min(round(s * 8 / ev), 8)
    code6 = pref[2:]
    chg = lambda k: (round((last / float(c.iloc[-1 - k]) - 1) * 100, 2)  # noqa: E731
                     if len(c) > k and float(c.iloc[-1 - k]) > 0 else None)
    chg20 = (round((last / float(c.iloc[-21]) - 1) * 100, 1)
             if len(c) > 21 and float(c.iloc[-21]) > 0 else 0.0)
    return {
        "ticker": pref.upper(), "prefixed": pref,
        "name": names.get(pref, pref), "sector": inds.get(code6, ""),
        "industry": inds.get(code6, ""), "date": str(c.index[-1].date()),
        "close": last, "chg20": chg20,
        "chg1": chg(1), "chg2": chg(2), "chg3": chg(3),
        "volume": float(v.iloc[-1]), "score": float(n),
        "passed": "uptrend7", "uptrend7": f"{n}/8",
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("mmdd", nargs="+", help="要重建的日期标签(如 0901 0902 ...)")
    ap.add_argument("--workers", type=int, default=16)
    a = ap.parse_args()

    with open(os.path.join(DATA, "cn_tickers.txt"), encoding="utf-8") as f:
        tickers = [ln.strip().lower() for ln in f if ln.strip()]
    with open(os.path.join(DATA, "cn_industry.json"), encoding="utf-8") as f:
        inds = json.load(f)
    names = _load_quote_names()
    print(f"[准备] A股 {len(tickers)} 只, 名称 {len(names)}, 行业 {len(inds)}")

    for mmdd in a.mmdd:
        path = os.path.join(OUT, f"cn_uptrend_{mmdd}.csv")
        if not os.path.exists(path):
            print(f"[跳过] {mmdd}: 无原文件 {path}")
            continue
        old = pd.read_csv(path, encoding="utf-8-sig")
        asof = pd.to_datetime(old["date"]).max().date()
        rows: list[dict] = []
        with ThreadPoolExecutor(max_workers=a.workers) as pool:
            futs = {pool.submit(_eval_one, t, asof, names, inds): t for t in tickers}
            for fut in as_completed(futs):
                try:
                    r = fut.result()
                except Exception:  # noqa: BLE001
                    r = None
                if r:
                    rows.append(r)
        df = pd.DataFrame(rows, columns=COLS)
        bak = path + ".v1.bak"
        if not os.path.exists(bak):
            shutil.copy2(path, bak)
        df.to_csv(path, index=False, encoding="utf-8-sig")
        print(f"[完成] {mmdd} (asof {asof}): V2 命中 {len(df)} 只 "
              f"(原 v1 {len(old)} 只) -> {path}")


if __name__ == "__main__":
    main()
