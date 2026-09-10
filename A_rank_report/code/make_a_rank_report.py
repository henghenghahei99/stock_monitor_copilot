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
import colorsys
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
NEW_BLUE = "#1a73e8"    # 新进池
NEW_PURPLE = "#7b1fa2"   # 完全新进池
MOM_COL = "#e65100"    # 代表股: ◎动量代表股(全部成分股短线动量) 橙色
NEW_MOM_COL = "#6a1b9a"  # 代表股: ◆动量代表股中相比前日新进入的 紫色
NUM_COLS = {"排名", "得分", "股票数", "趋势分", "动量分", "总分", "前日排名", "今日排名",
           "排名变化", "趋势分变化", "动量分变化", "总分变化"}


def _color(c: str, v):
    """变化列按正负返回红/绿(涨红跌绿); 状态列 新进池=蓝, 完全新进池=紫, 退出池=灰。"""
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
        if v == "完全新进池":
            return NEW_PURPLE
        if v == "新进池":
            return NEW_BLUE
        if v == "退出池":
            return "#666"
        return ""
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
                # 代表股: 趋势前5(常规色) + 动量前7(◎橙/◆紫); 小号字体均分3行, 每行 nowrap 保证不换行
                items = [x for x in str(v).split("、") if x]
                n = len(items)
                base, rem = divmod(n, 3)
                parts, start = [], 0
                for i in range(3):
                    s = base + (1 if i < rem else 0)
                    if s:
                        seg = []
                        for it in items[start:start + s]:
                            if it.startswith("◆"):
                                seg.append(f'<span style="color:{NEW_MOM_COL};font-weight:600">◆{it[1:]}</span>')
                            elif it.startswith("◎"):
                                seg.append(f'<span style="color:{MOM_COL};font-weight:600">◎{it[1:]}</span>')
                            else:
                                seg.append(f"<span>{it}</span>")
                        parts.append(f'<span style="white-space:nowrap">{"、".join(seg)}</span>')
                        start += s
                h += (f'<td style="padding:5px 10px;border:1px solid #e3e9f2;'
                      f'font-size:10.5px;color:#444;line-height:1.65">{"<br>".join(parts)}</td>')
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


def _pct_level(val, refs, top="很强"):
    """val 在 refs(越高越好)中的百分位等级: 前10% {top} / 10-30%强 / 30-70%一般 / 70-100%弱。"""
    if val is None or not refs:
        return "弱"
    pct = sum(1 for x in refs if x > val) / len(refs)
    if pct <= 0.10:
        return top
    if pct <= 0.30:
        return "强"
    if pct <= 0.70:
        return "一般"
    return "弱"


