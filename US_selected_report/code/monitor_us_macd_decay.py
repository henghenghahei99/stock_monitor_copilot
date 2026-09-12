#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
美股自选 MACD(12,26,9) 上涨衰减(顶背离) 监控

口径(用户确认, 2026-09-12):
  **股价创近期新高, 但 MACD 快慢线反而低于前一个高点的快慢线** —— 上涨动能衰减。

时间窗口(默认三个一起跑, 可用 --windows 选):
  1m = 1 个月 = 21 个交易日
  3m = 3 个月 = 63 个交易日
  6m = 6 个月 = 126 个交易日
三个窗口是**嵌套**的: 6月新高 ⊂ 3月新高 ⊂ 1月新高。

具体判定(对每个窗口各自算一次):
  1. 「新高」= 今日收盘价 > 该窗口内之前所有交易日的最高收盘。
     只报**今天正在创新高**的股票(信号是"当下发生"的, 不是历史遗留)。
  2. 「前高」= 该窗口内收盘最高的那根。
  3. DIF = EMA12 - EMA26(快线), DEA = EMA9(DIF)(慢线):
     两条都低于前高当日 → 双线衰减(默认 --mode both); 任一低于 → 单线衰减(--mode any)。
  4. 前高落在 MACD 预热区(前 40 根)内的窗口跳过, 避免未稳定的 DIF/DEA 误判。

一只票一行, 三级含义:
  - 新高级别 = 它创新高的**最长**窗口(6月 > 3月 > 1月) —— 新高越"大"越重要
  - 信号窗口 = 出现衰减的**最长**窗口(表里的 DIF/DEA 取该窗口的对比)
  - 衰减确认 = k/n, 它在创新高的 n 个窗口里有 k 个出现衰减 → 多周期共振程度

用 close(收盘)而非最高价: 复用腾讯日K缓存(只提供收盘/成交量), 且 MACD 本身基于收盘。

用法:
  cd US_selected_report
  # 1) 预缓存日K(6个月窗口需要长历史, 建议 bars=320)
  python code/monitor_us_macd_decay.py --cache --bars 320 --workers 8
  # 2) 出信号(默认 1m/3m/6m 全跑)
  python code/monitor_us_macd_decay.py
  python code/monitor_us_macd_decay.py --windows 6m          # 只看 6 个月新高
  python code/monitor_us_macd_decay.py --windows 1m,3m       # 看 1/3 个月
  python code/monitor_us_macd_decay.py --windows 21,63,126   # 也可直接给交易日数
  python code/monitor_us_macd_decay.py --mode any            # 任一条线低即报

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
DEFAULT_BARS = 320          # 6个月窗口 + MACD 预热需要长历史
WARMUP = 40                 # MACD(26)+DEA(9) 预热: 前高不得早于该下标
# 时间窗口预设: 1个月≈21个交易日, 3个月≈63, 6个月≈126
WINDOW_PRESETS = {"1m": 21, "3m": 63, "6m": 126}
DEFAULT_WINDOWS = "1m,3m,6m"


def parse_windows(spec: str) -> list[tuple[str, int]]:
    """'1m,3m,6m' 或 '21,63,126' -> [(标签, 交易日数), ...] 按天数升序去重。"""
    out: list[tuple[str, int]] = []
    for tok in str(spec).replace(" ", "").split(","):
        if not tok:
            continue
        low = tok.lower()
        if low in WINDOW_PRESETS:
            out.append((low, WINDOW_PRESETS[low]))
        else:
            try:
                d = int(tok)
            except ValueError:
                raise SystemExit(f"无法识别的窗口: {tok} (可用 1m/3m/6m 或交易日数)")
            if d <= 0:
                raise SystemExit(f"窗口必须为正: {tok}")
            out.append((f"{d}日", d))
    if not out:
        raise SystemExit("窗口为空")
    si = {}
    for lab, d in out:
        si.setdefault(d, lab)
    return sorted(((lab, d) for d, lab in si.items()), key=lambda x: x[1])


def macd_lines(close):
    """DIF=EMA12-EMA26(快线), DEA=EMA9(DIF)(慢线)。"""
    ema12 = close.ewm(span=12, adjust=False).mean()
    ema26 = close.ewm(span=26, adjust=False).mean()
    dif = ema12 - ema26
    dea = dif.ewm(span=9, adjust=False).mean()
    return dif, dea


