#!/usr/bin/env bash
# -*- coding: utf-8 -*-
# A_rank_report_v1 日报全自动一键: 跑当日榜单 -> 与前一日对比(delta) -> 生成HTML报告 -> 发邮件。
# 用法:
#   bash code/run_a_rank_daily_with_mail.sh               # 今天(收盘后跑, 建议15:00后)
#   bash code/run_a_rank_daily_with_mail.sh 20260903      # 指定日期 YYYYMMDD
# 说明:
#   发信用 ~/.mail_sender.json 里的 SMTP 凭据(163授权码), 收件人默认 lx20010@163.com,17530737@qq.com
#   TOP/WORKERS/DELAY 可用环境变量覆盖(默认 TOP=10、workers=30、delay=0.02; 代理池每批约50个IP轮换)
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"   # .../A_rank_report/code
API_DIR="$(dirname "$SCRIPT_DIR")"                            # .../A_rank_report
cd "$API_DIR"

PY="${PY:-/home/sld/miniconda3/envs/py12/bin/python}"
DATE="${1:-$(date +%m%d)}"   # MMDD, 如 0903
MM="${DATE:0:2}"; DD="${DATE:2:2}"
TOP="${TOP:-10}"         # 趋势/动量各入池数(默认10, 与美股/A股最新日报一致)
WORKERS="${WORKERS:-30}"   # 线程数(代理池每批约50个IP轮换, 默认30)
DELAY="${DELAY:-0.02}"    # 单只请求前节流(秒)
MIN_PRICE="${MIN_PRICE:-1.0}"  # A股参与价格门槛(元), 默认≥1元
NEW="output/cn_uptrend_${DATE}.csv"

echo "========== A_rank_report_v1 日报全自动 ${DATE} =========="

# 1) 当日榜单(今日结果已存在则跳过扫描)
if [[ -f "$NEW" ]]; then
  echo "[1/4] 今日结果已存在, 跳过扫描: $NEW"
else
  echo "[1/4] 扫描 A股上涨趋势 + A_rank 排名 (top=$TOP, workers=$WORKERS, delay=$DELAY, min_price=$MIN_PRICE) ..."
  "$PY" -u code/scan_rank.py --strategies a_rank_report_v2 \
    --workers "$WORKERS" --delay "$DELAY" --top "$TOP" --min-price "$MIN_PRICE" \
    --results-out "$NEW" --rank-out "output/a_rank_${DATE}.csv"
fi

# 1.5) 确保当日全市场动量表就绪: 缺失 或 只是盘中快照(早于当日A股收盘确认 15:05) -> 从320缓存离线重算(0联网)
ASOF=$("$PY" -c "import pandas as pd;print(pd.to_datetime(pd.read_csv('output/cn_uptrend_${DATE}.csv',encoding='utf-8-sig')['date']).max().strftime('%Y%m%d'))" 2>/dev/null || echo "20$(date +%y)${DATE}")
MOM="output/cn_momentum_${ASOF}.csv"
STALE=0
if [[ -z "$ASOF" ]]; then STALE=0
elif [[ ! -f "$MOM" ]]; then STALE=1
elif [[ "$ASOF" == "$(date +%Y%m%d)" && "$(stat -c %Y "$MOM")" -lt "$(date -d 'today 15:05' +%s)" ]]; then STALE=1
fi
if [[ "$STALE" == 1 ]]; then
  echo "[动量] 生成 cn_momentum_${ASOF}.csv (离线, 从320缓存) ..."
  "$PY" -u code/sector_momentum.py --momentum "$ASOF" --offline || echo "[动量] 离线生成失败, 继续(联网兜底)"
else
  [[ -n "$ASOF" ]] && echo "[动量] cn_momentum_${ASOF}.csv 已就绪"
fi

# 2) 找"前一交易日"(按交易日历, 跳过周末/节假日)的结果文件; 不存在则退回最新一份
PREV=$("$PY" -u code/cn_trading_days.py --prev --mmdd 2>/dev/null || true)
OLD=""
if [[ -n "$PREV" && -f "output/cn_uptrend_${PREV}.csv" && "cn_uptrend_${PREV}.csv" != "cn_uptrend_${DATE}.csv" ]]; then
  OLD="output/cn_uptrend_${PREV}.csv"
  echo "[2/4] 前一交易日(交易日历): ${PREV}"
else
  OLD=$(ls -1 output/cn_uptrend_*.csv 2>/dev/null \
    | grep -vE '_scan_rank|_delta|_industry|_em|_hitcount|_md' \
    | grep -vF "cn_uptrend_${DATE}.csv" \
    | sort | tail -1 || true)
  [[ -n "$OLD" ]] && echo "[2/4] 无前一交易日 ${PREV:-?} 的结果文件, 退回用 $(basename "$OLD")"
fi
if [[ -n "$OLD" ]]; then
  echo "[2/4] delta: $(basename "$OLD") -> $(basename "$NEW") (top=$TOP)"
  "$PY" -u code/a_rank_delta.py -o "$OLD" -n "$NEW" --top "$TOP" --col uptrend7 >/dev/null
else
  echo "[2/4] 未找到前一交易日, 跳过 delta"
fi

# 3) 生成 HTML/文本报告
echo "[3/4] 生成报告 (top=$TOP) ..."
"$PY" -u code/make_a_rank_report.py --date "$DATE" \
  --results "$NEW" --delta "output/cn_uptrend_${DATE}_delta.csv" --top "$TOP" --col uptrend7 >/dev/null
# 4) 发邮件(收件人取 ~/.mail_sender.json 的 to, 默认两个邮箱)
echo "[4/4] 发送邮件 ..."
"$PY" -u code/send_report.py \
  --file "output/a_rank_report_${DATE}.html" \
  --subject "A_rank_report_v1 日报 20${MM}-${DD}"
echo "========== 完成: 榜单 output/a_rank_${DATE}.csv | 报告 output/a_rank_report_${DATE}.html =========="
