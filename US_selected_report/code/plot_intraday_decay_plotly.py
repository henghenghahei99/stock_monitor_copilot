#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
美股自选 · 分时MACD「短线上涨衰减」交互式命中图 (Plotly 版)

与 plot_intraday_decay.py 完全同源(同一套离线缓存 + 同一套判定), 区别只在画法:
  - K线蜡烛图(涨红跌绿) 而不是两条折线
  - 下方 MACD 面板带柱状图 (DIF/DEA 线 + 柱)
  - 可缩放 / 拖动 / 悬停看每根 K 线的开高低收与 DIF/DEA
  - 全部命中合成**一个** HTML, 附带本地 plotly.min.js, 断网也能打开

只用本地缓存, 不发网络请求, 不消耗 Twelve Data 额度。

用法:
  python code/plot_intraday_decay_plotly.py
  python code/plot_intraday_decay_plotly.py --stocks CENX,LOGI
  python code/plot_intraday_decay_plotly.py --periods 60分钟,120分钟 --max 5
产物: output/us_intraday_decay_plotly_YYYYMMDD.html (+ output/plotly.min.js)
"""
from __future__ import annotations

import argparse
import html
import os
import sys
from datetime import datetime

import pandas as pd
import plotly.graph_objects as go
import plotly.io as pio
from plotly.subplots import make_subplots

CODE_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(CODE_DIR)
sys.path.insert(0, CODE_DIR)
sys.path.insert(0, os.path.join(os.path.dirname(ROOT), "A_rank_report", "code"))

import monitor_us_intraday_decay as M  # noqa: E402
import monitor_us_selected as mus  # noqa: E402
import plot_intraday_decay as P  # noqa: E402

OUT_DIR = os.path.join(ROOT, "output")

UP_C = "#d93025"      # 涨 / 最近新高
DN_C = "#0b8043"      # 跌 / 金叉
PV_C = "#1a73e8"      # 上个新高(蓝)
DIF_C = "#7b1fa2"
DEA_C = "#e65100"
FONT = "Microsoft YaHei, SimHei, Noto Sans CJK JP, sans-serif"


# ---------------------------------------------------------------- 找命中(与 png 版同源)
def gather_hits(a) -> list[dict]:
    windows = [int(x) for x in a.windows.split(",") if x.strip().isdigit()] or M.WINDOWS
    periods = M.PERIODS_TD
    if a.periods:
        want = {x.strip() for x in a.periods.split(",") if x.strip()}
        periods = [p for p in periods if p[0] in want]
    only = {x.strip().upper() for x in a.stocks.split(",") if x.strip()}

    watch = mus.load_watch()
    if only:
        watch = [p for p in watch if p["ticker"].upper() in only]

    hits: list[dict] = []
    n_cache = 0
    for p in watch:
        for label, src, param in periods:
            df = P.cached_df(p, label, src, param)
            if df is None or df.empty:
                continue
            if label != "日线":
                n_cache += 1
            for r in M.scan_period(df, label, "both", not a.no_cross, a.min_gold_bars):
                if r["window"] not in windows:
                    continue
                r.update({"ticker": p["ticker"], "code": p["code"], "name": p.get("name", "")})
                hits.append(r)
    hits.sort(key=lambda r: (r["ticker"], r["period"], r["window"]))
    print(f"[信息] 有缓存的分时序列 {n_cache} 条 | 命中 {len(hits)} 条")
    return hits, watch, periods


# ---------------------------------------------------------------- 单条命中 -> Figure
def build_fig(df: pd.DataFrame, r: dict, ticker: str, name: str,
              pad: int = 16, title_prefix: str = "") -> go.Figure | None:
    try:
        i_cur = df.index.get_loc(pd.Timestamp(r["cur_time"]))
        i_prv = df.index.get_loc(pd.Timestamp(r["prv_time"]))
    except KeyError:
        return None

    d = M.macd(df)
    lo = max(0, min(i_cur, i_prv) - pad)
    hi = min(len(d), i_cur + pad + 1)
    seg = d.iloc[lo:hi]
    xs = [f"{t:%m-%d %H:%M}" for t in seg.index]
    dif = seg["dif"].to_numpy()
    dea = seg["dea"].to_numpy()
    hist = dif - dea

    # 蜡烛图需要真实 OHLC; 腾讯日线缓存只有收盘价 -> 退化为折线
    ohlc_ok = bool((seg[["open", "high", "low", "close"]].nunique(axis=1) > 1).any())

    fig = make_subplots(rows=2, cols=1, shared_xaxes=True,
                        vertical_spacing=0.045, row_heights=[0.63, 0.37],
                        subplot_titles=("", ""))

    # ---- 价格 ----
    if ohlc_ok:
        fig.add_trace(go.Candlestick(
            x=xs, open=seg["open"], high=seg["high"], low=seg["low"], close=seg["close"],
            name="K线", increasing_line_color=UP_C, increasing_fillcolor=UP_C,
            decreasing_line_color=DN_C, decreasing_fillcolor=DN_C,
            line_width=1, whiskerwidth=0.4, showlegend=True,
            hovertext=[f"开 {o:.4g} 高 {h:.4g} 低 {l:.4g} 收 {c:.4g}"
                       for o, h, l, c in zip(seg["open"], seg["high"], seg["low"], seg["close"])],
            hoverinfo="x+text"), row=1, col=1)
    else:
        fig.add_trace(go.Scatter(x=xs, y=seg["close"], name="收盘价", mode="lines",
                                 line=dict(color="#37474f", width=1.6)), row=1, col=1)
        fig.add_trace(go.Scatter(x=xs, y=seg["high"], name="最高价", mode="lines",
                                 line=dict(color="#90a4ae", width=1, dash="dot")), row=1, col=1)

    xc, xp = xs[i_cur - lo], xs[i_prv - lo]
    hc, hp = float(df["high"].iloc[i_cur]), float(df["high"].iloc[i_prv])

    # 两个新高处: 图上只留短价标签, 全部数值信息移到卡片头部(避免遮挡 MACD 曲线)
    fig.add_trace(go.Scatter(x=[xp], y=[hp], mode="markers+text", name="上个新高",
                             marker=dict(symbol="triangle-down", size=14, color=PV_C,
                                         line=dict(width=1, color="#fff")),
                             text=[f"{hp:g}"], textposition="bottom right",
                             textfont=dict(color=PV_C, size=11), cliponaxis=False,
                             hovertemplate="上个新高<br>%{x}<br>%{y:.4g}<extra></extra>"),
                  row=1, col=1)
    fig.add_trace(go.Scatter(x=[xc], y=[hc], mode="markers+text", name="最近新高",
                             marker=dict(symbol="triangle-up", size=15, color=UP_C,
                                         line=dict(width=1, color="#fff")),
                             text=[f"{hc:g}"], textposition="top left",
                             textfont=dict(color=UP_C, size=11, ), cliponaxis=False,
                             hovertemplate="最近新高<br>%{x}<br>%{y:.4g}<extra></extra>"),
                  row=1, col=1)
    # 背离区间底色
    fig.add_vrect(x0=xp, x1=xc, fillcolor=UP_C, opacity=0.07, line_width=0,
                  layer="below", row=1, col=1)

    # ---- MACD ----
    fig.add_trace(go.Bar(x=xs, y=hist, name="MACD柱",
                         marker=dict(color=[UP_C if v >= 0 else DN_C for v in hist],
                                     line_width=0),
                         opacity=0.55, hoverinfo="x+y"), row=2, col=1)
    fig.add_trace(go.Scatter(x=xs, y=dif, name="DIF (快线)", mode="lines",
                             line=dict(color=DIF_C, width=2)), row=2, col=1)
    fig.add_trace(go.Scatter(x=xs, y=dea, name="DEA (慢线)", mode="lines",
                             line=dict(color=DEA_C, width=1.8, dash="dash")), row=2, col=1)

    # 金叉/死叉标记(区间内); 口径用到的那一对加粗
    g_ts = pd.Timestamp(r["gold_time"]) if r.get("gold_time") else None
    d_ts = pd.Timestamp(r["dead_time"]) if r.get("dead_time") else None
    for i in range(1, len(seg)):
        is_gold = dif[i - 1] <= dea[i - 1] and dif[i] > dea[i]
        is_dead = (not is_gold) and dif[i - 1] > dea[i - 1] and dif[i] <= dea[i]
        if not (is_gold or is_dead):
            continue
        used = (seg.index[i] == (g_ts if is_gold else d_ts))
        fig.add_trace(go.Scatter(
            x=[xs[i]], y=[dif[i]], mode="markers",
            name=None if not used else ("金叉(口径所用)" if is_gold else "死叉(口径所用)"),
            showlegend=used, legendgroup="gold" if is_gold else "dead",
            marker=dict(symbol="triangle-up" if is_gold else "triangle-down",
                        size=14 if used else 9, color=DN_C if is_gold else "#5f6368",
                        line=dict(width=1.6 if used else 0.6, color="#111")),
            hovertemplate=("金叉" if is_gold else "死叉") + "<br>%{x}<br>DIF %{y:.4f}<extra></extra>"),
            row=2, col=1)

    # 两个新高处的 DEA 取值(只留菱形点, 数值在卡片头部)
    fig.add_trace(go.Scatter(x=[xp, xc], y=[float(dea[i_prv - lo]), float(dea[i_cur - lo])],
                             mode="markers", name="新高处 DEA", showlegend=False,
                             marker=dict(size=9, color=[PV_C, UP_C], symbol="diamond",
                                         line=dict(width=1.2, color="#fff")),
                             hovertemplate="DEA %{y:.4f}<extra></extra>"), row=2, col=1)
    for x_ in (xp, xc):
        fig.add_vline(x=x_, line=dict(color="#b0bec5", width=1, dash="dot"), row="all")

    # 坐标轴留出文字余量, 短标签不被裁切
    ylo = float(seg["low"].min()) if ohlc_ok else float(seg["close"].min())
    yhi = float(seg["high"].max()) if ohlc_ok else float(seg["close"].max())
    span = max(yhi - ylo, 1e-9)
    fig.update_yaxes(range=[ylo - span * 0.06, yhi + span * 0.12], row=1, col=1)
    mlo = float(min(hist.min(), dif.min(), dea.min()))
    mhi = float(max(hist.max(), dif.max(), dea.max()))
    mspan = max(mhi - mlo, 1e-12)
    fig.update_yaxes(range=[mlo - mspan * 0.08, mhi + mspan * 0.16], row=2, col=1)

    fig.update_layout(
        template="plotly_white",
        font=dict(family=FONT, size=12),
        title=dict(text=f"{title_prefix}{ticker} {name} · {r['period']} · 窗口 {r['window']} 个交易日"
                        f"　<span style='font-size:12px;color:#666'>{r['kind']}</span>",
                   x=0.01, xanchor="left", yref="container", y=0.985,
                   font=dict(size=15, color="#1a3d7c")),
        height=640, margin=dict(l=66, r=26, t=120, b=46),
        hovermode="x unified",
        hoverlabel=dict(font=dict(size=11), namelength=-1),
        legend=dict(orientation="h", yanchor="bottom", y=1.005, x=0, font=dict(size=11),
                    bgcolor="rgba(255,255,255,.9)", bordercolor="#e0e0e0", borderwidth=1),
        dragmode="pan",
    )
    fig.update_xaxes(showgrid=True, gridcolor="#eceff1", rangeslider_visible=False,
                     nticks=12, tickangle=-30)
    fig.update_yaxes(showgrid=True, gridcolor="#eceff1", title_text="价格", row=1, col=1)
    fig.update_yaxes(showgrid=True, gridcolor="#eceff1", title_text="MACD", row=2, col=1,
                     zeroline=True, zerolinecolor="#9e9e9e", zerolinewidth=1)
    return fig


# ---------------------------------------------------------------- 组装一个 HTML
def build_page(cards: list[tuple[dict, go.Figure]], stamp: str) -> str:
    parts = []
    for i, (r, fig) in enumerate(cards, 1):
        frag = pio.to_html(fig, full_html=False, include_plotlyjs=False,
                           div_id=f"hit{i}", config={"displaylogo": False,
                                                     "scrollZoom": True,
                                                     "modeBarButtonsToRemove": ["lasso2d", "select2d"]})
        kind_c = UP_C if r["kind"] == "双线衰减" else "#e65100"
        hm = lambda s: (str(s) or "")[5:16] or "-"                         # noqa: E731
        detail = (f"<span class='k'>▲ 最近新高</span> <b>{r['cur_high']:g}</b> {hm(r['cur_time'])} "
                  f"<span class='pos'>({r['high_gain%']:+.2f}%)</span>"
                  f"　<span class='k'>▼ 上个新高</span> {r['prv_high']:g} {hm(r['prv_time'])}"
                  f"　<span class='k'>DIF</span> {r['prv_dif']:+.3f} → <b>{r['dif']:+.3f}</b> "
                  f"<span class='neg'>({r['dif_gap']:+.3f})</span>"
                  f"　<span class='k'>DEA</span> {r['prv_dea']:+.3f} → <b>{r['dea']:+.3f}</b> "
                  f"<span class='neg'>({r['dea_gap']:+.3f})</span>"
                  f"　<span class='k'>序列</span> 死叉 {hm(r.get('dead_time'))} → 金叉 {hm(r.get('gold_time'))} "
                  f"→ ▲ {r.get('gold_gap', '-')} 根后")
        parts.append(f"""
