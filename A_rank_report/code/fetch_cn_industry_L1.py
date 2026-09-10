#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""从东财 F10 拉取全部A股的"东财行业(一级)", 覆盖写入 data/cn_industry.json。

数据源: https://emweb.securities.eastmoney.com/PC_HSF10/CompanySurvey/PageAjax?code=SH600546
  -> jbzl[0].EM2016 = "化石能源-煤炭-煤炭开采洗选"  (一级-二级-三级)
取第二段(东财二级, 如 煤炭/白阆/证券/半导体...), 缺失时退回第一段。

用法:
  python fetch_cn_industry_L1.py            # 抓取并覆盖 data/cn_industry.json(旧文件先备份)
  python fetch_cn_industry_L1.py --workers 30 --limit 200   # 联调
"""
from __future__ import annotations

import argparse
import gzip
import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import find_overbought_stocks as fos  # noqa: E402

DATA = fos.DATA_DIR
TICKERS = os.path.join(DATA, "cn_tickers.txt")
OUT = os.path.join(DATA, "cn_industry.json")
BACKUP = os.path.join(DATA, "cn_industry_sw_backup.json")
URL = ("https://emweb.securities.eastmoney.com/PC_HSF10/CompanySurvey/PageAjax"
       "?code={code}")


def _f10code(prefixed: str) -> str:
    """sh600519 -> SH600519 ; 600519 -> SH600519"""
    t = prefixed.strip().lower()
    if t[:2] in ("sh", "sz", "bj"):
        return t[:2].upper() + t[2:]
    c = t.zfill(6)
    return ("SH" if c[0] == "6" else "SZ" if c[0] in "023" else "BJ") + c


def _code6(prefixed: str) -> str:
    t = prefixed.strip().lower()
    return t[2:] if t[:2] in ("sh", "sz", "bj") else t.zfill(6)


def fetch_one(prefixed: str) -> tuple[str, str]:
    code6 = _code6(prefixed)
    for attempt in range(3):
        try:
            raw = fos.http_get(URL.format(code=_f10code(prefixed)), timeout=15)
            if raw[:2] == b"\x1f\x8b":      # 部分代码东财返回 gzip
                raw = gzip.decompress(raw)
            em = (json.loads(raw.decode("utf-8", "ignore"))
                  .get("jbzl", [{}])[0].get("EM2016") or "")
            parts = [p.strip() for p in em.split("-") if p.strip()]
            l1 = parts[1] if len(parts) > 1 else (parts[0] if parts else "")
            return code6, l1
        except Exception:  # noqa: BLE001
            time.sleep(0.6 * (attempt + 1))
    return code6, ""


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--workers", type=int, default=30)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--fill", action="store_true",
                    help="补漏模式: 只重抓现有映射里缺失的代码, 合并后写回")
    a = ap.parse_args()

    old: dict[str, str] = {}
    if a.fill and os.path.exists(OUT):
        with open(OUT, encoding="utf-8") as f:
            old = json.load(f)

    with open(TICKERS, encoding="utf-8") as f:
        tickers = [ln.strip() for ln in f if ln.strip()]
    if a.fill:
        tickers = [t for t in tickers if not old.get(_code6(t))]
        print(f"[补漏] 待抓 {len(tickers)} 只 (已有 {len(old)} 只)")
    if a.limit:
        tickers = tickers[:a.limit]
    if not a.fill and not os.path.exists(BACKUP) and os.path.exists(OUT):
        with open(OUT, encoding="utf-8") as f:
            data = f.read()
        with open(BACKUP, "w", encoding="utf-8") as f:
            f.write(data)
        print(f"[备份] 旧映射 -> {BACKUP}")

    out: dict[str, str] = dict(old)
    done = 0
    t0 = time.time()
    with ThreadPoolExecutor(max_workers=a.workers) as pool:
        futs = {pool.submit(fetch_one, t): t for t in tickers}
        for fut in as_completed(futs):
            code6, l1 = fut.result()
            if l1:
                out[code6] = l1
            done += 1
            if done % 200 == 0:
                print(f"[进度] {done}/{len(tickers)}  有效{len(out)}  "
                      f"{time.time()-t0:.0f}s", flush=True)
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, separators=(",", ":"))
    from collections import Counter
    c = Counter(out.values())
    print(f"[完成] {OUT} 共 {len(out)} 只, 东财二级行业 {len(c)} 个, 用时 {time.time()-t0:.0f}s")
    if a.fill:
        still = [t for t in tickers if not out.get(_code6(t))]
        print(f"[补漏] 本轮新增 {len(out) - len(old)} 只, 仍缺 {len(still)} 只")
        if still:
            print("       仍未取到:", " ".join(still[:20]))
        return
    print("东财二级行业分布(前30):")
    for k, v in c.most_common(30):
        print(f"  {k}: {v}")


if __name__ == "__main__":
    main()
