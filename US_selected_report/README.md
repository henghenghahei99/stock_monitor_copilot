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
