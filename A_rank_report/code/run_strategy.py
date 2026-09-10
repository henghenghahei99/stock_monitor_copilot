#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
策略库运行器: 从 strategies/ 读取策略JSON, 按需启用并输出命中结果。

策略是"信号", 质量过滤(价格/成交额/停牌/壳股)对所有策略统一生效。

用法示例:
    python run_strategy.py --strategies rsi,uptrend          # 两个策略一起跑
    python run_strategy.py --strategies rsi                  # 只跑RSI
    python run_strategy.py --list-strategies                 # 列出可用策略
    python run_strategy.py --market cn --strategies uptrend  # A股趋势
    python run_strategy.py --market hk --strategies rsi,uptrend --min-volume 30000000

策略JSON格式(strategies/xxx.json):
    {
      "id": "rsi", "name": "RSI超买", "type": "rsi",
      "description": "...", "weight": 1.0,
      "params": { ... 策略专属参数 ... }
    }
  type 目前支持: rsi / uptrend
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date
from typing import Optional

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import find_overbought_stocks as fos  # noqa: E402
from find_uptrend import CONDITIONS as UPTREND_CONDITIONS  # noqa: E402
from find_uptrend import CONDITIONS_7D as UPTREND_CONDITIONS_7D  # noqa: E402
from find_uptrend import compute_ma as uptrend_ma  # noqa: E402
from find_uptrend import compute_ma7 as uptrend_ma7  # noqa: E402
from translations import translate  # noqa: E402  行业/板块中英翻译

STRATEGY_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "strategies")


def load_strategies(ids: Optional[list] = None) -> dict:
    """加载 strategies/ 下所有策略JSON; 若给 ids 则只取其中指定策略。"""
    strategies: dict[str, dict] = {}
    for fname in sorted(os.listdir(STRATEGY_DIR)):
        if not fname.endswith(".json"):
            continue
        try:
            with open(os.path.join(STRATEGY_DIR, fname), encoding="utf-8") as f:
                cfg = json.load(f)
            if cfg.get("id"):
                strategies[cfg["id"]] = cfg
        except Exception as exc:  # noqa: BLE001
            print(f"[警告] 策略文件 {fname} 解析失败: {exc}", file=sys.stderr)
    if ids:
        strategies = {k: v for k, v in strategies.items() if k in ids}
    return strategies


# ---------- 策略评估函数(type -> 评估器) ----------


def eval_rsi(close: pd.Series, volume: pd.Series, params: dict):
    """RSI超买: 只看最新一天 RSI 是否超阈值(单日信号, 不用时间窗)。"""
    period = int(params.get("period", 6))
    threshold = float(params.get("threshold", 80.0))
    rsi = float(fos.compute_rsi(close, period).iloc[-1])
    return rsi > threshold, round(rsi, 1)


def _uptrend_score(close: pd.Series, volume: pd.Series) -> tuple[int, int]:
    ma = uptrend_ma(close)
    score = 0
    evaluated = 0
    for cname, fn in UPTREND_CONDITIONS:
        try:
            res = fn(close, volume, ma)
        except Exception:  # noqa: BLE001
            res = None
        if res is None:
            continue
        evaluated += 1
        if res:
            score += 1
    return score, evaluated


def eval_uptrend(close: pd.Series, volume: pd.Series, params: dict):
    """上涨趋势: 只看当前天 8条件打分, 实际满足数 >= min_score 即命中。

    命中判定仍按实际满足数 s(不改变入选池); 当历史K线不足、只评估了部分
    条件(ev<8)时, 把得分按比例折算到 /8 统一口径(如 5/7->6/8, 7/7->8/8)。
    """
    min_score = int(params.get("min_score", 5))
    s, ev = _uptrend_score(close, volume)
    n = min(round(s * 8 / ev), 8) if ev > 0 else 0
    return s >= min_score, f"{n}/8"


