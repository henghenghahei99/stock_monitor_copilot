# US_rank_report — 美股板块 A_rank 日报（照搬 A股日报口径）

按**细分行业(149个)**把全市场美股聚合，输出与 A股日报同口径的板块排名日报：
趋势分(上涨趋势8条件) + 动量分(m=当日+近2+近3日涨幅) → 双口径入池 → 总分排名 + 与前一日升降。

## 目录
- `code/us_rank.py`：核心（建池/全市场扫描/入池排名/升降对比）
- `code/make_us_rank_report.py`：HTML/TXT 报告
- `code/run_us_rank_daily.sh`：一键流程
- `output/`：`us_uptrend_YYYYMMDD.csv`(命中) `us_momentum_YYYYMMDD.csv`(成分股动量) `us_rank_YYYYMMDD.csv`(入池排名) `us_delta_YYYYMMDD.csv`(升降) `us_rank_report_YYYYMMDD.html`

## 用法(conda py12)
```bash
cd US_rank_report
# 全市场扫描(首次需拉K线, 较长; 之后增量走缓存)
python code/us_rank.py --scan --workers 8
# 入池排名 + 与前一日对比 + 报告
python code/us_rank.py --rank --top 15
python code/us_rank.py --delta
python code/make_us_rank_report.py
# 或一键
bash code/run_us_rank_daily.sh
# 小批量联调
python code/us_rank.py --scan --rank --limit 200 --workers 8
```

## 口径(与 A股日报一致)
- 池：本地 nasdaq/nyse/amex full 名单, 取有行业归属、市值≥1亿美元、非空壳 → **4055只/149行业**
- 趋势：上涨趋势8条件(多头排列/站上年线MA200/MA20上行/低点抬高/高点抬高/斜率向上/近60日新高/放量) ≥5命中; 历史不足按比例折算/8
- 行业加权 8/8→8分、7/8→6分、6/8→4分(6分以下计0); 趋势分=得分/计入股票数
- 动量入池分=板块全部成分股按 m 降序前10 之和S; 展示动量分(±10)=S÷(5×只数)=前n只平均涨幅÷5
- 入池=原行业得分前15 ∪ 动量入池分前15(并集≤30); 总分=趋势×50%+动量×50%; 池内按总分降序
- 代表股=趋势前5 + ◎动量前5(全部成分股按m)
- 板块中文名来自 translations 表(106行业), 未收录保留英文

## 说明
- 行情：腾讯美股日K(320根前复权), 交易所后缀 纳=.OQ 纽=.N 美交所=.AM; 缓存共享 data/kline_cache
- 首次全市场扫描耗时长(数千只), 建议每日收盘后跑一次; 当日重复跑走增量缓存
- 升降/走势需 ≥2 个交易日的历史产物; 第一天只有排名表
