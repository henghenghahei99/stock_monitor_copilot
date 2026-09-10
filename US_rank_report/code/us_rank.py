#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
US_rank: 美股板块 A_rank（照搬 A股日报口径，按细分 industry 分组）

口径与 A股 A_rank_report_v1 对齐:
  - 个股上涨趋势 8 条件打分(>=5 命中, 历史不足按比例折算 /8)
  - 行业加权 8/8→8分、7/8→6分、6/8→4分(6分以下命中但计0)
  - 趋势分 = 行业得分/计入股票数(平均分)
  - 个股动量 m = 当日%×0.50 + 近2日%×0.30 + 近3日%×0.20 (加强当日权重)
  - 动量入池分(每板块) = 全部成分股按 m 降序前10 只 m 之和(不足按实际只数)
  - 展示动量分(±10) = S/(1.4×动量股数) (允许为负, ±10封顶; 1.4 为标定回旧口径量级的除数)
  - 入池 = 原行业得分前 top ∪ 动量入池分前 top(并集, 最多 2*top)
  - 池内按 总分 = 趋势分/动量分 动态配平(按当日池内量级) 降序
  - 代表股 = 趋势代表股(加权分前5, name(8/8,+20日%)) + ◎动量代表股(全部成分股按m前5, ◎name(+当日%))

数据源: 本地 nasdaq/nyse/amex full 名单(symbol→行业/市值/代码后缀) + 腾讯美股日K(320根, qfq)。
目录: US_rank_report/code → 输出 ../output/us_*_YYYYMMDD.csv ; 行业/市值 缓存写共享 data/。

用法:
  python us_rank.py --scan --workers 8                 # 全市场扫描(当日K线缓存), 产出 hits+动量表
  python us_rank.py --rank --top 10                    # 由当日 hits+动量表 算板块入池排名
  python us_rank.py --delta                            # 与前一日排名升降
  python us_rank.py --limit 300 --scan --rank --delta  # 小批量联调(先跑通再全量)
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date

import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))           # .../US_rank_report/code
ROOT = os.path.dirname(HERE)                                 # .../US_rank_report
REPO = os.path.dirname(ROOT)                                 # .../stock_monitor_copilot
DATA = os.path.join(REPO, "data")
OUT = os.path.join(ROOT, "output")
A_CODE = os.path.join(REPO, "A_rank_report", "code")
for p in (HERE, A_CODE):
    if p not in sys.path:
        sys.path.insert(0, p)

import find_overbought_stocks as fos  # noqa: E402
from translations import translate  # noqa: E402
from run_strategy import eval_uptrend_7d  # noqa: E402

BARS = 320            # 需覆盖 MA200/近60日
SCORE_MAP = {8: 8, 7: 6, 6: 4}
MIN_SCORE = 6
# 与 A股默认过滤一致(价格/成交额/成交量/停牌天数); 股价门槛已放宽到 $1
MIN_PRICE = 1.0
MIN_VOLUME = 10_000_000.0        # 日均成交额(USD) > 1000 万美元 才参与
MIN_SHARES = 100_000.0
MAX_STALE_DAYS = 7
# 市值过滤: 市值 > 5 亿美元才参与(不满足的连“成分股/动量成员”都不计入)
MIN_MARKET_CAP = 500_000_000.0      # 5 亿美金
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
EXCLUDE_INDUSTRY = {"空白支票公司(壳)"}
UNIVERSE_FILE = os.path.join(DATA, "us_universe.json")
EM_INDUSTRY_FILE = os.path.join(DATA, "us_em_industry.json")   # 东财美股行业(中文)
IND_MAP_FILE = os.path.join(DATA, "us_industry_map.json")      # 东财美股行业 -> A股东财二级(中文)

_em_map: dict[str, str] | None = None
_ind_map: dict[str, str] | None = None


def _load_ind_map() -> dict[str, str]:
    """东财美股行业名 -> A股东财二级行业名(与A股同口径, 便于跳市场比较)。"""
    global _ind_map
    if _ind_map is None:
        try:
            with open(IND_MAP_FILE, encoding="utf-8") as f:
                _ind_map = json.load(f)
        except Exception:  # noqa: BLE001
            _ind_map = {}
    return _ind_map


