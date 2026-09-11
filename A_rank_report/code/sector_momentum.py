#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
板块合计排名(A_rank_report_v1): 池内 趋势分/代表股短线动量分 按当日池内量级动态配平 得"总分"; 双口径入池。

个股动量(用户定义, 2026-09-10 起加强当日): m = 当日%×0.50 + 近2日%×0.30 + 近3日%×0.20
    当日% = 今收/昨收-1;  近2日% = 今收/2交易日前收-1;  近3日% = 今收/3交易日前收-1

展示口径(池内, "和以前一样"):
  趋势分 = 加权命中股平均分; 动量分(±10) = 前n只代表股 m 求和按 S/(1.4×只数) 归一;
  总分 = 趋势分×w_T + 动量分×w_M (动态配平, 见 dynamic_weights); 池内按总分降序; 代表股 = 加权分前10 命中股。

双口径入池(2026-09-06 用户口径):
  动量入池分(每板块) = 该板块"全部成分股"(东财行业, data/cn_sector_members.json)
    按 m 降序取前10 只的 m 之和(不足按实际只数);
  动量入池 = 动量入池分 前 top 板块 与 原行业得分前 top 并集(最多 2*top)。
  入池列: 趋势=按得分入池, 动量=按动量入池分入池, 趋势+动量=双口径。
