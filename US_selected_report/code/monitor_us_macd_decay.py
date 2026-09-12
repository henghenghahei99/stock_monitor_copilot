#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
美股自选 MACD(12,26,9) 上涨衰减(顶背离) 监控

口径(用户确认, 2026-09-12):
  **股价创新高, 但 MACD 快慢线反而低于前一个高点的快慢线** —— 上涨动能衰减。

具体判定:
  1. 「新高」= 今日收盘价 > 之前 lookback(默认60) 个交易日的最高收盘。
     只报**今天正在创新高**的股票(信号是"当下发生"的, 不是历史遗留)。
  2. 「前一个高点」= 该 lookback 窗口内收盘价最高的那根(前高)。
  3. 比较今日与前高当日的 MACD:
        DIF = EMA12 - EMA26 (快线), DEA = EMA9(DIF) (慢线)
     快线、慢线**都**低于前高当日 → 上涨衰减(默认 --mode both; --mode any 只要一条低即报)。
  4. 前高必须落在 MACD 预热区之外(DIF/DEA 已稳定), 否则跳过。

用 close(收盘)而非最高价: 复用腾讯日K缓存(只提供收盘/成交量), 且 MACD 本身基于收盘。

用法:
  cd US_selected_report
  # 1) 预缓存日K(信号需要长历史, 建议 bars=320)
  python code/monitor_us_macd_decay.py --cache --bars 320 --workers 8
  # 2) 出信号
  python code/monitor_us_macd_decay.py --lookback 60 --bars 320 --workers 8
  # 3) (可选) 只看快线或慢线其一走低
  python code/monitor_us_macd_decay.py --mode any

