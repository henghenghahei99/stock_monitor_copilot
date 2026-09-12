#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
美股自选 · 分时MACD「短线上涨衰减」扫描

口径(用户定义, 2026-09-12):
  分时K线: 30分钟 / 60分钟 / 120分钟 / 日线      (4 种)
  窗口    : 1 / 2 / 3 / 4 / 5 / 6 个交易日       (6 种)
  → 每只票 4×6 = 24 个组合

  最近新高 : 最近 W 个交易日内, 该分时K线的**最高价**那根
  上个新高 : 从最近窗口往前回溯、跳过最近窗口, 再往前 W 个交易日内最高价那根
  短线上涨衰减: 最近新高价 > 上个新高价(**必须是新高**), 且最近新高那根的
                MACD(12,26,9) DIF 与 DEA **都低于**上个新高那根 → 成立

数据源(两套, 用 --source 选择):
  [td] Twelve Data  (推荐, 默认优先)  api.twelvedata.com
       30分钟->30min / 60分钟->1h / 120分钟->2h(原生, 无需合成) / 日线->复用腾讯日K
       免费档: 800 次/日、8 次/分钟 -> 210 只 × 3 个分时周期 = 630 次(约 80 分钟跑完)
       key 放 ~/.twelvedata.json  {"apikey":"..."}  或环境变量 TWELVEDATA_API_KEY
  [em] 东财 push2his kline(klt=30/60, 前台复权), 120m 由 60m 两两合成
       注意: 东财对分钟级限流强, 且**实测(2026-09-12)本机 IP 已被 push2his/push2 封禁**
       (0.1s TCP 断开, 连它家日线也拒; datacenter.eastmoney.com 仍通 = 行情主机级封禁)

  其它美股分时源均已逐一验证不可用(2026-09-12):
    腾讯 web.ifzq/usfqkline m60/m5 + 控制器爆破 -> "bad params"/"Can't load controller"(不支持美股分钟线)
    腾讯 proxy.finance.qq.com mkline -> "param error"; 新浪 US_MinKService(scale=60/30) -> "Service not found/valid"
    雪球 stock.xueqiu.com -> 400(需 xq_a_token); 同花顺 d.10jqka 美股路径 -> 404
    富途 futunn quote-api -> 404; 东财 push2delay -> 仅元数据无K线
    Yahoo/WSJ/GoogleFinance/CNBC/investing/marketwatch/stooq分时 -> 一律 403/Errno 101(环境层屏蔽)
    公共 CORS 转代理(allorigins/codetabs/jina/corsproxy) -> 520/522/101(环境层屏蔽, 绕不过东财封禁)
    Nasdaq api: 分时仅当日 1 分钟, 带 fromdate/todate 只回日线

用法:
  cd US_selected_report
  python code/monitor_us_intraday_decay.py --cache            # 只拉取/缓存分时K
  python code/monitor_us_intraday_decay.py --limit 10         # 先小样本试跑
  python code/monitor_us_intraday_decay.py                    # 全量扫自选
  python code/monitor_us_intraday_decay.py --periods 60分钟,120分钟
  python code/monitor_us_intraday_decay.py --mode any         # 任一条线低即报
  python code/monitor_us_intraday_decay.py --source em        # 强制走东财(需 IP 未被封)

产物: output/us_selected_intraday_decay_YYYYMMDD.csv + us_intraday_decay_report_YYYYMMDD.html
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import ssl
import sys
import threading
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

import pandas as pd

CODE_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(CODE_DIR)                 # US_selected_report
REPO = os.path.dirname(ROOT)                     # stock_monitor_copilot
sys.path.insert(0, CODE_DIR)
sys.path.insert(0, os.path.join(REPO, "A_rank_report", "code"))

import monitor_us_selected as mus  # noqa: E402   (复用自选/代码映射/缓存)
import find_overbought_stocks as fos  # noqa: E402

OUT_DIR = os.path.join(ROOT, "output")
EM_CACHE_DIR = os.path.join(REPO, "data", "kline_cache_em")
TD_CACHE_DIR = os.path.join(REPO, "data", "kline_cache_td")

# 分时K线周期: 标签 -> (来源, 参数)
PERIODS_EM = [("30分钟", "em", 30), ("60分钟", "em", 60),
              ("120分钟", "from60", 120), ("日线", "tencent", 101)]
PERIODS_TD = [("30分钟", "td", "30min"), ("60分钟", "td", "1h"),
              ("120分钟", "td", "2h"), ("日线", "tencent", 101)]