<div class="card">
  <div class="hd">
    <b>#{i} {html.escape(r['ticker'])} {html.escape(r.get('name') or '')}</b>
    <span class="pill">{html.escape(r['period'])}</span>
    <span class="pill w">窗口 {r['window']} 日</span>
    <span class="pill" style="background:{kind_c}">{html.escape(r['kind'])}</span>
  </div>
  <div class="dt">{detail}</div>
  {frag}
</div>""")
    return f"""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<title>美股自选 · 分时上涨衰减 交互图 · {stamp}</title>
<script src="plotly.min.js"></script>
<style>
 body{{font-family:{FONT};margin:0;background:#f4f6fa;color:#222}}
 header{{position:sticky;top:0;z-index:9;background:#fff;border-bottom:1px solid #dfe6f0;
        padding:12px 18px;box-shadow:0 1px 6px rgba(0,0,0,.06)}}
 h1{{margin:0 0 6px;font-size:17px;color:#1a3d7c}}
 .tip{{font-size:12.5px;color:#5f6368;line-height:1.7}}
 .wrap{{padding:14px 18px 60px;max-width:1280px;margin:0 auto}}
 .card{{background:#fff;border:1px solid #e3e9f2;border-radius:8px;margin:14px 0;
        overflow:hidden;box-shadow:0 1px 3px rgba(0,0,0,.07)}}
 .hd{{padding:9px 14px;background:#f8fafd;border-bottom:1px solid #e9eef6;font-size:13.5px;
      display:flex;align-items:center;gap:8px;flex-wrap:wrap}}
 .pill{{background:#1a3d7c;color:#fff;border-radius:10px;padding:1px 9px;font-size:12px;
        font-weight:600}}
 .pill.w{{background:#5f6368}}
 .mut{{color:#5f6368}}
 .dt{{padding:8px 14px 10px;font-size:12.5px;color:#37474f;line-height:1.95;
      border-bottom:1px solid #eef2f8}}
 .dt .k{{color:#8a94a6}}
 .dt .pos{{color:{UP_C};font-weight:600}}
 .dt .neg{{color:{DN_C};font-weight:600}}
</style></head><body>
<header>
  <h1>美股自选 · 分时MACD 短线上涨衰减 · 交互图（{stamp}）</h1>
  <div class="tip">口径：▲价 &gt; ▼价、两点之间先死叉后金叉、新高在金叉之后（未再死叉）、▲距金叉 &gt;5 根、
    且 ▲ 那根的 DIF/DEA 都低于 ▼ 那根 → 短线上涨衰减。<br>
    图表**只用本地缓存离线重算**，未发任何网络请求；数据源 Twelve Data(分时) + 腾讯(日线)。
    共 {len(cards)} 条命中 —— 可滚轮缩放、拖动平移、悬停对齐看每根 K 线的数值。</div>
</header>
<div class="wrap">{''.join(parts)}</div>
</body></html>"""


def main() -> None:
    ap = argparse.ArgumentParser(description="分时上涨衰减交互图 (Plotly, 离线)")
    ap.add_argument("--stocks", default="", help="只画这些票(如 CENX,LOGI)")
    ap.add_argument("--windows", default="", help="只看这些窗口(如 1,2,3)")
    ap.add_argument("--periods", default="", help="只看这些分时K(如 60分钟,120分钟)")
    ap.add_argument("--max", type=int, default=0, help="最多画多少条(0=全部)")
    ap.add_argument("--pad", type=int, default=16, help="两端各多留多少根K线(默认16)")
    ap.add_argument("--out", default="", help="输出 HTML 路径")
    ap.add_argument("--no-cross", action="store_true", help="关闭「先死叉后金叉」过滤")
    ap.add_argument("--min-gold-bars", type=int, default=M.MIN_GOLD_BARS)
    a = ap.parse_args()

    hits, watch, periods = gather_hits(a)
    if a.max:
        hits = hits[:a.max]
    if not hits:
        print("[提示] 没有命中, 不生成页面")
        return

    os.makedirs(OUT_DIR, exist_ok=True)
    # 让 plotly 把 plotly.min.js 落到输出目录, 页面即可离线打开
    boot = os.path.join(OUT_DIR, "_plotly_boot.html")
    pio.write_html(go.Figure(), boot, include_plotlyjs="directory", full_html=True)
    if os.path.exists(boot):
        os.remove(boot)

    cards: list[tuple[dict, go.Figure]] = []
    cache: dict[tuple, pd.DataFrame] = {}
    for i, r in enumerate(hits, 1):
        p = next((x for x in watch if x["ticker"] == r["ticker"]), None)
        if p is None:
            continue
        src = next((s for lab, s, _ in periods if lab == r["period"]), "td")
        param = next((par for lab, _, par in periods if lab == r["period"]), "1h")
        key = (r["ticker"], r["period"])
        if key not in cache:
            dfx = P.cached_df(p, r["period"], src, param)
            if dfx is None or dfx.empty:
                continue
            cache[key] = dfx
        fig = build_fig(cache[key], r, r["ticker"], r["name"] or "", pad=a.pad)
        if fig is not None:
            cards.append((r, fig))
        if i % 10 == 0:
            print(f"   已画 {i}/{len(hits)}", file=sys.stderr, flush=True)

    stamp = datetime.now().strftime("%Y%m%d")
    out = a.out or os.path.join(OUT_DIR, f"us_intraday_decay_plotly_{stamp}.html")
    with open(out, "w", encoding="utf-8") as f:
        f.write(build_page(cards, stamp))
    print(f"[完成] 交互图 {len(cards)} 条 -> {out}")
    print(f"[完成] plotly.min.js -> {os.path.join(OUT_DIR, 'plotly.min.js')}")


if __name__ == "__main__":
    main()
