#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
生成 A_rank 日报 HTML/文本报告文件(不发送), 供查看或由 send_report.py 发送。

用法:
  python make_a_rank_report.py --date 0902
  -> output/a_rank_report_0902.html / .txt
"""

from __future__ import annotations

import argparse
import os
import sys

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import industry_score as isc  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "output")


def _fmt(v):
    """数值规整: 整数值去掉 .0; NaN/None 显示为 -。"""
    if v is None:
        return "-"
    if isinstance(v, float):
        if pd.isna(v):
            return "-"
        if v.is_integer():
            return str(int(v))
    return v


UP = "#d93025"      # 红: 上涨/名次上升
DOWN = "#188038"     # 绿: 下跌/名次下降
NEW_BLUE = "#1a73e8"  # 新进池
NUM_COLS = {"排名", "得分", "股票数", "平均分", "前日排名", "今日排名",
           "排名变化", "前日得分", "今日得分", "得分变化"}


def _color(c: str, v):
    """变化列按正负返回红/绿; 状态列 新进池=蓝。"""
    if c in ("排名变化", "得分变化"):
        try:
            val = float(v)
        except Exception:  # noqa: BLE001
            return ""
        if val > 0:
            return UP
        if val < 0:
            return DOWN
        return "#999"
    if c == "状态":
        return NEW_BLUE if v == "新进池" else ("#666" if v == "退出池" else "")
    return ""


def _html_table(df: pd.DataFrame) -> str:
    """美化 HTML 表格: 彩色表头 + 斑马纹 + 涨红跌绿。"""
    cols = list(df.columns)
    h = ('<table style="border-collapse:collapse;font-size:14px;font-family:Segoe UI,'
         'Microsoft YaHei,sans-serif;box-shadow:0 1px 3px rgba(0,0,0,.12);border-radius:6px;overflow:hidden">')
    h += ('<thead><tr style="background:#2b579a;color:#fff">'
          + "".join(f'<th style="padding:9px 14px;border:1px solid #1d3f6e">{c}</th>' for c in cols)
          + "</tr></thead>")
    for i, (_, r) in enumerate(df.iterrows()):
        bg = "#ffffff" if i % 2 == 0 else "#f2f6fc"
        h += f'<tr style="background:{bg}">'
        for c, v in zip(cols, r):
            if c == "代表股":
                # 代表股: 小一号字体, 均分3行(9只→3/3/3; 不足如8→3/3/2, 7→3/2/2)
                items = [x for x in str(v).split("、") if x]
                n = len(items)
                base, rem = divmod(n, 3)
                parts, start = [], 0
                for i in range(3):
                    s = base + (1 if i < rem else 0)
                    if s:
                        parts.append("、".join(items[start:start + s]))
                        start += s
                h += (f'<td style="padding:6px 14px;border:1px solid #e3e9f2;'
                      f'font-size:12px;color:#444;line-height:1.7">{"<br>".join(parts)}</td>')
                continue
            col = _color(c, v)
            colst = f';color:{col};font-weight:bold' if col else ""
            h += (f'<td style="padding:7px 14px;border:1px solid #e3e9f2{colst}">'
                  f"{_fmt(v)}</td>")
        h += "</tr>"
    return h + "</table>"


def _txt_table(df: pd.DataFrame) -> str:
    """DataFrame -> 对齐的纯文本表(整数值显示为整数, NaN 显示为 -)。"""
    rows = [[str(_fmt(v)) for v in r] for r in df.itertuples(index=False)]
    cols = list(df.columns)
    widths = [max([len(cols[i])] + [len(r[i]) for r in rows]) for i in range(len(cols))]
    def line(cells):
        return "  ".join(c.ljust(widths[i]) for i, c in enumerate(cells))
    out = [line(cols), "-" * (sum(widths) + 2 * (len(cols) - 1))]
    out += [line(r) for r in rows]
    return "\n".join(out)


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--date", required=True, help="报告日期, 如 0902")
    p.add_argument("--results", default=None, help="命中结果CSV(用于算今日榜单)")
    p.add_argument("--delta", default=None, help="delta CSV(可选)")
    args = p.parse_args()

    # 今日 A_rank 榜单
    if args.results:
        rk = isc.rank_table(
            isc.aggregate(pd.read_csv(args.results), "uptrend", {8: 8, 7: 6, 6: 4}),
            15, "平均分")
        rk.insert(0, "排名", range(1, len(rk) + 1))
        rk["股票数"] = rk["股票数"].astype(int)
    else:
        rk = None

    # delta
    delta = pd.read_csv(args.delta) if args.delta and os.path.exists(args.delta) else None

    title = f"A_rank 日报 · 2026-{args.date[:2]}-{args.date[2:]} A股板块得分排名"
    head = ("<head><meta charset='utf-8'><title>%s</title><style>"
            "body{font-family:Segoe UI,'Microsoft YaHei',sans-serif;color:#222}"
            "h2{color:#2b579a}h3{color:#444}.note{color:#999;font-size:12px}"
            "</style></head>" % title)
    parts = [f"<html>{head}<body style='padding:16px'>", f"<h2>{title}</h2>"]
    rank_note = ("<p class='note'>表后注(今日板块排名算法)：<br>"
                 "① 个股分=上涨趋势8个条件(多头排列/站上年线/MA20上行/低点抬高/高点抬高/斜率向上/"
                 "近60日新高/放量)中满足的个数，满足1个记1分，≥5分命中；历史K线不足只评得部分条件时按比例折算到 /8(如 5/7→6/8)。<br>"
                 "② 行业加权：8/8→8分、7/8→6分、6/8→4分；6分以下(如5/8)命中但计0分。<br>"
                 "③ 行业得分=该行业成员加权分之和；股票数=计入只数；平均分=得分/股票数。<br>"
                 "④ 排名：先按行业得分取总分前15名为池，池内再按平均分降序。<br>"
                 "⑤ 代表股=每行业加权分最高前9只(同分按20日涨幅降序，不足9只给全部)；X/8=满足条件数，+xx%=近20日涨幅。</p>")
    if rk is not None:
        parts.append("<h3>今日板块排名(上涨趋势→8/7/6加权→总分前15池→平均分)</h3>")
        parts.append(_html_table(rk.rename(columns={"industry": "行业"})))
        parts.append(rank_note)
    delta_note = ("<p class='note'>表后注(排名升降算法)：对前一交易日与今日各自独立计算上方A_rank"
                  "(总分前15池、池内按平均分排名)，得到两日的排名/得分/平均分/只数。"
                  "状态：池内=两日均在前15名；新进池=今日新进(前日不在池)；退出池=今日掉出池。"
                  "排名变化=前日排名−今日排名(正=名次上升，负=下降)；得分变化=今日得分−前日得分。</p>")
    if delta is not None:
        parts.append("<h3>与前一交易日排名升降 (排名变化: 正=名次上升)</h3>")
        d = delta.copy()
        for c in ["前日排名", "今日排名", "排名变化"]:
            if c in d.columns:
                d[c] = d[c].where(d[c].notna(), "-")
        parts.append(_html_table(d))
        parts.append(delta_note)
    parts.append("<p class='note'>AI生成，仅供研究，不构成投资建议</p>")
    parts.append("</body></html>")
    html = "".join(parts)

    base = os.path.join(OUT, f"a_rank_report_{args.date}")
    with open(base + ".html", "w", encoding="utf-8") as f:
        f.write(html)
    # 纯文本版
    lines = [title, ""]
    if rk is not None:
        lines.append("== 今日板块排名 ==")
        lines.append(_txt_table(rk))
        lines.append("算法: ①个股分=上涨趋势8条件(多头排列/站上年线/MA20上行/低点抬高/高点抬高/斜率向上/"
                     "近60日新高/放量)满足个数(1个1分, >=5命中; 历史不足按比例折到/8, 如5/7->6/8); "
                     "②行业加权 8/8->8分、7/8->6分、6/8->4分(6以下计0); ③行业得分=成员加权分之和, "
                     "平均分=得分/股票数; ④先按得分取总分前15为池, 池内按平均分降序; "
                     "⑤代表股=加权分最高前9只(同分按20日涨幅, 不足全给), X/8=满足条件数, +xx%=近20日涨幅")
    if delta is not None:
        lines.append("")
        lines.append("== 与前一日排名升降 ==")
        lines.append(_txt_table(delta))
        lines.append("算法: 前一日与今日各自算上方A_rank(总分前15池按平均分排名)后对比; "
                     "状态: 池内=两日均在前15, 新进池=今日新进, 退出池=今日掉出; "
                     "排名变化=前日排名-今日排名(正=名次上升); 得分变化=今日得分-前日得分")
    with open(base + ".txt", "w", encoding="utf-8") as f:
        f.write("\n".join(lines))

    print(f"已生成: {base}.html")
    print(f"已生成: {base}.txt")


if __name__ == "__main__":
    main()
