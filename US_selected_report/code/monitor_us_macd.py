#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
美股自选 MACD(12,26,9) 金叉/死叉监控

口径(按用户确认):
  - 信号: 只报"近 window(默认3) 个交易日"内刚发生的交叉(取最近一次), 不分类当前状态
  - DIF = EMA12 - EMA26 (12快线/26慢线), DEA = EMA9(DIF), MACD柱=2*(DIF-DEA)
  - 金叉: DIF 由下上穿 DEA;  死叉: DIF 由上向下穿 DEA
  - 0轴上下: 以交叉当日 DIF>0(0轴上) / DIF<0(0轴下) 判定
  输出: 控制台4类列表 + output/us_selected_macd_YYYYMMDD.csv + HTML报告
用法:
  python monitor_us_macd.py [--window 3] [--bars 80] [--workers 8]
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
DEFAULT_BARS = 80
DEFAULT_WINDOW = 3          # 近3个交易日的交叉信号

# ---- MACD 计算 ----
def macd_lines(close):
    ema12 = close.ewm(span=12, adjust=False).mean()
    ema26 = close.ewm(span=26, adjust=False).mean()
    dif = ema12 - ema26
    dea = dif.ewm(span=9, adjust=False).mean()
    hist = (dif - dea) * 2
    return dif, dea, hist


def analyze_one(p: dict, bars: int, window: int) -> dict | None:
    """取缓存K线, 找最近 window 根内最近一次金叉/死叉。返回信号或 None。"""
    if not p.get("prefixed"):
        mus._fetch_best(p, bars)
    if not p.get("prefixed"):
        return None
    k = fos.tencent_kline(p["prefixed"], bars, use_cache=True)
    if k is None:
        return None
    close, _ = k
    close = close.dropna()
    if len(close) < 35:          # EMA26+DEA9 需要足够预热
        return None
    dif, dea, _ = macd_lines(close)
    n = len(close)
    hit = None                   # (cross_type, idx, zone)
    for back in range(1, window + 1):          # 从最新往回找最近一次交叉
        j = n - back
        if j < 1:
            break
        d0, d1 = float(dif.iloc[j - 1]), float(dif.iloc[j])
        e0, e1 = float(dea.iloc[j - 1]), float(dea.iloc[j])
        if d0 <= e0 and d1 > e1:
            hit = ("金叉", j); break
        if d0 >= e0 and d1 < e1:
            hit = ("死叉", j); break
    if not hit:
        return None
    ctype, j = hit
    difv = float(dif.iloc[j])
    deav = float(dea.iloc[j])
    zone = "0轴上" if difv > 0 else "0轴下"
    closev = float(close.iloc[j])
    prev = float(close.iloc[j - 1])
    chg = (closev / prev - 1) * 100 if prev else 0.0
    return {
        "code": p["code"],
        "ticker": p["ticker"],
        "prefixed": p["prefixed"],
        "name": p.get("name", ""),
        "date": str(close.index[j].date()),
        "type": ctype,
        "zone": zone,
        "status": zone + ctype,           # 0轴上金叉 / 0轴下金叉 / 0轴上死叉 / 0轴下死叉
        "dif": round(difv, 3),
        "dea": round(deav, 3),
        "hist": round((difv - deav) * 2, 3),
        "close": round(closev, 2),
        "chg%": round(chg, 2),
        "days_ago": (n - 1) - j,          # 0=最新交易日
    }


def _name(r: dict) -> str:
    return (r["name"] or r["ticker"])


# ---- HTML 报告 ----
NAVY = "#2b579a"; NAVY_D = "#1d3f6e"
GOLD_RED = "#d93025"; GOLD_ORANGE = "#e65100"
DEATH_GREEN = "#188038"; DEATH_DARK = "#0b6e3a"
GREY = "#666"; ROW_BD = "#e3e9f2"


def _esc(s) -> str:
    return html.escape(str(s))


def _badge(status: str) -> str:
    cmap = {
        "0轴上金叉": (GOLD_RED, "#fdecea"),
        "0轴下金叉": (GOLD_ORANGE, "#fdeee2"),
        "0轴上死叉": (DEATH_GREEN, "#e6f4ea"),
        "0轴下死叉": (DEATH_DARK, "#d9efe2"),
    }
    c, bg = cmap.get(status, (GREY, "#f1f3f4"))
    return (f"<span style='display:inline-block;padding:2px 10px;border-radius:11px;"
            f"font-size:12px;color:{c};background:{bg};font-weight:600'>{status}</span>")


def _th(txt: str) -> str:
    return (f"<th style='padding:9px 12px;border:1px solid {NAVY_D};text-align:left;"
            f"white-space:nowrap'>{txt}</th>")


