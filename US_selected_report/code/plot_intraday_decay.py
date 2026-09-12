#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
美股自选 · 分时MACD「短线上涨衰减」命中图的离线绘制

只用**本地缓存**(data/kline_cache_td/ + 腾讯日K缓存)重算, 不发任何网络请求, 不消耗 Twelve Data 额度。
每个「分时K × 窗口」命中出一条独立图, 方便逐条核对:
  上图: 价格(最高价线) + 最近新高▲ / 上个新高▼ 标记
  下图: MACD DIF/DEA + 两个新高点的取值(竖虚线对齐)
输出: output/charts_intraday/*.png + output/us_intraday_decay_charts_YYYYMMDD.html (图表索引)

用法:
  cd US_selected_report
  python code/plot_intraday_decay.py                 # 用缓存里已有的票
  python code/plot_intraday_decay.py --stocks SPCX,SUJA
  python code/plot_intraday_decay.py --windows 1,2,3
"""
from __future__ import annotations

import argparse
import html
import os
import sys
from datetime import datetime

import matplotlib
matplotlib.use("Agg")
import matplotlib.dates as mdates  # noqa: E402
import matplotlib.pyplot as plt  # noqa: E402
import pandas as pd  # noqa: E402

CODE_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(CODE_DIR)
sys.path.insert(0, CODE_DIR)
sys.path.insert(0, os.path.join(os.path.dirname(ROOT), "A_rank_report", "code"))

import monitor_us_intraday_decay as M  # noqa: E402
import monitor_us_selected as mus  # noqa: E402
import find_overbought_stocks as fos  # noqa: E402

CHART_DIR = os.path.join(ROOT, "output", "charts_intraday")

plt.rcParams["font.sans-serif"] = ["Noto Sans CJK JP", "Droid Sans Fallback", "DejaVu Sans"]
plt.rcParams["axes.unicode_minus"] = False

UP_C = "#d93025"      # 最近新高(红)
PV_C = "#1a73e8"      # 上个新高(蓝)
DIF_C = "#7b1fa2"
DEA_C = "#e65100"


def cached_df(p: dict, label: str, src: str, param) -> pd.DataFrame | None:
    """只读缓存, 缺失返回 None(绝不联网)。"""
    if src == "td":
        safe = (p.get("ticker") or "").replace("/", "_").replace(" ", "")
        cf = os.path.join(M.TD_CACHE_DIR, f"{safe}_{param}.json")
        if not os.path.exists(cf):
            return None
        try:
            import json
            with open(cf, encoding="utf-8") as f:
                rows = json.load(f)
            return M.td_df(rows) if rows else None
        except Exception:  # noqa: BLE001
            return None
    if src == "tencent":
        if not p.get("prefixed"):
            mus._fetch_best(p, 320)
        if not p.get("prefixed"):
            return None
        k = fos.tencent_kline(p["prefixed"], 320, use_cache=True)
        if k is None:
            return None
        close = k[0].dropna()
        df = pd.DataFrame({"open": close, "close": close, "high": close, "low": close})
        df["date_us"] = [str(t.date()) for t in df.index]
        return df
    return None


def plot_one(df: pd.DataFrame, r: dict, ticker: str, name: str, out_png: str) -> bool:
    """画一条命中(分时K × 窗口)。df 为该周期K线(含 date_us), r 为 scan_period 的一行。"""
    try:
        i_cur = df.index.get_loc(pd.Timestamp(r["cur_time"]))
        i_prv = df.index.get_loc(pd.Timestamp(r["prv_time"]))
    except KeyError:
        return False
    d = M.macd(df)
    lo = max(0, min(i_cur, i_prv) - 12)
    seg = d.iloc[lo:]
    x = [t.to_pydatetime() for t in seg.index]
    difv = d["dif"].to_numpy()
    deav = d["dea"].to_numpy()
    gidx = [i + 1 for i in range(lo, len(difv) - 1)
            if difv[i] <= deav[i] and difv[i + 1] > deav[i + 1]]
    didx = [i + 1 for i in range(lo, len(difv) - 1)
            if difv[i] > deav[i] and difv[i + 1] <= deav[i + 1]]
    # 口径用到的那一对(粗边高亮): 最近新高之前的最后一次金叉 + 它之前的最后一次死叉
    try:
        i_gold = df.index.get_loc(pd.Timestamp(r["gold_time"])) if r.get("gold_time") else -1
        i_dead = df.index.get_loc(pd.Timestamp(r["dead_time"])) if r.get("dead_time") else -1
    except KeyError:
        i_dead = i_gold = -1
    post_txt = (f"\n金叉后第 {i_cur - i_gold} 根" if i_gold >= 0 else "")

    fig, (ax, ax2) = plt.subplots(
        2, 1, figsize=(11.5, 6.4), sharex=True,
        gridspec_kw={"height_ratios": [2.05, 1], "hspace": 0.08})
    fig.subplots_adjust(left=0.07, right=0.985, top=0.9, bottom=0.17)

    ax.plot(x, seg["high"].to_numpy(), color="#37474f", lw=1.5, label="最高价")
    ax.plot(x, seg["close"].to_numpy(), color="#90a4ae", lw=1.0, ls="--", label="收盘价")

    tc, tp = df.index[i_cur].to_pydatetime(), df.index[i_prv].to_pydatetime()
    hc, hp = float(df["high"].iloc[i_cur]), float(df["high"].iloc[i_prv])
    ax.scatter([tp], [hp], s=105, marker="v", color=PV_C, zorder=5)
    ax.scatter([tc], [hc], s=125, marker="^", color=UP_C, zorder=5)
    ax.annotate(f"上个新高 {hp:.2f}\n{df.index[i_prv]:%m-%d %H:%M}",
                (tp, hp), textcoords="offset points", xytext=(-8, -34),
                fontsize=9.5, color=PV_C, ha="right",
                bbox=dict(boxstyle="round,pad=0.28", fc="#e8f0fe", ec=PV_C, lw=0.8))
    ax.annotate(f"最近新高 {hc:.2f}  ({r['high_gain%']:+.2f}%)\n{df.index[i_cur]:%m-%d %H:%M}"
                f"{post_txt}",
                (tc, hc), textcoords="offset points", xytext=(8, 16),
                fontsize=9.5, color=UP_C, ha="left", fontweight="bold",
                bbox=dict(boxstyle="round,pad=0.28", fc="#fdecea", ec=UP_C, lw=0.8))
    ax.axvspan(tp, tc, color=UP_C, alpha=0.05)
    for tt in (tp, tc):
        ax.axvline(tt, color="#b0bec5", lw=0.8, ls=":", zorder=1)
    ax.set_ylabel("价格 (最高价/收盘价)", fontsize=10)
    ax.grid(alpha=0.25, ls=":")
    ax.legend(loc="upper left", fontsize=9, framealpha=0.9)

    ax2.plot(x, seg["dif"].to_numpy(), color=DIF_C, lw=1.6, label="DIF (快线)")
    ax2.plot(x, seg["dea"].to_numpy(), color=DEA_C, lw=1.4, ls="--", label="DEA (慢线)")
    ax2.axhline(0, color="#9e9e9e", lw=0.9)
    ax2.scatter([tp], [float(d["dea"].iloc[i_prv])], s=42, color=PV_C, zorder=5)
    ax2.scatter([tc], [float(d["dea"].iloc[i_cur])], s=52, color=UP_C, marker="^", zorder=5)
    # 两个新高点之间实际发生的金叉/死叉(口径要求: 先死叉、后金叉、新高在金叉后)
    for i in didx:
        if i_prv < i < i_cur:
            used = (i == i_dead)
            ax2.scatter([d.index[i].to_pydatetime()], [float(difv[i])], s=95 if not used else 140,
                        marker="v", color="#5f6368", zorder=6 if not used else 7,
                        edgecolors="#fff" if not used else "#212121", linewidths=0.7 if not used else 1.4)
            ax2.annotate("死叉", (d.index[i].to_pydatetime(), float(difv[i])),
                         textcoords="offset points", xytext=(0, 14), ha="center",
                         fontsize=9, fontweight="bold", color="#5f6368")
    for i in gidx:
        if i_prv < i < i_cur:
            used = (i == i_gold)
            ax2.scatter([d.index[i].to_pydatetime()], [float(difv[i])], s=95 if not used else 140,
                        marker="^", color="#0b8043", zorder=6 if not used else 7,
                        edgecolors="#fff" if not used else "#0b8043", linewidths=0.7 if not used else 1.4)
            ax2.annotate("金叉", (d.index[i].to_pydatetime(), float(difv[i])),
                         textcoords="offset points", xytext=(0, -20), ha="center",
                         fontsize=9, fontweight="bold", color="#0b8043")
    ax2.annotate(f"DIF {r['dif']:+.3f} / DEA {r['dea']:+.3f}",
                 (tc, float(d["dea"].iloc[i_cur])), textcoords="offset points",
                 xytext=(8, 8), fontsize=9.5, color=UP_C, fontweight="bold")
    ax2.annotate(f"DIF {r['prv_dif']:+.3f} / DEA {r['prv_dea']:+.3f}",
                 (tp, float(d["dea"].iloc[i_prv])), textcoords="offset points",
                 xytext=(-8, -24), fontsize=9.5, color=PV_C, ha="right")
    for tt in (tp, tc):
        ax2.axvline(tt, color="#b0bec5", lw=0.8, ls=":", zorder=1)
    ax2.set_ylabel("MACD(12,26,9)", fontsize=10)
    ax2.grid(alpha=0.25, ls=":")
    ax2.legend(loc="upper left", fontsize=9, ncol=2, framealpha=0.9)

    gap = (f"DIF差 {r['dif_gap']:+.3f}   DEA差 {r['dea_gap']:+.3f}   [{r['kind']}]"
           f"   死叉 {str(r.get('dead_time',''))[5:16]} → 金叉 {str(r.get('gold_time',''))[5:16]}")
    fig.suptitle(f"{ticker}  {name}   ·   {r['period']}   ·   窗口 {r['window']} 个交易日   ·   {gap}",
                 fontsize=13, fontweight="bold", color="#2b579a", y=0.975)
    ax2.xaxis.set_major_locator(mdates.AutoDateLocator())
    ax2.xaxis.set_major_formatter(mdates.DateFormatter("%m-%d %H:%M"))
    plt.setp(ax2.get_xticklabels(), rotation=30, ha="right", fontsize=9)
    fig.savefig(out_png, dpi=115)
    plt.close(fig)
    return True


def main() -> None:
    ap = argparse.ArgumentParser(description="分时上涨衰减命中图(离线, 只用缓存)")
    ap.add_argument("--stocks", default="", help="只画这些票(如 SPCX,SUJA)")
    ap.add_argument("--windows", default="", help="只看这些窗口(如 1,2,3)")
    ap.add_argument("--periods", default="", help="只看这些分时K(如 60分钟,120分钟)")
    ap.add_argument("--max-series", type=int, default=0, help="最多画多少张图(0=全部)")
    ap.add_argument("--no-cross", action="store_true",
                    help="关闭「先死叉后金叉 + 新高在金叉后」过滤")
    a = ap.parse_args()

    windows = [int(x) for x in a.windows.split(",") if x.strip().isdigit()] or M.WINDOWS
    periods = M.PERIODS_TD
    if a.periods:
        want = {x.strip() for x in a.periods.split(",") if x.strip()}
        periods = [p for p in periods if p[0] in want]
    only = {x.strip().upper() for x in a.stocks.split(",") if x.strip()}

    watch = mus.load_watch()
    if only:
        watch = [p for p in watch if p["ticker"].upper() in only]

    os.makedirs(CHART_DIR, exist_ok=True)
    hits: list[dict] = []
    have_cache = 0
    for p in watch:
        for label, src, param in periods:
            df = cached_df(p, label, src, param)
            if df is None or df.empty:
                continue
            if label != "日线":
                have_cache += 1
            for r in M.scan_period(df, label, "both", not a.no_cross):
                if r["window"] not in windows:
                    continue
                r.update({"ticker": p["ticker"], "code": p["code"], "name": p.get("name", "")})
                hits.append(r)
                if a.max_series and len(hits) >= a.max_series:
                    break

    hits.sort(key=lambda r: (r["ticker"], r["period"], r["window"]))
    print(f"[信息] 有缓存的分时序列 {have_cache} 条 | 命中 {len(hits)} 条, 开始画图 ...")

    # 重画: 每条命中一张图
    cards = []
    drawn = 0
    cache: dict[tuple, pd.DataFrame] = {}
    for i, r in enumerate(hits, 1):
        p = next((x for x in watch if x["ticker"] == r["ticker"]), None)
        if p is None:
            continue
        src = next((s for lab, s, par in periods if lab == r["period"]), "td")
        param = next((par for lab, s, par in periods if lab == r["period"]), "1h")
        key = (r["ticker"], r["period"])
        if key not in cache:
            dfx = cached_df(p, r["period"], src, param)
            if dfx is None:
                continue
            cache[key] = dfx
        fn = f"{i:03d}_{r['ticker']}_{r['period']}_W{r['window']}.png".replace(" ", "")
        out = os.path.join(CHART_DIR, fn)
        if not plot_one(cache[key], r, r["ticker"], r["name"], out):
            continue
        drawn += 1
        rel = f"charts_intraday/{fn}"
        cards.append((r, rel))
        if i % 20 == 0:
            print(f"   已画 {i}/{len(hits)}", file=sys.stderr)

    # HTML 索引
    rows = []
    for idx, (r, rel) in enumerate(cards, 1):
        kind_c = "#d93025" if r["kind"] == "双线衰减" else "#e65100"
        rows.append(f"""
<div style="border:1px solid #e3e9f2;border-radius:8px;margin:16px 0;overflow:hidden;
            box-shadow:0 1px 3px rgba(0,0,0,.08)">
  <div style="padding:8px 12px;background:#f5f7fa;font-size:13px;display:flex;
              flex-wrap:wrap;gap:14px;align-items:center">
    <b style="color:#2b579a;font-size:14px">#{idx} {r['ticker']} {html.escape(r['name'] or '')}</b>
    <span style="background:#2b579a;color:#fff;padding:2px 9px;border-radius:10px;font-weight:600">{r['period']}</span>
    <span>窗口 <b>{r['window']}</b> 日</span>
    <span>新高 <b style="color:#d93025">{r['cur_high']:.2f}</b> ({r['cur_time']})
          vs 前高 <b style="color:#1a73e8">{r['prv_high']:.2f}</b> ({r['prv_time']})
          <b style="color:#d93025">{r['high_gain%']:+.2f}%</b></span>
    <span>DIF差 <b style="color:#d93025">{r['dif_gap']:+.3f}</b>
          DEA差 <b style="color:#d93025">{r['dea_gap']:+.3f}</b></span>
    <span>死叉 <b style="color:#5f6368">{str(r.get('dead_time',''))[5:16]}</b>
          → 金叉 <b style="color:#0b8043">{str(r.get('gold_time',''))[5:16]}</b>
          <span style="color:#666">（区间内共 死叉×{r.get('n_dead',0)} / 金叉×{r.get('n_gold',0)}）</span></span>
    <span style="color:{kind_c};font-weight:600">{r['kind']}</span>
  </div>
  <img src="{rel}" style="width:100%;display:block">
</div>""")

    tag = datetime.now().strftime("%Y%m%d")
    out_html = os.path.join(ROOT, "output", f"us_intraday_decay_charts_{tag}.html")
    doc = f"""<!DOCTYPE html><html><head><meta charset="utf-8">
<title>美股自选 分时上涨衰减 命中图 · {tag}</title>
<style>body{{font-family:Segoe UI,'Noto Sans CJK JP','Microsoft YaHei',sans-serif;margin:0;color:#222}}
.wrap{{padding:18px 22px;max-width:1250px;margin:0 auto}}
table{{width:100%;border-collapse:collapse;font-size:12.5px}}
th,td{{border:1px solid #dfe6f0;padding:5px 8px;text-align:left}} th{{background:#2b579a;color:#fff}}
tr:nth-child(even) td{{background:#f7f9fc}}</style></head><body><div class='wrap'>
<h2 style="color:#2b579a;margin-bottom:4px">美股自选 · 分时MACD 短线上涨衰减 · 命中图（核对用）</h2>
<div style="background:#fff8e1;border:1px solid #ffe082;border-radius:6px;padding:8px 14px;
            font-size:13px;color:#8d6e00;margin:10px 0">
口径: 最近 W 个交易日内的<b>最高价</b>那根(▲) vs 跳过最近窗口后再往前 W 个交易日内的最高价那根(▼)；
▲价 &gt; ▼价、两点之间<b>先死叉、后金叉</b>、<b>最近新高位于金叉之后（MACD 仍多头、未再死叉）</b>、
且 ▲那根的 DIF/DEA <b>都低于</b> ▼那根 → 短线上涨衰减。<br>
本页图表**只用本地缓存离线重算**，未发任何网络请求；数据源 Twelve Data(分时) + 腾讯(日线)。
共 {len(cards)} 条命中，横轴为该分时K线的时间(美东)。</div>
{'<div style="padding:10px 0;color:#888">缓存里还没有命中可画</div>' if not cards else ''}
{''.join(rows)}
</div></body></html>"""
    with open(out_html, "w", encoding="utf-8") as f:
        f.write(doc)
    print(f"[完成] 画出 {drawn} 张图 -> {CHART_DIR}")
    print(f"[完成] 索引页 -> {out_html}")


if __name__ == "__main__":
    main()
