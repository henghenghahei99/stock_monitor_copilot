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

## 分时 MACD 短线上涨衰减（`code/monitor_us_intraday_decay.py`）

把上面「上涨衰减」的思路搬到**分时 K 线**上：分时尺度上最近新高，MACD 快慢线却低于上一个新高。

### 口径（用户定义 2026-09-12）

- **分时 K 线（4 种）**：30 分钟、60 分钟、120 分钟、日线
- **窗口（6 种）**：1 / 2 / 3 / 4 / 5 / 6 个交易日 → 每只票 `4 × 6 = 24` 个组合
- **最近新高**：最近 W 个交易日内，该分时 K 线**最高价**那根
- **上个新高**：跳过最近窗口、再往前 W 个交易日内的最高价那根
- **短线上涨衰减**：最近新高价 **>** 上个新高价（必须真创新高），且最近那根的
  `DIF=EMA12-EMA26` 与 `DEA=EMA9(DIF)` **都低于**前高那根 → 成立（`--mode any` 则任一低即报）
- **金叉/死叉条件（默认开启，`--no-cross` 关）**：两个背离点之间必须**先出现死叉**
  (DIF 下穿 DEA)、**后出现金叉** (DIF 上穿 DEA)，且**最近新高必须位于金叉之后**
  （即金叉到 ▲ 之间不能再死叉，▲那根仍处金叉后的多头区间）。完整含义：
  **高点 → 死叉回调 → 金叉重新走强 → 创更高的新高（但 MACD 反而更低）**。
  区间内可能有多组交叉，取 **▲之前最后一次金叉** 及其之前的最后一次死叉作为「对应」的那一对。
  这一步过滤很强（实测自选缓存：236 条 → 19 条）
- 前高落在 MACD 预热区（前 40 根）内则跳过；同一只票可命中多个「分时K × 窗口」组合（多尺度共振）

### 数据源

| 源 | 分时 | 说明 |
|---|---|---|
| **Twelve Data**（默认，推荐） | 30min / 1h / **2h 原生** | `api.twelvedata.com`；免费档 **800 次/日、8 次/分**，210 只 × 3 周期 = 630 次 ≈ 80 分钟 |
| 东财 `push2his` | klt=30/60 | 120m 由 60m 两两合成；**本机 IP 已被东财封禁**（2026-09-12，0.1s TCP 断开），需换出口 IP |

Twelve Data 的 key：注册后放 `~/.twelvedata.json` → `{"apikey": "你的key"}`（或环境变量 `TWELVEDATA_API_KEY`）。
日线仍走腾讯日 K 缓存（不消耗 Twelve Data 配额）。

已逐一验证**不可用**的其它美股分时源（2026-09-12）：
腾讯 `usfqkline` 只支持 `day`（m60/m5/控制器爆破全失败）· 新浪 `US_MinKService`（已下线）·
雪球（需 `xq_a_token`）· 同花顺 `d.10jqka` 美股 404 · 富途 `quote-api` 404 · Nasdaq api 分时只有当日 1 分钟 ·
Yahoo/WSJ/Google/CNBC/investing/marketwatch/stooq 分时一律 403/不可达（环境层屏蔽）·
公共 CORS 转代理（allorigins/codetabs/jina/corsproxy）同样被环境层屏蔽。

### 用法

```bash
cd US_selected_report
python code/monitor_us_intraday_decay.py --limit 10        # 先小样本试跑
python code/monitor_us_intraday_decay.py                   # 全量扫自选(约 80 分钟)
python code/monitor_us_intraday_decay.py --periods 60分钟,120分钟
python code/monitor_us_intraday_decay.py --cache           # 只拉取/缓存分时K线
python code/monitor_us_intraday_decay.py --mode any        # 任一条线低即报
python code/monitor_us_intraday_decay.py --source em       # 强制走东财(需 IP 未被封)
python code/monitor_us_intraday_decay.py --no-cross       # 关闭「先死叉后金叉」过滤
```

### 命中图核对（`code/plot_intraday_decay.py`）

只用本地缓存离线重算并画图（不发网络请求、不耗 Twelve Data 额度），每条命中一张：
上方价格（最高价/收盘价）+ 红色▲最近新高 / 蓝色▼上个新高；下方 MACD DIF/DEA +
标注区间内的金叉(绿▲)/死叉(灰▼)，**口径用到的那一对用粗边高亮**。

```bash
python code/plot_intraday_decay.py                     # 画缓存里全部命中
python code/plot_intraday_decay.py --stocks KEYS,SNAP   # 只看某几只
python code/plot_intraday_decay.py --periods 60分钟,120分钟 --windows 1,2,3
```

产物：`output/charts_intraday/*.png` + `output/us_intraday_decay_charts_YYYYMMDD.html`（图表索引）。

参数：`--source`(auto|td|em) `--periods` `--windows`(1,2,3,4,5,6) `--mode`(both|any)
`--workers`(4) `--bars`(320, 日线根数) `--td-interval-sec`(7.6) `--limit` `--cache`。
产物：`output/us_selected_intraday_decay_YYYYMMDD.csv` + `us_intraday_decay_report_YYYYMMDD.html`
（表列：分时K / 窗口(日) / 最近新高时间 / 新高价 / 上个新高时间 / 前高价 / 新高幅度 /
DIF / 前高DIF / DIF差 / DEA / 前高DEA / DEA差 / 衰减类型）。

本地 HTML 预览：`python -m http.server 8738 --bind 127.0.0.1`（cwd = `US_selected_report/output`）。
