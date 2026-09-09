#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
批量拉取"东方财富 美股 F10 公司概况 所属行业"(BELONG_INDUSTRY, 中文细分),
存 data/us_em_industry.json: {SYMBOL: 行业(中文)}。可每日/低频更新。

接口: datacenter.eastmoney.com/securities/api/data/v1/get
  reportName = RPT_USF10_INFO_ORGPROFILE  (不分页时按代码排序列出全部美股+OTC)
用法: python fetch_us_em_industry.py [--limit 0]
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
REPO = os.path.dirname(ROOT)
DATA = os.path.join(REPO, "data")
CACHE = os.path.join(DATA, "us_em_industry.json")
API = ("https://datacenter.eastmoney.com/securities/api/data/v1/get"
       "?reportName=RPT_USF10_INFO_ORGPROFILE"
       "&columns=SECUCODE,SECURITY_CODE,BELONG_INDUSTRY"
       "&pageNumber={pn}&pageSize=200&source=SECURITIES&client=PC")


def fetch_page(pn: int) -> dict | None:
    url = API.format(pn=pn)
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    try:
        with urllib.request.urlopen(req, timeout=25) as resp:
            return json.loads(resp.read().decode("utf-8", "ignore"))
    except Exception as exc:  # noqa: BLE001
        print(f"[EM] 第{pn}页失败: {exc}", file=sys.stderr)
        return None


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--limit", type=int, default=0, help="最多拉多少条(0=全部)")
    a = p.parse_args()
    out: dict[str, str] = {}
    pn = 1
    total = 0
    done = 0
    while True:
        d = fetch_page(pn)
        if not d or not (d.get("success")):
            print(f"[EM] 第{pn}页空/失败, 停止", file=sys.stderr)
            break
        rows = (d.get("result") or {}).get("data") or []
        if not rows:
            break
        for r in rows:
            total += 1
            sym = str(r.get("SECURITY_CODE") or "").strip().upper()
            ind = str(r.get("BELONG_INDUSTRY") or "").strip()
            if sym and ind:
                if sym not in out:          # 优先第一次(主上市代码排序在前)
                    out[sym] = ind
                    done += 1
        if a.limit and total >= a.limit:
            break
        nxt = (d.get("result") or {}).get("pages")
        if pn >= (nxt or 1):
            break
        pn += 1
        time.sleep(0.25)
    os.makedirs(DATA, exist_ok=True)
    with open(CACHE, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, separators=(",", ":"))
    print(f"[EM] 全量 {total} 条 -> 有行业 {done} 只 -> {CACHE}", file=sys.stderr)


if __name__ == "__main__":
    main()
