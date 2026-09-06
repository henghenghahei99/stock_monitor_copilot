#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
按行业汇总策略结果的分值, 输出行业排行。

计分规则(默认): 策略得分 -> 加权分
    8分 -> 8分, 7分 -> 6分, 6分 -> 4分, 6分以下(如5/8)不计入
行业来自结果CSV的 industry 列(美股=A股来自东财行业/美股full_info)。

用法:
  python industry_score.py -i output/cn_uptrend_industry.csv
  python industry_score.py -i output/us_pullback_full.csv --col pullback --score-map "8:8,7:6,6:4"
  python industry_score.py -i x.csv -o output/industry_rank.csv --top 20
"""

from __future__ import annotations

import argparse
import os
import sys

import pandas as pd

BASE_DIR = os.path.dirname(os.path.abspath(__file__))     # code/
ROOT_DIR = os.path.dirname(BASE_DIR)                      # A_rank_report/
OUTPUT_DIR = os.path.join(ROOT_DIR, "output")


def parse_score_map(spec: str) -> dict[int, int]:
    """'8:8,7:6,6:4' -> {8:8, 7:6, 6:4}"""
    out: dict[int, int] = {}
    for part in spec.split(","):
        k, v = part.split(":")
        out[int(k)] = int(v)
    return out


def score_to_points(s: str, mapping: dict[int, int]) -> int:
    """策略列值如 '8/8' -> 取分子 -> 查表 -> 分数(不在表内返回0, 即不计入)。"""
    try:
        sc = int(str(s).split("/")[0].strip())
    except Exception:  # noqa: BLE001
        return 0
    return mapping.get(sc, 0)


def _prepare(df: pd.DataFrame, col: str, mapping: dict[int, int]) -> pd.DataFrame:
    """加上加权分列, 只保留加权分>0(6分及以上)的行。"""
    df = df.copy()
    df["加权分"] = df[col].map(lambda s: score_to_points(s, mapping))
    return df[df["加权分"] > 0]


def representative_frame(df, col, mapping, n: int = 10) -> pd.DataFrame:
    """每个行业加权分最高前 n 只(同分按20日涨幅降序, 不足给全部)。返回这些行的子集。"""
    d = _prepare(df, col, mapping)
    if d.empty:
        return d
    d = d.sort_values(["加权分", "chg20"], ascending=[False, False])
    return d.groupby("industry", sort=False).head(n)


def aggregate(df: pd.DataFrame, col: str, mapping: dict[int, int]) -> pd.DataFrame:
    """策略结果df -> 行业汇总df(列: industry/得分/股票数/平均分/代表股), 已按得分降序。

    仅保留加权分>0的股票(即 6分及以上的计入, 默认映射 8->8, 7->6, 6->4)。
    代表股: 每行业加权分最高前 N_REP(10) 只。
    """
    d = _prepare(df, col, mapping)
    if d.empty:
        return d

    N_REP = 10
    rep = representative_frame(df, col, mapping, N_REP)

    def _fmt_rep(row) -> str:
        # 显示直观: 名字(趋势得分, 20日涨幅), 如 齐鲁银行(8/8,+17%)
        chg = row.get("chg20")
        chg_s = f"{chg:+.0f}%" if pd.notna(chg) else ""
        trend = str(row.get(col, "")) if col in row else ""
        return f"{row['name']}({trend},{chg_s})"

    rep_map: dict[str, str] = {}
    for ind, g in rep.groupby("industry", sort=False):
        rep_map[ind] = "、".join(_fmt_rep(r) for _, r in g.iterrows())

    agg = (d.groupby("industry")
             .agg(得分=("加权分", "sum"), 股票数=("加权分", "count"))
             .reset_index())
    agg["平均分"] = (agg["得分"] / agg["股票数"]).round(2)
    agg["代表股"] = agg["industry"].map(rep_map)
    agg = agg.sort_values("得分", ascending=False).reset_index(drop=True)
    return agg


def rank_table(agg: pd.DataFrame, top: int = 15, sort_by: str = "平均分") -> pd.DataFrame:
    """两步排名: 先按板块总分取前 top 名作为池, 再在池内按 sort_by(默认平均分)排序。

    与用户口径一致: "先按8/7/6打分得到板块总分, 取前15名, 再按平均分排名"。
    """
    pool = agg.sort_values("得分", ascending=False).head(top) if top and top > 0 else agg
    return pool.sort_values([sort_by, "得分", "股票数"], ascending=False).reset_index(drop=True)


def main() -> None:
    p = argparse.ArgumentParser(description="行业加权分统计")
    p.add_argument("-i", "--input", default=os.path.join(OUTPUT_DIR, "cn_uptrend_industry.csv"),
                   help="结果CSV(需含 industry 列和策略列)")
    p.add_argument("--col", default="uptrend", help="策略列名(默认 uptrend)")
    p.add_argument("--score-map", default="8:8,7:6,6:4",
                   help="得分->加权分映射(默认 '8:8,7:6,6:4', 未列出的得分不计入)")
    p.add_argument("-o", "--output", help="排行CSV输出路径")
    p.add_argument("--top", type=int, default=0, help="只显示前N个行业(0=全部)")
    p.add_argument("--sort", choices=["平均分", "得分", "股票数"], default="平均分",
                   help="排行排序字段(默认平均分)")
    args = p.parse_args()

    if not os.path.exists(args.input):
        print(f"文件不存在: {args.input}", file=sys.stderr)
        sys.exit(1)
    df = pd.read_csv(args.input)
    if args.col not in df.columns:
        print(f"CSV里没有策略列 '{args.col}', 现有列: {list(df.columns)}", file=sys.stderr)
        sys.exit(1)
    if "industry" not in df.columns:
        print(f"CSV里没有 industry 列, 现有列: {list(df.columns)}", file=sys.stderr)
        sys.exit(1)

    mapping = parse_score_map(args.score_map)
    agg = aggregate(df, args.col, mapping)
    if agg.empty:
        print("没有计入的股票(都在6分以下)。")
        return

    # 两步排名: 先按板块总分取前 top 名, 再在池内按 --sort(默认平均分)排序
    show = rank_table(agg, args.top, args.sort)
    print(f"计入股票 {int(agg['股票数'].sum())} 只, 共 {len(agg)} 个行业; 前{len(show)}名按{args.sort}排序:")
    print(show.to_string(index=False))

    base = os.path.splitext(os.path.basename(args.input))[0]
    if base.endswith("_industry"):
        base = base[:-len("_industry")]
    out = args.output or os.path.join(OUTPUT_DIR, f"{base}_industry_rank.csv")
    show.to_csv(out, index=False, encoding="utf-8-sig")
    print(f"\n已保存 -> {out}")


if __name__ == "__main__":
    main()
