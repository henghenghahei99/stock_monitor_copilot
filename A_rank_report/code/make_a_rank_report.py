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
import sector_momentum as sm  # noqa: E402

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
MOM_COL = "#e65100"    # 代表股: ◎动量代表股(全部成分股短线动量) 橙色
NUM_COLS = {"排名", "得分", "股票数", "趋势分", "动量分", "总分", "前日排名", "今日排名",
           "排名变化", "趋势分变化", "动量分变化", "总分变化"}


def _color(c: str, v):
    """变化列按正负返回红/绿(涨红跌绿); 状态列 新进池=蓝。"""
    if c in ("排名变化", "趋势分变化", "动量分变化", "总分变化"):
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


def _eval_cell(text: str) -> str:
    """评价列关键词强调: 很强/动量爆发=红色加粗(强势), 大幅下滑/很弱=绿色加粗(弱势)。"""
    # 长词在前, 避免 "动量大幅下滑" 只匹配到子串 "大幅下滑" 前的部分被跳过
    toks = [("动量大幅下滑", DOWN), ("动量爆发", UP), ("很弱", DOWN), ("很强", UP)]
    out, idx = [], 0
    while idx < len(text):
        best = None
        for t, col in toks:
            j = text.find(t, idx)
            if j != -1 and (best is None or j < best[0]):
                best = (j, t, col)
        if best is None:
            out.append(text[idx:])
            break
        j, t, col = best
        if j > idx:
            out.append(text[idx:j])
        out.append(f'<span style="color:{col};font-weight:bold">{t}</span>')
        idx = j + len(t)
    return "".join(out)


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
                # 代表股: 趋势前5(常规色) + ◎动量前5(橙色标色); 小号字体均分3行
                items = [x for x in str(v).split("、") if x]
                n = len(items)
                base, rem = divmod(n, 3)
                parts, start = [], 0
                for i in range(3):
                    s = base + (1 if i < rem else 0)
                    if s:
                        seg = []
                        for it in items[start:start + s]:
                            if it.startswith("◎"):
                                seg.append(f'<span style="color:{MOM_COL};font-weight:600">◎{it[1:]}</span>')
                            else:
                                seg.append(f"<span>{it}</span>")
                        parts.append("、".join(seg))
                        start += s
                h += (f'<td style="padding:6px 14px;border:1px solid #e3e9f2;'
                      f'font-size:12px;color:#444;line-height:1.7">{"<br>".join(parts)}</td>')
                continue
            if c == "评价":
                txt = _fmt(v)
                h += (f'<td style="padding:7px 14px;border:1px solid #e3e9f2">'
                      f'{_eval_cell(txt) if txt not in ("", "-") else txt}</td>')
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


# ---------- 升降表“评价”列 ----------
def _trend_word(chg):
    """趋势分变化 -> 词: 正增强/负减弱/0平稳。"""
    if chg is None or (isinstance(chg, float) and pd.isna(chg)):
        return ""
    if chg > 0:
        return "趋势增强"
    if chg < 0:
        return "趋势减弱"
    return "趋势平稳"


def _mom_word(chg):
    """动量分变化 -> 词: ≥1.5爆发 / 0~1.5增强 / -1.5~0减弱 / <-1.5大幅下滑 / 0平稳。"""
    if chg is None or (isinstance(chg, float) and pd.isna(chg)):
        return ""
    if chg >= 1.5:
        return "动量爆发"
    if chg > 0:
        return "动量增强"
    if chg == 0:
        return "动量平稳"
    if chg > -1.5:
        return "动量减弱"
    return "动量大幅下滑"


def _pct_level(val, refs):
    """val 在 refs(越高越好)中的百分位等级: 前10%很强 / 10-30%强 / 30-70%一般 / 70-100%弱。"""
    if val is None or not refs:
        return "弱"
    pct = sum(1 for x in refs if x > val) / len(refs)
    if pct <= 0.10:
        return "很强"
    if pct <= 0.30:
        return "强"
    if pct <= 0.70:
        return "一般"
    return "弱"