def _peak_of(close, start: int, end_excl: int) -> tuple[int, float]:
    """区间 [start, end_excl) 内收盘最高点的 (下标, 收盘价)。"""
    vals = [float(x) for x in close.iloc[start:end_excl]]
    pos = max(range(len(vals)), key=lambda j: vals[j])
    return start + pos, vals[pos]


def analyze_one(p: dict, bars: int, windows: list[tuple[str, int]],
                mode: str) -> tuple[dict | None, int]:
    """对每个窗口各判一次"新高+快慢线更低"。

    返回 (信号行 or None, 今日创新高的级别数 0..len(windows))。
    """
    if not p.get("prefixed"):
        mus._fetch_best(p, bars)
    if not p.get("prefixed"):
        return None, 0
    k = fos.tencent_kline(p["prefixed"], bars, use_cache=True)
    if k is None:
        return None, 0
    close = k[0].dropna()
    n = len(close)
    if n < WARMUP + 10:
        return None, 0
    dif, dea = macd_lines(close)

    i_now = n - 1
    c_now = float(close.iloc[i_now])
    dif_now, dea_now = float(dif.iloc[i_now]), float(dea.iloc[i_now])

    info: list[dict] = []
    for lab, days in windows:
        start = max(0, n - 1 - days)
        if start >= i_now:
            continue
        i_pk, c_pk = _peak_of(close, start, i_now)
        if i_pk < WARMUP:                 # 前高在预热区, 该窗口不可用
            continue
        is_high = c_now > c_pk
        rec = {"label": lab, "days": days, "high": is_high, "decay": False,
               "i_peak": i_pk, "peak_close": c_pk,
               "dif_pk": float(dif.iloc[i_pk]), "dea_pk": float(dea.iloc[i_pk])}
        if is_high:
            dl, el = dif_now < rec["dif_pk"], dea_now < rec["dea_pk"]
            rec["decay"] = (dl and el) if mode == "both" else (dl or el)
            rec["dif_lower"], rec["dea_lower"] = dl, el
        info.append(rec)

    highs = [w for w in info if w["high"]]
    if not highs:
        return None, 0
    sigs = [w for w in highs if w["decay"]]
    if not sigs:
        return None, len(highs)

    lvl = highs[-1]          # 最长新高窗口(窗口已按天数升序)
    sig = sigs[-1]           # 出现衰减的最长窗口
    c_pk = sig["peak_close"]
    if sig.get("dif_lower") and sig.get("dea_lower"):
        kind = "双线衰减"
    elif sig.get("dif_lower"):
        kind = "仅快线衰减"
    else:
        kind = "仅慢线衰减"
    detail = "; ".join(
        f"{w['label']}:" + ("新高" if w["high"] else "非新高") + ("/衰减" if w["decay"] else "")
        for w in info)
    return {
        "code": p["code"],
        "ticker": p["ticker"],
        "prefixed": p["prefixed"],
        "name": p.get("name", ""),
        "date": str(close.index[i_now].date()),
        "close": round(c_now, 2),
        "level": lvl["label"],
        "level_days": lvl["days"],
        "sig_window": sig["label"],
        "sig_days": sig["days"],
        "confirm": f"{len(sigs)}/{len(highs)}",
        "peak_date": str(close.index[sig["i_peak"]].date()),
        "peak_close": round(c_pk, 2),
        "bars_between": int(i_now - sig["i_peak"]),
        "price_new_high%": round((c_now / c_pk - 1) * 100, 2),
        "dif": round(dif_now, 3),
        "peak_dif": round(sig["dif_pk"], 3),
        "dif_gap": round(dif_now - sig["dif_pk"], 3),
        "dea": round(dea_now, 3),
        "peak_dea": round(sig["dea_pk"], 3),
        "dea_gap": round(dea_now - sig["dea_pk"], 3),
        "kind": kind,
        "windows_detail": detail,
    }, len(highs)


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


def _lvl_badge(lab: str, win_labels: list[str]) -> str:
    """新高级别: 6月(最大)→红, 3月→橙, 其余→蓝。"""
    c = {6: WARN, 3: MID}.get(0, NAVY)
    order = {"6m": 0, "3m": 1, "1m": 2}
    c = [WARN, MID, NAVY, GREY][order.get(lab, 3)]
    return (f"<span style='display:inline-block;padding:1px 8px;border-radius:9px;font-size:12px;"
            f"color:#fff;background:{c};font-weight:600'>{lab}新高</span>")