def _load_em_industry() -> dict[str, str]:
    """东财美股 symbol -> 行业(中文); 不存在返回空。"""
    global _em_map
    if _em_map is None:
        try:
            with open(EM_INDUSTRY_FILE, encoding="utf-8") as f:
                _em_map = json.load(f)
        except Exception:  # noqa: BLE001
            _em_map = {}
    return _em_map

os.makedirs(OUT, exist_ok=True)


# ---------------- 全市场股票池(含行业归属与腾讯代码后缀) ----------------

def ensure_universe(force: bool = False) -> list[dict]:
    """构建/读取美股 universe(有行业归属、非壳、有一定市值)。

    来源 nasdaq/nyse/amex_full.json, 腾讯代码后缀按交易所: 纳=.OQ 纽=.N 美交所=.AM。
    写缓存 data/us_universe.json。
    """
    if not force and os.path.exists(UNIVERSE_FILE):
        try:
            with open(UNIVERSE_FILE, encoding="utf-8") as f:
                return json.load(f)
        except Exception:  # noqa: BLE001
            pass
    files = [("nasdaq_full.json", ".OQ"), ("nyse_full.json", ".N"), ("amex_full.json", ".AM")]
    recs: list[dict] = []
    seen: set[str] = set()
    em = _load_em_industry()
    imap = _load_ind_map()
    unmapped: set[str] = set()
    n_em = 0
    for fname, suf in files:
        path = os.path.join(DATA, fname)
        if not os.path.exists(path):
            continue
        with open(path, encoding="utf-8") as f:
            for r in json.load(f):
                sym = str(r.get("symbol", "")).strip().upper()
                ind_en = str(r.get("industry") or "").strip()
                if not sym or not ind_en or sym in seen:
                    continue
                full_name = str(r.get("name") or "").strip()
                if fos.is_shell_name(full_name):
                    continue
                ind_zh = translate(ind_en)
                if ind_zh in EXCLUDE_INDUSTRY:
                    continue
                try:
                    mc = float(r.get("marketCap") or 0)
                except Exception:  # noqa: BLE001
                    mc = 0.0
                if mc < MIN_MARKET_CAP:
                    continue
                # 分组行业优先用东财美股行业(与A股同源中文口径); 缺失回退纳斯达克翻译
                em_ind = (em.get(sym) or "").strip()
                if em_ind:
                    ind_zh = em_ind
                    n_em += 1
                # 统一到 A股 东财二级口径
                mapped = imap.get(ind_zh)
                if mapped:
                    ind_zh = mapped
                else:
                    unmapped.add(ind_zh)
                seen.add(sym)
                recs.append({
                    "symbol": sym,
                    "prefixed": "us" + sym + suf,
                    "industry": ind_zh,
                    "sector": translate(str(r.get("sector") or "").strip()),
                    "name": full_name,
                    "market_cap": mc,
                })
    with open(UNIVERSE_FILE, "w", encoding="utf-8") as f:
        json.dump(recs, f, ensure_ascii=False)
    print(f"[universe] 美股池 {len(recs)} 只(东财行业覆盖 {n_em}) -> {UNIVERSE_FILE}", file=sys.stderr)
    if unmapped:
        print(f"[universe] 未映射到A股二级的行业 {len(unmapped)} 个: "
              + "、".join(sorted(unmapped)[:10]), file=sys.stderr)
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


