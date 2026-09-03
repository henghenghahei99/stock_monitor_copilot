#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
从东方财富拉取全部A股行业, 保存到 data/cn_industry.json (格式: {"600519": "酿酒行业", ...})。

东方财富 A股列表接口:
  https://push2.eastmoney.com/api/qt/clist/get
    ?pn={页}&pz=100&fs=m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23(沪深主板+创业+科创)
    &fields=f12,f14,f100   # f12=代码 f14=名称 f100=行业
行业字段已是中文(如 半导体/化学制药), 无需翻译。

用法:
  python fetch_cn_industry.py              # 拉取并保存 data/cn_industry.json
  python fetch_cn_industry.py --refresh    # 强制重拉(默认本地有且较新则跳过)
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import find_overbought_stocks as fos  # noqa: E402  复用 http_get / DATA_DIR

# 沪深A股: 深主板+创业板+沪主板+科创板
EM_CN_FS = "m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23"
EM_CN_LIST_URL = (
    "https://push2.eastmoney.com/api/qt/clist/get"
    "?pn={page}&pz=100&po=1&np=1"
    "&ut=bd1d9ddb04089700cf9c27f6f7426281&fltt=2&invt=2"
    "&fid=f12&fs={fs}&fields=f12,f14,f100"
)
CACHE_FILE = os.path.join(fos.DATA_DIR, "cn_industry.json")
CACHE_TTL_DAYS = 7  # 行业不常变


def fetch_all() -> dict[str, str]:
    """分页拉取全部A股行业, 返回 {6位代码: 行业}。"""
    out: dict[str, str] = {}
    # 先取一页拿总数
    first = json.loads(fos.http_get(
        EM_CN_LIST_URL.format(page=1, fs=EM_CN_FS), timeout=20
    ).decode("utf-8", "ignore"))
    total = int((first.get("data") or {}).get("total") or 0)
    pages = (total + 99) // 100
    print(f"东财A股总数: {total}, 共 {pages} 页")
    for page in range(1, pages + 1):
        for attempt in range(3):
            try:
                d = json.loads(fos.http_get(
                    EM_CN_LIST_URL.format(page=page, fs=EM_CN_FS), timeout=20
                ).decode("utf-8", "ignore"))
                diff = (d.get("data") or {}).get("diff") or []
                for it in diff:
                    code = str(it.get("f12", "")).strip()
                    ind = str(it.get("f100", "") or "").strip()
                    if code:
                        out[code] = ind
                break
            except Exception as exc:  # noqa: BLE001
                if attempt == 2:
                    print(f"  第{page}页失败: {exc}", file=sys.stderr)
                time.sleep(1.5 * (attempt + 1))
        if page % 10 == 0:
            print(f"  已拉取 {page}/{pages} 页, 累计 {len(out)} 只")
        time.sleep(0.15)
    return out


def main() -> None:
    p = argparse.ArgumentParser(description="拉取A股行业 -> data/cn_industry.json")
    p.add_argument("--refresh", action="store_true", help="强制重拉")
    args = p.parse_args()

    if (not args.refresh and os.path.exists(CACHE_FILE)
            and (time.time() - os.path.getmtime(CACHE_FILE)) < CACHE_TTL_DAYS * 86400):
        print(f"本地缓存已存在且未过期: {CACHE_FILE}")
        return

    ind = fetch_all()
    if not ind:
        print("未拉到任何行业数据。", file=sys.stderr)
        sys.exit(1)
    with open(CACHE_FILE, "w", encoding="utf-8") as f:
        json.dump(ind, f, ensure_ascii=False, indent=0)
    print(f"已保存 {len(ind)} 只 -> {CACHE_FILE}")
    print("样例:", dict(list(ind.items())[:5]))


if __name__ == "__main__":
    main()