def _th(txt: str) -> str:
    return (f"<th style='padding:8px 10px;border:1px solid {NAVY_D};text-align:left;"
            f"white-space:nowrap'>{txt}</th>")


def _num(v, gap: bool = False) -> str:
    if gap:
        c = WARN if v < 0 else "#188038"
        return f"<span style='color:{c};font-weight:700'>{v:+.3f}</span>"
    return f"{v:.3f}"


def _row(r: dict, i: int, win_labels: list[str]) -> str:
    bg = "#ffffff" if i % 2 == 0 else "#f7f9fc"
    td = f"padding:6px 10px;border:1px solid {ROW_BD}"
    return (
        f"<tr style='background:{bg}'>"
        f"<td style='{td}'>{i}</td>"
        f"<td style='{td}'>{_esc(r['ticker'])}</td>"
        f"<td style='{td}'>{_esc(r['code'])}</td>"
        f"<td style='{td}'>{_esc(r['name'])}</td>"
        f"<td style='{td}'>{_lvl_badge(r['level'], win_labels)}</td>"
        f"<td style='{td}'>{r['sig_window']}</td>"
        f"<td style='{td};text-align:center'>{r['confirm']}</td>"
        f"<td style='{td}'>{r['date']}</td>"
        f"<td style='{td};text-align:right'>{r['close']:.2f}</td>"
        f"<td style='{td}'>{r['peak_date']}</td>"
        f"<td style='{td};text-align:right'>{r['peak_close']:.2f}</td>"
        f"<td style='{td};text-align:right'>{r['bars_between']}</td>"
        f"<td style='{td};text-align:right;color:{WARN};font-weight:600'>{r['price_new_high%']:+.2f}%</td>"
        f"<td style='{td};text-align:right'>{_num(r['dif'])}</td>"
        f"<td style='{td};text-align:right'>{_num(r['peak_dif'])}</td>"
        f"<td style='{td};text-align:right'>{_num(r['dif_gap'], True)}</td>"
        f"<td style='{td};text-align:right'>{_num(r['dea'])}</td>"
        f"<td style='{td};text-align:right'>{_num(r['peak_dea'])}</td>"
        f"<td style='{td};text-align:right'>{_num(r['dea_gap'], True)}</td>"
        f"<td style='{td}'>{_badge(r['kind'])}</td></tr>")


