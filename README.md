# stock_monitor_copilot

A股板块趋势扫描与日报（策略 **A_rank_report_v1**）：每天收盘后拉取行情，按行业汇总上涨趋势强度得分排名，生成带「前一交易日」对比的美化 HTML 报告并邮件发送。

## 目录

- `v1/code/` — 全部源码
  - `scan_rank.py` / `run_strategy.py` / `find_uptrend.py` — 扫描与 8 条件打分
  - `industry_score.py` — 行业加权汇总 / A_rank 两步排名
  - `a_rank_delta.py` — 前一交易日排名升降对比
  - `make_a_rank_report.py` — 生成 HTML/文本日报（表后含算法注释）
  - `send_report.py` — SMTP 发邮件（收件人见 `~/.mail_sender.json`）
  - `cn_trading_days.py` — 交易日历（上证指数日K，无外部日历依赖）
  - `a_rank_report_v1_scheduler.py` — 常驻调度器（交易日 15:35 自动跑）
  - `run_a_rank_daily.sh` / `run_a_rank_daily_with_mail.sh` — 一键 / 一键+发信
  - `strategies/` — 策略配置 JSON（`a_rank_report_v1.json` 等）

## 计分口径（A_rank_report_v1）

- 个股：上涨趋势 **8 条件**（多头排列/站上年线/MA20上行/低点抬高/高点抬高/斜率向上/近60日新高/放量），满足 1 个记 1 分，≥5 分命中；历史不足按比例折算到 `/8`
- 行业加权：8/8→8分、7/8→6分、6/8→4分；6 分以下不计入
- 行业得分=成员加权分之和；平均分=得分/股票数；总分前 15 为池，池内按平均分排序
- 代表股：每行业加权分最高前 9 只（同分按 20 日涨幅降序，不足全给）

## 策略

策略配置都在 `v1/code/strategies/*.json`，分两类：单股扫描策略（扫描个股）和板块级/复合策略（对单股策略结果做行业加权排名）。

### 单股扫描策略

| 策略 id | 名称 | 说明 |
|---|---|---|
| `uptrend` | 上涨趋势 | 8 条件打分：多头排列/站上年线/MA20上行/低点抬高/高点抬高/斜率向上/近60日新高/放量，满足≥`min_score`(默认5) 命中；历史不足按比例折到 `/8` |
| `rsi` | RSI 超买 | RSI(`period`=6) 超过阈值 `threshold`(80) 判定为超买信号 |
| `pullback` | 大涨回撤缩量 | 近20日涨幅≥`rally_pct`(15%) 后，自高点回撤 `pullback_min`~`pullback_max`(3%~20%)，且量能萎缩到近20日均量的 `vol_shrink_ratio`(0.7) 以下，视为健康回踩 |

### 板块级/复合策略（行业加权排名）

| 策略 id | 名称 | 说明 |
|---|---|---|
| `a_rank` | A股板块得分排名 | 底层跑 `uptrend`，按 8/8=8分、7/8=6分、6/8=4分 得行业加权总分；取总分前 15 为池，池内按平均分排序 |
| `a_rank_delta` | 板块排名升降 | 基于 `a_rank`，与前一交易日对比，输出前日/今日排名、排名变化、新进池/退出池 |
| `a_rank_report_v1` | A_rank_report_v1 日报 | `a_rank` 的正式落地版（≥5 命中、历史不足按比例折 `/8`、前 15 池按平均分排、每行业给代表股前 9 只）；生成日报并邮件发送，每个交易日 15:35 由常驻调度器自动执行 |

> 使用：单股策略用 `scan_rank.py --strategies <id>` 直接扫描；板块级策略（`sector_rank`）同参数调用，底层会自动先跑其 `base` 策略再聚合。

## 运行

```bash
# 环境: conda py12, Python 3.12
# 邮件配置(本地, 勿提交): ~/.mail_sender.json
#   {"host":"smtp.163.com","port":465,"user":"xxx@163.com","pass":"授权码","to":"收件人1,收件人2"}

cd v1
bash code/run_a_rank_daily.sh                        # 扫当日 + 前一交易日 delta
bash code/run_a_rank_daily_with_mail.sh              # 同上并发邮件
nohup /path/to/python code/a_rank_report_v1_scheduler.py >> output/a_rank_report_v1_scheduler.log 2>&1 &  # 常驻调度
python code/a_rank_report_v1_scheduler.py --status   # 查调度状态
```

> 行情数据（腾讯）、A股行业（东方财富）运行时自动拉取并缓存到 `v1/data/`；每日产物在 `v1/output/`，两者均不入库。
