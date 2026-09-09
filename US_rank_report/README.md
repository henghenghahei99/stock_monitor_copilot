# US_rank_report — 美股板块 A_rank 日报（照搬 A股日报口径）

按**东财美股细分行业(与A股同源中文口径)**把全市场美股聚合，输出与 A股日报同构的板块排名日报：
趋势分(上涨趋势8条件) + 动量分(m=当日+近2+近3日涨幅) → 双口径入池 → 总分排名 + 与前一日升降。

## 目录
- `code/us_rank.py`：核心（建池/全市场扫描/入池排名/升降对比）
- `code/make_us_rank_report.py`：HTML/TXT 报告(走势图同时导出 PNG 供邮件内嵌)
- `code/fetch_us_em_industry.py`：拉东财美股细分行业 -> data/us_em_industry.json
- `code/run_us_rank_daily.sh`：一键流程(不发邮件)
- `code/run_us_rank_daily_with_mail.sh`：一键流程 + 发邮件(**只发 lx20010@163.com**)
- `output/`：`us_uptrend_YYYYMMDD.csv`(命中) `us_momentum_YYYYMMDD.csv`(成分股动量) `us_rank_YYYYMMDD.csv`(入池排名) `us_delta_YYYYMMDD.csv`(升降) `us_rank_report_YYYYMMDD.html` + `..._charts/chart_N.png`

## 用法(conda py12)
```bash
cd US_rank_report
# 全市场扫描(首次需拉K线, 较长; 之后增量走缓存)
python code/us_rank.py --scan --workers 8
# 入池排名 + 与前一日对比 + 报告
python code/us_rank.py --rank --top 10
python code/us_rank.py --delta
python code/make_us_rank_report.py --top 10
# 或一键
bash code/run_us_rank_daily.sh
# 一键 + 发信(只发 lx20010@163.com, 走势图转PNG内联)
bash code/run_us_rank_daily_with_mail.sh
# 小批量联调
python code/us_rank.py --scan --rank --limit 200 --workers 8
```

## 口径(与 A股日报一致)
- 池：本地 nasdaq/nyse/amex full 名单, 取有行业归属、市值>5亿美元、非空壳 → **3130只/东财行业约177个**(东财缺失回退纳斯达克翻译)
- **参与门槛**：市值>5亿美元 且 近20日均成交额>1000万美元 且 均持股>10万股 且 股价≥$1(不满足既不进动量成员也不算命中)
- 趋势：上涨趋势8条件(多头排列/站上年线MA200/MA20上行/低点抬高/高点抬高/斜率向上/近60日新高/放量) ≥5命中; 历史不足按比例折算/8
- 行业加权 8/8→8分、7/8→6分、6/8→4分(6分以下计0); 趋势分=得分/计入股票数
- 动量入池分=板块全部成分股按 m 降序前10 之和S; 展示动量分(±10)=S÷(5×只数)=前n只平均涨幅÷5
- 入池=原行业得分前10 ∪ 动量入池分前10(并集≤20, `--top` 可调); 总分=趋势×50%+动量×50%; 池内按总分降序
- 代表股=趋势前5 + ◎动量前5(全部成分股按m)
- 板块=东财美股细分行业(中文, 与A股同源口径); 少数缺失回退纳斯达克英文翻译
- 走势图只画最近5个交易日, 且板块须5日中≥3日在池(非退出池/未入池)才画线

## 说明
- 行情：腾讯美股日K(前复权, 最多可拉到800根; 池内缓存320根), 交易所后缀 纳=.OQ 纽=.N 美交所=.AM; 缓存共享 data/kline_cache
- 东财行业: data/us_em_industry.json(由 fetch_us_em_industry.py 分页拉取 RPT_USF10_INFO_ORGPROFILE)
- 发信: 复用 A_rank_report/code/send_report.py(SMTP 163授权码), 美股报告固定收件人 **lx20010@163.com**(用 `--to` 覆盖公共配置, 不带 qq 邮箱)
- 首次全市场扫描耗时长(数千只), 建议每日收盘后跑一次; 当日重复跑走增量缓存
- 升降/走势需 ≥2 个交易日的历史产物; 第一天只有排名表