def write_html(rows: list[dict], date_tag: str, data_date: str, watch_total: int,
               windows: list[tuple[str, int]], n_high: int, mode: str) -> str:
    win_labels = [lab for lab, _ in windows]
    win_txt = "/".join(f"{lab}({d}日)" for lab, d in windows)
    both = sorted([r for r in rows if r["kind"] == "双线衰减"],
                  key=lambda x: (-x["level_days"], x["dea_gap"]))
    partial = sorted([r for r in rows if r["kind"] != "双线衰减"],
                     key=lambda x: (-x["level_days"], x["dea_gap"]))
    cols = ["#", "代码", "腾讯码", "名称", "新高级别", "信号窗口", "衰减确认",
            "新高日", "收盘", "前高日", "前高收盘", "间隔(日)", "价格新高",
            "DIF", "前高DIF", "DIF差", "DEA", "前高DEA", "DEA差", "衰减类型"]
    head = ("<tr style='background:" + NAVY + ";color:#fff'>"
            + "".join(_th(c) for c in cols) + "</tr>")

    def block(lst: list[dict], title: str, accent: str, desc: str) -> str:
        if not lst:
            return (f"<h3 style='color:{accent};margin:22px 0 2px'>{title} "
                    f"<span style='color:#888;font-size:13px;font-weight:400'>(0 只)</span></h3>"
                    f"<div style='color:#888;font-size:13px'>本组无股票</div>")
        body = "".join(_row(r, i + 1, win_labels) for i, r in enumerate(lst))
        return (f"<h3 style='color:{accent};margin:22px 0 2px'>{title} "
                f"<span style='color:#888;font-size:13px;font-weight:400'>({len(lst)} 只 · {desc})</span></h3>"
                f"<table style='border-collapse:collapse;font-size:12.5px;font-family:Segoe UI,'Microsoft YaHei',sans-serif;"
                f"box-shadow:0 1px 3px rgba(0,0,0,.12);border-radius:6px;overflow:hidden'>"
                f"<thead>{head}</thead>{body}</table>")

    n_6m = len([r for r in rows if r["level"] == "6m"])
    cards = ""
    for label, val, accent, bg in (
            ("自选总数", watch_total, GREY, "#f1f3f4"),
            (f"创新高({win_txt})", n_high, NAVY, "#f0f4fa"),
            ("上涨衰减·双线", len(both), WARN, WARN_BG),
            ("上涨衰减·单线", len(partial), MID, MID_BG),
            ("其中 6月级", n_6m, "#7b1fa2", "#f3e8fa")):
        cards += (f"<div style='background:{bg};border:1px solid {accent}33;border-radius:8px;"
                  f"padding:12px 18px;min-width:130px'>"
                  f"<div style='font-size:12px;color:#555'>{label}</div>"
                  f"<div style='font-size:24px;font-weight:700;color:{accent}'>{val}</div></div>")
    cards = f"<div style='display:flex;gap:12px;flex-wrap:wrap;margin:14px 0'>{cards}</div>"

    mode_txt = "快线+慢线都低于前高" if mode == "both" else "快线或慢线任一低于前高"
    note = (f"<div style='background:#fff8e1;border:1px solid #ffe082;border-radius:6px;"
            f"padding:8px 14px;font-size:13px;color:#8d6e00;margin:10px 0'>"
            f"口径: 今日收盘 &gt; 该窗口内最高收盘(近期新高) 且 {mode_txt} · "
            f"窗口 {win_txt}(三窗口嵌套: 6m新高⊂3m新高⊂1m新高) · "
            f"MACD(12,26,9): DIF=EMA12−EMA26(快线), DEA=EMA9(DIF)(慢线) · "
            f"DIF差/DEA差 = 今日 − 前高(负数=低于前高) · 数据为最近收盘 {data_date}</div>")

    doc = f"""<!DOCTYPE html><html><head><meta charset='utf-8'>
<title>美股自选 MACD 上涨衰减监控 · {date_tag}</title>
<style>body{{font-family:Segoe UI,'Microsoft YaHei','Noto Sans CJK JP',sans-serif;color:#222;margin:0}}
.wrap{{padding:20px 24px;max-width:1650px;margin:0 auto}}h2{{color:{NAVY};margin-bottom:2px}}
h3{{margin:18px 0 4px}}table{{width:100%}}</style></head>
<body><div class='wrap'>
<h2>美股自选 MACD(12,26,9) 上涨衰减（顶背离）监控 · {date_tag}</h2>
{note}{cards}
{block(both, "上涨衰减 · 双线", WARN, "价格创新高, 快线DIF与慢线DEA双双走低 → 上涨动能明显衰减")}
{block(partial, "上涨衰减 · 单线", MID, "价格创新高, 仅一条线下行 → 衰减初期, 留意确认")}
<div style='margin:26px 0 40px;padding:12px 16px;background:#f5f7fa;border-radius:8px;font-size:13px;color:#555;line-height:1.8'>
<b>说明</b><br>
① 只列**今日正在创新高**且 MACD 快慢线低于前高的股票; 新高口径为收盘价, 窗口 {win_txt}。<br>
② **新高级别** = 它创新高的最长窗口(6m &gt; 3m &gt; 1m): 级别越大, 这个"新高"越关键; **信号窗口** = 出现衰减的最长窗口(表中 DIF/DEA 取该窗口对比)。<br>
③ **衰减确认** = k/n: 它在创新高的 n 个窗口里有 k 个出现衰减 → 多周期共振(如 3/3 表示三个尺度全部背离, 1/3 仅为短周期背离)。<br>
④ 「前高」= 该窗口内收盘最高的那根K线; 间隔列 = 今日与前高相隔的交易日数(越大衰减越久)。<br>
⑤ 前高若落在 MACD 预热区(前 {WARMUP} 根)内, 该窗口跳过, 避免未稳定的 DIF/DEA 造成误判。<br>
⑥ 顶背离通常是**减仓/止盈提示**, 不代表立刻下跌; 需结合量能与趋势结构确认。</div>
</div></body></html>"""
    return doc