def _row(r: dict, i: int, accent_side: str) -> str:
    bg = "#ffffff" if i % 2 == 0 else "#f7f9fc"
    ago = {0: "今日", 1: "1日前", 2: "2日前", 3: "3日前"}.get(r["days_ago"], f"{r['days_ago']}日前")
    chg = r["chg%"]
    chg_c = GOLD_RED if chg > 0 else (DEATH_GREEN if chg < 0 else GREY)
    tds = "".join(
        f"<td style='padding:7px 12px;border:1px solid {ROW_BD}'>{_esc(x)}</td>"
        for x in (r["ticker"], r["code"], _esc(r["name"]), r["date"], ago))
    return (
        f"<tr style='background:{bg}'>"
        f"<td style='padding:7px 12px;border:1px solid {ROW_BD}'>{i}</td>{tds}"
        f"<td style='padding:7px 12px;border:1px solid {ROW_BD};color:{GOLD_RED if r['type']=='金叉' else DEATH_GREEN};font-weight:700'>{r['type']}</td>"
        f"<td style='padding:7px 12px;border:1px solid {ROW_BD};text-align:right'>{r['dif']:.3f}</td>"
        f"<td style='padding:7px 12px;border:1px solid {ROW_BD};text-align:right'>{r['dea']:.3f}</td>"
        f"<td style='padding:7px 12px;border:1px solid {ROW_BD};text-align:right'>{r['hist']:.3f}</td>"
        f"<td style='padding:7px 12px;border:1px solid {ROW_BD};text-align:right'>{r['close']:.2f}</td>"
        f"<td style='padding:7px 12px;border:1px solid {ROW_BD};text-align:right;color:{chg_c};font-weight:600'>{chg:+.2f}%</td>"
        f"<td style='padding:7px 12px;border:1px solid {ROW_BD}'>{_badge(r['status'])}</td></tr>")