PERIODS = PERIODS_TD
WINDOWS = [1, 2, 3, 4, 5, 6]                     # 交易日
MACD_FAST, MACD_SLOW, MACD_SIGNAL = 12, 26, 9
WARMUP = 40                                      # MACD 预热根数
DEFAULT_BARS = 320
TD_OUTPUTSIZE = 1000                             # Twelve Data 单次返回根数

# 东财市场码: 105 纳斯达克 / 106 纽交所 / 107 美交所(ETF 多在 107)
_EX_TO_MKT = {".OQ": 105, ".N": 106, ".AM": 107, ".O": 107}

# ---------------- 东财限流: 全局节流 ----------------
_em_lock = threading.Lock()
_em_last = [0.0]
EM_MIN_INTERVAL = 1.5        # 秒: 两次东财请求的最小间隔
_CTX = ssl.create_default_context()
_CTX.check_hostname = False
_CTX.verify_mode = ssl.CERT_NONE
_HDR = {"User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"),
        "Referer": "https://quote.eastmoney.com/", "Accept": "*/*"}


def _direct_get(url: str, timeout: int = 8) -> bytes:
    """直连东财(实测直连比代理池快很多; 代理仅作兜底)。"""
    req = urllib.request.Request(url, headers=_HDR)
    with urllib.request.urlopen(req, timeout=timeout, context=_CTX) as r:
        return r.read()

def _em_throttle() -> None:
    with _em_lock:
        wait = EM_MIN_INTERVAL - (time.time() - _em_last[0])
        if wait > 0:
            time.sleep(wait)
        _em_last[0] = time.time()


def em_klines(secid: str, klt: int, lmt: int = 500, force: bool = False) -> list[list[str]]:
    """东财分时/日线K线: 返回原始 klines([[时间,开,收,高,低,量,额,振幅], ...])。带磁盘缓存。"""
    os.makedirs(EM_CACHE_DIR, exist_ok=True)
    cf = os.path.join(EM_CACHE_DIR, f"{secid.replace('.', '_')}_{klt}.json")
    if not force and os.path.exists(cf):
        try:
            with open(cf, encoding="utf-8") as f:
                rows = json.load(f)
            if rows:
                return rows
        except Exception:  # noqa: BLE001
            pass
    url = (f"https://push2his.eastmoney.com/api/qt/stock/kline/get?secid={secid}"
           f"&fields1=f1,f2,f3,f4,f5,f6&fields2=f51,f52,f53,f54,f55,f56,f57,f58"
           f"&klt={klt}&fqt=1&beg=0&end=20500101&lmt={lmt}")
    last_exc = None
    for attempt in range(3):
        _em_throttle()
        raw = None
        try:
            raw = _direct_get(url, timeout=8)          # 1) 直连优先
        except Exception as exc_direct:  # noqa: BLE001
            try:
                raw = fos.http_get(url, timeout=8)     # 2) 代理池兜底
            except Exception as exc_proxy:  # noqa: BLE001
                last_exc = exc_proxy
                time.sleep(2 + 3 * attempt)
                continue
        try:
            j = json.loads(raw.decode("utf-8", "ignore"))
            rows = ((j.get("data") or {}).get("klines") or [])
            if rows:
                with open(cf, "w", encoding="utf-8") as f:
                    json.dump(rows, f)
                return rows
            last_exc = RuntimeError(f"空数据 rc={j.get('rc')}")
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
        time.sleep(2 + 3 * attempt)
    raise last_exc if last_exc else RuntimeError("东财取数失败")


# ---------------- Twelve Data ----------------
_td_lock = threading.Lock()
_td_last = [0.0]
TD_MIN_INTERVAL = 7.6        # 免费档 8 次/分钟
_TD_KEY_FILES = [os.path.expanduser("~/.twelvedata.json"),
                 os.path.join(REPO, "data", "twelvedata.json")]
_td_key_cache = [None]


def td_apikey() -> str:
    """优先环境变量, 其次 ~/.twelvedata.json / data/twelvedata.json 的 {"apikey": "..."}。"""
    if _td_key_cache[0] is not None:
        return _td_key_cache[0]
    key = (os.environ.get("TWELVEDATA_API_KEY") or os.environ.get("TD_API_KEY") or "").strip()
    if not key:
        for f in _TD_KEY_FILES:
            try:
                with open(f, encoding="utf-8") as fh:
                    key = str(json.load(fh).get("apikey", "")).strip()
                if key:
                    break
            except Exception:  # noqa: BLE001
                continue
    _td_key_cache[0] = key
    return key


