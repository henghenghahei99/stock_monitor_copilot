#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
把 A_rank 日报 HTML 用 SMTP 发送到指定邮箱(默认 smtp.163.com, 支持腾讯/QQ等)。

【重要·凭据安全】
  发信需要邮箱的"授权码"(不是登录密码)。授权码属于敏感信息, 请在本地配置文件里填写,
  **不要发给任何人 / 不要粘贴到对话里**。
  配置文件: ~/.mail_sender.json  内容形如:
    {"host":"smtp.163.com","port":465,"user":"lx20010@163.com","pass":"你的163授权码","to":"lx20010@163.com,17530737@qq.com"}
  收件人 to 支持多个, 用逗号/分号/顿号/空格分隔, 或直接给 JSON 数组。
  也可用环境变量: SMTP_USER / SMTP_PASS(如 qq 邮箱改 host=smtp.qq.com)。

用法:
  python make_a_rank_report.py --date 0902            # 先生成报告
  python send_report.py --file output/a_rank_report_0902.html --subject "A_rank 日报 0902"
"""

from __future__ import annotations

import argparse
import json
import os
import re
import smtplib
import sys
from email.header import Header
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formataddr

CONF = os.path.expanduser("~/.mail_sender.json")


def load_conf():
    if os.path.exists(CONF):
        with open(CONF, encoding="utf-8") as f:
            c = json.load(f)
        c.setdefault("user", c.get("user") or os.environ.get("SMTP_USER", ""))
        c.setdefault("pass", c.get("pass") or os.environ.get("SMTP_PASS", ""))
        return c
    return {
        "host": os.environ.get("SMTP_HOST", "smtp.163.com"),
        "port": int(os.environ.get("SMTP_PORT", "465")),
        "user": os.environ.get("SMTP_USER", ""),
        "pass": os.environ.get("SMTP_PASS", ""),
        "to": os.environ.get("SMTP_TO", ""),
    }


def _to_list(to) -> list[str]:
    """收件人 -> 列表: 支持 str(逗号/分号/顿号/空格分隔) 或 list。"""
    if to is None:
        return []
    if isinstance(to, list):
        return [str(x).strip() for x in to if str(x).strip()]
    return [p for p in re.split(r"[,;，、\s]+", str(to)) if p]


def main() -> None:
    p = argparse.ArgumentParser(description="发送A_rank日报邮件")
    p.add_argument("--file", required=True, help="HTML报告文件路径")
    p.add_argument("--subject", default="A_rank 日报", help="邮件主题")
    p.add_argument("--to", default=None, help="收件人(默认取配置)")
    args = p.parse_args()

    c = load_conf()
    recipients = _to_list(args.to or c.get("to"))
    if not (c.get("user") and c.get("pass") and recipients):
        print("缺少SMTP凭据或收件人。请在本地填写授权码后重试:", file=sys.stderr)
        print(f"  编辑配置文件: {CONF}", file=sys.stderr)
        print("  格式: " + json.dumps({"host": "smtp.163.com", "port": 465,
              "user": "lx20010@163.com", "pass": "授权码", "to": "收件人1,收件人2"}, ensure_ascii=False),
              file=sys.stderr)
        sys.exit(1)

    with open(args.file, encoding="utf-8") as f:
        html = f.read()

    msg = MIMEMultipart("alternative")
    msg["From"] = formataddr((str(Header("A_rank日报", "utf-8")), c["user"]))
    msg["To"] = ", ".join(recipients)
    msg["Subject"] = Header(args.subject, "utf-8")
    msg.attach(MIMEText("详见HTML附件内容(请用支持HTML的邮件客户端查看)。", "plain", "utf-8"))
    msg.attach(MIMEText(html, "html", "utf-8"))

    print(f"连接 {c['host']}:{c['port']} 发送给 {', '.join(recipients)} ...")
    if c.get("port") == 465:
        s = smtplib.SMTP_SSL(c["host"], c["port"], timeout=30)
    else:
        s = smtplib.SMTP(c["host"], c.get("port", 25), timeout=30)
        s.starttls()
    s.login(c["user"], c["pass"])
    s.sendmail(c["user"], recipients, msg.as_string())
    s.quit()
    print("发送成功 ✅")


if __name__ == "__main__":
    main()