def main() -> None:
    ap = argparse.ArgumentParser(description="美股自选 MACD 上涨衰减(顶背离)监控")
    ap.add_argument("--cache", action="store_true", help="只拉取/缓存日K, 不判定")
    ap.add_argument("--bars", type=int, default=DEFAULT_BARS,
                    help=f"K线根数(默认{DEFAULT_BARS}, 需覆盖最长窗口+预热)")
    ap.add_argument("--windows", default=DEFAULT_WINDOWS,
                    help=f"时间窗口, 逗号分隔(默认 {DEFAULT_WINDOWS}; 可给 1m/3m/6m 或交易日数)")
    ap.add_argument("--lookback", type=int, default=0,
                    help="兼容旧参数: 只跑单一窗口(交易日数), 会覆盖 --windows")
    ap.add_argument("--mode", choices=["both", "any"], default="both",
                    help="both=快线慢线都需低于前高(默认); any=任一低于即报")
    ap.add_argument("--workers", type=int, default=16)
    a = ap.parse_args()

    windows = parse_windows(str(a.lookback) if a.lookback else a.windows)
    win_txt = "/".join(f"{lab}({d}日)" for lab, d in windows)
    watch = mus.load_watch()
    print(f"[信息] 自选美股 {len(watch)} 只 | MACD(12,26,9) 上涨衰减 "
          f"(bars={a.bars}, 窗口={win_txt}, mode={a.mode})")

    if a.cache:
        ok, total = mus.cache_klines(watch, a.bars, a.workers)
        print(f"[完成] 缓存 {ok}/{total} 只美股日K -> 共享 data/kline_cache")
        return

    rows: list[dict] = []
    n_high = 0
    with ThreadPoolExecutor(max_workers=a.workers) as ex:
        futs = [ex.submit(analyze_one, p, a.bars, windows, a.mode) for p in watch]
        for f in as_completed(futs):
            try:
                r, nh = f.result()
            except Exception:  # noqa: BLE001
                r, nh = None, 0
            n_high += 1 if nh else 0
            if r:
                rows.append(r)

    rows.sort(key=lambda r: (r["kind"] != "双线衰减", -r["level_days"], r["dea_gap"]))
    both = [r for r in rows if r["kind"] == "双线衰减"]
    part = [r for r in rows if r["kind"] != "双线衰减"]
    data_date = rows[0]["date"] if rows else ""
    print(f"\n创新高(任一窗口) {n_high} 只 | 上涨衰减 {len(rows)} 只 "
          f"(双线 {len(both)} / 单线 {len(part)})")
    for tag, lst in (("上涨衰减·双线(快线+慢线双双走低)", both),
                     ("上涨衰减·单线(仅一条走低)", part)):
        if not lst:
            continue
        print(f"\n== {tag} ({len(lst)}) ==")
        for r in lst:
            print(f"  {r['code']:<9} {_name(r):<16} [{r['level']}新高|信号{r['sig_window']}|确认{r['confirm']}] "
                  f"{r['date']} 收{r['close']:.2f}(+{r['price_new_high%']:.2f}% vs 前高{r['peak_date']} "
                  f"{r['peak_close']:.2f}, 隔{r['bars_between']}日) "
                  f"DIF {r['dif']:+.3f}/{r['peak_dif']:+.3f}({r['dif_gap']:+.3f}) "
                  f"DEA {r['dea']:+.3f}/{r['peak_dea']:+.3f}({r['dea_gap']:+.3f}) | {r['windows_detail']}")

    os.makedirs(OUT_DIR, exist_ok=True)
    tag = datetime.now().strftime("%Y%m%d")
    csv_path = os.path.join(OUT_DIR, f"us_selected_macd_decay_{tag}.csv")
    fields = ["code", "ticker", "prefixed", "name", "date", "close",
              "level", "level_days", "sig_window", "sig_days", "confirm",
              "peak_date", "peak_close", "bars_between", "price_new_high%",
              "dif", "peak_dif", "dif_gap", "dea", "peak_dea", "dea_gap",
              "kind", "windows_detail"]
    with open(csv_path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for r in rows:
            w.writerow(r)
    print(f"\n明细已存: {csv_path}")
    html_path = os.path.join(OUT_DIR, f"us_macd_decay_report_{tag}.html")
    with open(html_path, "w", encoding="utf-8") as f:
        f.write(write_html(rows, tag, data_date, len(watch), windows, n_high, a.mode))
    print("报告已生成:", html_path)


if __name__ == "__main__":
    main()