def eval_uptrend_7d(close: pd.Series, volume: pd.Series, params: dict):
    """上涨趋势(7交易日窗口版): 7个短周期条件打分, 满足数 >= min_score 即命中。

    条件全部只用最近7根K线(站上MA7/MA7上行/低点抬高/高点抬高/斜率向上/近7日新高/放量);
    展示值按比例折算到 /8(如 7/7->8/8, 6/7->7/8), 使下游 8:8,7:6,6:4 加权映射仍适用。
    """
    min_score = int(params.get("min_score", 4))
    ma = uptrend_ma7(close)
    s = ev = 0
    for _cname, fn in UPTREND_CONDITIONS_7D:
        try:
            r = fn(close, volume, ma)
        except Exception:  # noqa: BLE001
            r = None
        if r is None:
            continue
        ev += 1
        if r:
            s += 1
    n = min(round(s * 8 / ev), 8) if ev > 0 else 0
    return s >= min_score, f"{n}/8"


def eval_pullback(close: pd.Series, volume: pd.Series, params: dict):
    """大涨回撤缩量: 只看当前天的 涨/撤/缩量 组合。

    返回 (是否命中, 展示值如 涨20% 撤8% 量比0.5)。
    """
    rally_pct = float(params.get("rally_pct", 15.0))
    pb_min = float(params.get("pullback_min", 3.0))
    pb_max = float(params.get("pullback_max", 20.0))
    shrink = float(params.get("vol_shrink_ratio", 0.7))
    if len(close) < 21 or len(volume) < 20:
        return False, "-"
    last_close = float(close.iloc[-1])
    chg20 = (last_close / float(close.iloc[-21]) - 1) * 100
    high20 = float(close[-20:].max())
    drawdown = (1 - last_close / high20) * 100 if high20 else 100.0
    vol5 = float(volume[-5:].mean())
    vol20 = float(volume[-20:].mean())
    ratio = vol5 / vol20 if vol20 else 9.0
    ok = (
        chg20 >= rally_pct
        and pb_min <= drawdown <= pb_max
        and ratio < shrink
    )
    return ok, f"涨{chg20:.0f}% 撤{drawdown:.0f}% 量比{ratio:.2f}"


EVALUATORS = {"rsi": eval_rsi, "uptrend": eval_uptrend, "uptrend7": eval_uptrend_7d,
              "pullback": eval_pullback}


# ---------- 单只股票处理 ----------

def process_one(
    market: str,
    ticker: str,
    strategies: dict,
    bars: int,
    min_price: float,
    min_volume: float,
    min_shares: float,
    max_stale_days: int,
    keep_shells: bool,
    full_info: dict,
) -> Optional[dict]:
    try:
        if fos._REQUEST_DELAY:
            import time
            time.sleep(fos._REQUEST_DELAY)
        info = fos.tencent_quote(market, ticker)
        if info is None:
            return None
        prefixed, name = info
        k = fos.tencent_kline(prefixed, bars, use_cache=fos._USE_CACHE)
        if k is None:
            return None
        close, volume = k
        if len(close) < 60:
            return None

        last_close = float(close.iloc[-1])
        # 停牌/退市
        if (date.today() - close.index[-1].date()).days > max_stale_days:
            return None
        # 价格
        if last_close < min_price:
            return None
        # 成交额
        mult = fos.VOLUME_MULT.get(market, 1.0)
        avg_vol = float((close * volume * mult).tail(20).mean()) if len(volume) else 0.0
        if avg_vol < min_volume:
            return None
        # 日均成交量(股)
        avg_shares = float(volume.tail(20).mean()) * mult
        if avg_shares < min_shares:
            return None
        # 壳股
        meta = full_info.get(prefixed) or full_info.get(ticker.upper()) or {}
        if not meta and market == "us" and ticker.upper().startswith("US"):
            meta = full_info.get(ticker.upper()[2:]) or {}  # 去掉 us 前缀再查
        if market == "cn":  # A股行业来自 data/cn_industry.json(东财)
            code6 = ticker[2:] if ticker[:2].lower() in ("sh", "sz", "bj") else ticker
            cn_ind = fos.load_cn_industry().get(code6, "")
            if cn_ind:
                meta = dict(meta)
                meta["industry"] = cn_ind
        full_name = meta.get("full_name") or name
        if not keep_shells and fos.is_shell_name(full_name):
            return None

        # 评估各策略
        res: dict[str, str] = {}
        passed: list[str] = []
        total_score = 0.0
        for sid, cfg in strategies.items():
            ev = EVALUATORS.get(cfg.get("type"))
            if ev is None:
                continue
            ok, val = ev(close, volume, cfg.get("params", {}))
            res[sid] = val if ok else ""
            if ok:
                passed.append(sid)
                total_score += float(cfg.get("weight", 1.0))

        if not passed:
            return None

        chg20 = (
            (last_close / float(close.iloc[-21]) - 1) * 100
            if len(close) > 21 else 0.0
        )
        # 短线动量(供板块代表股积分用): 当日/近2日/近3日累计涨跌幅
        if len(close) >= 4:
            chg1 = (last_close / float(close.iloc[-2]) - 1) * 100
            chg2 = (last_close / float(close.iloc[-3]) - 1) * 100
            chg3 = (last_close / float(close.iloc[-4]) - 1) * 100
        else:
            chg1 = chg2 = chg3 = 0.0
        return {
            "ticker": ticker,
            "prefixed": prefixed,
            "name": name,
            "sector": translate(meta.get("sector", "")),
            "industry": translate(meta.get("industry", "")),
            "date": str(close.index[-1].date()),
            "close": round(last_close, 2),
            "chg20": round(chg20, 1),
            "chg1": round(chg1, 2),
            "chg2": round(chg2, 2),
            "chg3": round(chg3, 2),
            "volume": int(avg_vol),
            "score": round(total_score, 2),
            "passed": ",".join(passed),
            **{sid: res.get(sid, "") for sid in strategies},
        }
    except Exception as exc:  # noqa: BLE001
        print(f"[警告] {ticker} 获取失败: {exc}", file=sys.stderr)
    return None


