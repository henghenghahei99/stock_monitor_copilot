#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
美股自选 RSI(6) 监控 -> HTML 报告

读 output/us_selected_rsi_YYYYMMDD.csv(由 monitor_us_selected.py 生成),
渲染成与 A_rank 日报风格一致的自包含 HTML:
  output/us_monitor_report_YYYYMMDD.html

用法: python make_us_monitor_report.py [YYYYMMDD]   (默认最新 CSV)
"""
from __future__ import annotations

import argparse
import csv
import glob
import html
import os
from datetime import datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT_DIR = os.path.join(ROOT, "output")

NAVY = "#2b579a"
NAVY_D = "#1d3f6e"
RED = "#d93025"        # 超买
RED_BG = "#fdecea"
GREEN = "#188038"      # 超卖
GREEN_BG = "#e6f4ea"
GREY = "#666"
ROW_BD = "#e3e9f2"


def esc(s) -> str:
    return html.escape(str(s))


def _badge(status: str) -> str:
    if status == "超买":
        c, bg = RED, RED_BG
    elif status == "超卖":
        c, bg = GREEN, GREEN_BG
    else:
        c, bg = GREY, "#f1f3f4"
    return (f"<span style='display:inline-block;padding:2px 10px;border-radius:11px;"
            f"font-size:12px;color:{c};background:{bg};font-weight:600'>{status}</span>")


def _rsi_cell(rsi: float) -> str:
    """RSI 数值 + 迷你条(颜色随区间)。"""
    if rsi > 83:
        c = RED
    elif rsi < 26:
        c = GREEN
    else:
        c = NAVY
    w = max(2, min(100, int(rsi)))          # RSI 0-100
    return (f"<div style='min-width:150px'><span style='font-weight:700;color:{c}'>{rsi:.1f}</span>"
            f"<div style='position:relative;height:6px;background:#eceff1;border-radius:3px;margin-top:3px'>"
            f"<div style='width:{w}%;height:100%;background:{c};border-radius:3px'></div></div></div>")


def _th(txt: str) -> str:
    return (f"<th style='padding:9px 14px;border:1px solid {NAVY_D};"
            f"text-align:left;white-space:nowrap'>{txt}</th>")


def _table_head(cols: list[str], accent: str) -> str:
    return (f"<thead><tr style='background:{accent};color:#fff'>"
            + "".join(_th(c) for c in cols) + "</tr></thead>")


def _detail_row(r: dict, i: int) -> str:
    st = r["status"]
    chg = float(r["chg%"] or 0)
    chg_c = RED if chg > 0 else (GREEN if chg < 0 else GREY)
    chg_s = f"<span style='color:{chg_c};font-weight:600'>{chg:+.2f}%</span>"
    bg = "#ffffff" if i % 2 == 0 else "#f7f9fc"
    return (f"<tr style='background:{bg}'>"
            f"<td style='padding:7px 14px;border:1px solid {ROW_BD}'>{i}</td>"
            f"<td style='padding:7px 14px;border:1px solid {ROW_BD};font-weight:600;white-space:nowrap'>"
            f"{esc(r['ticker'])}</td>"
            f"<td style='padding:7px 14px;border:1px solid {ROW_BD};color:#555'>{esc(r['code'])}</td>"
            f"<td style='padding:7px 14px;border:1px solid {ROW_BD};font-size:12px;color:#444'>{esc(r['name'])}</td>"
            f"<td style='padding:7px 14px;border:1px solid {ROW_BD};text-align:right'>{r['close']}</td>"
            f"<td style='padding:7px 14px;border:1px solid {ROW_BD};text-align:right'>{chg_s}</td>"
            f"<td style='padding:7px 14px;border:1px solid {ROW_BD}'>{_rsi_cell(float(r['rsi6']))}</td>"
            f"<td style='padding:7px 14px;border:1px solid {ROW_BD}'>{_badge(st)}</td>"
            f"</tr>")


def build(csv_path: str, date_tag: str) -> str:
    with open(csv_path, encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))
    for r in rows:
        r["rsi6"] = float(r["rsi6"])
        r["close"] = f"{float(r['close']):.2f}"
    ob = sorted([r for r in rows if r["status"] == "超买"], key=lambda x: -x["rsi6"])
    os_ = sorted([r for r in rows if r["status"] == "超卖"], key=lambda x: x["rsi6"])
    nm = sorted([r for r in rows if r["status"] == "正常"], key=lambda x: -x["rsi6"])
    data_date = rows[0]["date"] if rows else date_tag
    n_total = len(rows)

    cards = (
        f"<div style='display:flex;gap:12px;flex-wrap:wrap;margin:14px 0'>"
        f"<div style='background:#fff;border:1px solid {ROW_BD};border-top:3px solid {NAVY};"
        f"border-radius:8px;padding:12px 20px;min-width:120px'><div style='font-size:12px;color:#888'>自选</div>"
        f"<div style='font-size:24px;font-weight:700;color:{NAVY}'>211</div></div>"
        f"<div style='background:#fff;border:1px solid {ROW_BD};border-top:3px solid #00897b;"
        f"border-radius:8px;padding:12px 20px;min-width:120px'><div style='font-size:12px;color:#888'>有K线数据</div>"
        f"<div style='font-size:24px;font-weight:700;color:#00897b'>{n_total}</div></div>"
        f"<div style='background:{RED_BG};border:1px solid #f5c6c1;border-radius:8px;padding:12px 20px;"
        f"min-width:120px'><div style='font-size:12px;color:#a52a20'>⚠️ 超买 RSI6&gt;83</div>"
        f"<div style='font-size:24px;font-weight:700;color:{RED}'>{len(ob)}</div></div>"
        f"<div style='background:{GREEN_BG};border:1px solid #b7dfc0;border-radius:8px;padding:12px 20px;"
        f"min-width:120px'><div style='font-size:12px;color:#1c5c30'>📉 超卖 RSI6&lt;26</div>"
        f"<div style='font-size:24px;font-weight:700;color:{GREEN}'>{len(os_)}</div></div>"
        f"<div style='background:#fff;border:1px solid {ROW_BD};border-radius:8px;padding:12px 20px;"
        f"min-width:120px'><div style='font-size:12px;color:#888'>正常区间</div>"
        f"<div style='font-size:24px;font-weight:700;color:{GREY}'>{len(nm)}</div></div></div>"
    )

    note = (f"<div style='background:#fff8e1;border:1px solid #ffe082;border-radius:6px;"
            f"padding:8px 14px;font-size:13px;color:#8d6e00;margin:10px 0'>"
            f"最近收盘日 {data_date} · 美东 09-07 为劳动节休市, 以 09-04 收盘为准 · "
            f"1 只自选(FNMA 房利美)腾讯无历史K线未纳入</div>")

    def status_block(title: str, accent: str, lst: list[str], note2: str = "") -> str:
        cols = ["#", "代码", "腾讯码", "名称", "收盘", "当日", "RSI(6)", "状态"]
        body = "".join(_detail_row(r, i + 1) for i, r in enumerate(lst))
        return (f"<h3 style='color:{accent};margin:22px 0 4px'>{title} "
                f"<span style='color:#888;font-size:13px;font-weight:400'>({len(lst)} 只{note2})</span></h3>"
                f"<table style='border-collapse:collapse;font-size:14px;font-family:Segoe UI,'Microsoft YaHei',sans-serif;"
                f"box-shadow:0 1px 3px rgba(0,0,0,.12);border-radius:6px;overflow:hidden'>"
                f"{_table_head(cols, accent)}{body}</table>")

    full_tbl = "".join(_detail_row(r, i + 1) for i, r in enumerate(rows))

    legend = (
        "<div style='font-size:12px;color:#888;margin:8px 0'>"
        "<span style='color:#d93025;font-weight:600'>■ RSI6&gt;83 超买</span> &nbsp;·&nbsp; "
        "<span style='color:#188038;font-weight:600'>■ RSI6&lt;26 超卖</span> &nbsp;·&nbsp; "
        "<span style='color:#2b579a'>■ 其余正常</span> &nbsp;·&nbsp; 迷你条=RSI 数值(0-100)</div>"
    )

    # 简单可点击排序 (raw 字符串, 避免 \s 转义告警)
    sort_js = r"""
