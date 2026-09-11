#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
HK_rank: 港股板块 A_rank（与 A股 A_rank_report_v2 同口径，按东财港股行业分组）

口径(与A股 V2 / 美股 V2 完全一致):
  - 个股: 上涨趋势 V2 9条件(7交易日窗口: 站上MA7/MA7上行/低点抬高/高点抬高/斜率向上/
    近7日新高/放量/7窗口持续性[>=6窗涨>0 且 >=4窗涨>2%]/7窗口量能放大), 满足>=6 命中;
    展示值按比例折算到 /8
  - 行业加权 8/8→8分、7/8→6分、6/8→4分(6分以下命中但计0)
  - 趋势分 = 行业得分/计入股票数 × 数量因子f ; f = max(0.64, 1-0.003N), N=板块参与只数
  - 个股动量 m = 当日%×0.50 + 近2日%×0.30 + 近3日%×0.20 (加强当日权重)
  - 动量入池分(每板块) = 全部成分股按 m 降序前10 只 m 之和(不足按实际只数) ×f
  - 展示动量分(±10) = S/(1.4×动量股数) ×f
  - 入池 = 趋势腿(得分×f 前 top) ∪ 动量腿(动量入池分×f 前 top)(并集, 最多 2*top)
  - 池内按 总分 = 趋势分/动量分 动态配平(按当日池内量级) 降序
  - 代表股 = 趋势代表股(加权分前5, 名称(8/8,+20日%)) + ◎动量代表股(全部成分股按m前7, ◎名称(+当日%))

参与门槛(用户口径): 近5日平均成交额(收盘价×成交量) > 4000万港元; 不满足的既不进动量成员表
也不计分(不参与板块计分)。

数据源: data/hk_tickers.txt(腾讯港股代码) + data/hk_em_industry.json(东财港股行业/简称)
        + 腾讯港股日K(320根, qfq)。
目录: HK_rank_report/code → 输出 ../output/hk_*_YYYYMMDD.csv ; 行业缓存写共享 data/。

用法:
  python hk_rank.py --scan --workers 30                # 全市场扫描(当日K线缓存), 产出 hits+动量表
  python hk_rank.py --rank --top 7                     # 由当日 hits+动量表 算板块入池排名
  python hk_rank.py --delta                            # 与前一日排名升降
  python hk_rank.py --limit 200 --scan --rank --delta  # 小批量联调(先跑通再全量)
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date

import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))           # .../HK_rank_report/code
ROOT = os.path.dirname(HERE)                                 # .../HK_rank_report
REPO = os.path.dirname(ROOT)                                 # .../stock_monitor_copilot
DATA = os.path.join(REPO, "data")
OUT = os.path.join(ROOT, "output")
A_CODE = os.path.join(REPO, "A_rank_report", "code")
for p in (HERE, A_CODE):
    if p not in sys.path:
        sys.path.insert(0, p)

import find_overbought_stocks as fos  # noqa: E402
from run_strategy import eval_uptrend_7d  # noqa: E402

BARS = 320            # 需覆盖 MA200/近60日
SCORE_MAP = {8: 8, 7: 6, 6: 4}
MIN_SCORE = 6
# 参与门槛(用户口径): 近5日平均成交额(收盘价×成交量, 港元) > 4000万
MIN_TURNOVER5 = 40_000_000.0
TURNOVER_WIN = 5              # 近5日
MAX_STALE_DAYS = 30           # 港股停牌/仙股多, 停牌超此天数不参与(仅最新日扫描时用)
# 动量 m 权重(加强当日): 当日%×0.50 + 近2日%×0.30 + 近3日%×0.20, 和为1
MOM_W1, MOM_W2, MOM_W3 = 0.50, 0.30, 0.20
# 动量代表股个数(◎橙色); 相比前一日新进入名单的用 ◆(报告端标紫)
MOM_REPS = 7
NEW_MOM_MARK = "◆"
OLD_MOM_MARK = "◎"
# 动量分归一除数: 权重和已由 3 变 1(加权 m 变小), 用 1.4(=7/5) 标定,
# 使动量分均值与"等权三条叠加+除5"的旧口径一致(美股实测 ~1.00×, A股 ~1.02×), 保持 ±10 量级
MOM_NORM = 1.4

