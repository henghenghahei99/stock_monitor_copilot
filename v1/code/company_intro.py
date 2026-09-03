#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""公司介绍/概况模块。

来源策略(按市场):
  - A股: 同花顺 basic.10jqka.com.cn/{code}/company.html 的公司简介(文字), 带本地缓存;
         失败时回退到腾讯报价字段(总市值/市盈率)。
  - 美股: 本地 *_full.json 的结构化概况(国家/板块/行业/市值/上市年份), 纯离线。
  - 港股: 腾讯报价字段(总市值/市盈率)结构化概况, 带本地缓存。

设计: 只在策略命中的结果上调用, 避免对全量股票池逐只请求。
"""

from __future__ import annotations

import os
import re
import sys
import time
import urllib.parse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import find_overbought_stocks as fos  # noqa: E402
from translations import translate  # noqa: E402  行业/板块中英翻译

CACHE_DIR = os.path.join(fos.DATA_DIR, "company_intro_cache")
TTL_DAYS = 7  # 公司简介不常变, 缓存久一点

# 常见国家英文 -> 中文(用于美股结构化概况)
COUNTRY_ZH = {
    "United States": "美国", "China": "中国", "Hong Kong": "中国香港",
    "Canada": "加拿大", "Japan": "日本", "United Kingdom": "英国",
    "Germany": "德国", "France": "法国", "Australia": "澳大利亚",
    "India": "印度", "Brazil": "巴西", "South Korea": "韩国",
    "Taiwan": "中国台湾", "Singapore": "新加坡", "Switzerland": "瑞士",
    "Netherlands": "荷兰", "Israel": "以色列", "Ireland": "爱尔兰",
    "Sweden": "瑞典", "Mexico": "墨西哥", "Russia": "俄罗斯",
    "Argentina": "阿根廷", "Chile": "智利", "Spain": "西班牙",
    "Italy": "意大利", "Bermuda": "百慕大", "Cayman Islands": "开曼群岛",
    "Jersey": "泽西岛", "Luxembourg": "卢森堡", "Norway": "挪威",
    "Finland": "芬兰", "Denmark": "丹麦", "Belgium": "比利时",
    "Greece": "希腊", "Portugal": "葡萄牙", "New Zealand": "新西兰",
    "Indonesia": "印度尼西亚", "Malaysia": "马来西亚", "Thailand": "泰国",
    "Vietnam": "越南", "Philippines": "菲律宾", "South Africa": "南非",
    "Turkey": "土耳其", "Poland": "波兰", "United Arab Emirates": "阿联酋",
    "Saudi Arabia": "沙特阿拉伯",
}


def _read_cache(path: str) -> str | None:
    if os.path.exists(path) and (time.time() - os.path.getmtime(path)) < TTL_DAYS * 86400:
        try:
            with open(path, encoding="utf-8") as f:
                return f.read().strip()
        except Exception:  # noqa: BLE001
            pass
    return None


def _write_cache(path: str, text: str) -> None:
    if not text:
        return  # 空结果不缓存, 下次重试
    try:
        os.makedirs(CACHE_DIR, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
    except Exception:  # noqa: BLE001
        pass


def _fmt_usd(v) -> str:
    """把美元市值数值格式化为中文(万亿/亿/百万/万)。"""
    try:
        v = float(v)
    except Exception:  # noqa: BLE001
        return ""
    if v >= 1e12:
        return f"{v / 1e12:.1f}万亿美元"
    if v >= 1e9:
        return f"{v / 1e9:.1f}亿美元"
    if v >= 1e6:
        return f"{v / 1e6:.1f}百万美元"
    return f"{v / 1e4:.0f}万美元"


def us_intro(prefixed: str, ticker: str, full_info: dict) -> str:
    """美股: 结构化概况(国家/板块·行业/市值/上市年份), 纯离线。"""
    meta = full_info.get(prefixed) or full_info.get(ticker.upper()) or {}
    if not meta and ticker.upper().startswith("US"):
        meta = full_info.get(ticker.upper()[2:]) or {}  # 去掉 us 前缀
    parts: list[str] = []
    country = meta.get("country", "")
    if country:
        parts.append(COUNTRY_ZH.get(country, country))
    sector = translate(meta.get("sector", ""))
    industry = translate(meta.get("industry", ""))
    if industry and industry != "-":
        parts.append(f"{sector}·{industry}" if sector else industry)
    mc = _fmt_usd(meta.get("market_cap", ""))
    if mc:
        parts.append(f"市值约{mc}")
    if meta.get("ipoyear"):
        parts.append(f"{meta['ipoyear']}年上市")
    return " · ".join(parts)


def _quote_fields(prefixed: str) -> list[str] | None:
    """拉取腾讯报价并按 ~ 切分字段。"""
    url = fos.TENCENT_QUOTE_URL.format(urllib.parse.quote(prefixed))
    raw = fos.http_get(url, timeout=10).decode("gbk", "ignore")
    m = re.search(r'="([^"]*)"', raw)
    if not m:
        return None
    parts = m.group(1).split("~")
    return parts if len(parts) > 44 else None


def _structured_quote(prefixed: str) -> str:
    """从报价字段生成结构化概况: 总市值([44], 亿) + 市盈率([39])。"""
    try:
        parts = _quote_fields(prefixed)
        if parts is None:
            return ""
        unit = "亿港元" if prefixed[:2].lower() == "hk" else "亿元"
        bits: list[str] = []
        try:
            mc = float(parts[44])
            if mc > 0:
                bits.append(f"总市值约{mc:,.0f}{unit}")
        except Exception:  # noqa: BLE001
            pass
        try:
            pe = float(parts[39])
            if pe > 0:
                bits.append(f"市盈率{pe:.1f}")
        except Exception:  # noqa: BLE001
            pass
        return " · ".join(bits)
    except Exception:  # noqa: BLE001
        return ""


def hk_intro(prefixed: str) -> str:
    """港股: 报价字段结构化概况(总市值/市盈率), 带缓存。"""
    cache = os.path.join(CACHE_DIR, f"{prefixed}.txt")
    hit = _read_cache(cache)
    if hit is not None:
        return hit
    text = _structured_quote(prefixed)
    _write_cache(cache, text)
    return text


def cn_intro(prefixed: str) -> str:
    """A股: 同花顺公司简介(文字), 带缓存; 失败回退报价结构化概况。"""
    code = prefixed[2:]  # 去掉 sh/sz/bj 前缀
    cache = os.path.join(CACHE_DIR, f"{prefixed}.txt")
    hit = _read_cache(cache)
    if hit is not None:
        return hit
    text = ""
    try:
        url = f"https://basic.10jqka.com.cn/{code}/company.html"
        raw = fos.http_get(url, timeout=15).decode("gbk", "ignore")
        i = raw.find("公司简介")
        if i != -1:
            seg = re.sub(r"<[^>]+>", " ", raw[i:i + 2500])
            seg = re.sub(r"\s+", " ", seg)
            for stop in ("高管介绍", "公司大事", "参股控股", "公司资料", "公司新闻"):
                j = seg.find(stop, 8)
                if j != -1:
                    seg = seg[:j]
                    break
            seg = seg.strip().strip("：: ")
            text = seg[:160]
    except Exception:  # noqa: BLE001
        pass
    if not text:
        text = _structured_quote(prefixed)
    _write_cache(cache, text)
    return text


def company_intro(market: str, prefixed: str, ticker: str, full_info: dict) -> str:
    """按市场返回公司介绍/概况字符串(可能为空)。"""
    if market == "us":
        return us_intro(prefixed, ticker, full_info)
    if market == "cn":
        return cn_intro(prefixed)
    if market == "hk":
        return hk_intro(prefixed)
    return ""


if __name__ == "__main__":
    # 自测: python company_intro.py usAAPL.OQ AAPL | sh600519 | hk00700
    for m, t in [("us", "AAPL"), ("cn", "600519"), ("hk", "00700")]:
        fi = fos.load_full_info()
        pre = fos.normalize_code(m, t)
        print(f"[{m}] {t} ->", company_intro(m, pre, t, fi) or "(空)")
