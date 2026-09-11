#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
批量拉取"东方财富 港股 F10 公司概况 所属行业"(BELONG_INDUSTRY, 港股行业体系/中文),
存 data/hk_em_industry.json: {"00700": {"name": "腾讯控股", "ind": "软件服务"}, ...}

接口: datacenter.eastmoney.com/securities/api/data/v1/get
  reportName = RPT_HKF10_INFO_ORGPROFILE (分页列出全部港股 F10 概况)

东财港股行业共 31 类(恒生行业体系), 如: 地产/工业工程/软件服务/药品及生物科技/
建筑/其他金融/旅游及消闲设施/专业零售/纺织及服饰/半导体/煤炭/黄金及贵金属 等。

用法:
  python fetch_hk_em_industry.py             # 拉取并保存(本地已有且较新则跳过)
  python fetch_hk_em_industry.py --refresh   # 强制重拉
  python fetch_hk_em_industry.py --print     # 只打印行业分布, 不写文件
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
CACHE = os.path.join(DATA, "hk_em_industry.json")
CACHE_TTL_DAYS = 7     # 行业归属不常变
API = ("https://datacenter.eastmoney.com/securities/api/data/v1/get"
       "?reportName=RPT_HKF10_INFO_ORGPROFILE"
       "&columns=SECURITY_CODE,SECURITY_NAME_ABBR,BELONG_INDUSTRY"
       "&pageNumber={pn}&pageSize=500&source=SECURITIES&client=PC")


def fetch_page(pn: int) -> dict | None:
    url = API.format(pn=pn)
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read().decode("utf-8", "ignore"))
        except Exception as exc:  # noqa: BLE001
            if attempt == 2:
                print(f"[EM] 第{pn}页失败: {exc}", file=sys.stderr)
                return None
            time.sleep(1.5 * (attempt + 1))
    return None


def fetch_all() -> dict[str, dict]:
    """分页拉取全部港股行业+简称, 返回 {5位代码: {"name": 简称, "ind": 行业}}。"""
    out: dict[str, dict] = {}
    pn = 1
    while True:
        d = fetch_page(pn)
        if not d or not d.get("success"):
            print(f"[EM] 第{pn}页空/失败, 停止", file=sys.stderr)
            break
        rows = (d.get("result") or {}).get("data") or []
        if not rows:
            break
        for r in rows:
            code = str(r.get("SECURITY_CODE") or "").strip()
            ind = str(r.get("BELONG_INDUSTRY") or "").strip()
            name = str(r.get("SECURITY_NAME_ABBR") or "").strip()
            if code and ind and code not in out:
                out[code] = {"name": name, "ind": ind}
        pages = (d.get("result") or {}).get("pages") or 1
        if pn >= pages:
            break
        pn += 1
        time.sleep(0.2)
    return out


def _cache_fresh() -> bool:
    try:
        return (time.time() - os.path.getmtime(CACHE)) < CACHE_TTL_DAYS * 86400
    except Exception:  # noqa: BLE001
        return False


def load(refresh: bool = False) -> dict[str, dict]:
    """读取(必要时拉取)港股行业缓存 {5位代码: {"name","ind"}}。"""
    if not refresh and _cache_fresh():
        try:
            with open(CACHE, encoding="utf-8") as f:
                return json.load(f)
        except Exception:  # noqa: BLE001
            pass
    data = fetch_all()
    if data:
        os.makedirs(DATA, exist_ok=True)
        with open(CACHE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, separators=(",", ":"))
        print(f"[EM] 港股行业 {len(data)} 只 -> {CACHE}", file=sys.stderr)
    return data


def main() -> None:
    p = argparse.ArgumentParser(description="东财港股行业抓取")
    p.add_argument("--refresh", action="store_true", help="强制重拉")
    p.add_argument("--print", dest="do_print", action="store_true", help="只打印行业分布")
    a = p.parse_args()
    if a.do_print:
        data = fetch_all()
        import collections
        c = collections.Counter(v["ind"] for v in data.values())
        print(f"共 {len(data)} 只 / 行业 {len(c)} 类")
        for k, v in c.most_common():
            print(f"  {k}: {v}")
        return
    load(a.refresh)


if __name__ == "__main__":
    main()