产物: output/us_selected_macd_decay_YYYYMMDD.csv + us_macd_decay_report_YYYYMMDD.html
"""
from __future__ import annotations

import argparse
import csv
import html
import os
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

CODE_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(CODE_DIR)                 # US_selected_report
REPO = os.path.dirname(ROOT)                     # stock_monitor_copilot
sys.path.insert(0, CODE_DIR)
sys.path.insert(0, os.path.join(REPO, "A_rank_report", "code"))

import monitor_us_selected as mus  # noqa: E402   (复用自选/代码映射/缓存)
import find_overbought_stocks as fos  # noqa: E402

OUT_DIR = os.path.join(ROOT, "output")
DEFAULT_BARS = 320          # 需要长历史: lookback + MACD 预热
DEFAULT_LOOKBACK = 60       # 新高回看窗口(交易日)
WARMUP = 40                 # MACD(26)+DEA(9) 预热: 前高不得早于该下标


def macd_lines(close):
    """DIF=EMA12-EMA26(快线), DEA=EMA9(DIF)(慢线)。"""
    ema12 = close.ewm(span=12, adjust=False).mean()
    ema26 = close.ewm(span=26, adjust=False).mean()
    dif = ema12 - ema26
    dea = dif.ewm(span=9, adjust=False).mean()
    return dif, dea


def analyze_one(p: dict, bars: int, lookback: int, mode: str) -> tuple[dict | None, bool]:
    """找"股价新高但快慢线双低"的上涨衰减信号。

    返回 (信号行 or None, 今日是否创新高)。今日未创新高时信号必为 None。
    """
    if not p.get("prefixed"):
        mus._fetch_best(p, bars)
    if not p.get("prefixed"):
        return None, False
    k = fos.tencent_kline(p["prefixed"], bars, use_cache=True)
    if k is None:
        return None, False
    close = k[0].dropna()
    n = len(close)
    if n < WARMUP + 10:
        return None, False
    dif, dea = macd_lines(close)

    i_now = n - 1
    start = max(0, n - 1 - lookback)          # 前高搜索区间(不含今日)
    if start >= i_now:
        return None, False
    prev_vals = [float(x) for x in close.iloc[start:i_now]]
    if not prev_vals:
        return None, False
    peak_pos = max(range(len(prev_vals)), key=lambda j: prev_vals[j])
    i_peak = start + peak_pos

    # ① 今日必须创窗口新高(否则不是"当下发生"的背离)
    if float(close.iloc[i_now]) <= prev_vals[peak_pos]:
        return None, False
    # ② 前高需在 MACD 预热区之外
    if i_peak < WARMUP:
        return None, True

    dif_now, dea_now = float(dif.iloc[i_now]), float(dea.iloc[i_now])
    dif_pk, dea_pk = float(dif.iloc[i_peak]), float(dea.iloc[i_peak])
    dif_lower, dea_lower = dif_now < dif_pk, dea_now < dea_pk
    hit = (dif_lower and dea_lower) if mode == "both" else (dif_lower or dea_lower)
    if not hit:
        return None, True

    c_now, c_pk = float(close.iloc[i_now]), float(close.iloc[i_peak])
    if dif_lower and dea_lower:
        kind = "双线衰减"
    elif dif_lower:
        kind = "仅快线衰减"
    else:
        kind = "仅慢线衰减"
    return {
        "code": p["code"],
        "ticker": p["ticker"],
        "prefixed": p["prefixed"],
        "name": p.get("name", ""),
        "date": str(close.index[i_now].date()),
        "close": round(c_now, 2),
        "peak_date": str(close.index[i_peak].date()),
        "peak_close": round(c_pk, 2),
        "bars_between": int(i_now - i_peak),
        "price_new_high%": round((c_now / c_pk - 1) * 100, 2),
        "dif": round(dif_now, 3),
        "peak_dif": round(dif_pk, 3),
        "dif_gap": round(dif_now - dif_pk, 3),
        "dea": round(dea_now, 3),
        "peak_dea": round(dea_pk, 3),
        "dea_gap": round(dea_now - dea_pk, 3),
        "hist": round((dif_now - dea_now) * 2, 3),
        "peak_hist": round((dif_pk - dea_pk) * 2, 3),
        "kind": kind,
        "dif_lower": int(dif_lower),
        "dea_lower": int(dea_lower),
    }, True


def _name(r: dict) -> str:
    return r["name"] or r["ticker"]


# ---- HTML 报告 ----
NAVY = "#2b579a"; NAVY_D = "#1d3f6e"
WARN = "#d93025"; WARN_BG = "#fdecea"
MID = "#e65100"; MID_BG = "#fdeee2"
GREY = "#666"; ROW_BD = "#e3e9f2"


def _esc(s) -> str:
    return html.escape(str(s))


def _badge(kind: str) -> str:
    c, bg = {"双线衰减": (WARN, WARN_BG), "仅快线衰减": (MID, MID_BG),
             "仅慢线衰减": (MID, MID_BG)}.get(kind, (GREY, "#f1f3f4"))
    return (f"<span style='display:inline-block;padding:2px 10px;border-radius:11px;"
            f"font-size:12px;color:{c};background:{bg};font-weight:600'>{kind}</span>")


def _th(txt: str) -> str:
    return (f"<th style='padding:9px 12px;border:1px solid {NAVY_D};text-align:left;"
            f"white-space:nowrap'>{txt}</th>")


def _num(v, gap: bool = False) -> str:
    c = WARN if (gap and v < 0) else ("#188038" if (gap and v > 0) else "#222")
    return f"<span style='color:{c};font-weight:{700 if gap else 400}'>{v:+.3f}</span>" if gap else f"{v:.3f}"


def _row(r: dict, i: int) -> str:
    bg = "#ffffff" if i % 2 == 0 else "#f7f9fc"
    td = f"padding:7px 12px;border:1px solid {ROW_BD}"
    return (
        f"<tr style='background:{bg}'>"
        f"<td style='{td}'>{i}</td>"
        f"<td style='{td}'>{_esc(r['ticker'])}</td>"
        f"<td style='{td}'>{_esc(r['code'])}</td>"
        f"<td style='{td}'>{_esc(r['name'])}</td>"
        f"<td style='{td}'>{r['date']}</td>"
        f"<td style='{td};text-align:right'>{r['close']:.2f}</td>"
        f"<td style='{td}'>{r['peak_date']}</td>"
        f"<td style='{td};text-align:right'>{r['peak_close']:.2f}</td>"
        f"<td style='{td};text-align:right'>{r['bars_between']}</td>"
        f"<td style='{td};text-align:right;color:{WARN};font-weight:600'>{r['price_new_high%']:+.2f}%</td>"
        f"<td style='{td};text-align:right'>{r['dif']:.3f}</td>"
        f"<td style='{td};text-align:right'>{r['peak_dif']:.3f}</td>"
        f"<td style='{td};text-align:right'>{_num(r['dif_gap'], True)}</td>"
        f"<td style='{td};text-align:right'>{r['dea']:.3f}</td>"
        f"<td style='{td};text-align:right'>{r['peak_dea']:.3f}</td>"
        f"<td style='{td};text-align:right'>{_num(r['dea_gap'], True)}</td>"
        f"<td style='{td}'>{_badge(r['kind'])}</td></tr>")


def write_html(rows: list[dict], date_tag: str, data_date: str, watch_total: int,
               lookback: int, n_high: int, mode: str) -> str:
    both = sorted([r for r in rows if r["kind"] == "双线衰减"],
                  key=lambda x: (x["dea_gap"], x["dif_gap"]))
    partial = sorted([r for r in rows if r["kind"] != "双线衰减"],
                     key=lambda x: (x["dea_gap"], x["dif_gap"]))
    cols = ["#", "代码", "腾讯码", "名称", "新高日", "收盘", "前高日", "前高收盘",
            f"间隔({lookback}日内)", "价格新高", "DIF", "前高DIF", "DIF差",
            "DEA", "前高DEA", "DEA差", "衰减类型"]
    head = ("<tr style='background:" + NAVY + ";color:#fff'>"
            + "".join(_th(c) for c in cols) + "</tr>")

    def block(lst: list[dict], title: str, accent: str, desc: str) -> str:
        if not lst:
            return (f"<h3 style='color:{accent};margin:22px 0 2px'>{title} "
                    f"<span style='color:#888;font-size:13px;font-weight:400'>(0 只)</span></h3>"
                    f"<div style='color:#888;font-size:13px'>本组无股票</div>")
        body = "".join(_row(r, i + 1) for i, r in enumerate(lst))
        return (f"<h3 style='color:{accent};margin:22px 0 2px'>{title} "
                f"<span style='color:#888;font-size:13px;font-weight:400'>({len(lst)} 只 · {desc})</span></h3>"
                f"<table style='border-collapse:collapse;font-size:13px;font-family:Segoe UI,'Microsoft YaHei',sans-serif;"
                f"box-shadow:0 1px 3px rgba(0,0,0,.12);border-radius:6px;overflow:hidden'>"
                f"<thead>{head}</thead>{body}</table>")

    cards = ""
    for label, val, accent, bg in (
            ("今日创新高", n_high, NAVY, "#f0f4fa"),
            ("上涨衰减(双线)", len(both), WARN, WARN_BG),
            ("单线衰减", len(partial), MID, MID_BG),
            ("自选总数", watch_total, GREY, "#f1f3f4")):
        cards += (f"<div style='background:{bg};border:1px solid {accent}33;border-radius:8px;"
                  f"padding:12px 18px;min-width:130px'>"
                  f"<div style='font-size:12px;color:#555'>{label}</div>"
                  f"<div style='font-size:24px;font-weight:700;color:{accent}'>{val}</div></div>")
    cards = f"<div style='display:flex;gap:12px;flex-wrap:wrap;margin:14px 0'>{cards}</div>"

    mode_txt = "快线+慢线都低于前高" if mode == "both" else "快线或慢线任一低于前高"
    note = (f"<div style='background:#fff8e1;border:1px solid #ffe082;border-radius:6px;"
            f"padding:8px 14px;font-size:13px;color:#8d6e00;margin:10px 0'>"
            f"口径: 今日收盘 &gt; 前 {lookback} 个交易日最高收盘(创新高) 且 {mode_txt} · "
            f"MACD(12,26,9): DIF=EMA12−EMA26(快线), DEA=EMA9(DIF)(慢线) · "
            f"DIF差/DEA差 = 今日 − 前高(负数=低于前高) · 数据为最近收盘 {data_date}</div>")

    doc = f"""<!DOCTYPE html><html><head><meta charset='utf-8'>
