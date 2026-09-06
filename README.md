# stock_monitor_copilot

A股板块趋势扫描与日报（策略 **A_rank_report_v1**）：每天收盘后拉取行情，按行业汇总上涨趋势强度得分排名，生成带「前一交易日」对比的美化 HTML 报告并邮件发送。

## 目录

- `data/` — 共享数据缓存（行情/行业/代码列表，其他策略可复用，不入库）
- `A_rank_report/code/` — 报告生成源码
  - `scan_rank.py` / `run_strategy.py` / `find_uptrend.py` — 扫描与 8 条件打分
  - `industry_score.py` — 行业加权汇总 / 代表股
  - `sector_momentum.py` — 板块合计排名（趋势分 50% + 代表股动量分 50%）
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
- 行业得分=成员加权分之和；趋势分=得分/股票数
- 个股动量 m = 当日%+近2日%+近3日%（三段叠加，即今收/昨收-1 + 今收/2交易日前收-1 + 今收/3交易日前收-1）
- **动量分(±10，展示) 与 动量入池分 同源**：板块全部成分股（东财行业归属）按 m 降序前 10（不足按实际只数）S=Σm；动量入池分=S（原始）；展示动量分=S÷(15%×只数/10)×0.5 归一（允许为负）
- **总分 = 趋势分×50% + 动量分×50%**，池内按总分降序
- **双口径入池**：入池 = 原行业得分前 15 ∪ 动量入池分前 15（并集，最多 30）；全市场动量表按日缓存 `output/cn_momentum_YYYYMMDD.csv`；报告“入池”列标注 趋势/动量/趋势+动量
- 代表股 = **趋势代表股前 5**（加权分最高，记 X/8、+近20日%）+ **◎动量代表股前 5**（板块全部成分股按 m 最强，记 +短线%，HTML 橙色标 ◎）

> 说明：扫描结果 CSV 会额外输出 `chg1/chg2/chg3`（当日/近2日/近3日累计涨跌幅）供板块动量积分使用。

## 策略

策略配置都在 `A_rank_report/code/strategies/*.json`，分两类：单股扫描策略（扫描个股）和板块级/复合策略（对单股策略结果做行业加权排名）。

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
| `a_rank_delta` | 板块排名升降 | 基于 `a_rank`，与前一交易日对比，输出前日/今日排名、排名变化、新进池/退出池（新口径对比趋势分/动量分/总分变化） |
| `a_rank_report_v1` | A_rank_report_v1 日报 | `a_rank` 的正式落地版：≥5 命中、历史不足按比例折 `/8`；**双口径入池**=原行业得分前15 ∪ 动量入池分前15（动量入池分=板块全部成分股按 m 降序前10之和；m=当日%+近2日%+近3日%，展示动量分=其归一±10 版），池内按总分=趋势分×50%+动量分×50% 降序，每行业代表股=趋势前5+动量前5；生成日报并邮件发送，每个交易日 15:35 由常驻调度器自动执行 |

> 使用：单股策略用 `scan_rank.py --strategies <id>` 直接扫描；板块级策略（`sector_rank`）同参数调用，底层会自动先跑其 `base` 策略再聚合。

## 运行

```bash
# 环境: conda py12, Python 3.12
# 邮件配置(本地, 勿提交): ~/.mail_sender.json
#   {"host":"smtp.163.com","port":465,"user":"xxx@163.com","pass":"授权码","to":"收件人1,收件人2"}

cd A_rank_report
bash code/run_a_rank_daily.sh                        # 扫当日 + 前一交易日 delta
bash code/run_a_rank_daily_with_mail.sh              # 同上并发邮件
nohup /path/to/python code/a_rank_report_v1_scheduler.py >> output/a_rank_report_v1_scheduler.log 2>&1 &  # 常驻调度
python code/a_rank_report_v1_scheduler.py --status   # 查调度状态
```

> 行情数据（腾讯）、A股行业（东方财富）运行时自动拉取并缓存到共享 `data/`；每日产物在 `A_rank_report/output/`，均不入库。
