#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
A_rank_delta: A_rank 板块得分排名的"前一日对比"榜单。

口径:
  1) 对 [昨日结果CSV] 和 [今日结果CSV] 各自做 A_rank(双口径入池):
     上涨趋势 -> 8/8=8分 7/8=6分 6/8=4分(6分以下不计) -> 行业加权总分
     -> 趋势分=得分/股票数; 动量分=代表股前10的 1/2/3日动量(±10)
     -> 入池 = 原得分前 top ∪ 动量分前 top; 池内按 总分=趋势分*50%+动量分*50% 排序得排名
  2) 对比两日排名, 输出: 前日排名 / 今日排名 / 排名变化(正=上升)
     / 状态(池内·新进池·退出池) / 趋势分·动量分·总分 及其变化

用法:
  python a_rank_delta.py --old output/cn_uptrend_0901.csv --new output/cn_uptrend_0902.csv
  python a_rank_delta.py -o output/cn_uptrend_0901.csv -n output/cn_uptrend_0902.csv --top 10
"""

from __future__ import annotations

import argparse
import os
import sys

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import industry_score as isc  # noqa: E402

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # A_rank_report/
OUTPUT_DIR = os.path.join(ROOT_DIR, "output")


def a_rank(path: str, col: str, mapping: dict[int, int], top: int,
           sort_by: str = "总分") -> pd.DataFrame:
    """对一份命中结果CSV计算 A_rank(结构分50%+代表股动量50%), 返回带 '排名'(1..top) 的行业表。"""
    import sector_momentum as sm  # noqa: PLC0415
    df = pd.read_csv(path)
    rk = sm.combined_rank(df, col, mapping, top).reset_index(drop=True)
    rk["排名"] = rk.index + 1
    return rk


def main() -> None:
    p = argparse.ArgumentParser(description="A_rank 板块排名 前一日对比(A_rank_delta)")
    p.add_argument("-o", "--old", required=True, help="昨日命中结果CSV(含 uptrend 列)")
    p.add_argument("-n", "--new", required=True, help="今日命中结果CSV(含 uptrend 列)")
    p.add_argument("--col", default="uptrend", help="策略列名(默认 uptrend)")
    p.add_argument("--score-map", default="8:8,7:6,6:4",
                   help="得分->加权分(默认 8:8,7:6,6:4)")
    p.add_argument("--top", type=int, default=10, help="池大小(默认10)")
    p.add_argument("--sort", default="平均分", help="池内排序字段(默认平均分)")
    p.add_argument("-o2", "--output", default=None, help="输出CSV路径")
    args = p.parse_args()

    mapping = isc.parse_score_map(args.score_map)
    old = a_rank(args.old, args.col, mapping, args.top, args.sort).set_index("industry")
    new = a_rank(args.new, args.col, mapping, args.top, args.sort).set_index("industry")

    inds = sorted(set(old.index) | set(new.index), key=lambda x: new["排名"].get(x, 99))
    rows = []
    for ind in inds:
        rows.append({
            "行业": ind,
            "前日排名": old["排名"].get(ind),
            "今日排名": new["排名"].get(ind),
            "前日趋势分": old["趋势分"].get(ind),
            "今日趋势分": new["趋势分"].get(ind),
            "前日动量分": old["动量分"].get(ind),
            "今日动量分": new["动量分"].get(ind),
            "前日总分": old["总分"].get(ind),
            "今日总分": new["总分"].get(ind),
        })
    m = pd.DataFrame(rows)
    m["状态"] = m.apply(
        lambda r: "池内" if (pd.notna(r["前日排名"]) and pd.notna(r["今日排名"]))
        else ("新进池" if pd.isna(r["前日排名"]) and pd.notna(r["今日排名"]) else "退出池"),
        axis=1)
    m["排名变化"] = m["前日排名"] - m["今日排名"]              # 正=名次上升
    m["趋势分变化"] = m["今日趋势分"].fillna(0) - m["前日趋势分"].fillna(0)
    m["动量分变化"] = m["今日动量分"].fillna(0) - m["前日动量分"].fillna(0)
    m["总分变化"] = m["今日总分"].fillna(0) - m["前日总分"].fillna(0)
    m = m.sort_values(["今日排名", "前日排名"], na_position="last").reset_index(drop=True)

    print(f"=== A_rank_delta: 前一日({args.old}) vs 今日({args.new}) 排名升降 (总分=趋势分*50%+动量分*50%) ===")
    show_cols = ["行业", "状态", "前日排名", "今日排名", "排名变化",
                 "前日趋势分", "今日趋势分", "趋势分变化",
                 "前日动量分", "今日动量分", "动量分变化",
                 "前日总分", "今日总分", "总分变化"]
    print(m[show_cols].to_string(index=False))

    out = args.output
    if out is None:
        base = os.path.splitext(os.path.basename(args.new))[0]
        out = os.path.join(OUTPUT_DIR, f"{base}_delta.csv")
    m[show_cols].to_csv(out, index=False, encoding="utf-8-sig")
    print(f"\n已保存 -> {out}")


if __name__ == "__main__":
    main()