def write_html(rows: list[dict], date_tag: str, data_date: str, watch_total: int) -> str:
    groups = {}
    for st in ("0轴上金叉", "0轴下金叉", "0轴上死叉", "0轴下死叉"):
        groups[st] = sorted([r for r in rows if r["status"] == st],
                            key=lambda x: (x["days_ago"], -abs(x["dif"])))
    n_obs = len(rows)
    cards = ""
    for st, accent in (("0轴上金叉", GOLD_RED), ("0轴下金叉", GOLD_ORANGE),
                       ("0轴上死叉", DEATH_GREEN), ("0轴下死叉", DEATH_DARK)):
        bg = {"0轴上金叉": "#fdecea", "0轴下金叉": "#fdeee2",
              "0轴上死叉": "#e6f4ea", "0轴下死叉": "#d9efe2"}[st]
        cards += (f"<div style='background:{bg};border:1px solid {accent}33;border-radius:8px;"
                  f"padding:12px 18px;min-width:130px'>"
                  f"<div style='font-size:12px;color:#555'>{st}</div>"
                  f"<div style='font-size:24px;font-weight:700;color:{accent}'>{len(groups[st])}</div></div>")
    cards = f"<div style='display:flex;gap:12px;flex-wrap:wrap;margin:14px 0'>{cards}</div>"

    def block(st: str, accent: str) -> str:
        lst = groups[st]
        desc = {"0轴上金叉": "多头信号·DIF在0轴上金叉(偏强)", "0轴下金叉": "超跌反弹初期·DIF在0轴下金叉(偏弱)",
                "0轴上死叉": "高位转弱·DIF在0轴上死叉(提防回调)", "0轴下死叉": "空头延续·DIF在0轴下死叉(最弱)"}[st]
        head = ("<tr style='background:" + accent + ";color:#fff'>"
                + "".join(_th(c) for c in
                          ["#", "代码", "腾讯码", "名称", "交叉日", "距今天数",
                           "信号", "DIF", "DEA", "MACD柱", "收盘", "当日", "状态"])
                + "</tr>")
        body = "".join(_row(r, i + 1, st) for i, r in enumerate(lst))
        return (f"<h3 style='color:{accent};margin:22px 0 2px'>{st} "
                f"<span style='color:#888;font-size:13px;font-weight:400'>({len(lst)} 只 · {desc})</span></h3>"
                f"<table style='border-collapse:collapse;font-size:13px;font-family:Segoe UI,'Microsoft YaHei',sans-serif;"
                f"box-shadow:0 1px 3px rgba(0,0,0,.12);border-radius:6px;overflow:hidden'>"
                f"<thead>{head}</thead>{body}</table>")

    note = (f"<div style='background:#fff8e1;border:1px solid #ffe082;border-radius:6px;padding:8px 14px;"
            f"font-size:13px;color:#8d6e00;margin:10px 0'>"
            f"统计口径: MACD(12,26,9) · DIF=EMA12−EMA26(12快/26慢), DEA=EMA9(DIF) · "
            f"报近3个交易日内最近一次 DIF 上穿/下穿 DEA 的信号 · 0轴上下以交叉当日 DIF 正负判定 · "
            f"数据为最近收盘 {data_date}</div>")
    doc = f"""<!DOCTYPE html><html><head><meta charset='utf-8'>
<title>美股自选 MACD 金叉/死叉监控 · {date_tag}</title>
<style>body{{font-family:Segoe UI,'Microsoft YaHei','Noto Sans CJK JP',sans-serif;color:#222;margin:0}}
.wrap{{padding:20px 24px;max-width:1250px;margin:0 auto}}h2{{color:{NAVY};margin-bottom:2px}}
h3{{margin:18px 0 4px}}table{{width:100%}}</style></head>
<body><div class='wrap'>
<h2>美股自选 MACD(12,26,9) 金叉/死叉监控 · {date_tag}</h2>
{note}{cards}
{block('0轴上金叉', GOLD_RED)}
{block('0轴下金叉', GOLD_ORANGE)}
{block('0轴上死叉', DEATH_GREEN)}
{block('0轴下死叉', DEATH_DARK)}
<div style='margin:26px 0 40px;padding:12px 16px;background:#f5f7fa;border-radius:8px;font-size:13px;color:#555;line-height:1.8'>
<b>说明</b><br>
① 自选 {watch_total} 只, 有足够K线(MACD预热)且近3日有交叉信号 {n_obs} 只; 无信号或数据不足的不列出。<br>
② 金叉=DIF由下上穿DEA(看多), 死叉=DIF向下穿DEA(看空); 取窗口内最近一次交叉, 距今天数 0/1/2 = 最新/前1/前2个交易日。<br>
③ 0轴上是强势区(DIF&gt;0), 0轴下是弱势区(DIF&lt;0)。</div>
</div></body></html>"""
    return doc


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--window", type=int, default=DEFAULT_WINDOW, help="交叉信号窗口(交易日)")
    ap.add_argument("--bars", type=int, default=DEFAULT_BARS)
    ap.add_argument("--workers", type=int, default=8)
    a = ap.parse_args()

    watch = mus.load_watch()
    print(f"[信息] 自选美股 {len(watch)} 只, MACD(12,26,9) 窗口={a.window}个交易日")

    rows: list[dict] = []
    with ThreadPoolExecutor(max_workers=a.workers) as ex:
        futs = [ex.submit(analyze_one, p, a.bars, a.window) for p in watch]
        for f in as_completed(futs):
            try:
                r = f.result()
            except Exception:  # noqa: BLE001
                r = None
            if r:
                rows.append(r)

    def keyf(x):
        return {"金叉": 0, "死叉": 1}[x["type"]], {"0轴上": 0, "0轴下": 1}[x["zone"]], x["days_ago"]
    rows.sort(key=keyf)

    data_date = rows[0]["date"] if rows else ""
    grp = {}
    for st in ("0轴上金叉", "0轴下金叉", "0轴上死叉", "0轴下死叉"):
        grp[st] = [r for r in rows if r["status"] == st]
    print(f"\n近{a.window}日交叉信号 {len(rows)} 只 | 金叉 {len(grp['0轴上金叉'])+len(grp['0轴下金叉'])} 只"
          f"(0轴上{len(grp['0轴上金叉'])}/0轴下{len(grp['0轴下金叉'])}), "
          f"死叉 {len(grp['0轴上死叉'])+len(grp['0轴下死叉'])} 只"
          f"(0轴上{len(grp['0轴上死叉'])}/0轴下{len(grp['0轴下死叉'])})")
    for st, tag, high in (("0轴上金叉", "金叉·0轴上(偏强)", True),
                          ("0轴下金叉", "金叉·0轴下(弱势区金叉)", True),
                          ("0轴上死叉", "死叉·0轴上(高位转弱)", False),
                          ("0轴下死叉", "死叉·0轴下(空头延续)", False)):
        lst = sorted(grp[st], key=lambda x: (x["days_ago"], -abs(x["dif"])))
        if not lst:
            continue
        print(f"\n== {tag} ({len(lst)}) ==")
        for r in lst:
            ago = {0: "今", 1: "昨", 2: "前2日"}.get(r["days_ago"], f"{r['days_ago']}日前")
            print(f"  {r['code']:<8} {_name(r):<16} {ago} {r['date']} "
                  f"DIF={r['dif']:+.3f} DEA={r['dea']:+.3f} 收盘{r['close']:.2f} {r['chg%']:+.1f}%")

    os.makedirs(OUT_DIR, exist_ok=True)
    tag = datetime.now().strftime("%Y%m%d")
    csv_path = os.path.join(OUT_DIR, f"us_selected_macd_{tag}.csv")
    with open(csv_path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=["code", "ticker", "prefixed", "name", "date",
                                          "type", "zone", "status", "dif", "dea", "hist",
                                          "close", "chg%", "days_ago"])
        w.writeheader()
        for r in rows:
            w.writerow(r)
    print(f"\n明细已存: {csv_path}")
    html_path = os.path.join(OUT_DIR, f"us_macd_report_{tag}.html")
    with open(html_path, "w", encoding="utf-8") as f:
        f.write(write_html(rows, tag, data_date, len(watch)))
    print("报告已生成:", html_path)


if __name__ == "__main__":
    main()
