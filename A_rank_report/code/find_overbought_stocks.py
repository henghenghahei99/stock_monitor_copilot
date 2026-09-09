#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
扫描美股/A股/港股, 用腾讯财经(gtimg)免费接口找出 RSI(6) 大于指定阈值(默认 80)的超买股票。

数据源: 腾讯财经接口, 国内直连可用、免费、无需注册。
  - 报价:   https://qt.gtimg.cn/q=usAAPL | sh600519 | hk00700
  - 日K(前复权): usfqkline(美股) / fqkline(A股港股)

用法示例:
    python find_overbought_stocks.py --market us               # 美股(默认)
    python find_overbought_stocks.py --market cn --list all   # A股(沪深北)
    python find_overbought_stocks.py --market hk --list all   # 港股
    python find_overbought_stocks.py --market us --list github # GitHub全美股代码(约7000只)
    python find_overbought_stocks.py --market cn --list builtin
    python find_overbought_stocks.py --update-list --market cn  # 更新A股代码缓存
    python find_overbought_stocks.py --tickers sh600519,hk00700
    python find_overbought_stocks.py --rsi 85 --period 120    # 自定义阈值/周期
    python find_overbought_stocks.py --limit 100 --workers 10  # 并发扫描前100只
    python find_overbought_stocks.py --min-price 10 --min-volume 500000  # 更严过滤
    python find_overbought_stocks.py --keep-shells           # 不过滤壳股/权证

说明:
    内置过滤: 剔除价格<5美元、近20日均成交额<1000万美金、最新K线>7天、
    名称含壳股/权证/优先股关键词的标的。
    行业列来自本地 nasdaq_full.json/nyse_full.json/amex_full.json (纳斯达克官方 screener)。

说明:
    美股代码列表来源(国内可访问):
      - GitHub (rreichel3/US-Stock-Symbols, 每日更新): 纳斯达克+纽交所+美交所, 约7000只
      - 东方财富 push2 API: 按总市值排序, 但可能被临时限流
      - 内置约500只: 完全离线, 永远可用
    首次用 --list github 会自动下载并缓存到 us_tickers.txt, 之后离线读取。

依赖:
    pip install pandas