# 池内动态权重(与A股同一套, 2026-09-10 起): 固定 50/50 会让量级小的一侧形同虚设,
# 改为按当日池内两分量"平均绝对值"取反比权重, 使两者对总分的平均贡献相等。
MOM_W_MIN, MOM_W_MAX = 0.35, 0.65
DISPLAY_SCALE = 2.0   # 展示倍率: 加权贡献×2 才与未加权原始值同量级(平均倍率=1)
LAST_WEIGHTS: tuple[float, float] = (0.5, 0.5)   # (趋势权重, 动量权重)


def dynamic_weights(trend, mom) -> tuple[float, float]:
    """按池内两分量量级动态配平: 返回 (趋势权重, 动量权重), 使两者的平均贡献相等。"""
    try:
        base_t = float(pd.to_numeric(pd.Series(trend), errors="coerce").abs().mean())
        base_m = float(pd.to_numeric(pd.Series(mom), errors="coerce").abs().mean())
    except Exception:  # noqa: BLE001
        return 0.5, 0.5
    if not (base_t > 0 and base_m > 0):
        return 0.5, 0.5
    w_m = min(MOM_W_MAX, max(MOM_W_MIN, base_t / (base_t + base_m)))
    return round(1.0 - w_m, 4), round(w_m, 4)
EXCLUDE_INDUSTRY: set[str] = set()          # 港股行业无需剔除(东财港股行业 31 类)
TICKERS_FILE = os.path.join(DATA, "hk_tickers.txt")           # 腾讯港股代码(hk00700)
UNIVERSE_FILE = os.path.join(DATA, "hk_universe.json")
EM_INDUSTRY_FILE = os.path.join(DATA, "hk_em_industry.json")  # 东财港股行业+简称

_em_map: dict[str, dict] | None = None


def _load_em_industry() -> dict[str, dict]:
    """东财港股 {5位代码: {"name": 简称, "ind": 行业}}; 不存在返回空。"""
    global _em_map
    if _em_map is None:
        try:
            with open(EM_INDUSTRY_FILE, encoding="utf-8") as f:
                _em_map = json.load(f)
        except Exception:  # noqa: BLE001
            _em_map = {}
    return _em_map

os.makedirs(OUT, exist_ok=True)


# ---------------- 全市场股票池(含东财港股行业归属) ----------------

def ensure_universe(force: bool = False) -> list[dict]:
    """构建/读取港股 universe(东财港股行业归属)。

    来源 data/hk_tickers.txt(腾讯港股代码, 如 hk00700; 去掉 hk8xxxx 人民币柜台重复代码)
    + data/hk_em_industry.json(东财港股F10: 简称 + 所属行业)。
    没有东财行业归属的代码不参与(无法分组)。
    写缓存 data/hk_universe.json。
    """
    if not force and os.path.exists(UNIVERSE_FILE):
        try:
            with open(UNIVERSE_FILE, encoding="utf-8") as f:
                return json.load(f)
        except Exception:  # noqa: BLE001
            pass
    em = _load_em_industry()
    recs: list[dict] = []
    seen: set[str] = set()
    no_ind = 0
    if not os.path.exists(TICKERS_FILE):
        print(f"[universe] 缺少 {TICKERS_FILE}", file=sys.stderr)
        return recs
    with open(TICKERS_FILE, encoding="utf-8") as f:
        for line in f:
            prefixed = line.strip()
            if not prefixed.startswith("hk"):
                continue
            code6 = prefixed[2:]
            # 人民币柜台(8xxxx)与港币柜台同股, 只留港币柜台避免重复计分
            if code6.startswith("8"):
                continue
            if len(code6) != 5 or not code6.isdigit() or code6 in seen:
                continue
            info = em.get(code6) or {}
            ind = str(info.get("ind") or "").strip()
            if not ind or ind in EXCLUDE_INDUSTRY:
                no_ind += 1
                continue
            seen.add(code6)
            recs.append({
                "symbol": code6,
                "code6": code6,
                "prefixed": "hk" + code6,
                "industry": ind,
                "name": str(info.get("name") or "").strip() or prefixed,
            })
    with open(UNIVERSE_FILE, "w", encoding="utf-8") as f:
        json.dump(recs, f, ensure_ascii=False)
    print(f"[universe] 港股池 {len(recs)} 只(缺东财行业 {no_ind} 只) -> {UNIVERSE_FILE}",
          file=sys.stderr)
    return recs


# ---------------- 单只: 计算 趋势分 与 动量 ----------------