def run(market, tickers, strategies, args) -> list[dict]:
    results: list[dict] = []
    total = len(tickers)
    full_info = fos.load_full_info()
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {
            pool.submit(
                process_one, market, t, strategies, args.bars,
                args.min_price, args.min_volume, args.min_shares, args.max_stale_days,
                args.keep_shells, full_info,
            ): t
            for t in tickers
        }
        done = 0
        for fut in as_completed(futures):
            try:
                r = fut.result()
            except Exception as exc:  # noqa: BLE001  单只失败不中断全量
                ticker = futures[fut]
                print(f"[警告] {ticker} 处理异常: {exc}", file=sys.stderr)
                r = None
            done += 1
            if r:
                results.append(r)
            if total > 10:
                print(f"[进度] 已检查 {done}/{total} ...", end="\r", file=sys.stderr)
    if total > 10:
        print(file=sys.stderr)
    return results


def main() -> None:
    p = argparse.ArgumentParser(description="策略库运行器: 按需启用策略扫描")
    p.add_argument("--strategies", help="要启用的策略id, 逗号分隔(如 rsi,uptrend; 默认全部)")
    p.add_argument("--list-strategies", action="store_true", help="列出可用策略后退出")
    p.add_argument("--market", choices=["us", "cn", "hk"], default="us")
    p.add_argument("--list", choices=["all", "github", "builtin"], default="all")
    p.add_argument("--tickers", help="逗号分隔的代码(自动按市场加前缀)")
    p.add_argument("--limit", type=int, default=0)
    p.add_argument("--bars", type=int, default=fos.DEFAULT_BARS)
    p.add_argument("--workers", type=int, default=8)
    p.add_argument("--min-price", type=float, default=fos.DEFAULT_MIN_PRICE)
    p.add_argument("--min-volume", type=float, default=fos.DEFAULT_MIN_VOLUME)
    p.add_argument("--min-shares", type=float, default=fos.DEFAULT_MIN_SHARES,
                   help=f"近20日均成交量(股)下限(默认 {fos.DEFAULT_MIN_SHARES:,.0f})")
    p.add_argument("--max-stale-days", type=int, default=fos.DEFAULT_MAX_STALE_DAYS)
    p.add_argument("--keep-shells", action="store_true")
    p.add_argument("--delay", type=float, default=0.0)
    p.add_argument("--no-cache", action="store_true")
    p.add_argument("--with-intro", action="store_true",
                   help="给命中结果补充公司介绍/概况(A股=同花顺简介, 美股/港股=结构化概况)")
    p.add_argument("--output", help="结果保存为 CSV")
    args = p.parse_args()
    execute(args)