def scan_one(rec: dict, no_cache: bool, asof: date | None = None) -> tuple[dict | None, dict | None]:
    """处理单只: 返回 (hits行, 动量成员行)。asof 给定时只算到该交易日(历史回补)。

    参与门槛(用户口径): 市值>5亿美元 且 近20日均成交额>1000万美元 且 股价≥$1 才算参与;
    不满足的股票既不进动量成员表也不算命中(不参与板块计分)。
    """
    try:
        k = fos.tencent_kline(rec["prefixed"], BARS, use_cache=not no_cache)
        if k is None:
            return None, None
        close, volume = k
        close = close.astype(float).dropna()
        if asof is not None:
            close = close[close.index.date <= asof]
        if len(close) < 4:
            return None, None
        last_date = close.index[-1].date()
        # 停牌过滤只在“最新日”扫描时用; 历史回补不因停牌剔除
        if asof is None and (date.today() - last_date).days > MAX_STALE_DAYS:
            return None, None
        last = float(close.iloc[-1])
        # ---- 参与门槛: 市值 > 5 亿美元 ----
        if float(rec.get("market_cap") or 0) < MIN_MARKET_CAP:
            return None, None
        # ---- 参与门槛: 近20日均成交额 > 1000 万美元(美股成交额=收盘*量, 单位 USD) ----
        vol = volume.reindex(close.index).fillna(0.0)
        mult = 1.0
        nv = min(20, len(vol))
        avg_vol = float((close * vol * mult).tail(nv).mean()) if nv else 0.0
        avg_shares = float(vol.tail(nv).mean()) * mult
        if avg_vol < MIN_VOLUME:
            return None, None
        if avg_shares < MIN_SHARES:
            return None, None
        if last < MIN_PRICE:
            return None, None
        m = _momentum_close(close)
        c1 = _chg1(close)
        # 动量成员: 参与门槛已过, 只要 m 可算就计入(不受是否命中趋势影响)
        mom_row = {"industry": rec["industry"], "prefixed": rec["prefixed"],
                   "m": m, "chg1": c1, "date": str(last_date)} \
            if m is not None else None
        if len(close) < 60:
            return None, mom_row
        hit_ok, val = eval_uptrend_7d(close, vol, {"min_score": MIN_SCORE})
        if not hit_ok:
            return None, mom_row
        chg20 = round((last / float(close.iloc[-21]) - 1) * 100, 1) if len(close) > 21 else 0.0
        row = {
            "ticker": rec["symbol"], "prefixed": rec["prefixed"],
            "name": rec["name"], "sector": rec["sector"], "industry": rec["industry"],
            "date": str(last_date), "close": round(last, 2), "chg20": chg20,
            "m": m, "chg1": c1, "uptrend": val, "market_cap": rec["market_cap"],
        }
        return row, mom_row
    except Exception as exc:  # noqa: BLE001
        print(f"[警告] {rec['prefixed']} 处理失败: {exc}", file=sys.stderr)
        return None, None


def scan_day(day_key: str, workers: int, no_cache: bool, limit: int,
             force: bool = False) -> tuple[pd.DataFrame, str]:
    """全市场扫描当日: 产出 hits 与 动量成员两表(日期=最后K线交易日)。

    返回 (hits df, 真实日期键 YYYYMMDD)。写文件:
      output/us_uptrend_{day}.csv / output/us_momentum_{day}.csv
    """
    # 若指定了具体 day_key 且文件已存在则直接读回
    if day_key != "auto":
        hits_path0 = os.path.join(OUT, f"us_uptrend_{day_key}.csv")
        if not force and os.path.exists(hits_path0):
            try:
                return pd.read_csv(hits_path0, encoding="utf-8-sig"), day_key
            except Exception:  # noqa: BLE001
                pass
    recs = ensure_universe()
    if limit:
        recs = recs[:limit]
    print(f"[scan] 扫描美股 {len(recs)} 只 (bars={BARS}, no_cache={no_cache})", file=sys.stderr)
    hits: list[dict] = []
    moms: list[dict] = []
    # 指定了具体日期(YYYYMMDD)时按该交易日切片回补历史; auto=最新日(不切片)
    asof = pd.Timestamp(day_key).date() if day_key != "auto" else None
    done = 0
    t0 = time.time()
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futs = {pool.submit(scan_one, r, no_cache, asof): r for r in recs}
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
    mom_path = os.path.join(OUT, f"us_momentum_{real_day}.csv")
    if not mom_df.empty:
        mom_df.to_csv(mom_path, index=False, encoding="utf-8-sig")
    hits_path = os.path.join(OUT, f"us_uptrend_{real_day}.csv")
    if not hits_df.empty:
        hits_df.to_csv(hits_path, index=False, encoding="utf-8-sig")
    print(f"[scan] 命中 {len(hits_df)} 只 / 动量成员 {len(mom_df)} 只 -> 日期 {real_day}", file=sys.stderr)
    return hits_df, real_day


# ---------------- 板块入池排名(照搬A股 combined_rank) ----------------

def _name(prefixed: str) -> str:
    """中文名: 报价(带缓存)取; 失败返回 prefixed。"""
    try:
        q = fos.tencent_quote("us", prefixed)
        if q and q[1]:
            return str(q[1]).strip()
    except Exception:  # noqa: BLE001
        pass
    return prefixed