def _momentum_close(close: pd.Series) -> float | None:
    """m = 当日%×0.50 + 近2日%×0.30 + 近3日%×0.20 (加强当日权重), 数据不足返回 None。"""
    c = close.astype(float).dropna()
    if len(c) < 4:
        return None
    c0, c1, c2, c3 = c.iloc[-1], c.iloc[-2], c.iloc[-3], c.iloc[-4]
    if c0 <= 0 or c1 <= 0 or c2 <= 0 or c3 <= 0:
        return None
    return round((c0 / c1 - 1) * 100 * MOM_W1
                 + (c0 / c2 - 1) * 100 * MOM_W2
                 + (c0 / c3 - 1) * 100 * MOM_W3, 2)


def _chg1(close: pd.Series) -> float | None:
    c = close.astype(float).dropna()
    if len(c) < 2:
        return None
    c0, c1 = c.iloc[-1], c.iloc[-2]
    return round((c0 / c1 - 1) * 100, 2) if c0 > 0 and c1 > 0 else None


def scan_one(rec: dict, no_cache: bool, asof: date | None = None,
             live: bool = False) -> tuple[dict | None, dict | None]:
    """处理单只: 返回 (hits行, 动量成员行)。asof 给定时只算到该交易日(历史回补/截断到已收盘日)。

    参与门槛(用户口径): 近5日平均成交额(收盘价×成交量, 港元) > 4000万 才算参与;
    不满足的股票既不进动量成员表也不算命中(不参与板块计分)。
    live=True 时额外做停牌过滤(最新日扫描才用)。
    """
    try:
        k = fos.tencent_kline(rec["prefixed"], BARS, use_cache=not no_cache)
        if k is None:
            return None, None
        return eval_stock(rec, k[0], k[1], asof, live)
    except Exception as exc:  # noqa: BLE001
        print(f"[警告] {rec['prefixed']} 处理失败: {exc}", file=sys.stderr)
        return None, None


def eval_stock(rec: dict, close: pd.Series, volume: pd.Series,
               asof: date | None = None, live: bool = False
               ) -> tuple[dict | None, dict | None]:
    """已拿到K线序列后的统一评估(联网扫描与离线历史回补共用): 返回 (hits行, 动量成员行)。

    asof: 只算到该交易日(含); live: 额外做停牌过滤(最新日扫描才用)。
    """
    try:
        close = close.astype(float).dropna()
        if asof is not None:
            close = close[close.index.date <= asof]
        if len(close) < 4:
            return None, None
        last_date = close.index[-1].date()
        # 停牌过滤只在“最新日”扫描时用; 历史回补不因停牌剔除
        if live and (date.today() - last_date).days > MAX_STALE_DAYS:
            return None, None
        last = float(close.iloc[-1])
        if last <= 0:
            return None, None
        # ---- 参与门槛: 近5日平均成交额(收盘×量, 港元) > 4000万 ----
        vol = volume.reindex(close.index).fillna(0.0)
        nv = min(TURNOVER_WIN, len(vol))
        avg_turnover = float((close * vol).tail(nv).mean()) if nv else 0.0
        if avg_turnover < MIN_TURNOVER5:
            return None, None
        m = _momentum_close(close)
        c1 = _chg1(close)
        # 动量成员: 参与门槛已过, 只要 m 可算就计入(不受是否命中趋势影响)
        mom_row = {"industry": rec["industry"], "prefixed": rec["prefixed"],
                   "code6": rec.get("code6") or rec["prefixed"][2:],
                   "name": rec.get("name"),
                   "m": m, "chg1": c1, "turnover5": round(avg_turnover, 1),
                   "date": str(last_date)} \
            if m is not None else None
        if len(close) < 60:
            return None, mom_row
        hit_ok, val = eval_uptrend_7d(close, vol, {"min_score": MIN_SCORE})
        if not hit_ok:
            return None, mom_row
        chg20 = round((last / float(close.iloc[-21]) - 1) * 100, 1) if len(close) > 21 else 0.0
        row = {
            "ticker": rec["symbol"], "prefixed": rec["prefixed"],
            "code6": rec.get("code6") or rec["prefixed"][2:],
            "name": rec["name"], "industry": rec["industry"],
            "date": str(last_date), "close": round(last, 3), "chg20": chg20,
            "m": m, "chg1": c1, "uptrend": val,
            "turnover5": round(avg_turnover, 1),
        }
        return row, mom_row
    except Exception as exc:  # noqa: BLE001
        print(f"[警告] {rec['prefixed']} 处理失败: {exc}", file=sys.stderr)
        return None, None


