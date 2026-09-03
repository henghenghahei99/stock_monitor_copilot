#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
把美股结果 CSV 里的 sector(板块)/industry(行业) 翻译成中文。

用法:
    python translate_industry.py --input trend_full.csv --output trend_full_zh.csv
    python translate_industry.py -i result_us_rsi6_amt.csv -o result_zh.csv

说明:
    未收录的行业会保留原文。
"""

from __future__ import annotations

import argparse
import csv
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from translations import translate  # noqa: E402  共享行业翻译


def main() -> None:
    p = argparse.ArgumentParser(description="把结果CSV的行业翻译成中文")
    p.add_argument("-i", "--input", required=True, help="输入 CSV")
    p.add_argument("-o", "--output", required=True, help="输出 CSV")
    p.add_argument("--inplace", action="store_true",
                   help="直接修改原文件(与 -o 互斥)")
    args = p.parse_args()

    out_path = args.input if args.inplace else args.output

    with open(args.input, encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        fieldnames = reader.fieldnames or []
        rows = list(reader)

    if "sector" in fieldnames:
        print(f"[信息] 翻译 sector 列: 共 {len({r['sector'] for r in rows})} 种板块")
    if "industry" in fieldnames:
        print(f"[信息] 翻译 industry 列: 共 {len({r['industry'] for r in rows})} 种行业")

    for r in rows:
        if "sector" in r:
            r["sector"] = translate(r["sector"])
        if "industry" in r:
            r["industry"] = translate(r["industry"])

    with open(out_path, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)

    print(f"已保存 {len(rows)} 行 -> {out_path}")


if __name__ == "__main__":
    main()
