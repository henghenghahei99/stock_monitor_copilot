#!/usr/bin/env bash
# -*- coding: utf-8 -*-
"""
A_rank_report_v1 日报一键脚本(不发邮件):
  1) 跑当天 A_rank: A股上涨趋势 -> 8/7/6行业加权 -> 总分前15池 -> 平均分排名
     (今日结果已存在则跳过扫描, 只做 delta)
  2) 自动找"前一日"结果, 跑 A_rank_delta 输出板块排名升降榜单

用法:
  bash code/run_a_rank_daily.sh            # 扫今天 + 与前一日对比
  bash code/run_a_rank_daily.sh 20260903   # 指定日期(回补历史某日)
"""
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"   # .../A_rank_report/code
API_DIR="$(dirname "$SCRIPT_DIR")"                            # .../A_rank_report
cd "$API_DIR"

PY="${PY:-/home/sld/miniconda3/envs/py12/bin/python}"
DATE="${1:-$(date +%m%d)}"   # MMDD, 如 0903
NEW="output/cn_uptrend_${DATE}.csv"

echo "=== A_rank_report_v1 日报 ${DATE} ==="

if [[ -f "$NEW" ]]; then
  echo "[跳过扫描] 今日结果已存在: $NEW"
else
  echo "[1/2] 运行 A_rank_report_v1 扫描 (A股上涨趋势 -> 行业加权排名) ..."
  "$PY" -u code/scan_rank.py --strategies a_rank_report_v1 \
    --workers 6 --delay 0.15 --top 15 \
    --results-out "$NEW" \
    --rank-out "output/a_rank_${DATE}.csv"
fi

# 找"前一交易日"(按交易日历, 跳过周末/节假日)的结果文件; 不存在则退回最新一份
PREV=$("$PY" -u code/cn_trading_days.py --prev --mmdd 2>/dev/null || true)
OLD=""
if [[ -n "$PREV" && -f "output/cn_uptrend_${PREV}.csv" && "cn_uptrend_${PREV}.csv" != "cn_uptrend_${DATE}.csv" ]]; then
  OLD="output/cn_uptrend_${PREV}.csv"
  echo "[delta] 前一交易日(交易日历): ${PREV}"
else
  OLD=$(ls -1 output/cn_uptrend_*.csv 2>/dev/null \
    | grep -vE '_scan_rank|_delta|_industry|_em|_hitcount|_md' \
    | grep -vF "cn_uptrend_${DATE}.csv" \
    | sort | tail -1 || true)
  [[ -n "$OLD" ]] && echo "[delta] 无前一交易日 ${PREV:-?} 的结果文件, 退回用 $(basename "$OLD")"
fi
if [[ -n "$OLD" ]]; then
  echo ""
  echo "[2/2] A_rank_delta: $(basename "$OLD") -> $(basename "$NEW")"
  "$PY" -u code/a_rank_delta.py -o "$OLD" -n "$NEW"
else
  echo ""
  echo "[提示] 未找到前一交易日结果, 本日只生成 A_rank 榜单, 无 delta 对比。"
fi
echo "完成: 排名见 output/a_rank_${DATE}.csv"
