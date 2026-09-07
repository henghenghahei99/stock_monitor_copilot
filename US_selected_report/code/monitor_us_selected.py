#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
美股自选 RSI(6) 监控

监控 data/selected/us.json 里的自选股(格式: [{"code":"UAAUS","name":"..."}, ...]):
  - 先缓存美股日K:  python monitor_us_selected.py --cache     (K线落到共享 data/kline_cache)
  - 正式出结果:     python monitor_us_selected.py             (只读缓存, 基本不联网)
  - 可选参数:       --bars 80 --period 6 --over 83 --under 26 --workers 8

代码转换: 自选 "XXXUS" -> ticker "XXX" -> 腾讯代码 "usXXX.市场后缀"
  (纳斯达克=.OQ, 纽交所=.N, 美交所/基金ETF/杠杆品=.AM(注意不是.A!),
   B类股用点号如 usBRK.B.N; 用本地名单判断, 未知则逐后缀探测)

判定:
  RSI6 > 83  -> 超买
  RSI6 < 26  -> 超卖
  其余         正常
输出: 控制台列表 + output/us_selected_rsi_YYYYMMDD.csv
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

CODE_DIR = os.path.dirname(os.path.abspath(__file__))     # .../US_selected_report/code
ROOT = os.path.dirname(CODE_DIR)                            # .../US_selected_report
REPO = os.path.dirname(ROOT)                                # .../stock_monitor_copilot
sys.path.insert(0, os.path.join(REPO, "A_rank_report", "code"))
import find_overbought_stocks as fos  # noqa: E402   (共用腾讯接口/缓存/代理/RSI)

DATA_DIR = os.path.join(REPO, "data")
WATCH_FILE = os.path.join(DATA_DIR, "selected", "us.json")
OUT_DIR = os.path.join(ROOT, "output")
DEFAULT_BARS = 80
DEFAULT_PERIOD = 6
DEFAULT_OVER = 83.0
DEFAULT_UNDER = 26.0

# 交易所后缀映射(读本地全量名单)
_EX_FILES = {".OQ": "nasdaq_full.json", ".N": "nyse_full.json", ".AM": "amex_full.json"}
_ex_symbols: dict[str, str] | None = None   # SYMBOL -> 后缀


def _exchange_map() -> dict[str, str]:
    global _ex_symbols
    if _ex_symbols is None:
        m: dict[str, str] = {}
        for suffix, fname in _EX_FILES.items():
            p = os.path.join(DATA_DIR, fname)
            try:
                with open(p, encoding="utf-8") as f:
                    for it in json.load(f):
                        sym = str(it.get("symbol", "")).strip().upper()
                        if sym:
                            m[sym] = suffix
            except Exception:  # noqa: BLE001
                pass
        _ex_symbols = m
    return _ex_symbols


def us_code_candidates(ticker: str) -> list[str]:
    """ticker -> 候选腾讯美股代码。

    关键坑: 腾讯对 ETF/杠杆ETF/ETN 与美交所(AMEX/Arca)个股的规范后缀是 .AM,
    不是 .A/.OQ/.N —— 用错后缀只返回"最近1根", 拿不到历史。
    另外 B类股(如 BRK_B -> BRK.B)要用点号写法。逐候选取第一个有足够K线的即可。
    """
    t = ticker.strip().upper()
    if not t:
        return []
    base_names = [t]
    if "_" in t:                     # BRK_B -> BRK.B 也要试
        base_names.append(t.replace("_", "."))
    cands: list[str] = []
    for name in base_names:
        ex = _exchange_map().get(name)
        if ex:
            c = "us" + name + ex
            if c not in cands:
                cands.append(c)
        for suf in (".OQ", ".N", ".AM", ".A"):
            c = "us" + name + suf
            if c not in cands:
                cands.append(c)
    return cands


def load_watch() -> list[dict]:
    """读取自选: 每项 {code,name}; "XXXUS" -> ticker XXX; 含候选腾讯代码。"""
    with open(WATCH_FILE, encoding="utf-8") as f:
        data = json.load(f)
    watch = []
    for it in data:
        code = str(it.get("code", "")).strip().upper()
        if not code:
            continue
        ticker = code[:-2] if code.endswith("US") else code
        watch.append({
            "code": code,
            "ticker": ticker,
            "cands": us_code_candidates(ticker),
            "prefixed": "",
            "name": str(it.get("name", "") or "").strip(),
        })
    return watch


def _name(p: dict) -> str:
    if p.get("name"):
        return p["name"]
    try:
        q = fos.tencent_quote("us", p["prefixed"][2:])
        if q and q[1]:
            return str(q[1]).strip()
    except Exception:  # noqa: BLE001
        pass
    return p["ticker"]


