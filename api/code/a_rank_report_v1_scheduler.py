#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
A_rank_report_v1 常驻调度器(daemon) —— 定时写在代码里。

规则:
  * 每个交易日 15:35(A股15:00收盘后)自动执行一次完整流程:
      拉当日市场数据 -> A_rank 榜单 + 前一交易日 delta -> 生成报告 -> 发邮件
    (流程由 code/run_a_rank_daily_with_mail.sh 完成, delta 基线按交易日历取"前一交易日")
  * 非交易日(周末/节假日): 按上证指数K线得到的交易日历自动跳过, 不发重复邮件。
  * 同一天只执行一次(状态记在 output/.a_rank_report_v1_state.json)。
  * 失败自动重试(最多3次), 每次间隔约一个检查周期。

启动方式(任选, 建议用系统 cron @reboot 开机自启, 见仓库说明):
  nohup python a_rank_report_v1_scheduler.py >> ../output/a_rank_report_v1_scheduler.log 2>&1 &

用法:
  python a_rank_report_v1_scheduler.py            # 常驻运行
  python a_rank_report_v1_scheduler.py --status   # 查看: 当前时间/今天是否交易日/前一交易日/最近完成日
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime, time as dtime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]  # api/
sys.path.insert(0, str(ROOT / "code"))
import cn_trading_days as ctd  # noqa: E402

TZ = ZoneInfo("Asia/Shanghai")
RUN_AT = dtime(15, 35)          # 收盘(15:00)后 15:35 跑当日数据
IDLE_EVERY = 60                 # 秒: 非决策时段空转间隔(纯本地, 不联网)
MAX_RETRY = 3                   # 同一天失败最多重试次数

STATE = ROOT / "output" / ".a_rank_report_v1_state.json"
LOCK = ROOT / "output" / ".a_rank_report_v1.lock"
SCRIPT = ROOT / "code" / "run_a_rank_daily_with_mail.sh"
BASH = "/usr/bin/bash"


def log(*a) -> None:
    print(datetime.now(TZ).strftime("[%F %T]"), *a, flush=True)


def load_state() -> dict:
    try:
        return json.loads(STATE.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {}


def save_state(st: dict) -> None:
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(st, ensure_ascii=False, indent=2), encoding="utf-8")


def now_sh() -> datetime:
    return datetime.now(TZ)


def acquire_lock() -> bool:
    try:
        if LOCK.exists():
            pid = int(LOCK.read_text().strip())
            os.kill(pid, 0)          # pid 存活则说明已有实例
            return False
    except (ValueError, ProcessLookupError):
        pass                         # 旧锁失效, 接管
    LOCK.write_text(str(os.getpid()))
    return True


def release_lock() -> None:
    try:
        if LOCK.exists() and LOCK.read_text().strip() == str(os.getpid()):
            LOCK.unlink()
    except Exception:  # noqa: BLE001
        pass


def run_pipeline(today: datetime.date) -> bool:
    mmdd = today.strftime("%m%d")
    log(f"触发 A_rank_report_v1 完整流程 DATE={mmdd} ...")
    try:
        r = subprocess.run([BASH, str(SCRIPT), mmdd], cwd=str(ROOT))
    except Exception as exc:  # noqa: BLE001
        log("启动流程异常:", exc)
        return False
    ok = r.returncode == 0
    log("流程", "成功 ✅" if ok else f"失败(rc={r.returncode}), 稍后重试")
    return ok


def sleep_until(next_dt: datetime) -> None:
    """睡到 next_dt(单次最多1小时, 醒后重算, 应对睡眠/时钟微调)。"""
    secs = max(0.0, (next_dt - now_sh()).total_seconds())
    time.sleep(min(secs, 3600))


def print_status() -> None:
    n = now_sh()
    st = load_state()
    print("本地时间       :", n.strftime("%F %T %A"))
    try:
        ds = ctd.index_trade_dates(force=True)
        print("最近交易日     :", ds[-1].isoformat())
        print("今天是否交易日 :", "是" if ctd.is_trade_date(n.date()) else "否")
        print("前一交易日     :", ctd.prev_trade_date().isoformat())
    except Exception as exc:  # noqa: BLE001
        print("交易日历获取失败:", exc)
    print("最近已完成日期 :", st.get("done"))
    print("运行时刻       : 交易日 15:35 (Asia/Shanghai)")


def main() -> None:
    ap = argparse.ArgumentParser(description="A_rank_report_v1 常驻调度器")
    ap.add_argument("--status", action="store_true", help="打印状态后退出")
    args = ap.parse_args()

    if not acquire_lock():
        log("已有另一个调度器在运行(锁占用), 退出")
        return
    try:
        if args.status:
            print_status()
            return
        log("A_rank_report_v1 调度器启动. 规则: 每个交易日 15:35 跑当日报告 + 前一交易日 delta; 非交易日自动跳过.")
        st = load_state()
        while True:
            n = now_sh()
            today = n.date()
            done = st.get("done")

            if done == today.isoformat():
                # 今天已跑完, 空转至次日(不联网)
                sleep_until(datetime.combine(today + timedelta(days=1), dtime(0, 5), tzinfo=TZ))
                continue

            if n.time() < RUN_AT:
                # 还没到点: 本地空转(不联网)
                time.sleep(IDLE_EVERY)
                continue

            # 决策窗口(>=15:35): 拉一次上证指数K线判断今天是否交易日(收盘后K线已含当日)
            try:
                trade = ctd.is_trade_date(today, force=True)
            except Exception as exc:  # noqa: BLE001
                log("交易日历获取失败:", exc)
                trade = False

            if not trade:
                # 非交易日(周末/节假日): 睡到明天 00:05, 次日再判断
                log(f"{today} 非交易日, 跳过(等下一交易日)")
                sleep_until(datetime.combine(today + timedelta(days=1), dtime(0, 5), tzinfo=TZ))
                continue

            if run_pipeline(today):
                st["done"] = today.isoformat()
                st.pop("retries", None)
                save_state(st)
            else:
                st["retries"] = int(st.get("retries", 0)) + 1
                if st["retries"] >= MAX_RETRY:
                    log(f"今天连续失败 {MAX_RETRY} 次, 不再重试(等次日)")
                    st["done"] = today.isoformat()
                save_state(st)
                time.sleep(120)
    finally:
        release_lock()


if __name__ == "__main__":
    main()