def _latest_final_session(probe: int = 6) -> date | None:
    """探测最新**已收盘确认**的港股交易日。

    做法: 取几只高成交标的的新鲜K线(不走缓存), 从最新一根往前走, 取第一根满足
    “已过港股收盘确认时刻”(bar_final_ts('hk', d) = 当日 16:05)的K线日期。
    这样既不会复用上一交易日的缓存(仅靠 mtime TTL 会), 也不会把当日盘中/未开盘的
    半根K线当成完整数据。
    """
    try:
        recs = ensure_universe()[:max(1, probe)]
    except Exception:  # noqa: BLE001
        return None
    now = time.time()
    best: date | None = None
    for r in recs:
        try:
            k = fos.tencent_kline(r["prefixed"], BARS, use_cache=False)
        except Exception:  # noqa: BLE001
            continue
        if not k:
            continue
        close = k[0].astype(float).dropna()
        for ts in reversed(list(close.index)):
            d = ts.date()
            if now >= fos.bar_final_ts("hk", d):
                best = d if best is None else max(best, d)
                break
    return best


def scan_day(day_key: str, workers: int, no_cache: bool, limit: int,
             force: bool = False) -> tuple[pd.DataFrame, str]:
    """全市场扫描当日: 产出 hits 与 动量成员两表(日期=最后K线交易日)。

    返回 (hits df, 真实日期键 YYYYMMDD)。写文件:
      output/hk_uptrend_{day}.csv / output/hk_momentum_{day}.csv
    """
    # 若指定了具体 day_key 且文件已存在则直接读回
    if day_key != "auto":
        hits_path0 = os.path.join(OUT, f"hk_uptrend_{day_key}.csv")
        if not force and os.path.exists(hits_path0):
            try:
                return pd.read_csv(hits_path0, encoding="utf-8-sig"), day_key
            except Exception:  # noqa: BLE001
                pass
    recs = ensure_universe()
    if limit:
        recs = recs[:limit]
    print(f"[scan] 扫描港股 {len(recs)} 只 (bars={BARS}, no_cache={no_cache})", file=sys.stderr)
    hits: list[dict] = []
    moms: list[dict] = []
    # 指定了具体日期(YYYYMMDD)时按该交易日切片回补历史; auto=最新"已收盘确认"交易日
    asof = pd.Timestamp(day_key).date() if day_key != "auto" else None
    live = day_key == "auto"
    if live and not no_cache:
        # 增量基准: 先探最新“已收盘确认”的港股交易日, 让过期缓存重拉(不靠 TTL)
        ref = _latest_final_session()
        if ref is not None:
            fos.set_kline_asof_ref(ref)
            asof = ref      # 关键: 序列截到已收盘日, 避免盘中/未开盘的半根K线混入
            print(f"[scan] K线增量基准(最新港股交易日) = {ref}", file=sys.stderr)
        else:
            print("[警告] 无法探测最新港股交易日, 退回缓存 TTL 判断", file=sys.stderr)
    done = 0
    t0 = time.time()
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futs = {pool.submit(scan_one, r, no_cache, asof, live): r for r in recs}
        for fut in as_completed(futs):
            try:
                h, mo = fut.result()
            except Exception:  # noqa: BLE001
                h = mo = None
            if h:
                hits.append(h)
            if mo:
                moms.append(mo)
            done += 1
            if done % 500 == 0:
                print(f"[scan] {done}/{len(recs)} 命中{len(hits)} "
                      f"{time.time() - t0:.0f}s", file=sys.stderr)
    hits_df = pd.DataFrame(hits)
    mom_df = pd.DataFrame(moms)
    if not mom_df.empty:
        mom_df = (mom_df.dropna(subset=["m"])
                       .sort_values(["industry", "m"], ascending=[True, False])
                       .reset_index(drop=True))
        real_day = pd.to_datetime(mom_df["date"]).max().strftime("%Y%m%d")
    else:
        real_day = day_key if day_key != "auto" else pd.Timestamp.today().strftime("%Y%m%d")
    if day_key != "auto":              # 历史回补: 文件名固定用所给日期
        real_day = day_key
    mom_path = os.path.join(OUT, f"hk_momentum_{real_day}.csv")
    if not mom_df.empty:
        mom_df.to_csv(mom_path, index=False, encoding="utf-8-sig")
    hits_path = os.path.join(OUT, f"hk_uptrend_{real_day}.csv")
    if not hits_df.empty:
        hits_df.to_csv(hits_path, index=False, encoding="utf-8-sig")
    print(f"[scan] 命中 {len(hits_df)} 只 / 动量成员 {len(mom_df)} 只 -> 日期 {real_day}", file=sys.stderr)
    return hits_df, real_day


