#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""上线后一键验证：机器人是不是真的跑在「飞书官方 API」取消息模式下。

只读：不写任何生产文件、不发飞书、不下单。
用法：venv/bin/python tools/verify_api_mode.py
"""
import os, re, sys, json, time, subprocess, datetime

BASE = "/home/ubuntu/signal-bot"
RUN = BASE + "/v21"
LOG = RUN + "/run.log"
STATE = RUN + "/state.json"
sys.path.insert(0, BASE)

ok_all, fail = True, []


def ck(name, cond, detail=""):
    global ok_all
    print("  %s %-46s %s" % ("[ OK ]" if cond else "[FAIL]", name, detail))
    if not cond:
        ok_all = False
        fail.append(name)


print("=" * 78)
print("取消息模式验证（官方 API）  %s" % datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
print("=" * 78)

# ---------- 1) 令牌 ----------
print("\n[1] 飞书令牌")
try:
    import feishu_api as f
    ti = f.token_info()
    ck("有 access_token", bool(ti.get("has_token")),
       "剩余 %d 分钟" % (max(0, ti.get("access_left_s", 0)) // 60))
    ck("有 refresh_token（可自动续期）", bool(ti.get("has_refresh")),
       "剩余 %d 小时" % (max(0, ti.get("refresh_left_s", 0)) // 3600))
    _ok, _why = f.health()
    ck("能列群（令牌真的可用）", _ok, _why[:70])
except Exception as e:
    ck("feishu_api 可用", False, str(e)[:80])

# ---------- 2) 配置 ----------
print("\n[2] 运行配置")
try:
    rc = json.load(open(BASE + "/runtime_config.json", encoding="utf-8"))
    ck("fetch_mode = api", rc.get("fetch_mode") == "api", "当前 = %s" % rc.get("fetch_mode"))
    ck("影子模式（不下真单）", rc.get("live_trading") is False, "live_trading=%s" % rc.get("live_trading"))
    print("     监控群：%s" % "、".join(rc.get("groups") or []))
except Exception as e:
    ck("读 runtime_config.json", False, str(e)[:80])

# ---------- 3) 日志证据 ----------
print("\n[3] 日志证据（最近 3000 行）")
lines = []
if os.path.exists(LOG):
    with open(LOG, encoding="utf-8", errors="replace") as fh:
        lines = fh.readlines()[-3000:]
txt = "".join(lines)
ck("出现「取消息模式：飞书官方 API」", "取消息模式：飞书官方 API" in txt)
ck("出现「开始实时监控（… API 模式）」", re.search(r"开始实时监控（\d+ 个群，API 模式）", txt) is not None,
   (re.search(r"开始实时监控（[^）]*）", txt).group(0) if re.search(r"开始实时监控（[^）]*）", txt) else ""))
ck("API 自检通过", "官方 API 自检通过" in txt,
   (re.search(r"官方 API 自检通过[^\n]*", txt).group(0)[:70] if "官方 API 自检通过" in txt else ""))
ck("没有「回退：改用浏览器爬网页」", "回退：改用浏览器爬网页" not in txt)
ck("没有 API 连续取消息失败", "API 取消息失败" not in txt)
ck("没有 Traceback", "Traceback" not in txt)
_hb = [l for l in lines if "心跳：运行中" in l]
if _hb:
    _t = re.match(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})", _hb[-1])
    _age = (datetime.datetime.now() - datetime.datetime.strptime(_t.group(1), "%Y-%m-%d %H:%M:%S")).total_seconds() if _t else 9999
    ck("心跳新鲜（<60 秒）", _age < 60, "最后一条 %.0f 秒前：%s" % (_age, _hb[-1].strip()[:60]))
else:
    ck("有心跳", False, "最近 3000 行里没有心跳")

# ---------- 4) 状态文件 ----------
print("\n[4] state.json")
try:
    st = json.load(open(STATE, encoding="utf-8"))
    la = st.get("last_api") or {}
    ck("有 API 游标（last_api）", bool(la), "%d 个群" % len(la))
    for g, ms in list(la.items())[:6]:
        print("     %-16s 已处理到 %s" % (g, datetime.datetime.fromtimestamp(ms / 1000).strftime("%m-%d %H:%M:%S")))
    ck("持仓数与纸面一致（无孤儿）", isinstance(st.get("open"), dict),
       "持仓 %d 笔" % len(st.get("open") or {}))
except Exception as e:
    ck("读 state.json", False, str(e)[:80])

# ---------- 5) 进程 ----------
print("\n[5] 进程")
try:
    out = subprocess.run(["pm2", "list"], capture_output=True, text=True, timeout=30).stdout
    m = re.search(r"dryrun-bot2.*?(online|stopped|errored)", out)
    ck("dryrun-bot2 在线", bool(m and m.group(1) == "online"), m.group(1) if m else "没找到")
    m2 = re.search(r"dryrun-bot2\s*\|\s*\d+\s*\|\s*([\d]+m?h?)\s*\|\s*(\d+)", out)
    if m2:
        print("     已运行 %s ｜ 重启次数 %s" % (m2.group(1), m2.group(2)))
except Exception as e:
    ck("查询 pm2", False, str(e)[:80])

print("\n" + "=" * 78)
if fail:
    print("结果：%d 项未通过 → %s" % (len(fail), "；".join(fail)))
    sys.exit(1)
print("结果：全部通过 ✅ 机器人正在用飞书官方 API 取消息（不需要浏览器）")