def _fetch_best(p: dict, bars: int) -> str | None:
    """逐个候选拉K线, 取第一个有足够数据的代码(回写 p['prefixed'])。"""
    for c in p["cands"]:
        try:
            k = fos.tencent_kline(c, bars, use_cache=True)
            if k is not None and len(k[0].dropna()) >= 30:
                p["prefixed"] = c
                return c
        except Exception:  # noqa: BLE001
            continue
    return None


def cache_klines(watch: list[dict], bars: int, workers: int) -> tuple[int, int]:
    """先缓存日K(usfqkline), 便于后续离线算 RSI。"""
    ok = 0
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = [ex.submit(_fetch_best, p, bars) for p in watch]
        for i, f in enumerate(as_completed(futs), 1):
            ok += 1 if f.result() else 0
            if i % 50 == 0 or i == len(watch):
                print(f"[缓存] {i}/{len(watch)} 完成, 成功 {ok}", file=sys.stderr)
    return ok, len(watch)


def analyze_one(p: dict, bars: int, period: int, over: float, under: float) -> dict | None:
    """单只: 读K线缓存 -> RSI(period) 最新值 -> 判定。"""
    if not p.get("prefixed"):
        _fetch_best(p, bars)
    if not p.get("prefixed"):
        return None
    k = fos.tencent_kline(p["prefixed"], bars, use_cache=True)
    if k is None:
        return None
    close, _ = k
    close = close.dropna()
    if len(close) < period + 1:
        return None
    rsi = float(fos.compute_rsi(close, period).iloc[-1])
    if rsi != rsi:
        return None
    status = "超买" if rsi > over else ("超卖" if rsi < under else "正常")
    last = float(close.iloc[-1])
    prev = float(close.iloc[-2]) if len(close) >= 2 else last
    chg = (last / prev - 1) * 100 if prev else 0.0
    return {
        "code": p["code"],
        "ticker": p["ticker"],
        "prefixed": p["prefixed"],
        "name": _name(p),
        "date": str(close.index[-1].date()),
        "close": round(last, 2),
        "chg%": round(chg, 2),
        "rsi6": round(rsi, 1),
        "status": status,
    }


def main() -> None:
    ap = argparse.ArgumentParser(description="美股自选 RSI(6) 超买/超卖监控")
    ap.add_argument("--cache", action="store_true", help="只拉取/缓存K线, 不判定")
    ap.add_argument("--bars", type=int, default=DEFAULT_BARS)
    ap.add_argument("--period", type=int, default=DEFAULT_PERIOD)
    ap.add_argument("--over", type=float, default=DEFAULT_OVER, help="超买阈值(默认83)")
    ap.add_argument("--under", type=float, default=DEFAULT_UNDER, help="超卖阈值(默认26)")
    ap.add_argument("--workers", type=int, default=8)
    args = ap.parse_args()

    watch = load_watch()
    print(f"[信息] 自选美股 {len(watch)} 只 (K线bars={args.bars})")

    if args.cache:
        ok, total = cache_klines(watch, args.bars, args.workers)
        print(f"[完成] 缓存 {ok}/{total} 只美股日K -> 共享 data/kline_cache")
        return

    rows = []
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futs = [ex.submit(analyze_one, p, args.bars, args.period, args.over, args.under)
                for p in watch]
        for f in as_completed(futs):
            try:
                r = f.result()
            except Exception:  # noqa: BLE001
                r = None
            if r:
                rows.append(r)
    rows.sort(key=lambda r: r["rsi6"], reverse=True)

    ob = [r for r in rows if r["status"] == "超买"]
    os_ = [r for r in rows if r["status"] == "超卖"]
    print(f"\n自选 {len(watch)} 只, 有数据 {len(rows)} 只 | RSI({args.period}) "
          f"> {args.over:g} 超买 {len(ob)} 只 | < {args.under:g} 超卖 {len(os_)} 只")
    for tag, lst, high in (("超买", ob, True), ("超卖", os_, False)):
        if not lst:
            continue
        thr = args.over if high else args.under
        print(f"\n== {tag} (RSI{'<' if not high else '>'} {thr:g}) ==")
        srt = (sorted(lst, key=lambda x: x["rsi6"], reverse=True) if high
               else sorted(lst, key=lambda x: x["rsi6"]))
        for r in srt:
            print(f"  {r['code']:<8} {r['name']:<22} RSI6={r['rsi6']:>6.1f}  "
                  f"收盘{r['close']:>9.2f}  {r['chg%']:+.1f}%  {r['date']}")

    os.makedirs(OUT_DIR, exist_ok=True)
    out = os.path.join(OUT_DIR, f"us_selected_rsi_{datetime.now():%Y%m%d}.csv")
    with open(out, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=["code", "ticker", "prefixed", "name", "date",
                                          "close", "chg%", "rsi6", "status"])
        w.writeheader()
        for r in rows:
            w.writerow(r)
    print(f"\n明细已存: {out}")


if __name__ == "__main__":
    main()