# ---------------- 板块入池排名(照搬A股 combined_rank) ----------------

_uni_names: dict[str, str] | None = None


def _name(prefixed: str) -> str:
    """中文名: 取东财港股简称(离线, 不联网); 失败返回 prefixed。"""
    global _uni_names
    if _uni_names is None:
        _uni_names = {r["prefixed"]: str(r.get("name") or r["prefixed"])
                      for r in ensure_universe()}
    return _uni_names.get(prefixed) or prefixed


def size_factor(n: int) -> float:
    """板块数量因子(沿用A股口径): 从 0 只起按 0.003/只(=0.06/20)连续线性递减;
    n>=120 封底 = 0.64, 之后不再减。乘到 趋势分/动量分/动量入池分/入池资格。

    计数口径 N = 该行业**参与门槛(近5日均成交额>4000万)通过**的股票数。
    """
    n = int(n or 0)
    return round(max(0.64, 1.0 - 0.003 * n), 4)


def aggregate_trend(hits: pd.DataFrame) -> pd.DataFrame:
    """命中 -> 每行业 得分/股票数/平均分(趋势分)。6分以下不计。"""
    d = hits.copy()
    d["加权分"] = d["uptrend"].map(
        lambda s: SCORE_MAP.get(int(str(s).split("/")[0]), 0))
    d = d[d["加权分"] > 0]
    if d.empty:
        return pd.DataFrame(columns=["industry", "得分", "股票数", "平均分"])
    agg = (d.groupby("industry")
             .agg(得分=("加权分", "sum"), 股票数=("加权分", "count"))
             .reset_index())
    agg["平均分"] = (agg["得分"] / agg["股票数"]).round(2)
    return agg


def momentum_entry(mom: pd.DataFrame) -> pd.DataFrame:
    """动量成员 -> 每板块 动量入池分(top10 m 和) 与 动量股数。"""
    if mom.empty or "m" not in mom:
        return pd.DataFrame(columns=["industry", "动量入池分", "动量股数"])
    top = mom.sort_values("m", ascending=False).groupby("industry", sort=False).head(10)
    out = (top.groupby("industry", sort=False)
              .agg(动量入池分=("m", "sum"), 动量股数=("m", "count"))
              .reset_index())
    out["动量入池分"] = out["动量入池分"].round(2)
    return out


def _short(name: str, prefixed: str) -> str:
    """港股用中文简称(不加代码后缀, 与A股日报一致); 名缺失时退回代码。"""
    n = (name or "").strip()
    return n or prefixed


def _rep_trend(name: str, prefixed: str, uptrend: str, chg20) -> str:
    chg = f"{float(chg20):+.0f}%" if chg20 == chg20 and chg20 is not None else ""
    return f"{_short(name, prefixed)}({uptrend},{chg})"


def _rep_mom(name: str, prefixed: str, chg1, new: bool = False) -> str:
    chg = f"{float(chg1):+.0f}%" if chg1 == chg1 and chg1 is not None else ""
    mark = NEW_MOM_MARK if new else OLD_MOM_MARK
    return f"{mark}{_short(name, prefixed)}({chg})"


# 入池下限(用户口径, 2026-09-11): 展示动量分 < 2 或 总分 < 5 的板块按“出池”对待。
# 注意权重与总分互相依赖(权重按池内量级配平), 故“配权→算总分→筛掉不达标”迭代到稳定。
POOL_MIN_MOM = 2.0
POOL_MIN_TOTAL = 5.0


def apply_pool_floor(df: pd.DataFrame, min_mom: float = POOL_MIN_MOM,
                     min_total: float = POOL_MIN_TOTAL) -> pd.DataFrame:
    """算 趋势贡献/动量贡献/总分, 并把 动量分<min_mom 或 总分<min_total 的板块剔除。

    返回筛后的 df(含 趋势贡献/动量贡献/总分, 未排序), 同时更新 LAST_WEIGHTS。
    """
    global LAST_WEIGHTS
    cur = df
    for _ in range(5):                      # 权重<->总分 互相依赖, 迭代到稳定
        if cur.empty:
            LAST_WEIGHTS = (0.5, 0.5)
            return cur
        wt, wm = dynamic_weights(cur["趋势分"], cur["动量分"])
        LAST_WEIGHTS = (wt, wm)
        cur = cur.copy()
        cur["趋势贡献"] = (cur["趋势分"] * wt * DISPLAY_SCALE).round(2)
        cur["动量贡献"] = (cur["动量分"] * wm * DISPLAY_SCALE).round(2)
        cur["总分"] = (cur["趋势贡献"] + cur["动量贡献"]).round(2)
        keep = (cur["动量贡献"] >= min_mom) & (cur["总分"] >= min_total)
        if bool(keep.all()):
            return cur
        cur = cur[keep]
    return cur