def execute(args) -> list[dict]:
    """执行扫描并返回命中结果(已按组合分+20日涨幅排序)。

    args 可以来自命令行解析, 也可以由其他脚本构造命名空间传入。
    """
    fos._REQUEST_DELAY = args.delay
    fos._USE_CACHE = not args.no_cache
    # 增量扫描: A股以"最新交易日"为K线缓存基准, 缓存已覆盖该日则复用, 不重复全量拉取
    if args.market == "cn":
        try:
            import cn_trading_days as _ctd  # noqa: PLC0415
            fos.set_kline_asof_ref(_ctd.last_trade_date())
        except Exception:  # noqa: BLE001  取不到基准就退回 mtime TTL 判断
            fos.set_kline_asof_ref(None)

    strategies = load_strategies()
    if args.list_strategies:
        print("可用策略:")
        for sid, cfg in strategies.items():
            print(f"  {sid:<12} {cfg.get('name','')}  type={cfg.get('type')}  "
                  f"params={cfg.get('params',{})}")
        return []

    if args.strategies:
        ids = [s.strip() for s in args.strategies.split(",") if s.strip()]
        strategies = load_strategies(ids)
    if not strategies:
        print("没有可用策略(检查 strategies/ 目录)。", file=sys.stderr)
        sys.exit(1)

    # 股票池
    if args.tickers:
        tickers = [fos.normalize_code(args.market, t)
                   for t in args.tickers.split(",") if t.strip()]
    else:
        tickers = fos.load_market_list(args.market, args.list, 500)
        if not tickers:
            tickers = fos.load_market_list(args.market, "builtin", 500)
    if args.limit and args.limit > 0:
        tickers = tickers[: args.limit]

    mkt_name = fos.MARKET_NAMES.get(args.market, args.market)
    strat_names = ",".join(strategies[s]["name"] for s in strategies)
    print(f"[信息] 策略: {strat_names}")
    print(f"[信息] 开始扫描 {len(tickers)} 只{mkt_name} ...")
    results = run(args.market, tickers, strategies, args)

    if not results:
        print("没有命中任何策略的股票。")
        return []

    results.sort(key=lambda x: (x["score"], x["chg20"]), reverse=True)

    # 给命中结果补充公司介绍/概况(只补命中, 不扫全量, 避免接口压力)
    if args.with_intro:
        from company_intro import company_intro  # noqa: PLC0415
        full_info = fos.load_full_info()
        for r in results:
            r["intro"] = company_intro(
                args.market, r.get("prefixed") or r["ticker"], r["ticker"], full_info
            )

    amt_header, amt_div = ("额($M)", 1e6) if args.market == "us" else ("额(亿)", 1e8)

    # 表头: 每启用一个策略就加一列
    strat_cols = list(strategies.keys())
    header = (f"{'代码':<9}{'名称':<12}{'行业':<16}{'收盘':>9}"
              + "".join(f"{strategies[s]['name'][:4]:<7}" for s in strat_cols)
              + f"{'组合分':>6}{'20日%':>8}"
              + (f"{'公司介绍':<46}" if args.with_intro else ""))
    print(f"\n命中 {len(results)} 只:")
    print(header)
    print("-" * len(header))
    for r in results:
        line = (f"{r['ticker']:<9}{(r['name'] or '')[:10]:<12}"
                f"{(r['industry'] or '-')[:14]:<16}{r['close']:>9.2f}")
        for s in strat_cols:
            line += f"{str(r.get(s) or '-'):<7}"
        line += f"{r['score']:>6.1f}{r['chg20']:>8.1f}"
        if args.with_intro:
            line += f"{(r.get('intro') or '')[:46]:<46}"
        print(line)

    if args.output:
        pd.DataFrame(results).to_csv(args.output, index=False, encoding="utf-8-sig")
        print(f"\n结果已保存到 {args.output}")
    return results


if __name__ == "__main__":
    main()