def _td_throttle() -> None:
    with _td_lock:
        wait = TD_MIN_INTERVAL - (time.time() - _td_last[0])
        if wait > 0:
            time.sleep(wait)
        _td_last[0] = time.time()


def td_klines(symbol: str, interval: str, outputsize: int = TD_OUTPUTSIZE,
              force: bool = False) -> list[dict]:
    """Twelve Data time_series -> [{'datetime','open','high','low','close'}, ...]。

    时间用 timezone=America/New_York, 所以 datetime 已是美东时间, 日期即美东交易日。
    """
    os.makedirs(TD_CACHE_DIR, exist_ok=True)
    safe = symbol.replace("/", "_").replace(" ", "")
    cf = os.path.join(TD_CACHE_DIR, f"{safe}_{interval}.json")
    if not force and os.path.exists(cf):
        try:
            with open(cf, encoding="utf-8") as f:
                rows = json.load(f)
            if rows:
                return rows
        except Exception:  # noqa: BLE001
            pass
    key = td_apikey()
    if not key:
        raise RuntimeError("缺少 Twelve Data API key(见 ~/.twelvedata.json)")
    url = (f"https://api.twelvedata.com/time_series?symbol={urllib.parse.quote(symbol)}"
           f"&interval={interval}&outputsize={outputsize}&order=ASC"
           f"&timezone=America/New_York&apikey={urllib.parse.quote(key)}")
    last_exc = None
    for attempt in range(4):
        _td_throttle()
        try:
            req = urllib.request.Request(url, headers={"User-Agent": _HDR["User-Agent"],
                                                       "Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=25, context=_CTX) as r:
                j = json.loads(r.read().decode("utf-8", "ignore"))
            if j.get("status") == "error":
                code = j.get("code")
                msg = str(j.get("message", ""))
                last_exc = RuntimeError(f"twelvedata error {code}: {msg[:60]}")
                if code in (429, 401, 403) or "limit" in msg.lower():
                    time.sleep(8 + 10 * attempt)          # 限流/无权限 -> 退避
                    continue
                raise last_exc
            rows = j.get("values") or []
            if rows:
                with open(cf, "w", encoding="utf-8") as f:
                    json.dump(rows, f)
                return rows
            last_exc = RuntimeError("twelvedata 空数据")
        except RuntimeError:
            raise
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
        time.sleep(3 + 5 * attempt)
    raise last_exc if last_exc else RuntimeError("twelvedata 取数失败")


def td_df(rows: list[dict]) -> pd.DataFrame:
    """Twelve Data values -> DataFrame(index=DatetimeIndex(美东), 列 open/close/high/low/date_us)。"""
    recs = []
    for r in rows:
        try:
            ts = pd.Timestamp(str(r.get("datetime", "")).strip())
            recs.append({"ts": ts, "open": float(r["open"]), "close": float(r["close"]),
                         "high": float(r["high"]), "low": float(r["low"])})
        except Exception:  # noqa: BLE001
            continue
    if not recs:
        return pd.DataFrame()
    df = pd.DataFrame(recs).set_index("ts").sort_index()
    df = df[~df.index.duplicated(keep="last")]
    df["date_us"] = [str(t.date()) for t in df.index]     # 美东时间, 直接取日期
    return df


def em_secid_candidates(prefixed: str) -> list[str]:
    """'usAAPL.OQ' -> ['105.AAPL', ...](按交易所后缀优先, 其余市场码兜底)。"""
    sym = prefixed[2:] if prefixed[:2].lower() == "us" else prefixed
    suf = ""
    for s in (".OQ", ".N", ".AM", ".O"):
        if sym.upper().endswith(s):
            suf, sym = s, sym[: -len(s)]
            break
    sym = sym.replace(".", "_").upper()
    pref = _EX_TO_MKT.get(suf.upper())
    order = ([pref] if pref else []) + [m for m in (105, 106, 107) if m != pref]
    return [f"{m}.{sym}" for m in order]


def _us_date(ts: pd.Timestamp) -> str:
    """东财美股分时用北京时间标注(如 22:30 属当日、次日 04:00 仍属前一日) -> 归到美股交易日。"""
    d = ts.normalize()
    if ts.hour < 12:
        d -= pd.Timedelta(days=1)
    return str(d.date())


def _bars_to_df(rows: list) -> pd.DataFrame:
    """东财 klines -> DataFrame(index=DatetimeIndex(北京), 列 open/close/high/low/date_us)。

    注意: 东财每条是**逗号分隔的字符串** "时间,开,收,高,低,量,额,振幅"(不是数组)。
    """
    recs = []
    for r in rows:
        parts = r.split(",") if isinstance(r, str) else list(r)
        if len(parts) < 5:
            continue
        try:
            ts = pd.Timestamp(str(parts[0]).strip())
        except Exception:  # noqa: BLE001
            continue
        try:
            recs.append({"ts": ts, "open": float(parts[1]), "close": float(parts[2]),
                         "high": float(parts[3]), "low": float(parts[4])})
        except (TypeError, ValueError):
            continue
    if not recs:
        return pd.DataFrame()
    df = pd.DataFrame(recs).set_index("ts").sort_index()
    df = df[~df.index.duplicated(keep="last")]
    df["date_us"] = [_us_date(t) for t in df.index]
    return df


def aggregate_120(df60: pd.DataFrame) -> pd.DataFrame:
    """60分钟 在同一美股交易日内两两合成 120分钟(首开/末收/最高/最低)。"""
    if df60.empty:
        return df60
    out = []
    for d, g in df60.groupby("date_us", sort=True):
        g = g.sort_index()
        i = 0
        while i < len(g):
            two = g.iloc[i:i + 2]
            i += 2
            out.append({"ts": two.index[0], "open": float(two["open"].iloc[0]),
                        "close": float(two["close"].iloc[-1]),
                        "high": float(two["high"].max()),
                        "low": float(two["low"].min()), "date_us": d})
    if not out:
        return pd.DataFrame()
    return pd.DataFrame(out).set_index("ts").sort_index()


def macd(df: pd.DataFrame) -> pd.DataFrame:
    """DIF/DEA(MACD 12,26,9)。"""
    c = df["close"].astype(float)
    dif = c.ewm(span=MACD_FAST, adjust=False).mean() - c.ewm(span=MACD_SLOW, adjust=False).mean()
    dea = dif.ewm(span=MACD_SIGNAL, adjust=False).mean()
    out = df.copy()
    out["dif"], out["dea"] = dif, dea
    return out


def scan_period(df: pd.DataFrame, label: str, mode: str,
                require_cross: bool = True) -> list[dict]:
    """在给定分时K线上, 遍历所有窗口, 返回命中的「短线上涨衰减」。

    require_cross=True: 两个背离点(上个新高 -> 最近新高)之间, 必须**先出现死叉
    (DIF 下穿 DEA)、后出现金叉(DIF 上穿 DEA)** —— 即先破位回调、再金叉重新走强,
    最后才创出更高的新高, 这一轮才算真背离(死叉/金叉都在两点之间, 不含端点)。
    """
    if df.empty or len(df) < WARMUP + 10:
        return []
    df = macd(df)
    dates = list(dict.fromkeys(df["date_us"]))          # 按时间升序的美股交易日
    n_days = len(dates)
    pos_of_day = {d: i for i, d in enumerate(dates)}
    day_idx = [pos_of_day[d] for d in df["date_us"]]    # 每根 bar 落在第几个交易日
    highs = df["high"].to_numpy()
    difv = df["dif"].to_numpy()
    deav = df["dea"].to_numpy()
    closev = df["close"].to_numpy()
    # 交叉点(下标 = 发生交叉的那根 bar)
    _dd = difv - deav
    gold_i = [i + 1 for i in range(len(_dd) - 1) if _dd[i] <= 0 < _dd[i + 1]]
    dead_i = [i + 1 for i in range(len(_dd) - 1) if _dd[i] > 0 >= _dd[i + 1]]
    res = []
    for w in WINDOWS:
        if n_days < 2 * w:
            continue
        cur = [i for i, di in enumerate(day_idx) if n_days - w <= di < n_days]
        prv = [i for i, di in enumerate(day_idx) if n_days - 2 * w <= di < n_days - w]
        if not cur or not prv:
            continue
        i_cur = max(cur, key=lambda i: highs[i])
        i_prv = max(prv, key=lambda i: highs[i])
        if i_prv < WARMUP or i_cur < WARMUP:
            continue
        if highs[i_cur] <= highs[i_prv]:           # 必须真的是「新高」
            continue
        g_in = [i for i in gold_i if i_prv < i < i_cur]
        d_in = [i for i in dead_i if i_prv < i < i_cur]
        i_dead = i_gold = -1
        if require_cross:
            # 必须「先死叉、后金叉」（存在一对 d < g）
            if not d_in or not g_in or min(d_in) > max(g_in):
                continue
            i_dead = min(d_in)
            i_gold = min(g for g in g_in if g > i_dead)
        dl, el = difv[i_cur] < difv[i_prv], deav[i_cur] < deav[i_prv]
        hit = (dl and el) if mode == "both" else (dl or el)
        if not hit:
            continue
        kind = "双线衰减" if (dl and el) else ("仅快线衰减" if dl else "仅慢线衰减")
        res.append({
            "period": label, "window": w,
            "gold_time": str(df.index[i_gold]) if i_gold >= 0 else "",
            "dead_time": str(df.index[i_dead]) if i_dead >= 0 else "",
            "n_gold": len(g_in), "n_dead": len(d_in),
            "cur_time": str(df.index[i_cur]), "cur_high": round(float(highs[i_cur]), 2),
            "cur_close": round(float(closev[i_cur]), 2),
            "prv_time": str(df.index[i_prv]), "prv_high": round(float(highs[i_prv]), 2),
            "high_gain%": round((highs[i_cur] / highs[i_prv] - 1) * 100, 2),
            "dif": round(float(difv[i_cur]), 3), "prv_dif": round(float(difv[i_prv]), 3),
            "dif_gap": round(float(difv[i_cur] - difv[i_prv]), 3),
            "dea": round(float(deav[i_cur]), 3), "prv_dea": round(float(deav[i_prv]), 3),
            "dea_gap": round(float(deav[i_cur] - deav[i_prv]), 3),
            "kind": kind, "dif_lower": int(dl), "dea_lower": int(el),
        })
    return res


def fetch_bars(p: dict, period: str, src: str, klt, bars: int,
               cache_only: bool) -> pd.DataFrame:
    """取某周期的K线 DataFrame(含 date_us)。src: tencent(日线) / em(东财分时) / td(Twelve Data)。"""
    if src == "tencent":
        if not p.get("prefixed"):
            mus._fetch_best(p, bars)
        if not p.get("prefixed"):
            return pd.DataFrame()
        k = fos.tencent_kline(p["prefixed"], bars, use_cache=True)
        if k is None:
            return pd.DataFrame()
        close = k[0].dropna()
        df = pd.DataFrame({"open": close, "close": close, "high": close, "low": close})
        df["date_us"] = [str(t.date()) for t in df.index]
        return df
    if src == "td":
        sym = (p.get("ticker") or "").replace(".", ".").strip().upper()
        if not sym:
            return pd.DataFrame()
        try:
            return td_df(td_klines(sym, klt))
        except Exception:  # noqa: BLE001
            return pd.DataFrame()
    # 东财
    for secid in em_secid_candidates(p["prefixed"] or mus._fetch_best(p, bars) or ""):
        if not secid or secid.endswith("."):
            continue
        try:
            rows = em_klines(secid, klt, lmt=500)
        except Exception:  # noqa: BLE001
            continue
        df = _bars_to_df(rows)
        if not df.empty:
            return df
    return pd.DataFrame()


def scan_one(p: dict, bars: int, mode: str, periods: list, windows: list,
             cache_only: bool, require_cross: bool = True) -> list[dict]:
    if not p.get("prefixed"):
        mus._fetch_best(p, bars)
    if not p.get("prefixed"):
        return []
    out: list[dict] = []
    df60 = None
    for label, src, klt in periods:
        try:
            if src == "from60":
                if df60 is None:
                    df60 = fetch_bars(p, label, "em", 60, bars, cache_only)
                df = aggregate_120(df60)
            else:
                df = fetch_bars(p, label, src, klt, bars, cache_only)
                if src == "em" and klt == 60:
                    df60 = df
            if df.empty:
                continue
            for r in scan_period(df, label, mode, require_cross):
                if r["window"] in windows or not windows:
                    r.update({"code": p["code"], "ticker": p["ticker"],
                              "name": p.get("name", ""), "bars": len(df)})
                    out.append(r)
        except Exception:  # noqa: BLE001
            continue
    return out


# ---------------- HTML ----------------
NAVY = "#2b579a"; NAVY_D = "#1d3f6e"
WARN = "#d93025"; WARN_BG = "#fdecea"
MID = "#e65100"; MID_BG = "#fdeee2"
GREY = "#666"; ROW_BD = "#e3e9f2"
PCOLOR = {"30分钟": "#7b1fa2", "60分钟": NAVY, "120分钟": "#e65100", "日线": "#0b6e3a"}


def _esc(s) -> str:
    import html as _h
    return _h.escape(str(s))


def _th(t):
    return (f"<th style='padding:8px 10px;border:1px solid {NAVY_D};text-align:left;"
            f"white-space:nowrap'>{t}</th>")


def _badge(kind):
    import html as _h
    c, bg = {"双线衰减": (WARN, WARN_BG)}.get(kind, (MID, MID_BG))
    return (f"<span style='display:inline-block;padding:2px 9px;border-radius:11px;font-size:12px;"
            f"color:{c};background:{bg};font-weight:600'>{_h.escape(kind)}</span>")


def write_html(rows, date_tag, watch_total, mode, periods, windows, stats, src="td",
               require_cross=True):
    import html as _h
    cols = ["#", "代码", "腾讯码", "名称", "分时K", "窗口(日)", "最近新高时间", "新高价",
            "上个新高时间", "前高价", "新高幅度", "DIF", "前高DIF", "DIF差",
            "DEA", "前高DEA", "DEA差", "衰减类型"]
    head = ("<tr style='background:" + NAVY + ";color:#fff'>" + "".join(_th(c) for c in cols) + "</tr>")
    td = f"padding:6px 10px;border:1px solid {ROW_BD}"
    body = []
    for i, r in enumerate(rows):
        bg = "#ffffff" if i % 2 == 0 else "#f7f9fc"
        body.append(
            f"<tr style='background:{bg}'><td style='{td}'>{i+1}</td>"
            f"<td style='{td}'>{_h.escape(r['ticker'])}</td>"
            f"<td style='{td}'>{_h.escape(r['code'])}</td>"
            f"<td style='{td}'>{_h.escape(r['name'] or '')}</td>"
            f"<td style='{td};color:#fff;background:{PCOLOR.get(r['period'], GREY)};font-weight:600'>{r['period']}</td>"
            f"<td style='{td};text-align:center'>{r['window']}</td>"
            f"<td style='{td}'>{r['cur_time']}</td>"
            f"<td style='{td};text-align:right'>{r['cur_high']:.2f}</td>"
            f"<td style='{td}'>{r['prv_time']}</td>"
            f"<td style='{td};text-align:right'>{r['prv_high']:.2f}</td>"
            f"<td style='{td};text-align:right;color:{WARN};font-weight:600'>{r['high_gain%']:+.2f}%</td>"
            f"<td style='{td};text-align:right'>{r['dif']:.3f}</td>"
            f"<td style='{td};text-align:right'>{r['prv_dif']:.3f}</td>"
            f"<td style='{td};text-align:right'><b style='color:{WARN}'>{r['dif_gap']:+.3f}</b></td>"
            f"<td style='{td};text-align:right'>{r['dea']:.3f}</td>"
            f"<td style='{td};text-align:right'>{r['prv_dea']:.3f}</td>"
            f"<td style='{td};text-align:right'><b style='color:{WARN}'>{r['dea_gap']:+.3f}</b></td>"
            f"<td style='{td}'>{_badge(r['kind'])}</td></tr>")

    cards = ""
    for lab, val, accent, bg in (("自选总数", watch_total, GREY, "#f1f3f4"),
                                 ("命中组合", len(rows), WARN, WARN_BG),
                                 ("涉及股票", stats["stocks"], NAVY, "#f0f4fa"),
                                 ("分时K×窗口", f"{len(periods)}×{len(windows)}", "#7b1fa2", "#f3e8fa")):
        cards += (f"<div style='background:{bg};border:1px solid {accent}33;border-radius:8px;"
                  f"padding:12px 18px;min-width:130px'><div style='font-size:12px;color:#555'>{lab}</div>"
                  f"<div style='font-size:24px;font-weight:700;color:{accent}'>{val}</div></div>")
    cards = f"<div style='display:flex;gap:12px;flex-wrap:wrap;margin:14px 0'>{cards}</div>"

    per = ""
    for lab, _, _ in periods:
        k = len([r for r in rows if r["period"] == lab])
        per += (f"<span style='display:inline-block;margin-right:14px;padding:3px 10px;border-radius:10px;"
                f"background:{PCOLOR.get(lab, GREY)}22;color:{PCOLOR.get(lab, GREY)};font-weight:600'>"
                f"{lab}: {k}</span>")
    mode_txt = "快线+慢线都低于前高" if mode == "both" else "快线或慢线任一低于前高"
    cross_txt = ("且两背离点之间<b>先死叉、后金叉</b>(破位回调后再走强)" if require_cross
                 else "(未启用「先死叉后金叉」过滤)")
    if src == "td":
        src_txt = "分时数据来自 Twelve Data(美东时间, 30min/1h/2h) · 日线来自腾讯"
        line3 = "③ 120分钟 = Twelve Data 原生 2h K线。"
    else:
        src_txt = "分时数据来自东财(北京时间标注, 已归到美股交易日) · 日线来自腾讯"
        line3 = "③ 120分钟由 60分钟 在同一美股交易日内两两合成(东财无美股 120m 接口)。"
    note = (f"<div style='background:#fff8e1;border:1px solid #ffe082;border-radius:6px;padding:8px 14px;"
            f"font-size:13px;color:#8d6e00;margin:10px 0'>"
            f"口径: 分时K {len(periods)} 种 × 窗口 {len(windows)} 种 = 每只 {len(periods)*len(windows)} 个组合 · "
            f"最近 W 日新高价 &gt; 再往前 W 日新高价 且 {mode_txt} → 短线上涨衰减 · "
            f"MACD(12,26,9) · {cross_txt} · {src_txt}</div>")

    doc = f"""<!DOCTYPE html><html><head><meta charset='utf-8'>
<title>美股自选 分时MACD 短线上涨衰减 · {date_tag}</title>
<style>body{{font-family:Segoe UI,'Microsoft YaHei','Noto Sans CJK JP',sans-serif;color:#222;margin:0}}
.wrap{{padding:20px 24px;max-width:1750px;margin:0 auto}}h2{{color:{NAVY};margin-bottom:2px}}
table{{width:100%;border-collapse:collapse}}</style></head>
<body><div class='wrap'>
<h2>美股自选 · 分时MACD 短线上涨衰减 · {date_tag}</h2>
{note}{cards}
<div style='margin:6px 0 12px'>{per}</div>
<table style='font-size:12.5px;box-shadow:0 1px 3px rgba(0,0,0,.12);border-radius:6px;overflow:hidden'>
<thead>{head}</thead><tbody>{''.join(body) if body else ''}</tbody></table>
{'<div style="color:#888;padding:14px 0">本次没有任何组合命中</div>' if not body else ''}
<div style='margin:26px 0 40px;padding:12px 16px;background:#f5f7fa;border-radius:8px;font-size:13px;color:#555;line-height:1.8'>
<b>说明</b><br>
① <b>最近新高</b> = 最近 W 个交易日内该分时K线最高价那根; <b>上个新高</b> = 跳过最近窗口后, 再往前 W 个交易日内的最高价那根。<br>
② 必须 <b>最近新高价 &gt; 上个新高价</b> 才算“新高”; 否则该组合不成立。<br>
{line3}<br>
④ 同一只票可命中多个「分时K × 窗口」组合 —— 命中越多, 说明背离在多个尺度上共振。<br>
⑤ 顶背离是<b>减仓/止盈提示</b>, 不代表立刻下跌。</div>
</div></body></html>"""
    return doc


def main() -> None:
    global TD_MIN_INTERVAL
    ap = argparse.ArgumentParser(description="美股自选 分时MACD 短线上涨衰减扫描")
    ap.add_argument("--cache", action="store_true", help="只拉取/缓存分时K线, 不判定")
    ap.add_argument("--limit", type=int, default=0, help="只扫前 N 只(调试)")
    ap.add_argument("--bars", type=int, default=DEFAULT_BARS, help="日线K线根数(腾讯)")
    ap.add_argument("--mode", choices=["both", "any"], default="both")
    ap.add_argument("--workers", type=int, default=4, help="并发(东财限流, 建议<=4)")
    ap.add_argument("--periods", default="", help="只看这些分时K, 逗号分隔(如 60分钟,日线)")
    ap.add_argument("--windows", default="", help="只看这些窗口(如 3,4,5)")
    ap.add_argument("--source", choices=["auto", "td", "em"], default="auto",
                    help="分时数据源: auto=有TwelveData key用td否则em; td=Twelve Data; em=东财")
    ap.add_argument("--td-interval-sec", type=float, default=TD_MIN_INTERVAL,
                    help=f"Twelve Data 请求最小间隔秒(免费档8次/分, 默认{TD_MIN_INTERVAL})")
    ap.add_argument("--no-cross", action="store_true",
                    help="关闭「两背离点之间必须先死叉后金叉」过滤(默认开启)")
    a = ap.parse_args()
    require_cross = not a.no_cross

    src = a.source
    if src == "auto":
        src = "td" if td_apikey() else "em"
    TD_MIN_INTERVAL = a.td_interval_sec
    base = PERIODS_TD if src == "td" else PERIODS_EM
    periods = base
    if a.periods:
        want = {x.strip() for x in a.periods.split(",") if x.strip()}
        periods = [p for p in base if p[0] in want]
    windows = [int(x) for x in a.windows.split(",") if x.strip().isdigit()] or WINDOWS

    watch = mus.load_watch()
    if a.limit:
        watch = watch[:a.limit]
    n_req = len(watch) * len([p for p in periods if p[1] == "td"])
    if src == "td" and n_req:
        print(f"[信息] Twelve Data key: {'已就绪' if td_apikey() else '缺失(见 ~/.twelvedata.json)'}"
              f" | 预计 {n_req} 次请求 ≈ {n_req * TD_MIN_INTERVAL / 60:.0f} 分钟"
              f"(免费档限 800次/日、8次/分)")
    print(f"[信息] 自选美股 {len(watch)} 只 | 源={src} | 分时K {[p[0] for p in periods]} | "
          f"窗口 {windows} | mode={a.mode} | 先死叉后金叉过滤={'开' if require_cross else '关'}")

    if a.cache:
        ok = 0
        with ThreadPoolExecutor(max_workers=a.workers) as ex:
            futs = [ex.submit(scan_one, p, a.bars, a.mode, periods, [], True, require_cross)
                    for p in watch]
            for i, _ in enumerate(as_completed(futs), 1):
                ok += 1
                if i % 20 == 0 or i == len(watch):
                    print(f"[缓存] {i}/{len(watch)}", file=sys.stderr)
        print(f"[完成] 分时K缓存 -> {EM_CACHE_DIR}")
        return

    rows: list[dict] = []
    n_data = 0
    with ThreadPoolExecutor(max_workers=a.workers) as ex:
        futs = [ex.submit(scan_one, p, a.bars, a.mode, periods, windows, False, require_cross)
                for p in watch]
        for i, f in enumerate(as_completed(futs), 1):
            try:
                r = f.result()
            except Exception:  # noqa: BLE001
                r = []
            if r:
                n_data += 1
                rows.extend(r)
            if i % 10 == 0 or i == len(watch):
                print(f"[进度] {i}/{len(watch)} 只  命中组合 {len(rows)}", file=sys.stderr, flush=True)

    rows.sort(key=lambda r: (r["kind"] != "双线衰减", r["ticker"], r["period"], r["window"]))
    print(f"\n命中 {len(rows)} 个「分时K×窗口」组合, 涉及 {n_data} 只股票")
    for r in rows[:40]:
        print(f"  {r['code']:<9} {(r['name'] or '')[:14]:<16} {r['period']:<6} 窗口{r['window']}日  "
              f"新高 {r['cur_time']} {r['cur_high']:.2f} (+{r['high_gain%']:.2f}% vs {r['prv_time']} {r['prv_high']:.2f})  "
              f"DIF {r['dif']:+.3f}/{r['prv_dif']:+.3f}({r['dif_gap']:+.3f}) "
              f"DEA {r['dea']:+.3f}/{r['prv_dea']:+.3f}({r['dea_gap']:+.3f})  {r['kind']}")
    if len(rows) > 40:
        print(f"  ... 共 {len(rows)} 条")

    os.makedirs(OUT_DIR, exist_ok=True)
    tag = datetime.now().strftime("%Y%m%d")
    csv_path = os.path.join(OUT_DIR, f"us_selected_intraday_decay_{tag}.csv")
    fields = ["code", "ticker", "name", "period", "window", "cur_time", "cur_high",
              "cur_close", "prv_time", "prv_high", "high_gain%", "dif", "prv_dif",
              "dif_gap", "dea", "prv_dea", "dea_gap", "kind", "gold_time", "dead_time",
              "n_gold", "n_dead", "dif_lower", "dea_lower", "bars"]
    with open(csv_path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow(r)
    print(f"\n明细已存: {csv_path}")
    html_path = os.path.join(OUT_DIR, f"us_intraday_decay_report_{tag}.html")
    with open(html_path, "w", encoding="utf-8") as f:
        f.write(write_html(rows, tag, len(watch), a.mode, periods, windows,
                           {"stocks": n_data}, src, require_cross))
    print("报告已生成:", html_path)


if __name__ == "__main__":
    main()