def size_factor(n: int) -> float:
    """板块数量因子(美股口径: 递减放慢 + 封底抬高): 从 0 只起按 0.00037/只 连续线性递减;
    n>=541 封底 = 0.80, 之后不再减。乘到 趋势分/动量分/动量入池分/入池资格。

    说明: 美股板块普遍比 A股 大(最大 409 只 vs A股 295 只), 若沿用 A股 的
    0.003/只 + 0.64 封底, 大板块会全部贴到封底而失去区分度。
    计数口径仍为"过滤后(市值>5亿美元/非壳)的板块成分股数"。
    """
    n = int(n or 0)
    return round(max(0.80, 1.0 - 0.00037 * n), 4)


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
    """短名称 + 代码: 去掉英文名常见的尾部(Common Stock/Ordinary Shares/Class A…/ADS 等), 附 [代码]。"""
    n = (name or "").strip()
    if not n:
        n = prefixed
    n = re.sub(r"\s+(Common Stock|Common Shares|Ordinary Shares|Ordinary share|ADS|American Depositary Shares|"
               r"Class [A-Z] Common Stock|Class [A-Z] Stock|Class [A-Z] Ordinary Shares|Registered Shares|Stock)$",
               "", n)
    sym = prefixed[2:].split(".", 1)[0] if prefixed.startswith("us") else prefixed
    return f"{n}[{sym}]"


def _rep_trend(name: str, prefixed: str, uptrend: str, chg20) -> str:
    chg = f"{float(chg20):+.0f}%" if chg20 == chg20 and chg20 is not None else ""
    return f"{_short(name, prefixed)}({uptrend},{chg})"


def _rep_mom(name: str, prefixed: str, chg1, new: bool = False) -> str:
    chg = f"{float(chg1):+.0f}%" if chg1 == chg1 and chg1 is not None else ""
    mark = NEW_MOM_MARK if new else OLD_MOM_MARK
    return f"{mark}{_short(name, prefixed)}({chg})"


def build_rank(day_key: str, top: int = 10) -> pd.DataFrame:
    """由当日 hits+动量表 -> 双口径入池排名表 us_rank_{day_key}.csv。"""
    hits = pd.read_csv(os.path.join(OUT, f"us_uptrend_{day_key}.csv"), encoding="utf-8-sig")
    mom = pd.read_csv(os.path.join(OUT, f"us_momentum_{day_key}.csv"), encoding="utf-8-sig")
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

    # 趋势代表股: 命中加权分前5(同分按 chg20) —— 名字用名单/命中英文全名(离线, 不联网)
    _uni = ensure_universe()
    uni_names = {r["prefixed"]: (r.get("name") or r["prefixed"]) for r in _uni}
    # 板块全部成分股总数(us_universe 同行业计数)=数量因子 N; 缺失退回今日命中数
    _uni_sz: dict[str, int] = {}
    for r in _uni:
        _uni_sz[str(r["industry"])] = _uni_sz.get(str(r["industry"]), 0) + 1

    def _fsz(ind: str, fallback: int) -> float:
        return size_factor(_uni_sz.get(str(ind)) or int(fallback))
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

    # 数量因子(不对称, N=板块全部成分股总数): <20轻度加成, >20每多20减0.06, >=120封底0.70(入围与排序均乘)
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
    # 池内动态配权(与A股同一套): 趋势/动量按当日池内量级取反比权重, 平均贡献相等
    global LAST_WEIGHTS
    wt, wm = dynamic_weights(rk["趋势分"], rk["动量分"])
    LAST_WEIGHTS = (wt, wm)
    # 展示口径: 趋势分/动量分原始值保留(走势图用原始量级), 另给加权后贡献两列(×2 保持原量级)
    rk["趋势贡献"] = (rk["趋势分"] * wt * DISPLAY_SCALE).round(2)
    rk["动量贡献"] = (rk["动量分"] * wm * DISPLAY_SCALE).round(2)
    rk["总分"] = (rk["趋势贡献"] + rk["动量贡献"]).round(2)
    rk = rk.sort_values("总分", ascending=False).reset_index(drop=True)
    rk.insert(0, "排名", range(1, len(rk) + 1))
    out = os.path.join(OUT, f"us_rank_{day_key}.csv")
    rk.to_csv(out, index=False, encoding="utf-8-sig")
    print(f"[rank] {day_key} 入池 {len(rk)} 板块 -> {out}", file=sys.stderr)
    return rk