"""

from __future__ import annotations

import argparse
import itertools
import json
import os
import re
import sys
import threading
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date
from typing import Optional

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from translations import translate  # noqa: E402  行业/板块中英翻译

DEFAULT_RSI_PERIOD = 6             # RSI 周期
DEFAULT_RSI_THRESHOLD = 80.0       # 超买阈值
DEFAULT_MIN_BARS = 30              # 至少需要多少根K线才算有效
DEFAULT_BARS = 320                 # 拉取多少根日K(320 ≈ 一年多的交易日)
DEFAULT_WORKERS = 8                # 并发拉取线程数
MAX_RETRIES = 5                    # 单个请求失败重试次数(腾讯偶尔限流501, 加重试自愈)
DEFAULT_MIN_PRICE = 1.0            # 剔除仙股: A股价格门槛≥1元(与美股$1一致)
DEFAULT_MIN_VOLUME = 10_000_000   # 剔除近20日均成交额(市场货币)低于该值的股票(默认1000万)
DEFAULT_MIN_SHARES = 100_000      # 剔除近20日均成交量(股)低于该值的股票(默认10万股)
DEFAULT_MAX_STALE_DAYS = 7         # 最新K线距今超过该天数视为停牌/退市, 剔除

# 壳股/权证/优先股名称关键词(命中即剔除, 可用 --keep-shells 关闭)
SHELL_KEYWORDS = [
    "ACQUISITION", "SPAC", "BLANK CHECK", "MERGER", "UNIT", "WARRANT",
    "WTS", "TO PUR", "RIGHT", "RIGHTS", "PFD", "PREFERRED", "CUM RED",
    "CUM PREF", "SER A", "SER B", "SER C", "SER D",
]

# 本地 板块/行业/市值 信息缓存(纳斯达克官方 screener 字段, 已下载到本目录)
FULL_INFO_FILES = ["nasdaq_full.json", "nyse_full.json", "amex_full.json"]

# 腾讯财经接口 (市场前缀: us/sh/sz/bj/hk, 如 usAAPL / sh600519 / hk00700)
TENCENT_QUOTE_URL = "https://qt.gtimg.cn/q={}"

# 东方财富列表接口(按总市值排序, 每页最多100; f100/f127=行业)
EASTMONEY_LIST_URL = (
    "https://push2.eastmoney.com/api/qt/clist/get"
    "?pn={page}&pz=100&po=1&np=1"
    "&ut=bd1d9ddb04089700cf9c27f6f7426281&fltt=2&invt=2"
    "&fid=f20&fs={fs}&fields=f12,f14,f100,f127"
)
EASTMONEY_FS = {
    "us": "m:105,m:106,m:107",
    "nasdaq": "m:105",
    "cn": "m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23",  # 深主板+创业板+沪主板+科创板
    "hk": "m:128",
}

# 新浪A股列表(可靠, 带 sh/sz/bj 前缀, 每页100)
SINA_CN_URL = (
    "https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/"
    "Market_Center.getHQNodeData?page={page}&num=100&sort=symbol&asc=1&node=hs_a"
)
MARKET_NAMES = {"us": "美股", "cn": "A股", "hk": "港股"}
# A股成交量单位为手(1手=100股), 需换算成股数后再算成交额
VOLUME_MULT = {"us": 1.0, "cn": 100.0, "hk": 1.0}
# 目录结构: code/(代码)  data/../..共享data  output/(结果)
BASE_DIR = os.path.dirname(os.path.abspath(__file__))   # code/
ROOT_DIR = os.path.dirname(BASE_DIR)                    # A_rank_report/
DATA_DIR = os.path.join(os.path.dirname(ROOT_DIR), "data")   # ../data 共享(其它策略共用)
OUTPUT_DIR = os.path.join(ROOT_DIR, "output")          # A_rank_report/output/
os.makedirs(DATA_DIR, exist_ok=True)
os.makedirs(OUTPUT_DIR, exist_ok=True)

# K线本地缓存(避免重复拉取触发限流; 同一代码+根数一天内只拉一次)
KLINE_CACHE_DIR = os.path.join(DATA_DIR, "kline_cache")
KLINE_CACHE_TTL_DAYS = 1
# 报价本地缓存(全量扫描时报价请求量巨大, 一天一拉避免反复触发限流)
QUOTE_CACHE_DIR = os.path.join(DATA_DIR, "quote_cache")
QUOTE_CACHE_TTL_DAYS = 1
_REQUEST_DELAY = 0.0  # 每个标的请求前延时(秒), 用 --delay 控制, 首次全量时建议设 0.1~0.3
_USE_CACHE = True      # K线本地缓存开关, 用 --no-cache 关闭
CN_TICKER_FILE = os.path.join(DATA_DIR, "cn_tickers.txt")
HK_TICKER_FILE = os.path.join(DATA_DIR, "hk_tickers.txt")

# GitHub 美股代码列表 (rreichel3/US-Stock-Symbols, 每日更新)
GITHUB_TICKER_URLS = [
    "https://raw.githubusercontent.com/rreichel3/US-Stock-Symbols/main/nasdaq/nasdaq_tickers.txt",
    "https://raw.githubusercontent.com/rreichel3/US-Stock-Symbols/main/nyse/nyse_tickers.txt",
    "https://raw.githubusercontent.com/rreichel3/US-Stock-Symbols/main/amex/amex_tickers.txt",
]
LOCAL_TICKER_FILE = os.path.join(DATA_DIR, "us_tickers.txt")

# 内置默认扫描池: 约500只流动性较好的美股(跨行业, 不依赖外部列表接口)
DEFAULT_TICKERS = [
    # 科技/通信 (80)
    "AAPL", "MSFT", "NVDA", "GOOGL", "GOOG", "AMZN", "META", "TSLA",
    "AVGO", "ORCL", "ADBE", "CRM", "CSCO", "AMD", "INTC", "QCOM",
    "TXN", "MU", "AMAT", "LRCX", "ACN", "ADI", "ADP", "NOW", "INTU",
    "IBM", "NFLX", "PLTR", "SNOW", "CRWD", "PANW", "MRVL", "KLAC",
    "ASML", "ARM", "SMCI", "DELL", "HPQ", "HPE", "STX", "WDC",
    "UBER", "PYPL", "ABNB", "DDOG", "NET", "ZS", "TEAM", "WDAY",
    "EA", "TTWO", "SPOT", "SNAP", "SHOP", "RBLX", "SE", "TSM",
    "SQ", "COIN", "HOOD", "OKTA", "MDB", "ANET", "ENPH", "FSLR",
    "ROP", "GLW", "NTAP", "VRSN", "ZM", "DOCU", "BILL", "HUBS",
    "PINS", "ROKU", "PATH", "TWLO", "TTD",
    # 消费 (60)
    "WMT", "COST", "HD", "LOW", "PG", "KO", "PEP", "MCD", "SBUX",
    "NKE", "DIS", "CMCSA", "T", "VZ", "TMUS", "LULU", "YUM", "DASH",
    "TGT", "DG", "DLTR", "KR", "MDLZ", "KHC", "GIS", "K", "CL", "EL",
    "PM", "MO", "STZ", "MNST", "CHD", "HSY", "KMB", "TSCO", "BBY",
    "ROST", "TJX", "EBAY", "ETSY", "CVNA", "MGM", "LVS", "MAR",
    "HLT", "BKNG", "EXPE", "RCL", "CCL", "WBD", "PARA", "CHTR",
    "LYV", "MAT", "HAS", "POOL", "LKQ", "AZO", "ORLY", "AAP",
    "DKS", "ADM", "BG", "TSN", "HRL", "CPB", "CAG", "SJM", "LW",
    "UL", "DEO", "BUD", "GPC", "SFM",
    # 医疗 (55)
    "LLY", "UNH", "JNJ", "ABBV", "MRK", "PFE", "TMO", "ABT", "DHR",
    "BMY", "AMGN", "GILD", "VRTX", "REGN", "ISRG", "MDT", "BSX",
    "SYK", "EW", "ELV", "CI", "CVS", "MCK", "BDX", "ZTS", "MRNA",
    "BIIB", "HCA", "DXCM", "IDXX", "ALGN", "RMD", "STE", "HOLX",
    "HUM", "CNC", "MOH", "COR", "VEEV", "INCY", "NBIX", "EXAS",
    "MTD", "WAT", "LH", "DGX", "BIO", "ILMN", "SNY", "AZN", "NVO",
    "GEHC", "VTRS", "CHE",
    # 金融 (65)
    "JPM", "BAC", "WFC", "C", "GS", "MS", "AXP", "V", "MA", "BLK",
    "SCHW", "PNC", "USB", "TFC", "COF", "DFS", "ALL", "PGR", "MET",
    "PRU", "AIG", "CB", "TRV", "MMC", "ICE", "CME", "SPGI", "MCO",
    "NDAQ", "SYF", "WTW", "BX", "KKR", "APO", "ARES", "TROW", "AMP",
    "IBKR", "SOFI", "AFRM", "CBOE", "MSCI", "STT", "BK", "HIG",
    "WRB", "FNF", "MKTX", "JKHY", "RJF", "FDS", "LPLA", "AIZ",
    "EG", "LNC", "CINF", "GL", "BEN",
    # 能源/公用 (50)
    "XOM", "CVX", "COP", "SLB", "OXY", "EOG", "MPC", "PSX", "VLO",
    "HES", "KMI", "WMB", "OKE", "FANG", "DVN", "BKR", "HAL", "MRO",
    "ET", "VST", "APA", "EQT", "CTRA", "CHK", "OVV", "NEE", "DUK",
    "SO", "D", "AEP", "EXC", "XEL", "SRE", "PEG", "ED", "EIX",
    "WEC", "DTE", "EVRG", "FE", "PPL", "CMS", "AEE", "CNP", "ES",
    "ATO", "NI", "LNT",
    # 工业 (55)
    "GE", "CAT", "HON", "BA", "LMT", "RTX", "NOC", "GD", "UNP",
    "UPS", "FDX", "DE", "ITW", "EMR", "ETN", "PH", "ROK", "CMI",
    "WM", "RSG", "CSX", "NSC", "PCAR", "PWR", "GEV", "CARR",
    "OTIS", "TT", "JCI", "DOV", "SWK", "XYL", "TEL", "PNR", "GRMN",
    "HWM", "URI", "FAST", "GWW", "FERG", "AAL", "DAL", "UAL", "LUV",
    "JBHT", "EXPD", "CHRW", "ODFL", "SAIA", "LECO", "TDY", "AXON",
    "VLTO", "PAYX",
    # 材料 (40)
    "LIN", "SHW", "APD", "ECL", "NEM", "FCX", "NUE", "DOW", "DD",
    "PPG", "VMC", "MLM", "EXP", "SUM", "BLDR", "CRH", "CMC", "STLD",
    "CLF", "X", "AA", "ALB", "CF", "MOS", "NTR", "IP", "WRK", "PKG",
    "BALL", "CCK", "SEE", "AMCR", "GPK", "AVI", "MP", "SQM", "LTHM",
    "EMN", "CE", "AME",
    # 地产 (30)
    "PLD", "AMT", "EQIX", "PSA", "O", "SPG", "WELL", "DLR", "CBRE",
    "VICI", "VTR", "ARE", "EXR", "MAA", "INVH", "ESS", "AVB", "EQR",
    "UDR", "KIM", "REG", "WPC", "BXP", "HST", "COLD", "CUBE", "GLPI",
    "LAMR", "DOC", "IRM",
    # 补充 (约60)
    "F", "GM", "STLA", "TM", "HMC", "RIVN", "LCID", "NIO", "LI", "XPEV",
    "SNPS", "CDNS", "IT", "DBX", "DT", "ESTC", "TOST", "S", "GTLB", "U",
    "BURL", "FIVE", "OLLI", "KSS", "JWN", "M", "ANF", "URBN", "WSM",
    "RH", "CROX", "DECK", "SKX", "ONON",
    "ALNY", "DVA", "UHS", "ENSG", "PRGO", "HIMS", "PODD", "PEN", "DOCS",
    "GMED",
    "AFL", "L", "ORI", "UNM",
    "CEG", "NRG", "OGE", "UGI",
    "LII", "BLD", "MLI", "GGG", "IR", "EME", "WSO",
    "RGLD", "ASH", "CDE", "HL",
    "WY", "ELS", "SUI", "BNL",
]


def compute_rsi(close: pd.Series, period: int = DEFAULT_RSI_PERIOD) -> pd.Series:
    """用 Wilder 平滑法计算 RSI(默认 6)。"""
    delta = close.diff()
    gain = delta.clip(lower=0.0)
    loss = (-delta).clip(lower=0.0)
    avg_gain = gain.ewm(alpha=1.0 / period, min_periods=period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1.0 / period, min_periods=period, adjust=False).mean()
    rs = avg_gain / avg_loss
    rsi = 100.0 - (100.0 / (1.0 + rs))
    # 期间只涨不跌(avg_loss == 0)时 RSI 记为 100
    rsi = rsi.where(avg_loss != 0, 100.0)
    return rsi


def load_default_tickers() -> list[str]:
    """返回内置常用美股列表。"""
    return list(DEFAULT_TICKERS)


def load_watchlist(path: str) -> list[str]:
    """从文本文件读取股票代码(每行一个, 支持 # 注释)。"""
    tickers = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.split("#")[0].strip()
            if line:
                tickers.append(line)
    return tickers


def fetch_us_tickers(limit: int = 500, market: str = "us") -> list[str]:
    """从东方财富获取市值前 limit 的美股代码(国内可访问, 不依赖维基百科)。

    market: "us"=全市场(纳斯达克+纽交所+美交所), "nasdaq"=仅纳斯达克。
    代码中的下划线转为点号(如 BRK_B -> BRK.B)以匹配腾讯接口。
    """
    fs = EASTMONEY_FS.get(market, EASTMONEY_FS["us"])
    tickers: list[str] = []
    pages = (limit + 99) // 100
    for page in range(1, pages + 1):
        url = EASTMONEY_LIST_URL.format(page=page, fs=fs)
        data = json.loads(http_get(url, timeout=20).decode("utf-8", "ignore"))
        items = (data.get("data") or {}).get("diff") or []
        for it in items:
            code = str(it.get("f12", "")).strip()
            if code:
                tickers.append(code.upper().replace("_", "."))
        if len(tickers) >= limit:
            break
        time.sleep(0.2)
    return tickers[:limit]


def fetch_github_tickers() -> list[str]:
    """从 GitHub 拉取纳斯达克+纽交所+美交所全部美股代码并去重。"""
    seen: set[str] = set()
    for url in GITHUB_TICKER_URLS:
        raw = http_get(url, timeout=30).decode("utf-8", "ignore")
        for line in raw.splitlines():
            code = line.strip().upper()
            if code:
                seen.add(code)
        time.sleep(0.2)
    return sorted(seen)


# 内置 A股/港股样本(东财/新浪不可用时兜底)
BUILTIN_CN = [
    "600519", "000001", "300750", "002594", "600036", "601318", "000858",
    "601012", "300059", "688981", "000002", "000651", "000333", "603288",
    "600276", "603259", "601398", "601888", "000725", "600031", "601899",
    "600900", "601166", "600030", "600887", "601628", "603501", "002475",
    "600309", "300015", "002415", "600585", "601088", "600809", "601668",
    "600016", "000568", "600104", "601601",
]
BUILTIN_HK = [
    "00700", "09988", "03690", "01810", "00005", "01299", "00388", "00941",
    "00939", "01398", "09618", "09999", "09888", "01211", "00981", "01024",
    "02015", "02269", "02382", "02020", "00728", "01088", "02628", "02318",
    "00960", "01313", "00669", "00027", "00011", "00688", "01113", "00322",
    "02018", "01093", "01177", "02331", "01876", "01772", "02196", "06618",
]


def normalize_code(market: str, ticker: str) -> str:
    """把用户输入的代码规范化为带市场前缀的代码(小写前缀)。"""
    t = ticker.strip().upper()
    if market == "us":
        if t.startswith("US"):
            t = t[2:]
        return "us" + t
    if market == "hk":
        if t.startswith("HK"):
            t = t[2:]
        if t.isdigit():
            return "hk" + t.zfill(5)
        return "hk" + t
    if market == "cn":  # A股: 6/9开头=沪, 0/2/3开头=深, 4/8开头=北交所
        if t[:2] in ("SH", "SZ", "BJ"):
            return t[:2].lower() + t[2:]
        if t.isdigit():
            if t.startswith(("6", "9")):
                return "sh" + t
            if t.startswith(("0", "2", "3")):
                return "sz" + t
            return "bj" + t
        return t.lower()
    return t.lower()


def fetch_cn_tickers() -> list[str]:
    """从新浪获取沪深A股(含北交所)代码, 已带 sh/sz/bj 前缀。"""
    codes: list[str] = []
    for page in range(1, 100):  # 上限保护
        try:
            d = json.loads(http_get(SINA_CN_URL.format(page=page), timeout=20)
                           .decode("gbk", "ignore"))
        except Exception:  # noqa: BLE001
            break
        if not d:
            break
        codes.extend(str(it.get("symbol", "")).strip() for it in d)
        if len(d) < 100:
            break
        time.sleep(0.2)
    return list(dict.fromkeys(c for c in codes if c))


def fetch_hk_tickers(limit: int = 3000) -> list[str]:
    """从东方财富获取港股代码(可能被限流, 失败返回空列表)。"""
    fs = EASTMONEY_FS["hk"]
    codes: list[str] = []
    for page in range(1, (limit + 99) // 100 + 1):
        try:
            url = EASTMONEY_LIST_URL.format(page=page, fs=fs)
            d = json.loads(http_get(url, timeout=20).decode("utf-8", "ignore"))
        except Exception:  # noqa: BLE001
            break
        items = (d.get("data") or {}).get("diff") or []
        if not items:
            break
        codes.extend(str(it.get("f12", "")).strip() for it in items)
        time.sleep(0.2)
    return ["hk" + c.zfill(5) for c in codes if c]


def load_market_list(market: str, list_mode: str, limit: int = 500) -> list[str]:
    """按市场加载股票列表, 返回带市场前缀的代码列表。"""
    if market == "us":
        # all/github 都优先用本地已缓存的全量列表(6741只, 来自GitHub每日更新),
        # 避免每次联网拉取(东财/raw.githubusercontent 在国内时好时坏)
        if list_mode in ("github", "all"):
            if os.path.exists(LOCAL_TICKER_FILE):
                return load_tickers_file(LOCAL_TICKER_FILE)
        if list_mode == "github":
            ts = fetch_github_tickers()
            if ts:
                save_tickers_file(ts, LOCAL_TICKER_FILE)
            return ts or load_default_tickers()
        if list_mode == "builtin":
            return load_default_tickers()
        return fetch_us_tickers(limit, market=("nasdaq" if list_mode == "nasdaq" else "us")) \
            or load_default_tickers()

    if market == "cn":
        if list_mode == "builtin":
            return [normalize_code("cn", t) for t in BUILTIN_CN]
        if os.path.exists(CN_TICKER_FILE):
            return load_tickers_file(CN_TICKER_FILE)
        ts = fetch_cn_tickers()
        if ts:
            save_tickers_file(ts, CN_TICKER_FILE)
        return ts or [normalize_code("cn", t) for t in BUILTIN_CN]

    if market == "hk":
        if list_mode == "builtin":
            return [normalize_code("hk", t) for t in BUILTIN_HK]
        if os.path.exists(HK_TICKER_FILE):
            return load_tickers_file(HK_TICKER_FILE)
        ts = fetch_hk_tickers()
        if ts:
            save_tickers_file(ts, HK_TICKER_FILE)
        return ts or [normalize_code("hk", t) for t in BUILTIN_HK]

    return []


def save_tickers_file(tickers: list[str], path: str) -> None:
    """将代码列表保存到本地文件(每行一个)。"""
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(tickers) + "\n")


def load_tickers_file(path: str) -> list[str]:
    """从本地文件读取代码列表。"""
    with open(path, encoding="utf-8") as f:
        return [line.strip().upper() for line in f if line.strip()]


_full_info_cache: Optional[dict] = None


def load_full_info() -> dict:
    """加载本地 板块/行业/市值 信息缓存(来自 nasdaq_full.json 等)。"""
    global _full_info_cache
    if _full_info_cache is not None:
        return _full_info_cache
    base = DATA_DIR
    merged: dict[str, dict] = {}
    for fname in FULL_INFO_FILES:
        path = os.path.join(base, fname)
        if not os.path.exists(path):
            continue
        try:
            with open(path, encoding="utf-8") as f:
                rows = json.load(f)
            for r in rows:
                sym = str(r.get("symbol", "")).strip().upper()
                if sym:
                    merged[sym] = {
                        "sector": str(r.get("sector") or "").strip(),
                        "industry": str(r.get("industry") or "").strip(),
                        "market_cap": r.get("marketCap") or "",
                        "full_name": str(r.get("name") or "").strip(),
                        "country": str(r.get("country") or "").strip(),
                        "ipoyear": str(r.get("ipoyear") or "").strip(),
                    }
        except Exception:  # noqa: BLE001
            pass
    _full_info_cache = merged
    return merged


_cn_industry_cache: Optional[dict] = None


def load_cn_industry() -> dict:
    """加载A股行业映射 data/cn_industry.json: {"600519": "酿酒行业", ...}。

    由 fetch_cn_industry.py 从东方财富拉取生成。
    """
    global _cn_industry_cache
    if _cn_industry_cache is not None:
        return _cn_industry_cache
    path = os.path.join(DATA_DIR, "cn_industry.json")
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as f:
                _cn_industry_cache = json.load(f)
            return _cn_industry_cache
        except Exception:  # noqa: BLE001
            pass
    _cn_industry_cache = {}
    return _cn_industry_cache


# ---------- 股票 <-> 板块(行业) 关系缓存 ----------

_cn_sector_members_cache: Optional[dict] = None


def cn_prefix(code6: str) -> str:
    """6位A股代码 -> 带市场前缀代码(小写 sh/sz/bj), 供行情接口使用。"""
    t = str(code6).strip().upper().zfill(6)
    if t.startswith(("6", "9")):
        return "sh" + t
    if t.startswith(("0", "2", "3")):
        return "sz" + t
    return "bj" + t


def load_cn_sector_members(force: bool = False) -> dict:
    """加载A股 "板块 -> 成分股" 关系缓存 data/cn_sector_members.json。

    结构: {"化学原料": ["600230","600596",...], ...} (值=6位代码, 已排序)。
    缓存源是 cn_industry.json(东财行业映射); 若 cn_industry.json 比本缓存新,
    会自动按板块重新分组并落盘(即行业刷新后关系缓存自动重建)。
    """
    global _cn_sector_members_cache
    if _cn_sector_members_cache is not None and not force:
        return _cn_sector_members_cache
    members_path = os.path.join(DATA_DIR, "cn_sector_members.json")
    ind_path = os.path.join(DATA_DIR, "cn_industry.json")
    if (not force
            and os.path.exists(members_path)
            and os.path.exists(ind_path)
            and os.path.getmtime(members_path) >= os.path.getmtime(ind_path)):
        try:
            with open(members_path, encoding="utf-8") as f:
                _cn_sector_members_cache = json.load(f)
            return _cn_sector_members_cache
        except Exception:  # noqa: BLE001
            pass
    # 重建: 由 cn_industry.json 按板块分组(空板块/占位"-"忽略)
    ind = load_cn_industry()
    members: dict[str, list] = {}
    for code6, sector in ind.items():
        sector = (sector or "").strip()
        if not sector or sector == "-":
            continue
        members.setdefault(sector, []).append(str(code6))
    for lst in members.values():
        lst.sort()
    _cn_sector_members_cache = members
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        with open(members_path, "w", encoding="utf-8") as f:
            json.dump(members, f, ensure_ascii=False, separators=(",", ":"))
    except Exception:  # noqa: BLE001
        pass
    return _cn_sector_members_cache


def is_shell_name(name: str) -> bool:
    """按名称关键词判断是否为壳股/权证/优先股。"""
    n = " " + (name or "").upper() + " "
    return any(k in n for k in SHELL_KEYWORDS)


_PROXY_CONF = os.path.expanduser("~/.gtimg_proxy.json")
# 可选配置: {"http":"..","https":"..","api":"取IP池URL","num":10,"ttl":180}
#   api: 一次取一批代理的URL(支持 num= 参数; 若 URL 没带 num= 会自动补 num)
#   num: 每批取的IP个数, 默认 10
#   ttl: 刷新周期(秒), 默认 180 —— 每个IP有效期约3分钟, 提前一点点刷新避免用失效IP
_POOL_DEFAULT_TTL = 180.0
_POOL_DEFAULT_NUM = 10
_opener: Optional[urllib.request.OpenerDirector] = None
_pool_lock = threading.Lock()
_pool: list = []
_pool_ts = 0.0


def _proxy_config() -> dict:
    """~/.gtimg_proxy.json 内容(不存在返回空)。"""
    try:
        with open(_PROXY_CONF, encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


def _load_proxies() -> dict:
    """静态代理配置(优先配置文件里的 http/https; 否则环境变量 HTTP(S)_PROXY)。"""
    cfg = _proxy_config()
    d = {k: v for k, v in cfg.items() if k in ("http", "https") and v}
    if d:
        return d
    return urllib.request.getproxies()


def _fetch_proxy_pool() -> list:
    """从配置的取IP池 API 拉一批代理(txt, 每行 ip:port), 缓存一个刷新周期。

    配置 ~/.gtimg_proxy.json:
      {"api":"...取IP池URL", "num":10, "ttl":180}
    - api: 支持 tianqiip(num=) 或 58ip(number=) 等; txt 格式每行 ip:port
    - num: 每次取的IP个数(仅当 URL 没带 num=/number= 时补), 默认10
    - ttl: 刷新周期(秒), 默认180; IP有效期一般3分钟, 到期前刷新避免用失效IP
    """
    global _pool, _pool_ts
    cfg = _proxy_config()
    url = cfg.get("api")
    if not url:
        return []
    ttl = float(cfg.get("ttl", _POOL_DEFAULT_TTL))
    num = int(cfg.get("num", _POOL_DEFAULT_NUM))
    # 58ip 等取IP池用 number= 参数; 只有 URL 里 num=/number= 都没有时才自动补 num=
    if "num=" not in url and "number=" not in url:
        url += ("&" if "?" in url else "?") + f"num={num}"
    with _pool_lock:
        if _pool and (time.time() - _pool_ts) < ttl:
            return list(_pool)
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=20) as resp:
                txt = resp.read().decode("utf-8", "ignore")
            got = [ln.strip() for ln in txt.splitlines()
                   if ln.strip() and ":" in ln and not ln.strip().startswith("{")]
            if got:
                _pool = got
                _pool_ts = time.time()
                print(f"[代理] 取到 {len(_pool)} 个IP代理 (刷新周期~{ttl / 60:.1f}分钟)",
                      file=sys.stderr)
            elif not _pool:
                print("[代理] 取IP池返回为空", file=sys.stderr)
        except Exception as exc:  # noqa: BLE001
            print(f"[代理] 拉取失败: {exc}", file=sys.stderr)
        return list(_pool)


def _candidate_proxies() -> list:
    """可用代理候选: 静态配置 http/https + 动态取IP池(轮换)。"""
    static = list(_load_proxies().values())
    pool = _fetch_proxy_pool()
    cands = [p if p.startswith("http") else f"http://{p}" for p in static]
    cands += [p if p.startswith("http") else f"http://{p}" for p in pool]
    return cands


def _http_opener(proxy: str | None = None) -> urllib.request.OpenerDirector:
    """返回带某代理的 opener; proxy=None 用默认(静态/直连)。"""
    if proxy is None:
        global _opener
        if _opener is None:
            _opener = urllib.request.build_opener(
                urllib.request.ProxyHandler(_load_proxies()))
        return _opener
    return urllib.request.build_opener(
        urllib.request.ProxyHandler({"http": proxy, "https": proxy}))


_rr = itertools.cycle(range(1 << 30))


def _http_get_once(opener, url: str, timeout: int) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with opener.open(req, timeout=timeout) as resp:
        return resp.read()


def http_get(url: str, timeout: int = 15) -> bytes:
    """带重试的 HTTP GET(指数退避), 可选代理池轮换; 无代理则直连。"""
    cands = _candidate_proxies()
    if cands:
        start = next(_rr) % len(cands)
        tries = [cands[(start + i) % len(cands)] for i in range(min(len(cands), 10))]
        last_exc: Optional[Exception] = None
        for proxy in tries:
            try:
                return _http_get_once(_http_opener(proxy), url, timeout)
            except Exception as exc:  # noqa: BLE001
                last_exc = exc
        if last_exc is not None:
            raise last_exc
    # 直连 + 重试退避
    last_exc: Optional[Exception] = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            return _http_get_once(_http_opener(), url, timeout)
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            if attempt < MAX_RETRIES:
                time.sleep(3 * (2 ** (attempt - 1)))
    assert last_exc is not None
    raise last_exc


def tencent_quote(market: str, ticker: str) -> Optional[tuple[str, str]]:
    """查询腾讯报价, 返回 (带前缀标准代码, 名称); 无效代码返回 None。

    美股返回如 ("usAAPL.OQ", "苹果"), A股 ("sh600519", "贵州茅台"), 港股 ("hk00700", "腾讯控股")。
    带本地缓存(一天一拉), 全量扫描时避免反复请求报价触发限流。
    """
    prefixed = normalize_code(market, ticker)
    cache_file = None
    if _USE_CACHE:
        os.makedirs(QUOTE_CACHE_DIR, exist_ok=True)
        key = f"{market}_{prefixed.replace('.', '_').replace('/', '_')}"
        cache_file = os.path.join(QUOTE_CACHE_DIR, f"{key}.json")
        if os.path.exists(cache_file) and (
            time.time() - os.path.getmtime(cache_file)
        ) < QUOTE_CACHE_TTL_DAYS * 86400:
            try:
                with open(cache_file, encoding="utf-8") as f:
                    data = json.load(f)
                if data is None:
                    return None
                return data[0], data[1]
            except Exception:  # noqa: BLE001
                pass

    url = TENCENT_QUOTE_URL.format(urllib.parse.quote(prefixed))
    raw = http_get(url, timeout=10).decode("gbk", "ignore")
    result: Optional[tuple[str, str]] = None
    m = re.search(r'="([^"]*)"', raw)
    if m:
        parts = m.group(1).split("~")
        if len(parts) >= 3 and parts[1].strip():
            status = parts[0]
            if market == "us":
                if status == "200":
                    result = (f"us{parts[2].strip()}", parts[1].strip())
            elif status in ("1", "100", "200", "0", "51", "62"):  # sh=1/sz=51/bj=62
                result = (prefixed, parts[1].strip())
    if cache_file is not None:
        try:
            with open(cache_file, "w", encoding="utf-8") as f:
                json.dump(None if result is None else list(result), f, ensure_ascii=False)
        except Exception:  # noqa: BLE001
            pass
    return result


def _kline_rows_to_series(rows):
    idx = pd.to_datetime([r[0] for r in rows])
    close = pd.Series([float(r[2]) for r in rows], index=idx).sort_index()
    volume = pd.Series([float(r[5]) for r in rows], index=idx).sort_index()
    close = close.dropna()
    return close, volume.reindex(close.index).fillna(0.0)


_ASOF_REF: Optional[date] = None  # 增量基准(最新交易日); 设了之后按缓存内容判断是否过期


def set_kline_asof_ref(d) -> None:
    """设置K线缓存增量基准(该市场最新交易日); None=退回按 mtime 判断。"""
    global _ASOF_REF
    _ASOF_REF = d


def _kline_cache_fresh(path: str, rows) -> bool:
    """K线缓存是否可用(增量): 设了 _ASOF_REF 时看"末根K线日期>=基准";
    否则退回 mtime < 1天(TTL)。"""
    if _ASOF_REF is not None:
        try:
            last = pd.to_datetime(rows[-1][0]).date()
            return last >= _ASOF_REF
        except Exception:  # noqa: BLE001
            return False
    try:
        return (time.time() - os.path.getmtime(path)) < KLINE_CACHE_TTL_DAYS * 86400
    except Exception:  # noqa: BLE001
        return False


def tencent_kline(
    prefixed: str, bars: int, use_cache: bool = True
) -> Optional[tuple[pd.Series, pd.Series]]:
    """拉取前复权日K, 返回 (收盘价 Series, 成交量 Series)。

    prefixed 形如 usAAPL.OQ / sh600519 / hk00700。带本地缓存(增量), 减少请求避免限流。
    """
    cache_file = None
    if use_cache:
        os.makedirs(KLINE_CACHE_DIR, exist_ok=True)
        cache_file = os.path.join(
            KLINE_CACHE_DIR, f"{prefixed.replace('.', '_')}_{bars}.json"
        )
        if os.path.exists(cache_file):
            try:
                with open(cache_file, encoding="utf-8") as f:
                    rows = json.load(f)
                if rows and _kline_cache_fresh(cache_file, rows):
                    return _kline_rows_to_series(rows)
            except Exception:  # noqa: BLE001
                pass

    if prefixed[:2].lower() == "us":
        url = (f"https://web.ifzq.gtimg.cn/appstock/app/usfqkline/get"
               f"?param={prefixed},day,,,{bars},qfq")
    else:
        url = (f"https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
               f"?param={prefixed},day,,,{bars},qfq")
    data = json.loads(http_get(url).decode("utf-8", "ignore"))
    node = (data.get("data") or {}).get(prefixed, {})
    rows = node.get("qfqday") or node.get("day") or []
    if not rows:
        return None
    if cache_file:
        try:
            with open(cache_file, "w", encoding="utf-8") as f:
                json.dump(rows, f)
        except Exception:  # noqa: BLE001
            pass
    return _kline_rows_to_series(rows)


def process_ticker(
    market: str,
    ticker: str,
    rsi_period: int,
    threshold: float,
    bars: int,
    min_bars: int,
    min_price: float,
    min_volume: float,
    min_shares: float,
    max_stale_days: int,
    keep_shells: bool,
    full_info: dict,
) -> Optional[dict]:
    """处理单只股票: 报价→K线→RSI→过滤, 命中返回结果字典, 否则返回 None。"""
    try:
        if _REQUEST_DELAY:
            time.sleep(_REQUEST_DELAY)
        info = tencent_quote(market, ticker)
        if info is None:
            return None
        prefixed, name = info
        k = tencent_kline(prefixed, bars, use_cache=_USE_CACHE)
        if k is None:
            return None
        close, volume = k
        if len(close) < min_bars:
            return None

        last_close = float(close.iloc[-1])
        # 停牌/退市过滤(最新K线距今过久)
        last_date = close.index[-1].date()
        if (date.today() - last_date).days > max_stale_days:
            return None
        # 价格过滤(剔除仙股)
        if last_close < min_price:
            return None
        # 成交量过滤(近20日均成交额 = 收盘价×成交量×手数换算, 单位市场货币)
        dollar_vol = close * volume * VOLUME_MULT.get(market, 1.0)
        avg_vol = float(dollar_vol.tail(20).mean()) if len(volume) else 0.0
        if avg_vol < min_volume:
            return None
        # 成交量过滤(近20日均成交量股数, 剔除太小的死票)
        avg_shares = float(volume.tail(20).mean()) * VOLUME_MULT.get(market, 1.0)
        if avg_shares < min_shares:
            return None

        # 壳股/权证/优先股过滤 (主要针对美股)
        meta = full_info.get(prefixed) or full_info.get(ticker.upper()) or {}
        full_name = meta.get("full_name") or name
        if not keep_shells and is_shell_name(full_name):
            return None

        rsi = compute_rsi(close, rsi_period)
        last_rsi = float(rsi.iloc[-1])
        if pd.notna(last_rsi) and last_rsi > threshold:
            return {
                "ticker": ticker,
                "name": name,
                "sector": translate(meta.get("sector", "")),
                "industry": translate(meta.get("industry", "")),
                "date": str(close.index[-1].date()),
                "close": round(last_close, 2),
                "volume": int(avg_vol),  # 近20日均成交额(美元)
                "rsi": round(last_rsi, 2),
            }
    except Exception as exc:  # noqa: BLE001
        print(f"[警告] {ticker} 获取失败: {exc}", file=sys.stderr)
    return None


def scan(
    market: str,
    tickers: list[str],
    rsi_period: int,
    threshold: float,
    bars: int,
    min_bars: int,
    workers: int = 8,
    min_price: float = DEFAULT_MIN_PRICE,
    min_volume: float = DEFAULT_MIN_VOLUME,
    min_shares: float = DEFAULT_MIN_SHARES,
    max_stale_days: int = DEFAULT_MAX_STALE_DAYS,
    keep_shells: bool = False,
    full_info: Optional[dict] = None,
) -> list[dict]:
    """用线程池并行拉取行情并扫描 RSI 超买股票。"""
    results: list[dict] = []
    total = len(tickers)
    full_info = full_info if full_info is not None else load_full_info()

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {
            pool.submit(
                process_ticker,
                market, t, rsi_period, threshold, bars, min_bars,
                min_price, min_volume, min_shares, max_stale_days, keep_shells, full_info,
            ): t
            for t in tickers
        }
        done = 0
        for fut in as_completed(futures):
            r = fut.result()
            done += 1
            if r:
                results.append(r)
            if total > 10:
                print(f"[进度] 已检查 {done}/{total} ...", end="\r", file=sys.stderr)

    if total > 10:
        print(file=sys.stderr)
    return results


def main() -> None:
    parser = argparse.ArgumentParser(description="用腾讯财经扫描 RSI 超买的美股/A股/港股")
    parser.add_argument(
        "--market", choices=["us", "cn", "hk"], default="us",
        help="市场: us=美股, cn=A股, hk=港股(默认 us)",
    )
    parser.add_argument(
        "--tickers",
        help="逗号分隔的股票代码, 自动按市场加前缀(如 600519 / sh600519 / hk00700)",
    )
    parser.add_argument("--watchlist", help="从文本文件读取股票代码(每行一个)")
    parser.add_argument(
        "--rsi", type=float, default=DEFAULT_RSI_THRESHOLD,
        help=f"RSI 阈值(默认 {DEFAULT_RSI_THRESHOLD})",
    )
    parser.add_argument(
        "--period", type=int, default=DEFAULT_RSI_PERIOD,
        help=f"RSI 计算周期(默认 {DEFAULT_RSI_PERIOD})",
    )
    parser.add_argument(
        "--bars", type=int, default=DEFAULT_BARS,
        help=f"每只股票拉取的日K根数(默认 {DEFAULT_BARS})",
    )
    parser.add_argument(
        "--workers", type=int, default=DEFAULT_WORKERS,
        help=f"并发线程数(默认 {DEFAULT_WORKERS})",
    )
    parser.add_argument(
        "--limit", type=int, default=0,
        help="只扫描前 N 只(0 表示全部)",
    )
    parser.add_argument(
        "--list", choices=["all", "github", "builtin"], default="all",
        help="股票池来源: all=该市场全量/市值前, github=GitHub全美股(仅us), "
             "builtin=内置样本(默认 all)",
    )
    parser.add_argument(
        "--update-list", action="store_true",
        help="仅下载并更新当前市场代码缓存后退出",
    )
    parser.add_argument(
        "--min-price", type=float, default=DEFAULT_MIN_PRICE,
        help=f"剔除低于该价格的股票(默认 {DEFAULT_MIN_PRICE})",
    )
    parser.add_argument(
        "--min-volume", type=float, default=DEFAULT_MIN_VOLUME,
        help=f"剔除近20日均成交额(市场货币)低于该值的股票(默认 {DEFAULT_MIN_VOLUME:,.0f})",
    )
    parser.add_argument(
        "--min-shares", type=float, default=DEFAULT_MIN_SHARES,
        help=f"剔除近20日均成交量(股)低于该值的股票(默认 {DEFAULT_MIN_SHARES:,.0f})",
    )
    parser.add_argument(
        "--max-stale-days", type=int, default=DEFAULT_MAX_STALE_DAYS,
        help=f"最新K线距今超过该天数的视为停牌/退市并剔除(默认 {DEFAULT_MAX_STALE_DAYS})",
    )
    parser.add_argument(
        "--keep-shells", action="store_true",
        help="保留壳股/权证/优先股(默认剔除)",
    )
    parser.add_argument(
        "--delay", type=float, default=0.0,
        help="每个标的请求前延时秒数, 首次全量拉取时建议 0.1~0.3 以降低限流风险(默认 0)",
    )
    parser.add_argument(
        "--no-cache", action="store_true",
        help="关闭K线本地缓存(默认开启, 缓存1天)",
    )
    parser.add_argument("--output", help="将结果保存为 CSV 文件")
    args = parser.parse_args()

    global _REQUEST_DELAY, _USE_CACHE
    _REQUEST_DELAY = args.delay
    _USE_CACHE = not args.no_cache

    mkt_name = MARKET_NAMES.get(args.market, args.market)

    # 仅更新本地代码缓存
    if args.update_list:
        if args.market == "us":
            ts = fetch_github_tickers()
            if not ts:
                print("GitHub 列表获取失败。", file=sys.stderr)
                sys.exit(1)
            save_tickers_file(ts, LOCAL_TICKER_FILE)
            print(f"[信息] 已更新美股列表 {len(ts)} 只 -> {LOCAL_TICKER_FILE}")
        elif args.market == "cn":
            ts = fetch_cn_tickers()
            if not ts:
                print("新浪A股列表获取失败。", file=sys.stderr)
                sys.exit(1)
            save_tickers_file(ts, CN_TICKER_FILE)
            print(f"[信息] 已更新A股列表 {len(ts)} 只 -> {CN_TICKER_FILE}")
        else:
            ts = fetch_hk_tickers()
            if not ts:
                print("东方财富港股列表获取失败(可能被限流)。", file=sys.stderr)
                sys.exit(1)
            save_tickers_file(ts, HK_TICKER_FILE)
            print(f"[信息] 已更新港股列表 {len(ts)} 只 -> {HK_TICKER_FILE}")
        return

    # 确定要扫描的股票列表
    if args.tickers:
        tickers = [normalize_code(args.market, t)
                   for t in args.tickers.split(",") if t.strip()]
    elif args.watchlist:
        tickers = [normalize_code(args.market, t)
                   for t in load_watchlist(args.watchlist)]
    else:
        tickers = load_market_list(args.market, args.list, 500)
        if not tickers:
            print(
                f"[警告] 列表获取失败, 使用内置样本。", file=sys.stderr
            )
            tickers = load_market_list(args.market, "builtin", 500)

    if not tickers:
        print("没有可用的股票代码, 退出。", file=sys.stderr)
        sys.exit(1)

    if args.limit and args.limit > 0:
        tickers = tickers[: args.limit]
        print(f"[信息] 仅扫描前 {len(tickers)} 只。")

    print(f"[信息] 开始扫描 {len(tickers)} 只{mkt_name}, RSI({args.period}) > {args.rsi} ...")
    print(
        f"[信息] 过滤: 价格≥{args.min_price} 近20日均成交额≥{args.min_volume / 1e6:.0f}百万 "
        f"近20日均量≥{int(args.min_shares):,}股 "
        f"停牌>{args.max_stale_days}天剔除 剔除壳股={'否' if args.keep_shells else '是'}"
    )
    results = scan(
        args.market, tickers, args.period, args.rsi, args.bars, DEFAULT_MIN_BARS,
        args.workers, args.min_price, args.min_volume, args.min_shares,
        args.max_stale_days, args.keep_shells,
    )

    if not results:
        print(f"没有找到 RSI({args.period}) > {args.rsi} 的股票。")
        return

    results.sort(key=lambda x: x["rsi"], reverse=True)
    amt_header, amt_div = ("额($M)", 1e6) if args.market == "us" else ("额(亿)", 1e8)
    print(f"\n找到 {len(results)} 只 RSI({args.period}) > {args.rsi} 的股票:")
    print(f"{'代码':<9}{'名称':<14}{'行业':<18}{'日期':<12}{'收盘':>10}{amt_header:>9}{'RSI':>7}")
    print("-" * 80)
    for r in results:
        ind = (r["industry"] or "-")[:16]
        print(
            f"{r['ticker']:<9}{(r['name'] or '')[:12]:<14}{ind:<18}"
            f"{r['date']:<12}{r['close']:>10.2f}{r['volume'] / amt_div:>9.0f}"
            f"{r['rsi']:>7.1f}"
        )

    if args.output:
        df = pd.DataFrame(results)
        df.to_csv(args.output, index=False, encoding="utf-8-sig")
        print(f"\n结果已保存到 {args.output}")


if __name__ == "__main__":
    main()