def _add_eval_col(delta, today_scores, pool_inds):
    """给 delta 加“评价”列:
    池内  -> 按 趋势分变化(增强/减弱) + 动量分变化(爆发/增强/减弱/大幅下滑);
    新进/退出 -> 按该板块当日 动量分/趋势分 在今日池内百分位: 前10%很强/前10-30%强/30-70%一般/70-100%弱。
    """
    pool_df = today_scores[today_scores["industry"].isin(pool_inds)]
    mom_refs = list(pool_df["动量分"])
    tr_refs = list(pool_df["趋势分"])
    evals = []
    for _, r in delta.iterrows():
        st = r.get("状态")
        ind = r.get("行业")
        if st == "池内":
            tw, mw = _trend_word(r.get("趋势分变化")), _mom_word(r.get("动量分变化"))
            evals.append("、".join(x for x in (tw, mw) if x))
            continue
        sub = today_scores[today_scores["industry"] == ind]
        if sub.empty:
            evals.append("趋势弱、动量弱")
            continue
        tv = float(sub["趋势分"].iloc[0])
        mv = float(sub["动量分"].iloc[0])
        in_pool = ind in pool_inds
        evals.append("趋势" + _pct_level(tv, tr_refs if in_pool else tr_refs + [tv])
                     + "、动量" + _pct_level(mv, mom_refs if in_pool else mom_refs + [mv]))
    delta["评价"] = evals


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--date", required=True, help="报告日期, 如 0902")
    p.add_argument("--results", default=None, help="命中结果CSV(用于算今日榜单)")
    p.add_argument("--delta", default=None, help="delta CSV(可选)")
    args = p.parse_args()

    # 今日 A_rank 榜单
    dfr = pd.read_csv(args.results) if args.results else None
    if dfr is not None:
        rk = sm.combined_rank(dfr, "uptrend", {8: 8, 7: 6, 6: 4}, 15)
        # 榜单不展示: 行业得分/股票数/动量入池分(原始分, 只看归一后的动量分)
        rk = rk.drop(columns=["得分", "股票数", "动量入池分"], errors="ignore")
        rk.insert(0, "排名", range(1, len(rk) + 1))
    else:
        rk = None

    # delta + 评价列
    delta = pd.read_csv(args.delta) if args.delta and os.path.exists(args.delta) else None
    if delta is not None and dfr is not None:
        try:
            today_scores = sm.industry_momentum_scores(dfr)
            pool_inds = set(rk["industry"]) if rk is not None else set(today_scores["industry"])
            _add_eval_col(delta, today_scores, pool_inds)
        except Exception as exc:  # noqa: BLE001
            print(f"[警告] 评价列计算失败: {exc}", file=sys.stderr)
    # 第二表数值保留两位小数(排名仍为整数, 由后面的 "-" 处理)
    if delta is not None:
        for c in delta.columns:
            if c in {"前日趋势分", "今日趋势分", "趋势分变化",
                     "前日动量分", "今日动量分", "动量分变化",
                     "前日总分", "今日总分", "总分变化"}:
                delta[c] = delta[c].apply(
                    lambda v: "-" if pd.isna(v) else f"{float(v):.2f}")
        # 升降表: 只保留"有变化"的板块(新进/退出, 或名次变动), 且只展示"变化"类列
        if "排名变化" in delta.columns and "状态" in delta.columns:
            rank_chg = pd.to_numeric(delta["排名变化"], errors="coerce").fillna(0)
            delta = delta[(delta["状态"] != "池内") | (rank_chg != 0)].reset_index(drop=True)
            # 新进池/退出池无前日基线, 变化列显示 "-"
            for c in ["排名变化", "趋势分变化", "动量分变化", "总分变化"]:
                if c in delta.columns:
                    delta.loc[delta["状态"] != "池内", c] = "-"
            keep = [c for c in ["行业", "状态", "排名变化",
                                "趋势分变化", "动量分变化", "总分变化", "评价"] if c in delta.columns]
            delta = delta[keep]

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
                 "③ 行业得分=该行业成员加权分之和；趋势分=得分/计入股票数。<br>"
                 "④ 动量分(±10)与动量入池分同源：取板块全部成分股按 m=当日%+近2日%+近3日%(三段叠加) 降序前10"
                 "(不足按实际只数)，S=Σ前10的m；动量入池分=S；展示动量分=S÷(15%×只数/10)×0.5 归一(允许为负)。<br>"
                 "⑤ 总分 = 趋势分×50% + 动量分×50%；入池=原行业得分前15名 ∪ 动量入池分前15名(并集, 最多30个)，"
                 "池内按总分降序排名；入池列: 趋势=按得分入池、动量=按动量入池分入池、趋势+动量=双口径都占。<br>"
                 "⑥ 代表股=趋势前5(加权分最高, 记X/8,+近20日涨幅) + ◎动量前5(板块全部成分股按m最强, 橙色◎=短线动量, 记+短线%)。</p>")
    if rk is not None:
        parts.append("<h3>今日板块排名(趋势分50% + 动量分50% → 入池=原得分前15 ∪ 动量前15 → 总分)</h3>")
        parts.append(_html_table(rk.rename(columns={"industry": "行业"})))
        parts.append(rank_note)
    delta_note = ("<p class='note'>表后注(排名升降算法)：对前一交易日与今日各自按上方A_rank"
                  "(入池=原得分前15 ∪ 动量入池分前15、池内按总分=趋势分×50%+动量分×50% 降序)计算后对比。"
                  "状态：池内=两日均在池内；新进池=今日新进(前日不在池)；退出池=今日掉出池。"
                  "涨红跌绿：排名变化=前日排名−今日排名(正=名次上升)；"
                  "趋势分变化/动量分变化/总分变化=今日−前日(正=升，负=降)。<br>"
                  "评价：池内按 趋势分变化(正=趋势增强/负=趋势减弱) + 动量分变化(≥1.5动量爆发 / 0~1.5动量增强 / -1.5~0动量减弱 / <-1.5动量大幅下滑)；"
                  "新进/退出按该板块当日动量分、趋势分在今日池内百分位：前10%很强 / 前10-30%强 / 30-70%一般 / 70-100%弱。</p>")
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
                     "趋势分=得分/股票数; ④动量分(±10)与动量入池分同源: 板块全部成分股按 m=当日%+近2日%+近3日% 降序前10"
                     "(不足按实际只数)S=Σ, 动量入池分=S, 展示动量分=S/(15%*只数/10)*0.5(允许为负); "
                     "⑤总分=趋势分*50%+动量分*50%, 入池=原得分前15 ∪ 动量入池分前15(并集, 最多30), 池内按总分降序; "
                     "入池列: 趋势=得分入池, 动量=动量入池, 趋势+动量=双口径; "
                     "⑥代表股=趋势前5(加权分, X/8,+近20日%) + ◎动量前5(板块全部成分股按m最强, 记+短线%)")
    if delta is not None:
        lines.append("")
        lines.append("== 与前一日排名升降 ==")
        lines.append(_txt_table(delta))
        lines.append("算法: 前一日与今日各自按上方A_rank(入池=原得分前15 ∪ 动量入池分前15, 池内按总分=趋势分*50%+动量分*50%降序)后对比; "
                     "状态: 池内=两日均在池内, 新进池=今日新进, 退出池=今日掉出; "
                     "涨红跌绿: 排名变化=前日排名-今日排名(正=名次上升); 趋势分变化/动量分变化/总分变化=今日-前日; "
                     "评价: 池内按趋势分变化(正=增强/负=减弱)+动量分变化(>=1.5爆发/0~1.5增强/-1.5~0减弱/<-1.5大幅下滑); "
                     "新进/退出按当日动量分·趋势分在今日池内百分位(前10%很强/前10-30%强/30-70%一般/70-100%弱)")
    with open(base + ".txt", "w", encoding="utf-8") as f:
        f.write("\n".join(lines))

    print(f"已生成: {base}.html")
    print(f"已生成: {base}.txt")


if __name__ == "__main__":
    main()