def _prev_mrep_index(day_key: str) -> dict[str, set[str]]:
    """前一交易日各板块动量代表股(prefixed 集合, m 前 MOM_REPS), 供标记今日新进。

    直接读已缓存的 us_momentum_<前一交易日>.csv; 无则返回 {}。
    """
    import glob as _glob
    keys = [os.path.basename(p)[len("us_momentum_"):-4]
            for p in sorted(_glob.glob(os.path.join(OUT, "us_momentum_*.csv")))]
    prev = [k for k in keys if k < day_key]
    if not prev:
        return {}
    try:
        df = pd.read_csv(os.path.join(OUT, f"us_momentum_{prev[-1]}.csv"), encoding="utf-8-sig")
    except Exception:  # noqa: BLE001
        return {}
    if df.empty or "m" not in df.columns:
        return {}
    top = df.sort_values("m", ascending=False).groupby("industry", sort=False).head(MOM_REPS)
    return {str(i): set(g["prefixed"].astype(str))
            for i, g in top.groupby("industry", sort=False)}


def _rank_index(day_key: str) -> dict[str, int]:
    """某日板块 -> 排名(int, 只含入池)。"""
    p = os.path.join(OUT, f"us_rank_{day_key}.csv")
    if not os.path.exists(p):
        return {}
    df = pd.read_csv(p, encoding="utf-8-sig")
    return {str(i).strip(): int(r) for i, r in zip(df["industry"], df["排名"])}


def _scores_index(day_key: str, pool: set[str]) -> dict[str, dict]:
    """某日入池板块分数(趋势/动量/总分), 供 delta。"""
    p = os.path.join(OUT, f"us_rank_{day_key}.csv")
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
    """对比前一可用日期的 us_rank_*.csv -> us_delta_{day_key}.csv。"""
    rks = sorted(f for f in os.listdir(OUT)
                 if f.startswith("us_rank_") and f.endswith(".csv") and day_key not in f)
    if not rks:
        print("[delta] 无更早日排名, 跳过", file=sys.stderr)
        return pd.DataFrame()
    prev = sorted(rks)[-1].replace("us_rank_", "").replace(".csv", "")
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
    out = os.path.join(OUT, f"us_delta_{day_key}.csv")
    m.to_csv(out, index=False, encoding="utf-8-sig")
    print(f"[delta] {prev} -> {day_key}: {len(m)} 板块 -> {out}", file=sys.stderr)
    return m


def main() -> None:
    p = argparse.ArgumentParser(description="美股板块 A_rank")
    p.add_argument("--scan", action="store_true")
    p.add_argument("--rank", action="store_true")
    p.add_argument("--delta", action="store_true")
    p.add_argument("--day", default="auto", help="YYYYMMDD 日期键(默认取K线最新交易日)")
    p.add_argument("--workers", type=int, default=30)
    p.add_argument("--no-cache", action="store_true")
    p.add_argument("--limit", type=int, default=0, help="只扫前N只(联调用)")
    p.add_argument("--top", type=int, default=10)
    p.add_argument("--force", action="store_true")
    a = p.parse_args()

    day_key = a.day
    if a.scan:
        _, real = scan_day(day_key, a.workers, a.no_cache, a.limit, force=a.force)
        day_key = real
    if a.rank:
        if day_key == "auto":
            import glob
            files = sorted(glob.glob(os.path.join(OUT, "us_uptrend_*.csv")))
            day_key = os.path.basename(files[-1]).replace("us_uptrend_", "").replace(".csv", "") \
                if files else day_key
        rk = build_rank(day_key, a.top)
        print(rk[["排名", "industry", "趋势分", "动量分", "总分", "入池"]].to_string(index=False)
              if not rk.empty else "rank空")
    if a.delta:
        if day_key == "auto":
            import glob
            files = sorted(glob.glob(os.path.join(OUT, "us_rank_*.csv")))
            day_key = os.path.basename(files[-1]).replace("us_rank_", "").replace(".csv", "") \
                if files else day_key
        build_delta(day_key)


if __name__ == "__main__":
    main()
