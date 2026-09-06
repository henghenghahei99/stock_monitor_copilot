#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
A股交易日历(轻量): 以上证指数 sh000001 的日K线为准, 得到最近交易日序列。
用途: 供常驻调度器判断"今天是否交易日"、以及取"前一交易日"(跳过周末/节假日)。

说明: 不依赖任何外部交易日历服务/包, 直接用行情K线(腾讯)里的日期;
      A股休市日(周末/法定节假日)没有当日K线, 自然被排除。

用法:
  python cn_trading_days.py               # 输出最近交易日 YYYYMMDD
  python cn_trading_days.py --prev        # 输出最近交易日的前一交易日 YYYYMMDD
  python cn_trading_days.py --mmdd        # 输出 MMDD
  python cn_trading_days.py --prev --mmdd # 前一交易日 MMDD
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import find_overbought_stocks as fos  # noqa: E402

_memo: dict = {}


def index_trade_dates(n: int = 60, force: bool = False) -> list[date]:
    """最近 n 个交易日(升序)。force=True 时强制重新拉取。"""
    key = (n, date.today())
    if not force and key in _memo:
        return _memo[key]
    k = fos.tencent_kline("sh000001", n, use_cache=False)
    if k is None:
        raise RuntimeError("无法获取上证指数K线, 得不到交易日历")
    close, _ = k
    dates = sorted({d.date() for d in close.index})
    _memo[key] = dates
    return dates


def last_trade_date(force: bool = False) -> date:
    """最近一个交易日(通常=今天若今天开盘, 否则=上一交易日)。"""
    return index_trade_dates(force=force)[-1]


def prev_trade_date(d: date | None = None, force: bool = False) -> date:
    """d(默认最近交易日)的前一个交易日。"""
    ds = index_trade_dates(force=force)
    d = d or ds[-1]
    for x in reversed(ds):
        if x < d:
            return x
    raise RuntimeError(f"最近 {len(ds)} 个交易日里找不到 {d} 的前一交易日")


def is_trade_date(d: date, force: bool = False) -> bool:
    """d 是否是交易日(在最近K线交易日序列里)。"""
    try:
        return d in index_trade_dates(force=force)
    except RuntimeError:
        return False


def main() -> None:
    p = argparse.ArgumentParser(description="A股交易日历工具")
    p.add_argument("--prev", action="store_true", help="输出前一交易日")
    p.add_argument("--mmdd", action="store_true", help="输出 MMDD 而非 YYYYMMDD")
    args = p.parse_args()
    d = prev_trade_date() if args.prev else last_trade_date()
    fmt = "%m%d" if args.mmdd else "%Y%m%d"
    print(d.strftime(fmt))


if __name__ == "__main__":
    main()
