#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
美股板块 A_rank HTML/TXT 日报 —— 版式与 A股日报(a_rank_report) 对齐

读 output/us_rank_{YYYYMMDD}.csv(入池排名) + us_delta_{YYYYMMDD}.csv(升降,可选)
多日(us_rank_*.csv)自动生成 4 张走势图(动量↑/↓、趋势↑/↓), 与 A股一致放报告末尾。
用法: python make_us_rank_report.py [YYYYMMDD] [--top 15]
"""
from __future__ import annotations

import argparse
import glob
import html
import os

import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
OUT = os.path.join(ROOT, "output")

UP = "#d93025"
DOWN = "#188038"
NEW_BLUE = "#1a73e8"
NEW_PURPLE = "#7b1fa2"
MOM_COL = "#e65100"
NAVY = "#2b579a"
NAVY_D = "#1d3f6e"
ROW_BD = "#e3e9f2"


def _esc(s) -> str:
    return html.escape(str(s))


def _fmt(v) -> str:
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return "-"
    if isinstance(v, float):
        return "-" if v != v else (f"{v:g}" if v == int(v) else f"{v:.2f}")
    return str(v)


def _color(c: str, v):
    if c in ("排名变化", "趋势分变化", "动量分变化", "总分变化"):
        try:
            val = float(v)
        except Exception:  # noqa: BLE001
            return "#999"
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


# ---------------- 评价(与A股一致) ----------------
def _num(x):
    """转数值; "-"/None/NaN -> NaN。"""
    try:
        v = float(x)
    except Exception:  # noqa: BLE001
        return float("nan")
    return v


def _trend_word(chg):
    chg = _num(chg)
    if pd.isna(chg):
        return ""
    if chg > 0:
        return "趋势增强"
    if chg < 0:
        return "趋势减弱"
    return "趋势平稳"


def _mom_word(chg):
    chg = _num(chg)
    if pd.isna(chg):
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


def _pct_level(val, refs, top):
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


def _eval_span(text: str) -> str:
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


def _add_eval(delta: pd.DataFrame, rank: pd.DataFrame) -> pd.DataFrame:
    d = delta.copy()
    refs = {str(r["industry"]).strip(): r for _, r in rank.iterrows()}
    tr_refs = [float(r["趋势分"]) for r in refs.values()]
    mo_refs = [float(r["动量分"]) for r in refs.values()]
    evals = []
    for _, r in d.iterrows():
        st = r.get("状态")
        if st == "池内":
            ev = "、".join(x for x in (_trend_word(r.get("趋势分变化")),
                                       _mom_word(r.get("动量分变化"))) if x)
            evals.append(ev or "平稳")
            continue
        ind = str(r["行业"]).strip()
        rec = refs.get(ind)
        if rec is None:
            evals.append("趋势弱、动量弱")
            continue
        tv = float(rec["趋势分"])
        mv = float(rec["动量分"])
        evals.append("趋势" + _pct_level(tv, tr_refs, "很强")
                     + "、动量" + _pct_level(mv, mo_refs, "爆发"))
    d["评价"] = evals
    return d


# ---------------- 表 ----------------


def _html_table(df: pd.DataFrame) -> str:
    cols = list(df.columns)
    h = ('<table style="border-collapse:collapse;font-size:14px;font-family:Segoe UI,'
         'Microsoft YaHei,sans-serif;box-shadow:0 1px 3px rgba(0,0,0,.12);border-radius:6px;overflow:hidden;width:100%">')
    h += ('<thead><tr style="background:#2b579a;color:#fff">'
          + "".join(f'<th style="padding:9px 14px;border:1px solid {NAVY_D}">{c}</th>' for c in cols)
          + "</tr></thead>")
    for i, (_, r) in enumerate(df.iterrows()):
        bg = "#ffffff" if i % 2 == 0 else "#f2f6fc"
        h += f'<tr style="background:{bg}">'
        for c, v in zip(cols, r):
            if c == "代表股":
                items = [x for x in str(v).split("、") if x]
                n = len(items)
                base, rem = divmod(n, 3)
                parts, start = [], 0
                for k in range(3):
                    s = base + (1 if k < rem else 0)
                    if s:
                        seg = []
                        for it in items[start:start + s]:
                            if it.startswith("◎"):
                                seg.append(f'<span style="color:{MOM_COL};font-weight:600">◎{_esc(it[1:])}</span>')
                            else:
                                seg.append(f"<span>{_esc(it)}</span>")
                        parts.append("、".join(seg))
                        start += s
                h += (f'<td style="padding:6px 14px;border:1px solid {ROW_BD};'
                      f'font-size:12px;color:#444;line-height:1.7">{"<br>".join(parts)}</td>')
                continue
            if c == "评价":
                txt = _fmt(v)
                h += (f'<td style="padding:7px 14px;border:1px solid {ROW_BD}">'
                      f'{_eval_span(txt) if txt not in ("", "-") else txt}</td>')
                continue
            col = _color(c, v)
            colst = f';color:{col};font-weight:bold' if col else ""
            h += (f'<td style="padding:7px 14px;border:1px solid {ROW_BD}{colst}">'
                  f"{_fmt(v)}</td>")
        h += "</tr>"
    return h + "</table>"


def _txt_table(df: pd.DataFrame) -> str:
    rows = [[str(_fmt(v)) for v in r] for r in df.itertuples(index=False)]
    cols = list(df.columns)
    widths = [max([len(cols[i])] + [len(r[i]) for r in rows]) for i in range(len(cols))]

    def line(cells):
        return "  ".join(c.ljust(widths[i]) for i, c in enumerate(cells))
    return "\n".join([line(cols), "-" * (sum(widths) + 2 * (len(cols) - 1))] + [line(r) for r in rows])


# ---------------- 走势图(多日; 与A股同构, 放报告末尾) ----------------
CHART_DAYS = 5            # 走势图只画最近 5 个交易日
CHART_MIN_POOL_DAYS = 3   # 板块须 5 日中有 ≥3 日在池(非退出池/未入池)才画走势


def _load_series(tag: str, days: int = CHART_DAYS) -> list[dict]:
    """从 us_rank_*.csv 取 <= tag 的最近 days 天: 每天 入池/分数。"""
    cand = []
    for fn in os.listdir(OUT):
        if not (fn.startswith("us_rank_") and fn.endswith(".csv")):
            continue
        core = fn[len("us_rank_"):-4]
        if core.isdigit() and core <= tag:
            cand.append(core)
    cand = sorted(set(cand))[-days:]
    series = []
    for core in cand:
        p = os.path.join(OUT, f"us_rank_{core}.csv")
        try:
            df = pd.read_csv(p, encoding="utf-8-sig")
            if df.empty:
                continue
            series.append({
                "label": f"{core[4:6]}-{core[6:]}",
                "pool": [str(x) for x in df["industry"]],
                "scores": {str(r["industry"]): {"trend": float(r["趋势分"]),
                                                "mom": float(r["动量分"])}
                           for _, r in df.iterrows()},
            })
        except Exception:  # noqa: BLE001
            pass
    return series


def _chart_svg(metric: str, series: list[dict], order: list[str], colors: dict) -> str:
    n = len(series)
    if n < 2:
        return ""
    W, H, pl, pr, pt, pb = 900, 300, 52, 18, 30, 40
    vals = [p["scores"][ind][metric] for p in series for ind in order
            if ind in p["scores"]]
    if not vals:
        return ""
    lo, hi = min(vals), max(vals)
    if hi <= lo:
        hi = lo + 1
    pad = (hi - lo) * 0.12
    lo, hi = lo - pad, hi + pad

    def X(i):
        return pl + (W - pl - pr) * (i / (n - 1) if n > 1 else 0)

    def Y(v):
        return pt + (H - pt - pb) - (v - lo) / (hi - lo) * (H - pt - pb)

    span = hi - lo
    out = ""
    for k in range(5):
        v = lo + span * k / 4
        y = Y(v)
        out += (f'<line x1="{pl}" y1="{y:.1f}" x2="{W - pr}" y2="{y:.1f}" stroke="#ececec"/>'
                f'<text x="{pl - 7}" y="{y + 4:.1f}" text-anchor="end" font-size="11" fill="#888">'
                f'{v:.1f}</text>')
    for i, p in enumerate(series):
        x = X(i)
        out += (f'<text x="{x:.1f}" y="{H - 14}" text-anchor="middle" font-size="12" fill="#444">'
                f'{p["label"]}</text>')
    for ind in order:
        c = colors[ind]
        pts = []
        mn = None
        for p in series:
            s = p["scores"].get(ind)
            if s is not None:
                mn = s[metric] if mn is None else min(mn, s[metric])
            pts.append(s[metric] if s is not None else None)
        real = [v for v in pts if v is not None]
        if not real:
            continue
        mmin = mn if mn is not None else 0.0
        ys = [v if v is not None else mmin for v in pts]
        d = "M " + " L ".join(f"{X(i):.1f} {Y(ys[i]):.1f}" for i in range(n))
        out += f'<path d="{d}" fill="none" stroke="{c}" stroke-width="2.4" stroke-linejoin="round"/>'
        for i, v in enumerate(pts):
            x, y = X(i), Y(ys[i])
            if v is not None:
                out += f'<circle cx="{x:.1f}" cy="{y:.1f}" r="2.8" fill="{c}"/>'
            else:
                out += (f'<circle cx="{x:.1f}" cy="{y:.1f}" r="2.8" fill="#fff" stroke="{c}" '
                        f'stroke-width="1.6"/>')
    return (f'<svg viewBox="0 0 {W} {H}" style="width:100%;max-width:900px" '
            f'xmlns="http://www.w3.org/2000/svg"><rect width="{W}" height="{H}" fill="#fff"/>'
            f'{out}</svg>')


def _chart_png_bytes(metric: str, series: list[dict], order: list[str],
                     colors: dict, title: str) -> bytes:
    """走势图渲染成 PNG bytes(含中文标题/图例), 供邮件内嵌(QQ/163 不渲染内嵌SVG)。"""
    import io
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt  # noqa: PLC0415
    plt.rcParams["font.sans-serif"] = [
        "Noto Sans CJK JP", "Noto Sans CJK KR", "Noto Sans CJK HK",
        "Noto Sans CJK SC", "WenQuanYi Zen Hei", "Droid Sans Fallback"]
    plt.rcParams["axes.unicode_minus"] = False
    ndays = len(series)
    labels = [p["label"] for p in series]
    drawn = set(order)
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
    for ind in order:
        color = colors[ind]
        vals = [p["scores"].get(ind) for p in series]
        real = [v[metric] for v in vals if v is not None]
        if not real:
            continue
        mn = min(real)
        ys = [v[metric] if v is not None else mn for v in vals]  # 出池日以最低分代替
        xs = list(range(ndays))
        h, = ax.plot(xs, ys, color=color, linewidth=2.2)
        handles.append(h)
        names.append(ind)
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


def _chart_legend(order: list[str], colors: dict) -> str:
    items = []
    for ind in order:
        items.append(f'<div style="display:flex;align-items:center;white-space:nowrap;gap:6px">'
                     f'<svg width="26" height="8"><line x1="0" y1="4" x2="26" y2="4" '
                     f'stroke="{colors[ind]}" stroke-width="3"/></svg>{_esc(ind)}</div>')
    return ('<div style="display:grid;grid-template-columns:repeat(auto-fill,minmax(170px,1fr));'
            f'gap:2px 10px;font-size:11px;color:#444;margin:4px 0 2px">{"".join(items)}</div>')


def _direction_groups(series: list[dict], order: list[str], metric: str):
    up, down = [], []
    for ind in order:
        pts = [p["scores"][ind][metric] for p in series if ind in p["scores"]]
        net = pts[-1] - pts[0] if len(pts) >= 2 else 0.0
        (up if net >= 0 else down).append(ind)
    return up, down


# ---------------- main ----------------


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("day", nargs="?", default="")
    p.add_argument("--top", type=int, default=15)
    a = p.parse_args()
    tag = a.day or os.path.basename(sorted(glob.glob(os.path.join(OUT, "us_rank_*.csv")))[-1]) \
        .replace("us_rank_", "").replace(".csv", "")
    rank = pd.read_csv(os.path.join(OUT, f"us_rank_{tag}.csv"), encoding="utf-8-sig")
    disp = f"2026-{tag[4:6]}-{tag[6:]}"
    top = a.top
    title = f"美股板块 A_rank 日报 · {disp}"

    rk = rank.drop(columns=["得分", "股票数", "动量入池分"], errors="ignore")
    rk = rk.rename(columns={"industry": "行业"})

    delta_html = ""
    delta_txt_lines: list[str] = []
    delta_note = ""
    series = _load_series(tag)
    dpath = os.path.join(OUT, f"us_delta_{tag}.csv")
    if os.path.exists(dpath):
        d = pd.read_csv(dpath, encoding="utf-8-sig")
        for c in ["前日趋势分", "今日趋势分", "趋势分变化", "前日动量分", "今日动量分",
                  "动量分变化", "前日总分", "今日总分", "总分变化"]:
            if c in d.columns:
                d[c] = d[c].apply(lambda v: "-" if pd.isna(v) else f"{float(v):.2f}")
        if "排名变化" in d.columns and "状态" in d.columns:
            rc = pd.to_numeric(d["排名变化"], errors="coerce").fillna(0)
            d = d[(d["状态"] != "池内") | (rc != 0)].reset_index(drop=True)
            for c in ["排名变化", "趋势分变化", "动量分变化", "总分变化"]:
                if c in d.columns:
                    d.loc[d["状态"] != "池内", c] = "-"
            if series:
                prev_pool = {str(i).strip() for sd in series[:-1] for i in sd["pool"]}
                newm = (d["状态"].astype(str).eq("新进池")
                        & ~d["行业"].astype(str).str.strip().isin(prev_pool))
                d.loc[newm, "状态"] = "完全新进池"
            d = _add_eval(d, rank)
            keep = [c for c in ["行业", "状态", "排名变化", "趋势分变化",
                                "动量分变化", "总分变化", "评价"] if c in d.columns]
            d = d[keep]
            mom_today = {str(r["industry"]).strip(): float(r["动量分"]) for _, r in rank.iterrows()} \
                if "动量分" in rank else {}
            _gm = {"完全新进池": 0, "新进池": 0, "池内": 1, "退出池": 2}
            d["_g"] = d["状态"].map(lambda s: _gm.get(s, 9))
            d["_mc"] = d.apply(
                lambda r: (pd.to_numeric(r["动量分变化"], errors="coerce")
                           if r["状态"] == "池内" else mom_today.get(str(r["行业"]).strip(), -999.0)),
                axis=1)
            d["_mc"] = pd.to_numeric(d["_mc"], errors="coerce").fillna(-999.0)
            d = (d.sort_values(["_g", "_mc"], ascending=[True, False], kind="stable")
                   .drop(columns=["_g", "_mc"]).reset_index(drop=True))
        if not d.empty:
            delta_html = ("<h3>与前一交易日排名升降 (排名变化: 正=名次上升)</h3>" + _html_table(d))
            delta_txt_lines = ["", "== 与前一交易日排名升降 ==", _txt_table(d)]
            delta_note = ("<p class='note' style='color:#555;background:#f5f7fa;border-radius:8px;"
                          "padding:10px 14px;font-size:12px;line-height:1.8'>表后注(排名升降算法)：对前一交易日与今日各自按上方A_rank"
                          f"(入池=原得分前{top} ∪ 动量入池分前{top}、池内按总分=趋势分×50%+动量分×50% 降序)计算后对比。"
                          "状态：池内=两日均在池内；新进池=今日新进(前日不在池)；完全新进池=此前几日均未入池、今日首次入池(紫)；退出池=今日掉出池。"
                          "涨红跌绿：排名变化=前日排名−今日排名(正=名次上升)；"
                          "趋势分变化/动量分变化/总分变化=今日−前日(正=升，负=降)。<br>"
                          "评价：池内按 趋势分变化(正=趋势增强/负=趋势减弱) + 动量分变化(≥1.5动量爆发 / 0~1.5动量增强 / -1.5~0动量减弱 / <-1.5动量大幅下滑)；"
                          "新进/退出按该板块当日趋势分、动量分在今日池内百分位：趋势 前10%很强 / 动量 前10%爆发；10-30%强 / 30-70%一般 / 70-100%弱。</p>")

    chart_parts: list[str] = []
    today_pool = [str(x) for x in rank["industry"]]
    # 只画近5日中≥3日在池(非退出池/未入池)的板块; 临时进池的板块不画走势
    today_pool = [x for x in today_pool
                  if sum(1 for p in series if x in p["pool"]) >= CHART_MIN_POOL_DAYS]
    if len(series) >= 2 and today_pool:
        import colorsys
        colors = {}
        for i, ind in enumerate(today_pool):
            hue = (i * 360.0 / len(today_pool)) / 360.0
            r, g, b = colorsys.hls_to_rgb(hue, 0.62, 0.30 + 0.16 * (i % 2))
            colors[ind] = "#%02x%02x%02x" % (round(r * 255), round(g * 255), round(b * 255))
        seq = ["①", "②", "③", "④"]
        gi = 0
        chart_no = 0
        png_dir = os.path.join(OUT, f"us_rank_report_{tag}_charts")
        for metric, mname in (("mom", "动量分走势"), ("trend", "趋势分走势")):
            up, down = _direction_groups(series, today_pool, metric)
            for tag2, g in (("整体上升", up), ("整体下降", down)):
                if not g:
                    continue
                title = f"{seq[gi]} {mname} · {tag2} {len(g)}条"
                # 邮件可见性: QQ/163 不渲染内嵌SVG, 故每张图同时导出 PNG(chart_N.png)
                try:
                    os.makedirs(png_dir, exist_ok=True)
                    png = _chart_png_bytes(metric, series, g, colors, title)
                    if png:
                        with open(os.path.join(png_dir, f"chart_{chart_no}.png"), "wb") as fp:
                            fp.write(png)
                except Exception as exc:  # noqa: BLE001
                    print(f"[警告] 图表PNG生成失败(chart_{chart_no}): {exc}", file=sys.stderr)
                chart_parts.append(f"<!--CHART:{chart_no}-->")
                chart_parts.append(f"<div style='font-weight:600;color:#444;margin:14px 0 4px'>{title}</div>")
                chart_parts.append(_chart_svg(metric, series, g, colors))
                chart_parts.append(_chart_legend(g, colors))
                chart_parts.append("<!--/CHART-->")
                gi += 1
                chart_no += 1
    if chart_parts:
        chart_parts.insert(0, "<h3>板块近{}个交易日走势 — 今日池板块 (仅选5日中≥{}日在池者; 空心圈=出池/未入池以最低分代替)</h3>".format(len(series), CHART_MIN_POOL_DAYS))

    parts = [f"<h2>{title}</h2>"]
    parts.append(f"<h3>今日板块排名(趋势分50% + 动量分50% → 入池=原得分前{top} ∪ 动量前{top} → 总分)</h3>")
    parts.append(_html_table(rk))
    rank_note = ("<p class='note' style='color:#555;background:#f5f7fa;border-radius:8px;padding:10px 14px;font-size:12px;line-height:1.8'>"
                 "表后注(今日板块排名算法)：<br>"
                 "① 个股上涨趋势8条件(多头排列/站上年线MA200/MA20上行/低点抬高/高点抬高/斜率向上/近60日新高/放量)满足数≥5命中；"
                 "历史K线不足只评得部分条件时按比例折算到 /8(如 5/7→6/8)。<br>"
                 "② 行业加权：8/8→8分、7/8→6分、6/8→4分；6分以下(如5/8)命中但计0分。<br>"
                 "③ 行业得分=该行业成员加权分之和；趋势分=得分/计入股票数。<br>"
                 "④ 动量分(±10)与动量入池分同源：取板块全部成分股按 m=当日%+近2日%+近3日%(三段叠加)降序前10(不足按实际只数)，S=Σ前10的m；"
                 "动量入池分=S；展示动量分=S÷(5×只数)=前n只平均涨幅÷5 归一(允许为负, ±10封顶)。<br>"
                 f"⑤ 总分=趋势分×50%+动量分×50%；入池=原行业得分前{top} ∪ 动量入池分前{top}(并集, 最多{2 * top})，池内按总分降序排名；"
                 "入池列: 趋势=按得分入池、动量=按动量入池分入池、趋势+动量=双口径都占。<br>"
                 "⑥ 代表股=趋势前5(加权分最高, 记X/8,+近20日%) + ◎动量前5(板块全部成分股按m最强, 橙色◎=短线动量, 括号=当日%)。<br>"
                 "⑦ 板块=美股细分行业(英文industry经内置翻译, 少数未收录保留英文); 全市场口径(市值>5亿美元、剔除空壳)。<br>"
                 "⑧ 数量因子f: 从0只起按0.06/20/只(=0.003/只)连续线性递减(如20只≈0.94、120只=0.64)，n≥120封底0.64不再减；趋势分、动量分(±10)、动量入池分及入池资格(得分/动量两条腿)均乘f。</p>")
    parts.append(rank_note)
    if delta_html:
        parts.append(delta_html)
        parts.append(delta_note)
    if chart_parts:
        parts.append("<hr style='border:none;border-top:1px solid #e3e9f2;margin:24px 0'/>")
        parts += chart_parts
    elif len(series) < 2:
        parts.append("<p class='note' style='color:#999;font-size:12px'>板块走势图需积累≥2个交易日(明天起自动显示, 与A股日报同构)。</p>")
    parts.append("<p class='note' style='color:#999;font-size:12px'>AI生成，仅供研究，不构成投资建议</p>")

    doc = ("<!DOCTYPE html><html><head><meta charset='utf-8'><title>%s</title><style>"
           "body{font-family:Segoe UI,'Microsoft YaHei','Noto Sans CJK JP',sans-serif;color:#222;padding:16px}"
           "h2{color:#2b579a}h3{color:#444}table{width:100%%}"
           "</style></head><body>%s</body></html>" % (title, "".join(parts)))
    html_path = os.path.join(OUT, f"us_rank_report_{tag}.html")
    with open(html_path, "w", encoding="utf-8") as f:
        f.write(doc)

    lines = [title, ""]
    lines.append("== 今日板块排名 ==")
    lines.append(_txt_table(rk))
    lines += delta_txt_lines
    txt_path = os.path.join(OUT, f"us_rank_report_{tag}.txt")
    with open(txt_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    print("已生成:", html_path, "/", txt_path)


if __name__ == "__main__":
    main()