def build_rank(day_key: str, top: int = 7) -> pd.DataFrame:
    """由当日 hits+动量表 -> 双口径入池排名表 hk_rank_{day_key}.csv。"""
    hits = pd.read_csv(os.path.join(OUT, f"hk_uptrend_{day_key}.csv"), encoding="utf-8-sig")
    mom = pd.read_csv(os.path.join(OUT, f"hk_momentum_{day_key}.csv"), encoding="utf-8-sig")
    agg = aggregate_trend(hits)
    entry = momentum_entry(mom)
    if agg.empty:
        return pd.DataFrame()
    raw_map = dict(zip(entry["industry"], entry["动量入池分"])) if not entry.empty else {}
    cnt_map = dict(zip(entry["industry"], entry["动量股数"])) if not entry.empty else {}

    def _pts(ind) -> float:
        r, c = raw_map.get(ind), cnt_map.get(ind)
        if r is None or c is None or int(c) <= 0:
            return 0.0
        # 除 1.4 (而非原 5): 权重和由3变1, m 整体变小, 用标定值 1.4 使动量分量级与旧口径一致
        pts = float(r) / (MOM_NORM * int(c))
        return round(max(-10.0, min(10.0, pts)), 2)

    # 趋势代表股: 命中加权分前5(同分按 chg20) —— 名字用东财港股简称(离线, 不联网)
    _uni = ensure_universe()
    uni_names = {r["prefixed"]: (r.get("name") or r["prefixed"]) for r in _uni}
    # 数量因子 N = 该行业**通过参与门槛(近5日均成交额>4000万)**的股票数
    # (动量成员表里就是全部参与股); 完全缺失时退回今日命中数
    _sz: dict[str, int] = {}
    if not mom.empty and "industry" in mom.columns:
        for ind, g in mom.groupby("industry", sort=False):
            _sz[str(ind)] = int(g["prefixed"].nunique())

    def _fsz(ind: str, fallback: int) -> float:
        return size_factor(_sz.get(str(ind)) or int(fallback))
    h = hits.copy()
    h["加权分"] = h["uptrend"].map(lambda s: SCORE_MAP.get(int(str(s).split("/")[0]), 0))
    h = h[h["加权分"] > 0]
    trep = (h.sort_values(["加权分", "chg20"], ascending=[False, False])
              .groupby("industry", sort=False).head(5))
    trep_by: dict[str, list] = {}
    if not trep.empty:
        for ind, g in trep.groupby("industry", sort=False):
            trep_by[ind] = [_rep_trend(str(r.get("name") or uni_names.get(str(r["prefixed"]), str(r["prefixed"]))),
                                       str(r["prefixed"]), str(r["uptrend"]),
                                       r.get("chg20")) for _, r in g.iterrows()]
    # 动量代表股: 全部成分股按 m 前 MOM_REPS(◆=相比前日新进入)
    mrep_by: dict[str, list] = {}
    if not mom.empty:
        prev_reps = _prev_mrep_index(day_key)
        m_top = mom.sort_values("m", ascending=False).groupby("industry", sort=False).head(MOM_REPS)
        for ind, g in m_top.groupby("industry", sort=False):
            reps = []
            for _, r in g.iterrows():
                pref = str(r["prefixed"])
                is_new = bool(prev_reps) and pref not in prev_reps.get(str(ind), set())
                reps.append(_rep_mom(uni_names.get(pref, pref), pref, r.get("chg1"), new=is_new))
            mrep_by[ind] = reps

    # 数量因子(沿用A股口径): N=板块参与只数(_sz, 近5日均成交额>4000万), f=max(0.64, 1-0.003*N);
    # 趋势腿(得分×f)与动量腿(动量入池分×f)、以及展示的趋势分/动量分 均乘同一 f
    fmap = {str(a["industry"]): _fsz(str(a["industry"]), int(a["股票数"])) for _, a in agg.iterrows()}
    _agg = agg.copy()
    _agg["_f"] = _agg.apply(lambda r: _fsz(str(r["industry"]), int(r["股票数"])), axis=1)
    _agg["_s"] = _agg["得分"] * _agg["_f"]
    score_top = set(_agg.sort_values("_s", ascending=False).head(top)["industry"])
    mom_sorted = sorted(raw_map.items(),
                        key=lambda kv: (kv[1] * fmap.get(kv[0], 1.0), kv[0]),
                        reverse=True)
    mom_top = {ind for ind, _ in mom_sorted[:top]}

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
        f = _fsz(str(ind), int(a["股票数"]))
        rawv = raw_map.get(ind)
        trend = round(float(a["平均分"]) * f, 2)      # 趋势分 ×数量因子
        pts = round(_pts(ind) * f, 2)                   # 动量分 ×数量因子
        rows.append({
            "industry": ind,
            "得分": int(a["得分"]),
            "股票数": int(a["股票数"]),
            "趋势分": trend,
            "动量分": pts,
            "动量入池分": round(rawv * f, 2) if rawv is not None else None,
            "代表股": "、".join(trep_by.get(ind, [])[:5] + mrep_by.get(ind, [])[:MOM_REPS]) or "-",
            "入池": "+".join(src),
        })
    rk = pd.DataFrame(rows)
    # 池内动态配权 + 入池下限(动量分<2 或 总分<5 -> 按出池剔除; 与A股同一套)
    rk = apply_pool_floor(rk)
    rk = rk.sort_values("总分", ascending=False).reset_index(drop=True)
    rk.insert(0, "排名", range(1, len(rk) + 1))
    out = os.path.join(OUT, f"hk_rank_{day_key}.csv")
    rk.to_csv(out, index=False, encoding="utf-8-sig")
    print(f"[rank] {day_key} 入池 {len(rk)} 板块 -> {out}", file=sys.stderr)
    return rk


