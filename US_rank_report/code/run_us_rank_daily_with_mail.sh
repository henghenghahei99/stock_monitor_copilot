#!/usr/bin/env bash
# -*- coding: utf-8 -*-
# 美股板块 A_rank 日报: 跑当日榜单 -> delta -> 生成HTML报告 -> 发邮件(只发 lx20010@163.com)
# 用法:
#   bash code/run_us_rank_daily_with_mail.sh               # 今天(美股收盘后跑)
#   bash code/run_us_rank_daily_with_mail.sh 20260908      # 指定日期 YYYYMMDD
# 说明:
#   收件人固定为 lx20010@163.com(不带公共配置里的 qq 邮箱)。
#   走势图已由 make_us_rank_report 导出 PNG(us_rank_report_<D>_charts/chart_N.png),
#   send_report.py 会转成内联图嵌进邮件正文(163/QQ 不渲染内嵌SVG)。
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."
PY="/home/sld/miniconda3/envs/py12/bin/python"
DAY="${1:-auto}"
TOP="${TOP:-10}"      # 趋势/动量各入池数(默认10)

echo "=== 美股板块 A_rank ${DAY} (top=${TOP}) ==="
"$PY" -u code/us_rank.py --scan --day "$DAY" --workers 8
# 反查实际日期(scan 输出按K线最新交易日命名)
DAY=$("$PY" -c "import glob,os;fs=sorted(glob.glob('output/us_uptrend_*.csv'));print(os.path.basename(fs[-1]).replace('us_uptrend_','').replace('.csv',''))")
echo "=== 实际日期 $DAY ==="
"$PY" -u code/us_rank.py --rank --day "$DAY" --top "$TOP"
"$PY" -u code/us_rank.py --delta --day "$DAY"
"$PY" code/make_us_rank_report.py "$DAY" --top "$TOP"

echo "=== 发邮件(只发 lx20010@163.com) ==="
"$PY" -u ../A_rank_report/code/send_report.py \
  --file "output/us_rank_report_${DAY}.html" \
  --subject "美股板块 A_rank 日报 ${DAY:0:4}-${DAY:4:2}-${DAY:6:2}" \
  --to "lx20010@163.com"
echo "完成: output/us_rank_report_${DAY}.html (已发 lx20010@163.com)"
