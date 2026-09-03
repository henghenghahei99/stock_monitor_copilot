#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
把筛选结果 CSV 导出为东方财富可批量导入的自选股文本(txt)。

东方财富批量导入说明(PC版):
  菜单: 工具 -> 自选股 -> 高级 -> 批量导入(或 文本导入)
  支持文本文件, 每行一只股票, 格式: "代码" 或 "代码 名称"。
  A股: 6位数字(自动识别沪深), 或带前缀 SH600519/SZ000001
  港股: 5位数字(如 00700), 或带前缀 HK00700
  美股: 原代码(如 AAPL, 不带 .OQ/.N 交易所后缀)
App版: 我的自选 -> 批量导入 -> 粘贴文本(每行一个代码) 也可用本文件内容。

用法:
  python export_watchlist.py output/us_pullback_full.csv                # 默认 代码+名称
  python export_watchlist.py -i output/us_uptrend.csv -o my_em.txt      # 指定输出
  python export_watchlist.py -i a.csv -i b.csv --style code             # 合并多文件, 只要代码
  python export_watchlist.py -i out.csv --prefix                        # A股/港股加 SH/SZ/HK 前缀
  python export_watchlist.py -i out.csv --style code --names none        # 纯代码(最稳)
"""

from __future__ import annotations

import argparse
import os
import re
import sys

import pandas as pd

BASE_DIR = os.path.dirname(os.path.abspath(__file__))     # code/
ROOT_DIR = os.path.dirname(BASE_DIR)                      # v1/
OUTPUT_DIR = os.path.join(ROOT_DIR, "output")

# 美股交易所后缀(东财美股代码不带后缀)
US_SUFFIX = re.compile(r"\.(OQ|N|A|O)$", re.I)


def detect_market(ticker: str) -> str:
    """根据 ticker 前缀判断市场: cn / hk / us。"""
    t = ticker.strip().upper()
    if t[:2] in ("SH", "SZ", "BJ"):
        return "cn"
    if t[:2] == "HK":
        return "hk"
    if t[:2] == "US":
        return "us"
    if re.fullmatch(r"\d{6}", t):
        return "cn"     # A股6位
    if re.fullmatch(r"\d{5}", t):
        return "hk"     # 港股5位
    return "us"         # 其余按美股(字母代码)


def to_em_code(ticker: str, prefixed: str = "", with_prefix: bool = False) -> str:
    """把内部代码转成东财可识别的代码。"""
    t = ticker.strip()
    mkt = detect_market(t)
    if mkt == "cn":
        code = re.sub(r"^(SH|SZ|BJ)", "", t.upper())
        code = re.sub(r"^(sh|sz|bj)", "", code)
        if re.fullmatch(r"\d{6}", code):
            return f"{t[:2].upper()}{code}" if with_prefix else code
        # 可能带 .SH/.SZ 后缀
        mm = re.match(r"(\d{6})\.(SH|SZ|BJ)", code.upper())
        if mm:
            return f"{mm.group(2)}{mm.group(1)}" if with_prefix else mm.group(1)
        return code
    if mkt == "hk":
        code = re.sub(r"^HK", "", t.upper())
        mm = re.match(r"(\d{5})", code)
        if mm:
            return f"HK{mm.group(1)}" if with_prefix else mm.group(1)
        return code
    # 美股: 去 us 前缀和交易所后缀
    code = re.sub(r"^US", "", t.upper())
    code = US_SUFFIX.sub("", code)
    return code


def build_rows(df: pd.DataFrame, style: str, with_prefix: bool):
    """生成 (代码, 名称) 列表。"""
    rows = []
    for _, r in df.iterrows():
        ticker = str(r.get("ticker", "")).strip()
        prefixed = str(r.get("prefixed", "")).strip()
        if not ticker:
            continue
        code = to_em_code(ticker, prefixed, with_prefix)
        name = ""
        if style in ("name", "both"):
            name = str(r.get("name", "") or "").strip()
            if not name:
                name = str(r.get("full_name", "") or "").strip()
        rows.append((code, name))
    # 去重(保持顺序)
    seen = set()
    uniq = []
    for c, n in rows:
        if c in seen:
            continue
        seen.add(c)
        uniq.append((c, n))
    return uniq


def write_output(rows, out_path: str, style: str) -> None:
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    lines = []
    for c, n in rows:
        if style == "code":
            lines.append(c)
        elif style == "name":
            lines.append(f"{c} {n}".rstrip())
        else:  # both: 代码,名称 (同花顺/东财兼容)
            lines.append(f"{c},{n}".rstrip(","))
    with open(out_path, "w", encoding="utf-8-sig") as f:
        f.write("\n".join(lines) + "\n")
    print(f"已导出 {len(lines)} 只 -> {out_path}")
    print("示例:")
    for l in lines[:5]:
        print("  " + l)


def main() -> None:
    p = argparse.ArgumentParser(description="筛选结果 -> 东方财富批量导入文本")
    p.add_argument("-i", "--input", action="append", required=True,
                   help="结果CSV(可多次, 自动合并去重)")
    p.add_argument("-o", "--output", help="输出txt路径(默认 output/xxx_em.txt)")
    p.add_argument("--style", choices=["code", "name", "both"], default="name",
                   help="code=纯代码; name=代码+名称(空格); both=代码,名称")
    p.add_argument("--prefix", action="store_true",
                   help="A股/港股加 SH/SZ/HK 前缀")
    args = p.parse_args()

    frames = []
    for path in args.input:
        if not os.path.exists(path):
            print(f"文件不存在: {path}", file=sys.stderr)
            sys.exit(1)
        frames.append(pd.read_csv(path))
    df = pd.concat(frames, ignore_index=True)

    rows = build_rows(df, args.style, args.prefix)
    if not rows:
        print("没有可导出的代码。", file=sys.stderr)
        sys.exit(1)

    if args.output:
        out = args.output
    else:
        base = os.path.splitext(os.path.basename(args.input[0]))[0]
        out = os.path.join(OUTPUT_DIR, f"{base}_em.txt")
    write_output(rows, out, args.style)


if __name__ == "__main__":
    main()