<script>
function sortTable(tbl, i){
  var tb=tbl.tBodies[0], rows=[].slice.call(tb.rows);
  var asc = tbl._asc===undefined ? true : !tbl._asc; tbl._asc=asc;
  rows.sort(function(a,b){
    var x=a.cells[i].innerText.replace(/[+%,\s]/g,''), y=b.cells[i].innerText.replace(/[+%,\s]/g,'');
    var nx=parseFloat(x), ny=parseFloat(y);
    var c = (!isNaN(nx)&&!isNaN(ny)) ? nx-ny : (x<y?-1:x>y?1:0);
    return asc?c:-c;
  });
  rows.forEach(function(r){tb.appendChild(r);});
}
</script>
<script>
function onReady(){
  var t=document.getElementById('fulltbl');
  if(!t)return; var ths=t.tHead.rows[0].cells;
  [].forEach.call(ths,function(th,i){ th.style.cursor='pointer';
    th.addEventListener('click',function(){sortTable(t,i);}); });
}
document.addEventListener('DOMContentLoaded', onReady);
</script>
"""

    html_doc = f"""<!DOCTYPE html><html><head><meta charset='utf-8'>
<title>美股自选 RSI 监控 · {date_tag}</title>
<style>body{{font-family:Segoe UI,'Microsoft YaHei','Noto Sans CJK JP',sans-serif;color:#222;margin:0}}
.wrap{{padding:20px 24px;max-width:1200px;margin:0 auto}}
h2{{color:{NAVY};margin-bottom:2px}}h3{{color:#444;margin:18px 0 4px}}
table{{width:100%}}</style></head>
<body><div class='wrap'>
<h2>美股自选 RSI(6) 监控日报 · {date_tag}</h2>
{note}{cards}
{status_block('⚠️ 超买(过热, 注意回调)', RED, ob)}
{status_block('📉 超卖(超跌, 可能反弹)', GREEN, os_)}
<h3 style='color:{NAVY};margin:26px 0 6px'>全部自选明细 ({n_total} 只, 点表头可排序)</h3>
{legend}
<table id='fulltbl' style='border-collapse:collapse;font-size:13px;font-family:Segoe UI,'Microsoft YaHei',sans-serif;box-shadow:0 1px 3px rgba(0,0,0,.12);border-radius:6px;overflow:hidden'>
{_table_head(['#','代码','腾讯码','名称','收盘','当日','RSI(6)','状态'], NAVY)}
<tbody>{full_tbl}</tbody></table>
<div style='margin:26px 0 40px;padding:12px 16px;background:#f5f7fa;border-radius:8px;font-size:13px;color:#555;line-height:1.8'>
<b>算法/口径</b><br>
① RSI(6): Wilder 平滑(EWMA, alpha=1/6), 基于腾讯前复权日K收盘价, 取最新一根; K线历史不足不纳入。<br>
② 判定: RSI6 &gt; 83 → <b style='color:{RED}'>超买</b>; RSI6 &lt; 26 → <b style='color:{GREEN}'>超卖</b>; 其余正常。<br>
③ 自选代码 → 腾讯码: 纳斯达克 .OQ / 纽交所 .N / 美交所与基金ETF .AM / B类股点号写法(如 BRK.B); 逐候选取首个有足够历史者。<br>
④ 数据来源: 腾讯行情(延迟行情), 最近一个完整交易日收盘 {data_date}(美东劳动节 09-07 休市)。FNMA(房利美) 已摘牌转OTC, 腾讯无历史K线, 不计入。
</div>
</div>{sort_js}</body></html>"""
    return html_doc


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("date", nargs="?", default="")
    ap.add_argument("--open", action="store_true", help="生成后自动打开浏览器")
    a = ap.parse_args()
    tag = a.date or datetime.now().strftime("%Y%m%d")
    pat = os.path.join(OUT_DIR, f"us_selected_rsi_{tag}.csv")
    matches = glob.glob(pat) or sorted(glob.glob(os.path.join(OUT_DIR, "us_selected_rsi_*.csv")))[-1:]
    if not matches:
        raise SystemExit("未找到 CSV, 先运行 monitor_us_selected.py")
    csv_path = matches[-1]
    if a.date and not os.path.exists(csv_path):
        csv_path = matches[-1]
    tag2 = os.path.basename(csv_path).replace("us_selected_rsi_", "").replace(".csv", "")
    doc = build(csv_path, tag2)
    os.makedirs(OUT_DIR, exist_ok=True)
    out = os.path.join(OUT_DIR, f"us_monitor_report_{tag2}.html")
    with open(out, "w", encoding="utf-8") as f:
        f.write(doc)
    print("已生成:", out)
    if a.open:
        import webbrowser
        webbrowser.open("file://" + out)


if __name__ == "__main__":
    main()