def _add_eval_col(delta, today_scores, pool_inds):
    """给 delta 加“评价”列:
    池内  -> 趋势分变化(增强/减弱/平稳) + 动量分变化(≥1.5爆发/0~1.5增强/-1.5~0减弱/<-1.5大幅下滑);
    新进/退出(新上榜) -> 按该板块当日 趋势分/动量分 在今日池内百分位:
        趋势 前10%很强 / 动量 前10%爆发; 10-30%强 / 30-70%一般 / 70-100%弱。
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
        evals.append("趋势" + _pct_level(tv, tr_refs if in_pool else tr_refs + [tv], "很强")
                     + "、动量" + _pct_level(mv, mom_refs if in_pool else mom_refs + [mv], "爆发"))
    delta["评价"] = evals


# ---------- 近N日走势: 每个板块一条线 ----------


def _coverage_tag(df: pd.DataFrame) -> str:
    """结果CSV覆盖口径: 按 prefixed 前2位判断 仅沪/沪深北 等。"""
    try:
        if "prefixed" not in df.columns:
            return ""
        pref = set(str(x)[:2] for x in df["prefixed"].dropna())
        have = {p for p in pref if p in ("sh", "sz", "bj")}
        if not have:
            return ""
        if {"sz", "bj"} & have:
            return "沪深北"
        return "仅沪市"
    except Exception:  # noqa: BLE001
        return ""


def _recent_pool_series(date_mmdd: str, days: int = 5, top: int = 15) -> list[dict]:
    """近 days 个有结果CSV的交易日(<=报告日, 含报告日本身), 各自"当天池"的每板块分数。

    每天用当天自己的池(combined_rank 双口径: 得分前 top ∪ 动量入池分前 top),
    记录 池内每板块 趋势分/动量分。某板块某天不在池则当天无分数(=出池, 画图时断线)。
    返回按日期升序, 有多少天返回多少。
    """
    cand = []
    for fn in os.listdir(OUT):
        if not fn.startswith("cn_uptrend_") or not fn.endswith(".csv"):
            continue
        core = fn[len("cn_uptrend_"):-4]
        if len(core) == 4 and core.isdigit() and core <= date_mmdd:
            cand.append(core)
    cand = sorted(set(cand))[-days:]
    series = []
    for core in cand:
        path = os.path.join(OUT, f"cn_uptrend_{core}.csv")
        try:
            dfr = pd.read_csv(path, encoding="utf-8-sig")
            rk = sm.combined_rank(dfr, "uptrend", {8: 8, 7: 6, 6: 4}, top)
            if rk.empty:
                continue
            series.append({
                "label": f"{core[:2]}-{core[2:]}",
                "date": core,
                "n": int(len(rk)),
                "pool": [str(x) for x in rk["industry"]],
                "scores": {str(r["industry"]): {"trend": float(r["趋势分"]),
                                              "mom": float(r["动量分"])}
                           for _, r in rk.iterrows()},
                "cov": _coverage_tag(dfr),
            })
        except Exception as exc:  # noqa: BLE001
            print(f"[警告] 近N日走势跳过 {core}: {exc}", file=sys.stderr)
    return series


# 近N日走势 配色/线型: 均匀色相 + 深/浅两档明度交替 + 4 种线型, 提升区分度
_DASHES = ("", "6 4", "2 3", "9 4 2 4")   # 实线/短虚线/点线/长短短线


def _sector_color(i: int, total: int) -> str:
    """按序分色: 均匀色相 + 明度 27%/45% 两档交替(深色系), 返回 hex(SVG/PNG 通用)。"""
    hue = ((i * 360.0 / total) if total else 0.0) / 360.0
    light = (27 + 18 * (i % 2)) / 100.0
    r, g, b = colorsys.hls_to_rgb(hue, 0.62, light)
    return "#%02x%02x%02x" % (round(r * 255), round(g * 255), round(b * 255))


def _dash(i: int) -> str:
    """按序取线型(实线/短虚/点/长短短), 相邻板块线型不同。"""
    return _DASHES[i % len(_DASHES)]


# 近N日走势筛选: 退出的不画; 需有连续在池段(>=CHART_MIN_RUN); 整体起伏过小(横盘)的也不画。
# 画图时某日不在池(出池/未入池)的缺点用该板块窗口内最低分代替, 线连续(空心圈标注)。
CHART_MIN_RUN = 3           # 板块至少要有 连续 >=3 个交易日在池才画
CHART_MIN_TREND_CHG = 0.4   # 窗口内 趋势分 max-min 至少达此值
CHART_MIN_MOM_CHG = 1.6     # 窗口内 动量分 max-min 至少达此值


def _active_view(series: list[dict]) -> list[tuple[str, int]]:
    """只画今日池中"窗口内有实际走势"的板块: 有 连续>=CHART_MIN_RUN 日在池, 且 趋势/动量 起伏达标。

    返回 [(行业, 今日排名)] (按今日池总分序, 排名=下标+1)。
    """
    today_pool = [str(x) for x in series[-1]["pool"]]
    out: list[tuple[str, int]] = []
    for i, ind in enumerate(today_pool, start=1):
        present = [k for k, p in enumerate(series) if ind in p["scores"]]
        maxrun = cur = 0
        last = None
        for k in present:
            cur = cur + 1 if (last is not None and k == last + 1) else 1
            maxrun = max(maxrun, cur)
            last = k
        if maxrun < CHART_MIN_RUN:
            continue
        pts = [p["scores"][ind] for p in series if ind in p["scores"]]
        tr = [s["trend"] for s in pts]
        mo = [s["mom"] for s in pts]
        if (max(tr) - min(tr) < CHART_MIN_TREND_CHG
                and max(mo) - min(mo) < CHART_MIN_MOM_CHG):
            continue
        out.append((ind, i))
    return out


def _direction_groups(order: list[tuple[str, int]], series: list[dict],
                      metric: str) -> tuple[list[tuple[str, int]], list[tuple[str, int]]]:
    """按某指标窗口内 首在池日->末在池日 净变化, 把板块分为 整体上升 / 整体下降 两组。

    返回 (上升组, 下降组), 各保持今日排名顺序。
    """
    up: list[tuple[str, int]] = []
    down: list[tuple[str, int]] = []
    for ind, rank in order:
        pts = [p["scores"][ind][metric] for p in series if ind in p["scores"]]
        net = (pts[-1] - pts[0]) if len(pts) >= 2 else 0.0
        (up if net >= 0 else down).append((ind, rank))
    return up, down


def _fmt_axis(v: float, span: float) -> str:
    if span >= 8:
        return f"{v:.0f}"
    if span >= 0.8:
        return f"{v:.1f}"
    return f"{v:.2f}"


def _run_path(run: list[tuple[int, float]], X, Y, color: str, n_days: int,
              dash: str = "") -> str:
    """一段连续在池日期 -> 折线段(带线型) + 圆点(今日点加大)。"""
    if not run:
        return ""
    out = ""
    if len(run) >= 2:
        d = "M " + " L ".join(f"{X(i):.1f} {Y(v):.1f}" for i, v in run)
        out += (f'<path d="{d}" fill="none" stroke="{color}" stroke-width="2.4" '
                f'stroke-linejoin="round"'
                + (f' stroke-dasharray="{dash}"' if dash else "") + "/>")
    for i, v in run:
        r = 3.6 if i == n_days - 1 else 2.4
        out += (f'<circle cx="{X(i):.1f}" cy="{Y(v):.1f}" r="{r}" fill="{color}"/>')
    return out


def _metric_svg(metric: str, series: list[dict], order: list[tuple[str, int | None]],
                colors: dict, idx: dict[str, int]) -> str:
    """每个板块一条连续折线; 不在池的日期以该板块窗口内最低分代替(空心圈标记)。metric: trend|mom。"""
    n_days = len(series)
    W, H, pl, pr, pt, pb = 900, 300, 52, 18, 30, 44
    iw, ih = W - pl - pr, H - pt - pb
    drawn = {ind for ind, _rank in order}
    vals = [p["scores"][ind][metric] for p in series
            for ind in p["scores"] if ind in drawn]
    if not vals:
        return ""
    lo, hi = min(vals), max(vals)
    if metric == "mom":
        lo, hi = min(lo, 0.0), max(hi, 0.0)
    if hi <= lo:
        hi = lo + 1.0
    pad = (hi - lo) * 0.12
    lo, hi = lo - pad, hi + pad

    def X(i): return pl + iw * (i / (n_days - 1) if n_days > 1 else 0.0)

    def Y(v): return pt + ih - (v - lo) / (hi - lo) * ih

    span = hi - lo
    grid = ""
    for k in range(5):
        v = lo + span * k / 4
        y = Y(v)
        grid += (f'<line x1="{pl}" y1="{y:.1f}" x2="{W - pr}" y2="{y:.1f}" '
                 f'stroke="#ececec" stroke-width="1"/>'
                 f'<text x="{pl - 7}" y="{y + 4:.1f}" text-anchor="end" '
                 f'font-size="11" fill="#888">{_fmt_axis(v, span)}</text>')
    if metric == "mom" and lo < 0 < hi:
        grid += (f'<line x1="{pl}" y1="{Y(0.0):.1f}" x2="{W - pr}" y2="{Y(0.0):.1f}" '
                 f'stroke="#c9c9c9" stroke-width="1" stroke-dasharray="4 3"/>')
    xlab = ""
    for i, p in enumerate(series):
        x = X(i)
        xlab += (f'<text x="{x:.1f}" y="{pt + ih + 18}" text-anchor="middle" '
                 f'font-size="12" fill="#444">{p["label"]}</text>'
                 f'<text x="{x:.1f}" y="{pt + ih + 32}" text-anchor="middle" '
                 f'font-size="10" fill="#999">{p["n"]}池</text>')
    segs = ""
    for ind, _rank in order:
        color = colors[ind]
        dash = _dash(idx[ind])          # 线型按全局序号, 与图例一致
        vals = [p["scores"].get(ind) for p in series]
        real = [v[metric] for v in vals if v is not None]
        if not real:
            continue
        mn = min(real)
        ys = [v[metric] if v is not None else mn for v in vals]  # 出池日以最低分代替
        d = "M " + " L ".join(f"{X(i):.1f} {Y(ys[i]):.1f}" for i in range(n_days))
        segs += (f'<path d="{d}" fill="none" stroke="{color}" stroke-width="2.4" '
                 f'stroke-linejoin="round"'
                 + (f' stroke-dasharray="{dash}"' if dash else "") + "/>")
        for i, v in enumerate(vals):
            x, y = X(i), Y(ys[i])
            if v is not None:
                r = 3.6 if i == n_days - 1 else 2.6
                segs += f'<circle cx="{x:.1f}" cy="{y:.1f}" r="{r}" fill="{color}"/>'
            else:      # 出池替补点: 空心圈, 表示此处为最低点代替
                segs += (f'<circle cx="{x:.1f}" cy="{y:.1f}" r="2.8" fill="#ffffff" '
                         f'stroke="{color}" stroke-width="1.6"/>')
    return (f'<svg viewBox="0 0 {W} {H}" style="width:100%;max-width:900px;'
            f'font-family:Segoe UI,Microsoft YaHei,sans-serif" xmlns="http://www.w3.org/2000/svg">'
            f'<rect width="{W}" height="{H}" fill="#fff"/>'
            f'{grid}{segs}{xlab}</svg>')


def _chart_png_bytes(metric: str, series: list[dict], order: list[tuple[str, int | None]],
                     colors: dict, idx: dict[str, int], title: str) -> bytes:
    """把某张走势图渲染成 PNG bytes(含中文标题/图例), 供邮件内嵌(QQ/163 不渲染内嵌SVG)。"""
    import io
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt  # noqa: PLC0415
    plt.rcParams["font.sans-serif"] = [
        # 注意: 必须选覆盖中文+数字+①±·→等全符号的字体(如 Noto Sans CJK);
        # Droid Sans Fallback 缺数字/±/①/·等字形会渲染成方块
        "Noto Sans CJK JP", "Noto Sans CJK KR", "Noto Sans CJK HK",
        "Noto Sans CJK SC", "WenQuanYi Zen Hei", "Droid Sans Fallback"]
    plt.rcParams["axes.unicode_minus"] = False

    labels = [p["label"] for p in series]
    ndays = len(series)
    drawn = {ind for ind, _r in order}
    vals = [p["scores"][ind][metric] for p in series
            for ind in p["scores"] if ind in drawn]
    if not vals:
        return b""
    lo, hi = min(vals), max(vals)
    if metric == "mom":
        lo, hi = min(lo, 0.0), max(hi, 0.0)
    if hi <= lo:
        hi = lo + 1.0
    pad = (hi - lo) * 0.12
    lo, hi = lo - pad, hi + pad

    ncol = max(1, (len(order) + 2) // 3)
    rows = (len(order) + ncol - 1) // ncol
    fig, ax = plt.subplots(figsize=(12.5, 3.4 + 0.32 * rows), dpi=120)
    ax.set_ylim(lo, hi)
    ax.set_xlim(-0.25, ndays - 0.75)
    ax.set_xticks(range(ndays))
    ax.set_xticklabels(labels, fontsize=11)
    ax.grid(True, axis="y", linestyle="--", alpha=0.4)
    if metric == "mom":
        ax.axhline(0, color="#c9c9c9", linewidth=1, linestyle=":")
    handles, names = [], []
    for ind, rank in order:
        color = colors[ind]
        ls = _LS[idx[ind] % len(_LS)]
        vals = [p["scores"].get(ind) for p in series]
        real = [v[metric] for v in vals if v is not None]
        if not real:
            continue
        mn = min(real)
        ys = [v[metric] if v is not None else mn for v in vals]  # 出池日以最低分代替
        xs = list(range(ndays))
        h, = ax.plot(xs, ys, color=color, linestyle=ls, linewidth=2.2)
        handles.append(h)
        names.append(f"{rank}. {ind}" if rank else ind)
        real_x = [i for i, v in enumerate(vals) if v is not None]
        miss_x = [i for i, v in enumerate(vals) if v is None]
        if real_x:
            ax.scatter(real_x, [ys[i] for i in real_x], color=color, s=18, zorder=3)
        if miss_x:     # 出池替补点: 空心圈
            ax.scatter(miss_x, [ys[i] for i in miss_x], facecolors="none",
                       edgecolors=color, s=24, linewidths=1.2, zorder=3)
    if handles:
        ax.legend(handles, names, ncol=ncol, fontsize=9, loc="upper center",
                  bbox_to_anchor=(0.5, -0.18), frameon=False)
    fig.suptitle(title, fontsize=14, y=0.99)
    buf = io.BytesIO()
    fig.savefig(buf, format="png", bbox_inches="tight")
    plt.close(fig)
    return buf.getvalue()


_LS = ["-", "--", ":", "-."]          # matplotlib 线型(与 _DASHES 顺序对应)


def _legend_html(order: list[tuple[str, int | None]], colors: dict,
                 idx: dict[str, int], min_col: int = 150) -> str:
    """小图例: 每个板块画一段与图中同色同线型的线段样本(色+线型可辨)。"""
    items = []
    for ind, rank in order:
        c = colors[ind]
        dash = _dash(idx[ind])
        da = f' stroke-dasharray="{dash}"' if dash else ""
        tag = f"{rank}. " if rank else ""
        suffix = "" if rank else "<span style='color:#999'>（窗口内出池）</span>"
        sample = (f'<svg width="40" height="14" style="flex:none;margin-right:6px">'
                  f'<line x1="2" y1="8" x2="38" y2="8" stroke="{c}" stroke-width="3"{da}/></svg>')
        items.append(
            f'<div style="display:flex;align-items:center;white-space:nowrap">'
            f'{sample}{tag}{ind}{suffix}</div>')
    return ('<div style="display:grid;grid-template-columns:repeat(auto-fill,'
            f'minmax({min_col}px,1fr));gap:2px 12px;font-size:11px;color:#444;'
            f'margin:4px 0 2px">{"".join(items)}</div>')


def _view_txt(order: list[tuple[str, int | None]], series: list[dict]) -> list[str]:
    """文本版: 每个板块一行的近N日序列(出池日为 -)。"""
    out = []
    for ind, rank in order:
        head = f"{rank}. {ind}" if rank else f"{ind}(出池)"
        cells = []
        for p in series:
            s = p["scores"].get(ind)
            if s is None:
                cells.append(f"{p['label']}: -")
            else:
                cells.append(f"{p['label']}: 趋{s['trend']:.1f}/动{s['mom']:.1f}")
        out.append(head + "  " + "  ".join(cells))
    return out


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--date", required=True, help="报告日期, 如 0902")
    p.add_argument("--results", default=None, help="命中结果CSV(用于算今日榜单)")
    p.add_argument("--delta", default=None, help="delta CSV(可选)")
    p.add_argument("--top", type=int, default=10,
                   help="趋势/动量各入池数(默认10)")
    args = p.parse_args()

    # 今日 A_rank 榜单
    dfr = pd.read_csv(args.results) if args.results else None
    if dfr is not None:
        rk = sm.combined_rank(dfr, "uptrend", {8: 8, 7: 6, 6: 4}, args.top)
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
        # 升降表: 列出自"今日池内" + "今日退出池"的全部板块(不再按名次变化过滤); 变化类列按需展示
        if "排名变化" in delta.columns and "状态" in delta.columns:
            # 保持全量(名次未变但分数变动的板块也要能看到, 如长期第1的板块)
            # 新进池/退出池无前日基线, 变化列显示 "-"
            for c in ["排名变化", "趋势分变化", "动量分变化", "总分变化"]:
                if c in delta.columns:
                    delta.loc[delta["状态"] != "池内", c] = "-"
            keep = [c for c in ["行业", "状态", "排名变化",
                                "趋势分变化", "动量分变化", "总分变化", "评价"] if c in delta.columns]
            delta = delta[keep]
            # 分组: 新进池 → 池内 → 退出池; 池内按 动量分变化 降序;
            # 新进池/退出池无变化基线, 按当日 动量分(绝对值) 降序排列(即动量强度高在前)
            mom_today = {}
            if "today_scores" in locals() and today_scores is not None and not today_scores.empty:
                mom_today = dict(zip(today_scores["industry"],
                                     pd.to_numeric(today_scores["动量分"], errors="coerce")))

    title = f"A_rank 日报 · 2026-{args.date[:2]}-{args.date[2:]} A股板块得分排名"
    head = ("<head><meta charset='utf-8'><title>%s</title><style>"
            "body{font-family:Segoe UI,'Microsoft YaHei',sans-serif;color:#222}"
            "h2{color:#2b579a}h3{color:#444}.note{color:#999;font-size:12px}"
            "</style></head>" % title)
    parts = [f"<html>{head}<body style='padding:16px'>", f"<h2>{title}</h2>"]

    # 近N日走势图(放在报告末尾): 每个板块一条线(趋势分/动量分各一组方向图); 太碎短线不画
    today_file = os.path.join(OUT, f"cn_uptrend_{args.date}.csv")
    series = (_recent_pool_series(args.date, top=args.top)
              if (args.results and os.path.exists(today_file)) else [])
    order = _active_view(series) if series else []
    chart_parts: list[str] = []

    # 状态细化: 前5日(窗口内除今天)从未入过池的“新进池” → 标记为 完全新进池
    if delta is not None and series:
        prev_pool = {str(i).strip() for p in series[:-1] for i in p["pool"]}
        new_mask = (delta["状态"].astype(str).eq("新进池")
                    & ~delta["行业"].astype(str).str.strip().isin(prev_pool))
        delta.loc[new_mask, "状态"] = "完全新进池"
    # 排序: 完全新进池 → 新进池 → 池内(按动量分变化降序: 提高多→没变→降低少→降低多) → 退出池
    # 组内次键: 当日动量分降序(新进/退出按动量强弱; 池内同动量变化时按动量强弱)
    if delta is not None and "状态" in delta.columns:
        _mom_today = {}
        if "today_scores" in locals() and today_scores is not None and not today_scores.empty:
            _mom_today = {str(i).strip(): float(v) for i, v in zip(
                today_scores["industry"],
                pd.to_numeric(today_scores["动量分"], errors="coerce").fillna(-999.0))}
        _gmap = {"完全新进池": 0, "新进池": 1, "池内": 2, "退出池": 3}
        delta["_g"] = delta["状态"].astype(str).map(lambda s: _gmap.get(s, 9))
        _pm = pd.to_numeric(delta["动量分变化"], errors="coerce").fillna(0)
        delta["_pm"] = _pm.where(delta["状态"].astype(str).eq("池内"), 0.0)
        delta["_mc"] = pd.to_numeric(
            delta["行业"].map(lambda i: _mom_today.get(str(i).strip(), -999.0)),
            errors="coerce").fillna(-999.0)
        delta = (delta.sort_values(["_g", "_pm", "_mc"], ascending=[True, False, False], kind="stable")
                      .drop(columns=["_g", "_pm", "_mc"]).reset_index(drop=True))
    if series:
        if not order:
            chart_parts.append(f"<h3>板块近{len(series)}日走势 — 今日池板块窗口内无满足条件的走势(太碎或横盘)</h3>")
        else:
            colors = {ind: _sector_color(i, len(order)) for i, (ind, _r) in enumerate(order)}
            idx = {ind: i for i, (ind, _r) in enumerate(order)}
            chart_parts.append(
                f"<h3>板块近{len(series)}日走势 — 今日池中走势明显的板块 (连续在池≥{CHART_MIN_RUN}日才画, 出池日以最低分代替)</h3>")
            seq = ["①", "②", "③", "④"]
            gi = 0
            chart_no = 0
            for metric, mname in (("mom", "动量分走势（±10）"),
                                  ("trend", "趋势分走势（每日该板块入池得分）")):
                up, down = _direction_groups(order, series, metric)
                for tag, g in (("整体上升", up), ("整体下降", down)):
                    if not g:
                        continue
                    title = f"{seq[gi]} {mname} · {tag} {len(g)}条"
                    # 邮件可见性: QQ/163 不渲染内嵌SVG, 故每张图同时导出 PNG(chart_N.png)
                    png_dir = os.path.join(OUT, f"a_rank_report_{args.date}_charts")
                    try:
                        os.makedirs(png_dir, exist_ok=True)
                        png = _chart_png_bytes(metric, series, g, colors, idx, title)
                        if png:
                            with open(os.path.join(png_dir, f"chart_{chart_no}.png"), "wb") as fp:
                                fp.write(png)
                    except Exception as exc:  # noqa: BLE001
                        print(f"[警告] 图表PNG生成失败(chart_{chart_no}): {exc}", file=sys.stderr)
                    chart_parts.append(f"<!--CHART:{chart_no}-->")
                    chart_parts.append(
                        f"<div style='font-weight:600;color:#444;margin:8px 0 2px'>{title}</div>")
                    chart_parts.append(_metric_svg(metric, series, g, colors, idx))
                    chart_parts.append(_legend_html(g, colors, idx))   # 该图自己的小图例
                    chart_parts.append("<!--/CHART-->")
                    gi += 1
                    chart_no += 1
            note_lines = [
                "覆盖: " + "、".join(f"{p['label']}({p['cov'] or '?'})" for p in series) + "；x 轴下方数字 = 该日池内板块数。",
                f"筛选: 退出的不画；窗口内无连续≥{CHART_MIN_RUN}个交易日在池的(零散孤点/频繁进出)也不画；",
                f"整体近乎横盘(趋势起伏<{CHART_MIN_TREND_CHG} 且 动量起伏<{CHART_MIN_MOM_CHG})的也不画。",
                "分组: 趋势分、动量分各自成图，组内按该指标窗口内 首日→末日 净变化 分“整体上升 / 整体下降”。",
                f"口径: 每日取当天自己的入池板块(原得分前{args.top} ∪ 动量入池分前{args.top})；每个板块一条连续线——某日不在池(出池/未入池)时以该板块窗口内最低分代替该点(空心圈标注)。",
            ]
            if any(p.get("cov") == "仅沪市" for p in series):
                note_lines.append("注意: 标“仅沪市”的日期为行情状态码修复前的扫描产物，仅沪市口径，与“沪深北”日期不可直接比绝对值。")
            chart_parts.append("<p class='note'>" + "<br>".join(note_lines) + "</p>")

    rank_note = ("<p class='note'>表后注(今日板块排名算法)：<br>"
                 "① 个股分=上涨趋势8个条件(多头排列/站上年线/MA20上行/低点抬高/高点抬高/斜率向上/"
                 "近60日新高/放量)中满足的个数，满足1个记1分，≥5分命中；历史K线不足只评得部分条件时按比例折算到 /8(如 5/7→6/8)。<br>"
                 "② 行业加权：8/8→8分、7/8→6分、6/8→4分；6分以下(如5/8)命中但计0分。<br>"
                 "③ 行业得分=该行业成员加权分之和；趋势分=得分/计入股票数。<br>"
                 "④ 动量分(±10)与动量入池分同源：取板块全部成分股按 m=当日%×50%+近2日%×30%+近3日%×20%(加强当日)降序前10"
                 "(不足按实际只数)，S=Σ前10的m；动量入池分=S；展示动量分=S÷(1.4×动量股数) 归一(允许为负, ±10封顶)。<br>"
                 f"⑤ 总分 = 趋势分×50% + 动量分×50%；入池=原行业得分前{args.top}名 ∪ 动量入池分前{args.top}名(并集, 最多{2 * args.top}个)，"
                 "池内按总分降序排名；入池列: 趋势=按得分入池、动量=按动量入池分入池、趋势+动量=双口径都占。<br>"
                 "⑥ 代表股=趋势前5(加权分最高, 记X/8,+近20日涨幅) + ◎动量前7(板块全部成分股按m最强, 橙色◎=短线动量, 括号=当日涨幅); ◆紫=相比前日新进入动量前7。<br>"
                 "⑦ 数量因子f: 从0只起按0.003/只(=0.06/20)连续线性递减(如20只≈0.94、120只=0.64)，n≥120封底0.64不再减；趋势分、动量分(±10)、动量入池分及入池资格(得分/动量两条腿)均乘f。</p>")
    if rk is not None:
        parts.append(f"<h3>今日板块排名(趋势分50% + 动量分50% → 入池=原得分前{args.top} ∪ 动量前{args.top} → 总分)</h3>")
        parts.append(_html_table(rk.rename(columns={"industry": "行业"})))
        parts.append(rank_note)
    delta_note = (f"<p class='note'>表后注(排名升降算法)：对前一交易日与今日各自按上方A_rank"
                  f"(入池=原得分前{args.top} ∪ 动量入池分前{args.top}、池内按总分=趋势分×50%+动量分×50% 降序)计算后对比。"
                  "状态：池内=两日均在池内；新进池=今日新进(前日不在池)；完全新进池=前5个交易日均未入池、今日首次入池(紫)；退出池=今日掉出池。"
                  "本表全量列出(排序: 完全新进池 → 新进池 → 池内按动量分变化降序[提高多→没变→降低少→降低多] → 退出池; 组内按当日动量分降序)。"
                  "涨红跌绿：排名变化=前日排名−今日排名(正=名次上升)；"
                  "趋势分变化/动量分变化/总分变化=今日−前日(正=升，负=降)。<br>"
                  "评价：池内按 趋势分变化(正=趋势增强/负=趋势减弱) + 动量分变化(≥1.5动量爆发 / 0~1.5动量增强 / -1.5~0动量减弱 / <-1.5动量大幅下滑)；"
                  "新进/退出按该板块当日趋势分、动量分在今日池内百分位：趋势 前10%很强 / 动量 前10%爆发；10-30%强 / 30-70%一般 / 70-100%弱。</p>")
    if delta is not None:
        parts.append("<h3>与前一交易日排名升降 (排名变化: 正=名次上升)</h3>")
        d = delta.copy()
        for c in ["前日排名", "今日排名", "排名变化"]:
            if c in d.columns:
                d[c] = d[c].where(d[c].notna(), "-")
        parts.append(_html_table(d))
        parts.append(delta_note)
    if chart_parts:
        parts.append("<hr style='border:none;border-top:1px solid #e3e9f2;margin:24px 0'/>")
        parts += chart_parts
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
                     "趋势分=得分/股票数; ④动量分(±10)与动量入池分同源: 板块全部成分股按 m=当日%×50%+近2日%×30%+近3日%×20% 降序前10"
                     "(不足按实际只数)S=Σ, 动量入池分=S, 展示动量分=S/(1.4×动量股数)(允许为负, ±10封顶); "
                     f"⑤总分=趋势分*50%+动量分*50%, 入池=原得分前{args.top} ∪ 动量入池分前{args.top}(并集, 最多{2 * args.top}), 池内按总分降序; "
                     "入池列: 趋势=得分入池, 动量=动量入池, 趋势+动量=双口径; "
                     "⑥代表股=趋势前5(加权分, X/8,+近20日%) + ◎动量前7(板块全部成分股按m最强, 括号=当日涨幅; ◆=相比前日新进入)")
    if delta is not None:
        lines.append("")
        lines.append("== 与前一日排名升降 ==")
        lines.append(_txt_table(delta))
        lines.append(f"算法: 前一日与今日各自按上方A_rank(入池=原得分前{args.top} ∪ 动量入池分前{args.top}, 池内按总分=趋势分*50%+动量分*50%降序)后对比; "
                     "本表排序: 完全新进池 → 新进池 → 池内(按动量分变化降序: 提高多→没变→降低少→降低多) → 退出池(组内按当日动量分降序); "
                     "状态: 池内=两日均在池内, 新进池=今日新进(蓝), 完全新进池=前5个交易日均未入池今日首次入池(紫), 退出池=今日掉出(灰); "
                     "涨红跌绿: 排名变化=前日排名-今日排名(正=名次上升); 趋势分变化/动量分变化/总分变化=今日-前日; "
                     "评价: 池内按趋势分变化(正=增强/负=减弱)+动量分变化(>=1.5爆发/0~1.5增强/-1.5~0减弱/<-1.5大幅下滑); "
                     "新进/退出按当日趋势分、动量分在今日池内百分位(趋势前10%很强/动量前10%爆发/10-30%强/30-70%一般/70-100%弱)")
    if series:
        lines.append("")
        lines.append("== 附: 板块近%d日走势 (今日池中走势明显的板块; 不在池日为 -) ==" % len(series))
        if order:
            lines += _view_txt(order, series)
        else:
            lines.append("(今日池板块窗口内无满足条件的走势)")
        if any(p.get("cov") == "仅沪市" for p in series):
            lines.append("注: 标'仅沪市'的日期为修复前扫描产物, 仅沪市口径, 与'沪深北'日期不可直接比绝对值。")
    with open(base + ".txt", "w", encoding="utf-8") as f:
        f.write("\n".join(lines))

    print(f"已生成: {base}.html")
    print(f"已生成: {base}.txt")


if __name__ == "__main__":
    main()
