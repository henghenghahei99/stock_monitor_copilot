#!/usr/bin/env bash
# 港股板块 A_rank 日报一键脚本(不发邮件)
#   1) 拉取/更新 东财港股行业(data/hk_em_industry.json)
#   2) 扫描全市场(参与门槛: 近5日平均成交额>4000万港元) -> hits + 动量成员
#   3) 板块入池排名(双口径: 趋势腿∪动量腿) + 与前一交易日升降
#   4) 生成 HTML/TXT 报告(走势图 4 张)
# 用法:
#   bash code/run_hk_rank_daily.sh              # 最新"已收盘确认"交易日
#   bash code/run_hk_rank_daily.sh 20260910     # 指定日期(回补历史)
#   TOP/WORKERS 可用环境变量覆盖(默认 TOP=10、WORKERS=30)
set -euo pipefail
cd "$(dirname "$0")/.."              # .../HK_rank_report

PY="${PY:-/home/sld/miniconda3/envs/py12/bin/python}"
DAY="${1:-auto}"
TOP="${TOP:-10}"
WORKERS="${WORKERS:-30}"

echo "=== 港股板块 A_rank ${DAY} (top=${TOP}, workers=${WORKERS}) ==="

# 0) 行业缓存(缺失/过期会自动拉取, 失败不阻断)
"$PY" -u code/fetch_hk_em_industry.py >/dev/null 2>&1 || echo "[行业] 拉取失败, 用现有缓存"

# 1) 扫描
"$PY" -u code/hk_rank.py --scan --day "$DAY" --workers "$WORKERS"
DAY=$("$PY" -c "import glob,os;fs=sorted(glob.glob('output/hk_uptrend_*.csv'));print(os.path.basename(fs[-1]).replace('hk_uptrend_','').replace('.csv',''))")
echo "=== 实际日期 $DAY ==="

# 2) 排名 + 升降
"$PY" -u code/hk_rank.py --rank --day "$DAY" --top "$TOP"
"$PY" -u code/hk_rank.py --delta --day "$DAY"

# 3) 报告
"$PY" -u code/make_hk_rank_report.py "$DAY" --top "$TOP"
echo "完成: output/hk_rank_report_${DAY}.html"