def _prev_mrep_index(day_key: str) -> dict[str, set[str]]:
    """前一交易日各板块动量代表股(prefixed 集合, m 前 MOM_REPS), 供标记今日新进。

    直接读已缓存的 hk_momentum_<前一交易日>.csv; 无则返回 {}。
    """
    import glob as _glob
    keys = [os.path.basename(p)[len("hk_momentum_"):-4]
            for p in sorted(_glob.glob(os.path.join(OUT, "hk_momentum_*.csv")))]
    prev = [k for k in keys if k < day_key]
    if not prev:
        return {}
    try:
        df = pd.read_csv(os.path.join(OUT, f"hk_momentum_{prev[-1]}.csv"), encoding="utf-8-sig")
    except Exception:  # noqa: BLE001
        return {}
    if df.empty or "m" not in df.columns:
        return {}
    top = df.sort_values("m", ascending=False).groupby("industry", sort=False).head(MOM_REPS)
    return {str(i): set(g["prefixed"].astype(str))
            for i, g in top.groupby("industry", sort=False)}


def _rank_index(day_key: str) -> dict[str, int]:
    """某日板块 -> 排名(int, 只含入池)。"""
    p = os.path.join(OUT, f"hk_rank_{day_key}.csv")
    if not os.path.exists(p):
        return {}
    df = pd.read_csv(p, encoding="utf-8-sig")
    return {str(i).strip(): int(r) for i, r in zip(df["industry"], df["排名"])}


def _scores_index(day_key: str, pool: set[str]) -> dict[str, dict]:
    """某日入池板块分数(趋势/动量/总分), 供 delta。"""
    p = os.path.join(OUT, f"hk_rank_{day_key}.csv")
    out: dict[str, dict] = {}
    if not os.path.exists(p):
        return out
    df = pd.read_csv(p, encoding="utf-8-sig")
    for _, r in df.iterrows():
        ind = str(r["industry"]).strip()
        if ind in pool or ind in {str(x).strip() for x in df["industry"]}:
            # 展示口径取“加权后贡献”列(老数据无此列时退回原始列)
            tk = r["趋势贡献"] if "趋势贡献" in df.columns else r["趋势分"]
            mk = r["动量贡献"] if "动量贡献" in df.columns else r["动量分"]
            out[ind] = {"趋势分": float(tk), "动量分": float(mk),
                        "总分": float(r["总分"])}
    return out


