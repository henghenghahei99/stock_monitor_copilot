#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
港股历史回补(离线, 0 联网): 用本地 320 根K线缓存按历史交易日切片重算 hits/动量表,
再依次生成排名表与升降表, 让日报的"走势图 / 升降表"有完整历史。

原理: 与联网扫描同一套评估逻辑(hk_rank.eval_stock), 只是数据来自
      data/kline_cache/hk<code>_320.json(不判新鲜度、不补拉), 每只只读一次,
      然后对目标交易日逐日切片。
目标日期: 默认取 K 线里最后 N 个交易日(以参考标的 hk00700 的K线日期为准)。

用法:
  python rebuild_history_hk.py                 # 默认最近 6 个交易日
  python rebuild_history_hk.py --days 10       # 最近10个交易日
  python rebuild_history_hk.py 20260908 20260909 20260910   # 指定日期
  python rebuild_history_hk.py --report        # 跑完顺带生成最新一日的报告
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import date

import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import hk_rank as hk  # noqa: E402  (导入它会把 A_rank_report/code 加进 sys.path)
import find_overbought_stocks as fos  # noqa: E402

REFS = ("hk00700", "hk00005", "hk09988")     # 参考标的(取日期并集, 避免单只停牌)


def _cache_rows(prefixed: str) -> list | None:
    p = os.path.join(fos.KLINE_CACHE_DIR, f"{prefixed}_{hk.BARS}.json")
    if not os.path.exists(p):
        return None
    try:
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    except Exception:  # noqa: BLE001
        return None


def trading_days(days: int, asof: date | None = None) -> list[str]:
    """从参考标的的K线缓存取最近 days 个**已收盘确认**的交易日(YYYYMMDD, 升序)。

    港股当日K线在未收盘时也存在(半根), 必须用 hk 的收盘确认时刻(16:05)过滤掉,
    否则会把"今天"当成一个残缺的交易日。
    """
    ds: set[date] = set()
    for pref in REFS:
        rows = _cache_rows(pref)
        if not rows:
            continue
        for r in rows:
            try:
                d = pd.Timestamp(r[0]).date()
            except Exception:  # noqa: BLE001
                continue
            if asof is not None and d > asof:
                continue
            if time.time() < fos.bar_final_ts("hk", d):
                continue                      # 尚未收盘确认(如当日盘中)
            ds.add(d)
    out = sorted(ds)
    return [d.strftime("%Y%m%d") for d in (out[-days:] if days else out)]


def main() -> None:
    ap = argparse.ArgumentParser(description="港股历史回补(离线)")
    ap.add_argument("dates", nargs="*", help="指定交易日 YYYYMMDD(默认取最近 N 日)")
    ap.add_argument("--days", type=int, default=6, help="默认回补最近 N 个交易日")
    ap.add_argument("--top", type=int, default=10, help="入池各取前 N(默认10)")
    ap.add_argument("--report", action="store_true", help="顺带生成最新一日的报告")
    a = ap.parse_args()

    recs = hk.ensure_universe()
    dates = a.dates or trading_days(a.days)
    if not dates:
        print("[回补] 没有可用交易日(缺K线缓存?)", file=sys.stderr)
        return
    print(f"[回补] 交易日 {dates[0]} ~ {dates[-1]} ({len(dates)} 天), 股票 {len(recs)} 只",
          file=sys.stderr)

    asofs = [pd.Timestamp(d).date() for d in dates]
    acc: dict[str, tuple[list, list]] = {d: ([], []) for d in dates}
    t0 = time.time()
    done = 0
    for rec in recs:
        rows = _cache_rows(rec["prefixed"])
        if not rows:
            continue
        try:
            close, volume = fos._kline_rows_to_series(rows)
        except Exception:  # noqa: BLE001
            continue
        for d, asof in zip(dates, asofs):
            try:
                h, mo = hk.eval_stock(rec, close, volume, asof=asof, live=False)
            except Exception:  # noqa: BLE001
                h = mo = None
            if h:
                acc[d][0].append(h)
            if mo:
                acc[d][1].append(mo)
        done += 1
        if done % 500 == 0:
            print(f"[回补] {done}/{len(recs)} {time.time() - t0:.0f}s", file=sys.stderr)

    for d in dates:
        hits, moms = acc[d]
        hdf = pd.DataFrame(hits)
        mdf = pd.DataFrame(moms)
        if not mdf.empty:
            mdf = (mdf.dropna(subset=["m"])
                      .sort_values(["industry", "m"], ascending=[True, False])
                      .reset_index(drop=True))
        if not hdf.empty:
            hdf.to_csv(os.path.join(hk.OUT, f"hk_uptrend_{d}.csv"), index=False,
                       encoding="utf-8-sig")
        if not mdf.empty:
            mdf.to_csv(os.path.join(hk.OUT, f"hk_momentum_{d}.csv"), index=False,
                       encoding="utf-8-sig")
        print(f"[回补] {d}: 命中 {len(hdf)} 只 / 动量成员 {len(mdf)} 只", file=sys.stderr)

    for d in dates:                     # 逐日排名 + 升降(升降需要前一日的排名表)
        try:
            hk.build_rank(d, a.top)
        except Exception as exc:  # noqa: BLE001
            print(f"[回补] {d} 排名失败: {exc}", file=sys.stderr)
            continue
        try:
            hk.build_delta(d)
        except Exception as exc:  # noqa: BLE001
            print(f"[回补] {d} 升降失败: {exc}", file=sys.stderr)
    print(f"[回补] 完成, 用时 {time.time() - t0:.0f}s", file=sys.stderr)

    if a.report:
        import subprocess
        subprocess.run([sys.executable, os.path.join(HERE, "make_hk_rank_report.py"),
                        dates[-1], "--top", str(a.top)], check=False)


if __name__ == "__main__":
    main()
