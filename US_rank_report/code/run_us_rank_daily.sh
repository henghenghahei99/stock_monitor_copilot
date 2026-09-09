#!/usr/bin/env bash
# 美股板块 A_rank 日报一键(不发邮件)
# 用法: bash code/run_us_rank_daily.sh [YYYYMMDD]
set -euo pipefail
cd "$(dirname "$0")/.."
PY="/home/sld/miniconda3/envs/py12/bin/python"
DAY="${1:-auto}"
TOP="${TOP:-10}"          # 趋势/动量各入池数(默认10, 与A股最新日报一致)
WORKERS="${WORKERS:-30}"  # 线程数(代理池每批约50个IP轮换, 默认30)
echo "=== 美股板块 A_rank ${DAY} (top=${TOP}, workers=${WORKERS}) ==="
"$PY" -u code/us_rank.py --scan --day "$DAY" --workers "$WORKERS"
# 反查实际日期(scan 输出按K线最新交易日命名)
DAY=$($PY -c "import glob,os;fs=sorted(glob.glob('output/us_uptrend_*.csv'));print(os.path.basename(fs[-1]).replace('us_uptrend_','').replace('.csv',''))")
echo "=== 实际日期 $DAY ==="
"$PY" -u code/us_rank.py --rank --day "$DAY" --top "$TOP"
"$PY" -u code/us_rank.py --delta --day "$DAY"
"$PY" code/make_us_rank_report.py "$DAY" --top "$TOP"
echo "完成: output/us_rank_report_${DAY}.html"