def build_delta(day_key: str) -> pd.DataFrame:
    """对比**最近一个更早交易日**的 hk_rank_*.csv -> hk_delta_{day_key}.csv。"""
    rks = sorted(f for f in os.listdir(OUT)
                 if f.startswith("hk_rank_") and f.endswith(".csv")
                 and f[len("hk_rank_"):-4] < day_key)     # 必须更早, 不能取最新那份
    if not rks:
        print("[delta] 无更早日排名, 跳过", file=sys.stderr)
        return pd.DataFrame()
    prev = sorted(rks)[-1].replace("hk_rank_", "").replace(".csv", "")
    old_rank = _rank_index(prev)
    new_rank = _rank_index(day_key)
    old_scores = {i: {"趋势分": s.get("趋势分"), "动量分": s.get("动量分"),
                      "总分": s.get("总分")} for i, s in _scores_index(prev, set()).items()}
    new_scores = {i: {"趋势分": s.get("趋势分"), "动量分": s.get("动量分"),
                      "总分": s.get("总分")} for i, s in _scores_index(day_key, set()).items()}
    inds = sorted(set(old_rank) | set(new_rank),
                  key=lambda x: (new_rank.get(x, 10 ** 6)))
    rows = []
    for ind in inds:
        r_old, r_new = old_rank.get(ind), new_rank.get(ind)
        s_old, s_new = old_scores.get(ind, {}), new_scores.get(ind, {})
        rows.append({
            "行业": ind,
            "状态": ("池内" if (r_old is not None and r_new is not None)
                     else ("新进池" if r_old is None and r_new is not None else "退出池")),
            "前日排名": r_old, "今日排名": r_new,
            "前日趋势分": s_old.get("趋势分"), "今日趋势分": s_new.get("趋势分"),
            "前日动量分": s_old.get("动量分"), "今日动量分": s_new.get("动量分"),
            "前日总分": s_old.get("总分"), "今日总分": s_new.get("总分"),
        })
    m = pd.DataFrame(rows)
    if m.empty:
        return m
    m["排名变化"] = m["前日排名"] - m["今日排名"]            # 正=上升
    m["趋势分变化"] = m["今日趋势分"].fillna(0) - m["前日趋势分"].fillna(0)
    m["动量分变化"] = m["今日动量分"].fillna(0) - m["前日动量分"].fillna(0)
    m["总分变化"] = m["今日总分"].fillna(0) - m["前日总分"].fillna(0)
    m = m.sort_values(["今日排名", "前日排名"], na_position="last").reset_index(drop=True)
    out = os.path.join(OUT, f"hk_delta_{day_key}.csv")
    m.to_csv(out, index=False, encoding="utf-8-sig")
    print(f"[delta] {prev} -> {day_key}: {len(m)} 板块 -> {out}", file=sys.stderr)
    return m


def main() -> None:
    p = argparse.ArgumentParser(description="港股板块 A_rank")
    p.add_argument("--scan", action="store_true")
    p.add_argument("--rank", action="store_true")
    p.add_argument("--delta", action="store_true")
    p.add_argument("--day", default="auto", help="YYYYMMDD 日期键(默认取K线最新交易日)")
    p.add_argument("--workers", type=int, default=30)
    p.add_argument("--no-cache", action="store_true")
    p.add_argument("--limit", type=int, default=0, help="只扫前N只(联调用)")
    p.add_argument("--top", type=int, default=7)
    p.add_argument("--force", action="store_true")
    a = p.parse_args()

    day_key = a.day
    if a.scan:
        _, real = scan_day(day_key, a.workers, a.no_cache, a.limit, force=a.force)
        day_key = real
    if a.rank:
        if day_key == "auto":
            import glob
            files = sorted(glob.glob(os.path.join(OUT, "hk_uptrend_*.csv")))
            day_key = os.path.basename(files[-1]).replace("hk_uptrend_", "").replace(".csv", "") \
                if files else day_key
        rk = build_rank(day_key, a.top)
        print(rk[["排名", "industry", "趋势分", "动量分", "总分", "入池"]].to_string(index=False)
              if not rk.empty else "rank空")
    if a.delta:
        if day_key == "auto":
            import glob
            files = sorted(glob.glob(os.path.join(OUT, "hk_rank_*.csv")))
            day_key = os.path.basename(files[-1]).replace("hk_rank_", "").replace(".csv", "") \
                if files else day_key
        build_delta(day_key)


if __name__ == "__main__":
    main()
