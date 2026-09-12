# US_selected_report — 美股自选 RSI 监控

监控 `data/selected/us.json` 里的美股自选股（211 只，格式 `{"code":"UAAUS","name":"..."}`）：

- **RSI(6) > 83 → 超买**（短线过热，注意回调风险）
- **RSI(6) < 26 → 超卖**（短线超跌，可能反弹）

## 用法

```bash
# 环境: conda py12
cd US_selected_report

# 1) 先缓存美股日K(腾讯 usfqkline, 落到共享 data/kline_cache, 避免每次联网/限流)
python code/monitor_us_selected.py --cache --workers 8

# 2) 正式监控(读缓存算 RSI6, 打印超买/超卖 + 存 CSV)
python code/monitor_us_selected.py --period 6 --over 83 --under 26 --workers 8

# 3) (可选) 生成 HTML 报告, 与 A 股日报风格一致
python code/make_us_monitor_report.py [YYYYMMDD]   # 默认取最新 CSV; 加 --open 自动打开
```

参数：`--bars`(默认80) `--period`(6) `--over`(83) `--under`(26) `--workers`(8)。
产物：`output/us_selected_rsi_YYYYMMDD.csv`。

## 说明

- 自选代码 `XXXUS` → 腾讯代码 `usXXX.OQ/.N/.A`（按本地 nasdaq/nyse/amex 名单映射交易所后缀，未知的自动逐个探测）。
- 复用 `A_rank_report/code/find_overbought_stocks`（腾讯行情/K线缓存/代理池/RSI 算法），K线缓存与美股扫描共享。
- 当日美股以最近一个完整交易日收盘为准（如北京时间傍晚运行时为美股上一交易日收盘）。
- 无数据的少数自选（如已退市/更名/腾讯无K线）会跳过并在“有数据 N/211”中体现。

---

## MACD 金叉/死叉监控（`code/monitor_us_macd.py`）

报**近 N 个交易日（默认 3）内刚发生**的 DIF/DEA 交叉，按 0 轴上下分 4 类。

```bash
python code/monitor_us_macd.py --window 3 --bars 80 --workers 8
```
产物：`output/us_selected_macd_YYYYMMDD.csv` + `us_macd_report_YYYYMMDD.html`。

## MACD 上涨衰减（顶背离）监控（`code/monitor_us_macd_decay.py`）

**股价创近期新高，但 MACD 快慢线反而低于前一个高点的快慢线** → 上涨动能衰减（减仓/止盈提示）。

### 时间窗口（默认三个一起跑，可 `--windows` 选）

| 代号 | 交易日 | 含义 |
|---|---|---|
| `1m` | 21 | 1 个月 —— 短线新高 |
| `3m` | 63 | 3 个月 —— 中期新高 |
| `6m` | 126 | 6 个月 —— 长期新高 |

三窗口**嵌套**：`6m新高 ⊂ 3m新高 ⊂ 1m新高`。

### 判定（对每个窗口各自算一次，用户口径 2026-09-12）

1. 新高：今日**收盘价** > 该窗口内之前所有交易日的最高收盘；只报**今天正在创新高**的股票（信号"当下发生"，不是历史遗留）。
2. 前高：该窗口内收盘最高的那根 K 线。
3. 比较今日与前高当日的 `DIF=EMA12-EMA26`（快线）、`DEA=EMA9(DIF)`（慢线）：
   - `--mode both`（默认）：两条**都**低于前高 → **双线衰减**
   - `--mode any`：任一低于 → 单线衰减（标注「仅快线衰减 / 仅慢线衰减」）
4. 前高若落在 MACD 预热区（前 40 根）内，该窗口跳过，避免未稳定的 DIF/DEA 误判。

### 报告三列（一只票一行）

| 列 | 含义 |
|---|---|
| **新高级别** | 它创新高的**最长**窗口（6m > 3m > 1m）—— 级别越大，"新高"越关键 |
| **信号窗口** | 出现衰减的**最长**窗口（表中 DIF/DEA 取该窗口的对比） |
| **衰减确认** | `k/n`：它在创新高的 n 个窗口里有 k 个出现衰减 → 多周期共振程度 |

```bash
# 信号需要长历史(6m窗口+预热), 建议 bars=320(可直接复用美股扫描的 K 线缓存)
python code/monitor_us_macd_decay.py --cache --bars 320 --workers 8
python code/monitor_us_macd_decay.py                       # 默认 1m/3m/6m 全跑
python code/monitor_us_macd_decay.py --windows 6m          # 只看 6 个月新高
python code/monitor_us_macd_decay.py --windows 1m,3m       # 或 --windows 21,63,126
python code/monitor_us_macd_decay.py --mode any            # 任一条线低即报
```

参数：`--bars`(320) `--windows`(1m,3m,6m) `--mode`(both|any) `--workers`(16) `--cache`。
产物：`output/us_selected_macd_decay_YYYYMMDD.csv` + `us_macd_decay_report_YYYYMMDD.html`。

基准率（自选 202 只有数据、`--windows 6m` 单窗口、近 320 根 K 线）：**创新高事件 4653 次，
其中双线衰减 535 次 ≈ 11.5%** —— 大约每 9 次创新高出现 1 次衰减。弱市里"创新高"的标的本身很少
（如 2026-09-11 三个窗口合计仅 5 只），所以当天没有信号属正常，不是脚本失效。