全市场动量表 output/cn_momentum_YYYYMMDD.csv 按交易日缓存(行情走本地K线缓存, 缺失才联网)。
"""
from __future__ import annotations

import glob
import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import find_overbought_stocks as fos  # noqa: E402
import industry_score as isc  # noqa: E402

DEFAULT_MAP = {8: 8, 7: 6, 6: 4}
OUTPUT_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "output")

# 动量 m 权重(加强当日): 当日%×0.50 + 近2日%×0.30 + 近3日%×0.20, 和为1
MOM_W1, MOM_W2, MOM_W3 = 0.50, 0.30, 0.20
# 动量分归一除数: 权重和已由 3 变 1(加权 m 变小), 用 1.4(=7/5) 标定,
# 使动量分均值与"等权三条叠加+除5"的旧口径一致(A股实测 ~1.02×, 美股 ~1.00×), 保持 ±10 量级
MOM_NORM = 1.4

# 池内动态权重(2026-09-10 起): 固定 50/50 时, 两分量量级差太大(如趋势分均值 4.5 vs 动量分 2.8)
# -> 动量分实际只占总分约 39%, 等于白给。改为按当日池内两分量的量级动态配平:
#   以池内"平均绝对值"为量级基准(base-T / base-M), 权重取反比 -> 两分量对总分的**平均贡献相等**;
#   动量整体接近0的极端日按 [MOM_W_MIN, MOM_W_MAX] 截断, 避免权重失真。
MOM_W_MIN, MOM_W_MAX = 0.35, 0.65
# 展示倍率: 加权贡献再×2才与“未加权原始值”同一量级(w_T+w_M=1 -> 平均倍率 2×0.5=1),
# 否则加权后每个分值看起来“小一半”, 总分也对应偏小。
DISPLAY_SCALE = 2.0
LAST_WEIGHTS: tuple[float, float] = (0.5, 0.5)   # (趋势权重, 动量权重), 供报告脚注展示当日实际值


def dynamic_weights(trend, mom) -> tuple[float, float]:
    """按池内两分量量级动态配平: 返回 (趋势权重, 动量权重), 使两者的平均贡献相等。

    base-T = mean(|趋势分|), base-M = mean(|动量分|);  w_m = base-T/(base-T+base-M)。
    量级缺失/异常时退回 50/50。
    """
    try:
        base_t = float(pd.to_numeric(pd.Series(trend), errors="coerce").abs().mean())
        base_m = float(pd.to_numeric(pd.Series(mom), errors="coerce").abs().mean())
    except Exception:  # noqa: BLE001
        return 0.5, 0.5
    if not (base_t > 0 and base_m > 0):
        return 0.5, 0.5
    w_m = min(MOM_W_MAX, max(MOM_W_MIN, base_t / (base_t + base_m)))
    return round(1.0 - w_m, 4), round(w_m, 4)

# 动量代表股个数(◎橙色); 相比前一日新进入名单的用 ◆(报告端标紫)
MOM_REPS = 7
NEW_MOM_MARK = "◆"
OLD_MOM_MARK = "◎"


def _weighted_m(d1: float, d2: float, d3: float) -> float:
    """m = 当日%×0.50 + 近2日%×0.30 + 近3日%×0.20 (三段累计再加权, 权重和=1)。"""
    return round(d1 * MOM_W1 + d2 * MOM_W2 + d3 * MOM_W3, 2)


def size_factor(n: int) -> float:
    """板块数量因子: 从 0 只起按 0.003/只(=0.06/20) 连续线性递减;
    n>=120 封底 = 1 - 0.003×120 = 0.64, 之后不再减。乘到 趋势分/动量分/动量入池分/入池资格。"""
    n = int(n or 0)
    return round(max(0.64, 1.0 - 0.003 * n), 4)


_mem_sz: dict[str, int] | None = None


# 细分并入主板块(A股报告聚合口径): 现用东财二级行业, 煤炭已含焦炭, 无需合并
INDUSTRY_MERGE: dict[str, str] = {}


def _merge_ind(ind) -> str:
    return INDUSTRY_MERGE.get(str(ind), str(ind))


def _sector_size(ind: str, fallback: int) -> int:
    """板块全部成分股总数(合并后计数, cn_sector_members)=数量因子N; 缺失退回 fallback。"""
    global _mem_sz
    if _mem_sz is None:
        _mem_sz = {}
        for k, v in fos.load_cn_sector_members().items():
            t = _merge_ind(k)
            _mem_sz[t] = _mem_sz.get(t, 0) + len(v)
    n = _mem_sz.get(ind)
    return int(n if n else int(fallback))


def as_of_date(df: pd.DataFrame) -> date:
    """该结果数据的交易日(取 date 列最大值)。"""
    return pd.to_datetime(df["date"]).max().date()


def stock_momentum(prefixed: str, asof: date) -> float | None:
    """截至 asof(含)收盘: m = 当日%×0.50 + 近2日%×0.30 + 近3日%×0.20; 数据不足返回 None。

    必须“K线覆盖到 asof”: 缓存末根早于 asof(如缓存停在上一交易日)时, 强刷一次再算;
    否则会把上一交易日当成 asof 静默算出错值。
    """
    try:
        for force in (False, True):
            k = fos.tencent_kline(str(prefixed), 80, use_cache=not force)
            if k is None:
                continue
            close, _ = k
            c = close[close.index <= pd.Timestamp(asof)].astype(float).dropna()
            if len(c) >= 4 and c.index[-1].date() >= pd.Timestamp(asof).date():
                break
        if k is None or len(c) < 4:
            return None
        c0, c1, c2, c3 = c.iloc[-1], c.iloc[-2], c.iloc[-3], c.iloc[-4]
        if c0 <= 0 or c1 <= 0 or c2 <= 0 or c3 <= 0:
            return None
        d1 = (c0 / c1 - 1) * 100
        d2 = (c0 / c2 - 1) * 100
        d3 = (c0 / c3 - 1) * 100
        return _weighted_m(d1, d2, d3)
    except Exception:  # noqa: BLE001
        return None


def stock_daily_chg(prefixed: str, asof: date) -> float | None:
    """当日涨幅 = 今收/昨收 - 1(%), 供动量代表股括号展示; 数据不足返回 None。"""
    try:
        k = fos.tencent_kline(str(prefixed), 80, use_cache=True)
        if k is None:
            return None
        close, _ = k
        c = close[close.index <= pd.Timestamp(asof)].astype(float).dropna()
        if len(c) < 2:
            return None
        c0, c1 = c.iloc[-1], c.iloc[-2]
        if c0 <= 0 or c1 <= 0:
            return None
        return round((c0 / c1 - 1) * 100, 2)
    except Exception:  # noqa: BLE001
        return None


def _momentum_file_final(path: str, asof) -> bool:
    """当日动量表是否可信: 历史日期直接可信; 当日文件要求生成于A股收盘确认之后,
    否则是盘中快照(与当日K线缓存同理), 必须重算。"""
    try:
        d = pd.Timestamp(asof).date()
        if d < date.today():
            return True
        return os.path.getmtime(path) >= fos.bar_final_ts("cn", d)
    except Exception:  # noqa: BLE001
        return True


def _cached_stock_momentum(prefixed: str, asof: date, bars: int = 80) -> float | None:
    """只读本地缓存算 m(缓存缺失/过期/盘中快照不联网, 返回 None)。供 cache_only 快速扫描。"""
    cf = os.path.join(fos.KLINE_CACHE_DIR, f"{prefixed.replace('.', '_')}_{bars}.json")
    try:
        with open(cf, encoding="utf-8") as f:
            rows = json.load(f)
        fresh = bool(rows) and fos._kline_cache_fresh(cf, rows)
    except Exception:  # noqa: BLE001
        fresh = False
    return stock_momentum(prefixed, asof) if fresh else None


def market_momentum(asof: date, force: bool = False, workers: int = 10,
                    cache_only: bool = False) -> pd.DataFrame:
    """全市场成分股动量表: 板块全部成分股逐只 m=当日%×0.50+近2日%×0.30+近3日%×0.20。

    板块归属来自 data/cn_sector_members.json(东财行业); 行情优先本地K线缓存
    (当日扫描已预热), 缺失才联网; cache_only=True 时只读缓存、缺失不联网。
    结果持久化 output/cn_momentum_YYYYMMDD.csv, 同日再次调用直接读缓存。
    返回列: sector/code6/prefixed/m。
    """
    key = pd.Timestamp(asof).strftime("%Y%m%d")
    path = os.path.join(OUTPUT_DIR, f"cn_momentum_{key}.csv")
    if (not force) and os.path.exists(path) and _momentum_file_final(path, asof):
        try:
            return pd.read_csv(path, encoding="utf-8-sig")
        except Exception:  # noqa: BLE001
            pass
    # 增量: A股K线缓存以"最新交易日"为基准, 已覆盖则复用, 只补缺/过期(仅需联网时取一次)
    if fos._ASOF_REF is None:
        try:
            import cn_trading_days as _ctd  # noqa: PLC0415
            fos.set_kline_asof_ref(_ctd.last_trade_date())
        except Exception:  # noqa: BLE001
            fos.set_kline_asof_ref(None)
    members = fos.load_cn_sector_members()
    jobs = [(sec, code6, fos.cn_prefix(code6)) for sec, codes in members.items()
            for code6 in codes]
    work = _cached_stock_momentum if cache_only else stock_momentum
    rows: list[dict] = []
    done = 0
    t0 = time.time()
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futs = {pool.submit(work, pref, asof): (sec, code6, pref)
                for sec, code6, pref in jobs}
        for fut in as_completed(futs):
            sec, code6, pref = futs[fut]
            try:
                m = fut.result()
            except Exception:  # noqa: BLE001
                m = None
            if m is not None:
                rows.append({"sector": sec, "code6": code6, "prefixed": pref, "m": m})
            done += 1
            if done % 1000 == 0:
                print(f"[动量] 已算 {done}/{len(jobs)} 只, {time.time() - t0:.0f}s", file=sys.stderr)
    if not rows:
        print("[动量] 全市场动量无有效数据!", file=sys.stderr)
        return pd.DataFrame(columns=["sector", "code6", "prefixed", "m"])
    df = (pd.DataFrame(rows)
            .sort_values(["sector", "m"], ascending=[True, False])
            .reset_index(drop=True))
    try:
        os.makedirs(OUTPUT_DIR, exist_ok=True)
        df.to_csv(path, index=False, encoding="utf-8-sig")
        print(f"[动量] 全市场动量表 -> {path} ({len(df)} 只, cache_only={cache_only})", file=sys.stderr)
    except Exception:  # noqa: BLE001
        pass
    return df


def ensure_momentum_offline(asof, workers: int = 30) -> pd.DataFrame:
    """缺 cn_momentum_YYYYMMDD.csv 时, 从已预热的 320 根K线缓存离线生成并持久化。

    动量只需近4日收盘; 若 80 根缓存新鲜则直接读, 否则用 320 缓存截断生成 80 并写回
    (全程 0 联网)。供 delta/报告前兑底, 避免现场全市场联网重算卡死。
    返回与 market_momentum 同构的 DataFrame(sector/code6/prefixed/m)。
    """
    key = pd.Timestamp(asof).strftime("%Y%m%d")
    path = os.path.join(OUTPUT_DIR, f"cn_momentum_{key}.csv")
    if os.path.exists(path) and _momentum_file_final(path, asof):
        try:
            return pd.read_csv(path, encoding="utf-8-sig")
        except Exception:  # noqa: BLE001
            pass
    members = fos.load_cn_sector_members()
    jobs = [(sec, code6, fos.cn_prefix(code6))
            for sec, codes in members.items() for code6 in codes]
    kdir = fos.KLINE_CACHE_DIR

    def _offline(pref: str) -> float | None:
        f80 = os.path.join(kdir, f"{pref.replace('.', '_')}_80.json")
        f320 = os.path.join(kdir, f"{pref.replace('.', '_')}_320.json")

        def _covers(rs) -> bool:
            """缓存末根K线必须 >= asof, 否则会把上一交易日当成 asof 算出错值。"""
            try:
                return bool(rs) and pd.to_datetime(rs[-1][0]).date() >= pd.Timestamp(asof).date()
            except Exception:  # noqa: BLE001
                return False

        rows = None
        if os.path.exists(f80):
            try:
                with open(f80, encoding="utf-8") as f:
                    rows = json.load(f)
                if not rows or not fos._kline_cache_fresh(f80, rows) or not _covers(rows):
                    rows = None
            except Exception:  # noqa: BLE001
                rows = None
        if rows is None and os.path.exists(f320):
            try:
                with open(f320, encoding="utf-8") as f:
                    rows = json.load(f)[-80:]
                if not _covers(rows):
                    rows = None
                else:
                    with open(f80, "w", encoding="utf-8") as f:
                        json.dump(rows, f, ensure_ascii=False)
            except Exception:  # noqa: BLE001
                rows = None
        if not rows:
            return None
        try:
            close = (pd.Series([float(r[2]) for r in rows],
                               index=pd.to_datetime([r[0] for r in rows]))
                     .astype(float).dropna())
            c = close[close.index <= pd.Timestamp(asof)]
            if len(c) < 4:
                return None
            c0, c1, c2, c3 = c.iloc[-1], c.iloc[-2], c.iloc[-3], c.iloc[-4]
            if c0 <= 0 or c1 <= 0 or c2 <= 0 or c3 <= 0:
                return None
            return _weighted_m((c0 / c1 - 1) * 100, (c0 / c2 - 1) * 100,
                               (c0 / c3 - 1) * 100)
        except Exception:  # noqa: BLE001
            return None

    rows_out: list[dict] = []
    done = 0
    t0 = time.time()
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futs = {pool.submit(_offline, pref): (sec, code6, pref)
                for sec, code6, pref in jobs}
        for fut in as_completed(futs):
            sec, code6, pref = futs[fut]
            try:
                m = fut.result()
            except Exception:  # noqa: BLE001
                m = None
            if m is not None:
                rows_out.append({"sector": sec, "code6": code6, "prefixed": pref, "m": m})
            done += 1
            if done % 1000 == 0:
                print(f"[动量离线] {done}/{len(jobs)} 只, {time.time() - t0:.0f}s", file=sys.stderr)
    if not rows_out:
        print("[动量离线] 320缓存缺失严重, 无有效数据!", file=sys.stderr)
        return pd.DataFrame(columns=["sector", "code6", "prefixed", "m"])
    df = (pd.DataFrame(rows_out)
            .sort_values(["sector", "m"], ascending=[True, False])
            .reset_index(drop=True))
    try:
        os.makedirs(OUTPUT_DIR, exist_ok=True)
        df.to_csv(path, index=False, encoding="utf-8-sig")
        print(f"[动量离线] -> {path} ({len(df)} 只)", file=sys.stderr)
    except Exception:  # noqa: BLE001
        pass
    return df


def sector_momentum_entry(asof: date, force: bool = False,
                          cache_only: bool = False) -> pd.DataFrame:
    """每板块"动量入池分" = 全部成分股按 m 降序 前10 只 m 之和(不足按实际只数)。

    返回列: sector / 动量入池分 / 动量股数。
    """
    df = market_momentum(asof, force=force, cache_only=cache_only)
    if df.empty:
        return pd.DataFrame(columns=["sector", "动量入池分", "动量股数"])
    top = df.sort_values("m", ascending=False).groupby("sector", sort=False).head(10)
    out = (top.groupby("sector", sort=False)
              .agg(动量入池分=("m", "sum"), 动量股数=("m", "count"))
              .reset_index())
    out["动量入池分"] = out["动量入池分"].round(2)
    return out


def _cn_name(prefixed: str) -> str:
    """A股中文名: 报价走 tencent_quote(缓存优先, 缺失联网补); 取不到返回空串。"""
    try:
        q = fos.tencent_quote("cn", prefixed)
        return str(q[1]).strip() if q and q[1] else ""
    except Exception:  # noqa: BLE001
        return ""


def _fmt_momentum_rep(prefixed: str, code6: str, chg1: float, new: bool = False) -> str:
    """动量代表股文本: ◎名字(+当日涨幅%) / ◆名字(相比前日新进入动量代表股)。"""
    name = _cn_name(prefixed)
    label = name or prefixed
    mark = NEW_MOM_MARK if new else OLD_MOM_MARK
    return f"{mark}{label}({chg1:+.0f}%)"


def _prev_momentum_reps(asof: date) -> dict[str, set[str]]:
    """前一交易日各板块的动量代表股(prefixed 集合, 按 m 前 MOM_REPS), 供标记今日新进。

    直接读已缓存的 cn_momentum_<前一交易日>.csv(离线, 0 联网); 无缓存返回 {}。
    """
    key = pd.Timestamp(asof).strftime("%Y%m%d")
    cands = sorted(glob.glob(os.path.join(OUTPUT_DIR, "cn_momentum_*.csv")))
    prev = [p for p in cands
            if os.path.basename(p)[len("cn_momentum_"):-4] < key]
    if not prev:
        return {}
    try:
        df = pd.read_csv(prev[-1], encoding="utf-8-sig")
    except Exception:  # noqa: BLE001
        return {}
    if df.empty or "m" not in df.columns:
        return {}
    df = df.assign(sector=df["sector"].astype(str).map(_merge_ind))
    top = df.sort_values("m", ascending=False).groupby("sector", sort=False).head(MOM_REPS)
    return {str(i): set(g["prefixed"].astype(str))
            for i, g in top.groupby("sector", sort=False)}


def _row_momentum(r) -> float | None:
    """优先用扫描时已存的 chg1/chg2/chg3(=当日/近2日/近3日累计%); 缺失则联网K线回退。"""
    try:
        has = all(pd.notna(r.get(k)) for k in ("chg1", "chg2", "chg3"))
        if has:
            return _weighted_m(float(r["chg1"]), float(r["chg2"]), float(r["chg3"]))
    except Exception:  # noqa: BLE001
        pass
    # 回退: 联网/缓存取K线按 asof 切片
    # 需要 asof, 在调用处处理
    return None


def industry_momentum_stats(rep_rows: pd.DataFrame, asof: date) -> tuple[int, float, float]:
    """某板块代表股动量: 返回 (只数 n, 板块S, 动量分 pts 允许为负, ±10 封顶)。"""
    n, S = 0, 0.0
    for _, r in rep_rows.iterrows():
        m = _row_momentum(r)
        if m is None:
            pref = r.get("prefixed") or r.get("ticker")
            m = stock_momentum(pref, asof) if pref is not None else None
        if m is not None:
            n += 1
            S += m
    if n == 0:
        return 0, 0.0, 0.0
    pts = S / (MOM_NORM * n)   # 权重和已由3变1, 除 1.4 标定回原量级, 允许为负, ±10 封顶
    pts = max(-10.0, min(10.0, pts))
    return n, round(S, 2), round(pts, 2)


def industry_momentum_scores(df: pd.DataFrame, col: str = "uptrend",
                             mapping: dict | None = None,
                             cache_only: bool = False) -> pd.DataFrame:
    """当日"全部有命中股的行业"的分数(供新进/退出评价用)。

    返回列: industry / 趋势分(加权命中平均) / 动量分(±10, 与动量入池分同源归一) / 动量入池分。
    """
    mapping = mapping or DEFAULT_MAP
    df = df.copy()
    if "industry" in df.columns:
        df["industry"] = df["industry"].astype(str).map(_merge_ind)
    agg = isc.aggregate(df, col, mapping)
    asof = as_of_date(df)
    entry = sector_momentum_entry(asof, cache_only=cache_only)
    if not entry.empty:
        entry = (entry.assign(sector=entry["sector"].astype(str).map(_merge_ind))
                     .groupby("sector", sort=False)
                     .agg(动量入池分=("动量入池分", "sum"), 动量股数=("动量股数", "sum"))
                     .reset_index())
    raw = dict(zip(entry["sector"], entry["动量入池分"])) if not entry.empty else {}
    cnt = dict(zip(entry["sector"], entry["动量股数"])) if not entry.empty else {}
    rows = []
    for _, a in agg.iterrows():
        ind = a["industry"]
        f = size_factor(_sector_size(str(ind), int(a["股票数"])))
        r, c = raw.get(ind), cnt.get(ind)
        pts = 0.0
        if r is not None and c is not None and int(c) > 0:
            pts = round(max(-10.0, min(10.0, float(r) / (MOM_NORM * int(c)))) * f, 2)
        raw_adj = round(float(r) * f, 2) if r is not None else None
        rows.append({"industry": ind, "趋势分": round(float(a["平均分"]) * f, 2),
                     "动量分": pts, "动量入池分": raw_adj})
    return pd.DataFrame(rows)


# 入池下限(用户口径, 2026-09-11): 展示动量分 < 2 或 总分 < 5 的板块按“出池”对待。
# 注意权重与总分互相依赖(权重按池内量级配平), 故“配权→算总分→筛掉不达标”迭代到稳定。
POOL_MIN_MOM = 2.0
POOL_MIN_TOTAL = 5.0


def pool_floor_reason(mom: float, total: float, min_mom: float = POOL_MIN_MOM,
                      min_total: float = POOL_MIN_TOTAL) -> str:
    """出池原因文本(达标返回空串): "动量<2" / "总分<5" / "动量<2+总分<5"。"""
    r = []
    if mom < min_mom:
        r.append(f"动量<{min_mom:g}")
    if total < min_total:
        r.append(f"总分<{min_total:g}")
    return "+".join(r)


def apply_pool_floor(df: pd.DataFrame, min_mom: float = POOL_MIN_MOM,
                     min_total: float = POOL_MIN_TOTAL) -> pd.DataFrame:
    """算 趋势贡献/动量贡献/总分, 并标出每行是否达标: **池内**(1/0) 与 **出池原因**。

    达标线(用户口径): 动量分 >= min_mom 且 总分 >= min_total, 否则视为“出池”。
    注意: **返回全部行**(不剔除), 出池行保留分数——升降表要用它们的当日分数与评价;
    调用方按 `池内 == 1` 取真正的池子。
    权重与总分互相依赖(权重按池内量级配平), 故“配权->算总分->剔不达标”迭代到稳定(<=5轮)。
    """
    global LAST_WEIGHTS
    cur = df
    for _ in range(5):
        if cur.empty:
            LAST_WEIGHTS = (0.5, 0.5)
            break
        wt, wm = dynamic_weights(cur["趋势分"], cur["动量分"])
        LAST_WEIGHTS = (wt, wm)
        tmp = cur.copy()
        tmp["趋势贡献"] = (tmp["趋势分"] * wt * DISPLAY_SCALE).round(2)
        tmp["动量贡献"] = (tmp["动量分"] * wm * DISPLAY_SCALE).round(2)
        tmp["总分"] = (tmp["趋势贡献"] + tmp["动量贡献"]).round(2)
        bad = (tmp["动量贡献"] < min_mom) | (tmp["总分"] < min_total)
        cur = tmp
        if not bool(bad.any()):
            break
        cur = tmp[~bad]
    # 用最终权重(固定点)给**全部**行打分与打标
    out = df.copy()
    wt, wm = LAST_WEIGHTS
    out["趋势贡献"] = (out["趋势分"] * wt * DISPLAY_SCALE).round(2)
    out["动量贡献"] = (out["动量分"] * wm * DISPLAY_SCALE).round(2)
    out["总分"] = (out["趋势贡献"] + out["动量贡献"]).round(2)
    out["池内"] = ((out["动量贡献"] >= min_mom) & (out["总分"] >= min_total)).astype(int)
    out["出池原因"] = [pool_floor_reason(m, t, min_mom, min_total)
                     for m, t in zip(out["动量贡献"], out["总分"])]
    return out


def combined_rank(df: pd.DataFrame, col: str = "uptrend",
                  mapping: dict | None = None, top: int = 15,
                  momentum_force: bool = False, cache_only: bool = False) -> pd.DataFrame:
    """双口径入池 A_rank 表。

    动量(展示与入池同源, 基于板块全部成分股):
      板块成分股按 m=当日%×50%+近2日%×30%+近3日%×20% 降序前10(不足按实际只数), S=Σm;
      动量入池分 = S(原始);  展示动量分(±10) = S ÷ (1.4×动量股数);
      入池 = 原行业"得分"前 top ∪ "动量入池分"前 top(并集, 最多 2*top)。
    池内按 总分 = 趋势贡献 + 动量贡献 降序; 列含 趋势分/动量分(原始) 与 趋势贡献/动量贡献(加权后, 相加=总分)。
    代表股列 = 趋势代表股(加权分前5) + ◎动量代表股(全部成分股按 m 前5, ◎=短线动量, 报告端标色)。
    列: industry/得分/股票数/趋势分/动量分/总分/动量入池分/代表股/入池
    """
    mapping = mapping or DEFAULT_MAP
    df = df.copy()
    if "industry" in df.columns:
        df["industry"] = df["industry"].astype(str).map(_merge_ind)
    agg = isc.aggregate(df, col, mapping)
    asof = as_of_date(df)
    entry = sector_momentum_entry(asof, force=momentum_force, cache_only=cache_only)
    if not entry.empty:
        entry = (entry.assign(sector=entry["sector"].astype(str).map(_merge_ind))
                     .groupby("sector", sort=False)
                     .agg(动量入池分=("动量入池分", "sum"), 动量股数=("动量股数", "sum"))
                     .reset_index())
    raw_map = dict(zip(entry["sector"], entry["动量入池分"])) if not entry.empty else {}
    cnt_map = dict(zip(entry["sector"], entry["动量股数"])) if not entry.empty else {}

    def _display_pts(raw, cnt) -> float:
        """动量分(±10) = S/(1.4×动量股数), 与动量入池分同源归一(1.4 为标定回旧口径量级的除数)。"""
        if raw is None or cnt is None or int(cnt) <= 0:
            return 0.0
        pts = float(raw) / (MOM_NORM * int(cnt))
        return round(max(-10.0, min(10.0, pts)), 2)

    # 1) 趋势代表股(命中股加权分前5)
    trend_reps = isc.representative_frame(df, col, mapping, 5)
    trend_by: dict[str, list] = {}
    if not trend_reps.empty:
        for ind, g in trend_reps.groupby("industry", sort=False):
            items = []
            for _, r in g.iterrows():
                chg = r.get("chg20")
                chg_s = f"{chg:+.0f}%" if pd.notna(chg) else ""
                trend_s = str(r.get(col, "")) if col in r else ""
                items.append(f"{r['name']}({trend_s},{chg_s})")
            trend_by[ind] = items

    # 2) 动量代表股(板块全部成分股按 m 前 MOM_REPS, ◎标注; ◆=相比前日新进入)
    mom = market_momentum(asof, force=momentum_force, cache_only=cache_only)
    if not mom.empty:
        mom = mom.assign(sector=mom["sector"].astype(str).map(_merge_ind))
    prev_reps = _prev_momentum_reps(asof)
    mom_by: dict[str, list] = {}
    if not mom.empty:
        top_n = mom.sort_values("m", ascending=False).groupby("sector", sort=False).head(MOM_REPS)
        for ind, g in top_n.groupby("sector", sort=False):
            reps = []
            for _, r in g.iterrows():
                chg1 = stock_daily_chg(str(r["prefixed"]), asof)
                pref = str(r["prefixed"])
                is_new = bool(prev_reps) and pref not in prev_reps.get(str(ind), set())
                reps.append(_fmt_momentum_rep(
                    pref, str(r["code6"]),
                    chg1 if chg1 is not None else float(r["m"]), new=is_new))
            mom_by[ind] = reps

    # 数量因子(不对称, N=板块全部成分股总数): <20轻度加成, >20每多20减0.06, >=120封底0.70(入围与排序均乘)
    fmap = {str(a["industry"]): size_factor(_sector_size(str(a["industry"]), int(a["股票数"])))
            for _, a in agg.iterrows()}

    # 3) 两条入池路径各自取前 top(入围也乘数量因子; 并列按板块名稳定排序)
    _agg = agg.copy()
    _agg["_f"] = _agg.apply(lambda r: size_factor(_sector_size(str(r["industry"]), int(r["股票数"]))), axis=1)
    _agg["_s"] = _agg["得分"] * _agg["_f"]
    score_top = set(_agg.sort_values("_s", ascending=False).head(top)["industry"])
    mom_sorted = sorted(raw_map.items(),
                        key=lambda kv: (kv[1] * fmap.get(kv[0], 1.0), kv[0]),
                        reverse=True)
    mom_top = {ind for ind, _ in mom_sorted[:top]}

    # 4) 并集入池, 池内按 总分(趋势/动量动态配平) 降序
    rows = []
    for _, a in agg.iterrows():
        ind = a["industry"]
        if ind not in score_top and ind not in mom_top:
            continue
        src = []
        if ind in score_top:
            src.append("趋势")
        if ind in mom_top:
            src.append("动量")
        pts = _display_pts(raw_map.get(ind), cnt_map.get(ind))
        f = size_factor(_sector_size(str(ind), int(a["股票数"])))
        rawv = raw_map.get(ind)
        pts = round(pts * f, 2)
        trend = round(float(a["平均分"]) * f, 2)      # 结构分=>趋势分, 乘数量因子
        total = round(trend * 0.5 + pts * 0.5, 2)
        reps = "、".join((trend_by.get(ind, [])[:5]) + (mom_by.get(ind, [])[:MOM_REPS]))
        rows.append({
            "industry": ind,
            "得分": int(a["得分"]),
            "股票数": int(a["股票数"]),
            "趋势分": trend,
            "动量分": pts,
            "动量入池分": round(rawv * f, 2) if rawv is not None else None,
            "代表股": reps or "-",
            "入池": "+".join(src),
        })
    out = pd.DataFrame(rows)
    # 池内动态配权 + 入池下限(动量分<2 或 总分<5 -> 标为出池; 行仍保留, 供升降表显示)
    out = apply_pool_floor(out)
    if out.empty:
        return out
    # 池内在前, 各自按总分降序(排名只给池内行, 见调用方)
    return out.sort_values(["池内", "总分"], ascending=[False, False]).reset_index(drop=True)


if __name__ == "__main__":
    argv = sys.argv[1:]
    # 构建全市场动量表: python sector_momentum.py --momentum [YYYY-MM-DD|YYYYMMDD] [--force] [--cache-only]
    if argv and argv[0] == "--momentum":
        d = argv[1] if len(argv) > 1 else pd.Timestamp.today().strftime("%Y-%m-%d")
        asof = pd.Timestamp(d).date()
        if "--offline" in argv:
            tbl = ensure_momentum_offline(asof, workers=30)
            print(f"数据日期: {asof}  离线动量表 {len(tbl)} 只")
            print(tbl.groupby("sector")["m"].count().rename("只数").head(20).to_string())
            sys.exit(0)
        tbl = sector_momentum_entry(asof, force="--force" in argv,
                                    cache_only="--cache-only" in argv)
        print(f"数据日期: {asof}  板块数: {len(tbl)}")
        print(tbl.sort_values("动量入池分", ascending=False)
                .rename(columns={"sector": "板块"})
                .head(20).to_string(index=False))
        sys.exit(0)
    # 自测: python sector_momentum.py <results.csv>
    f = argv[0] if argv else "output/cn_uptrend_0902.csv"
    df = pd.read_csv(f, encoding="utf-8-sig")
    rk = combined_rank(df)
    rk.insert(0, "排名", range(1, len(rk) + 1))
    pd.set_option("display.width", 200)
    pd.set_option("display.max_colwidth", 50)
    print(f"数据日期: {as_of_date(df)}  (文件 {f})  入池数: {len(rk)}")
    cols = ["排名", "industry", "得分", "股票数", "趋势分", "动量分", "总分", "动量入池分", "入池"]
    print(rk[cols].to_string(index=False))