<title>美股自选 MACD 上涨衰减监控 · {date_tag}</title>
<style>body{{font-family:Segoe UI,'Microsoft YaHei','Noto Sans CJK JP',sans-serif;color:#222;margin:0}}
.wrap{{padding:20px 24px;max-width:1450px;margin:0 auto}}h2{{color:{NAVY};margin-bottom:2px}}
h3{{margin:18px 0 4px}}table{{width:100%}}</style></head>
<body><div class='wrap'>
<h2>美股自选 MACD(12,26,9) 上涨衰减（顶背离）监控 · {date_tag}</h2>
{note}{cards}
{block(both, "上涨衰减 · 双线", WARN, "价格新高, 快线DIF与慢线DEA双双走低 → 上涨动能明显衰减")}
{block(partial, "上涨衰减 · 单线", MID, "价格新高, 仅一条线下行 → 衰减初期, 留意确认")}
<div style='margin:26px 0 40px;padding:12px 16px;background:#f5f7fa;border-radius:8px;font-size:13px;color:#555;line-height:1.8'>
<b>说明</b><br>
① 只列**今日正在创新高**且 MACD 快慢线低于前高的股票; 新高口径为收盘价, 窗口 {lookback} 个交易日。<br>
② 「前高」= 该窗口内收盘最高的那根K线; 间隔列 = 今日与前高相隔的交易日数(越大衰减越久)。<br>
③ DIF/DEA 为当日收盘计算值; 差值为负即低于前高, 负得越多衰减越重; 两张表均按 DEA差 升序。<br>
④ 前高若落在 MACD 预热区(前 {WARMUP} 根)内会跳过, 避免未稳定的 DIF/DEA 造成误判。<br>
⑤ 顶背离通常是**减仓/止盈提示**, 不代表立刻下跌; 需结合量能与趋势结构确认。</div>
</div></body></html>"""
    return doc


def main() -> None:
    ap = argparse.ArgumentParser(description="美股自选 MACD 上涨衰减(顶背离)监控")
    ap.add_argument("--cache", action="store_true", help="只拉取/缓存日K, 不判定")
    ap.add_argument("--bars", type=int, default=DEFAULT_BARS,
                    help=f"K线根数(默认{DEFAULT_BARS}, 需覆盖 lookback+预热)")
    ap.add_argument("--lookback", type=int, default=DEFAULT_LOOKBACK,
                    help=f"新高回看窗口(交易日, 默认{DEFAULT_LOOKBACK})")
    ap.add_argument("--mode", choices=["both", "any"], default="both",
                    help="both=快线慢线都需低于前高(默认); any=任一低于即报")
    ap.add_argument("--workers", type=int, default=16)
    a = ap.parse_args()

    watch = mus.load_watch()
    print(f"[信息] 自选美股 {len(watch)} 只 | MACD(12,26,9) 上涨衰减 "
          f"(bars={a.bars}, lookback={a.lookback}, mode={a.mode})")

    if a.cache:
        ok, total = mus.cache_klines(watch, a.bars, a.workers)
        print(f"[完成] 缓存 {ok}/{total} 只美股日K -> 共享 data/kline_cache")
        return

    rows: list[dict] = []
    n_high = 0
    with ThreadPoolExecutor(max_workers=a.workers) as ex:
        futs = [ex.submit(analyze_one, p, a.bars, a.lookback, a.mode) for p in watch]
        for f in as_completed(futs):
            try:
                r, is_new_high = f.result()
            except Exception:  # noqa: BLE001
                r, is_new_high = None, False
            n_high += 1 if is_new_high else 0
            if r:
                rows.append(r)

    rows.sort(key=lambda r: (r["kind"] != "双线衰减", r["dea_gap"]))
    both = [r for r in rows if r["kind"] == "双线衰减"]
    part = [r for r in rows if r["kind"] != "双线衰减"]
    data_date = rows[0]["date"] if rows else ""
    print(f"\n今日创新高 {n_high} 只 | 上涨衰减 {len(rows)} 只 "
          f"(双线 {len(both)} / 单线 {len(part)})")
    for tag, lst in (("上涨衰减·双线(快线+慢线双双走低)", both),
                     ("上涨衰减·单线(仅一条走低)", part)):
        if not lst:
            continue
        print(f"\n== {tag} ({len(lst)}) ==")
        for r in lst:
            print(f"  {r['code']:<8} {_name(r):<18} 新高{r['date']} {r['close']:.2f}"
                  f"(+{r['price_new_high%']:.2f}% vs 前高{r['peak_date']} {r['peak_close']:.2f}, "
                  f"隔{r['bars_between']}日) "
                  f"DIF {r['dif']:+.3f}/{r['peak_dif']:+.3f}({r['dif_gap']:+.3f}) "
                  f"DEA {r['dea']:+.3f}/{r['peak_dea']:+.3f}({r['dea_gap']:+.3f})")

    os.makedirs(OUT_DIR, exist_ok=True)
    tag = datetime.now().strftime("%Y%m%d")
    csv_path = os.path.join(OUT_DIR, f"us_selected_macd_decay_{tag}.csv")
    fields = ["code", "ticker", "prefixed", "name", "date", "close", "peak_date",
              "peak_close", "bars_between", "price_new_high%", "dif", "peak_dif",
              "dif_gap", "dea", "peak_dea", "dea_gap", "hist", "peak_hist",
              "kind", "dif_lower", "dea_lower"]
    with open(csv_path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for r in rows:
            w.writerow(r)
    print(f"\n明细已存: {csv_path}")
    html_path = os.path.join(OUT_DIR, f"us_macd_decay_report_{tag}.html")
    with open(html_path, "w", encoding="utf-8") as f:
        f.write(write_html(rows, tag, data_date, len(watch), a.lookback, n_high, a.mode))
    print("报告已生成:", html_path)


if __name__ == "__main__":
    main()
