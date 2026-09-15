#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""外部日志巡检（B2）—— **只读**，不依赖机器人进程活着，也不需要重启机器人。

背景（2026-09-15 B1 事故）：机器人瞎了 93 分钟，而 health_check.py 其实能扫出那批
    `BrowserContext.new_page: Target page, context or browser has been closed`，
    只是没人定时跑、也没人把结论推给用户。

⚠️ 为什么不直接"每 5 分钟跑 health_check.py，输出里有 BrowserContext 就告警"：
    health_check.py 扫的是 run.log 的**最近 400 行**，而心跳占 66%。
    实测 18:56 跑它时，它把 17:40 那批**历史**故障原样列了出来 →
    照那个方案做会**永久每轮告警**（旧行一直赖在窗口里），几天内就被无视 = 告警疲劳。
    本脚本改为**按字节游标只认新增行**，游标落盘；并处理日志轮转（文件变小即重置游标）。
    另加了 health_check 没有的两个维度：**心跳停摆** 与 **进程/状态文件停更**。

用法：
  log_watch.py             正常巡检：只有"新增"故障或异常才推飞书
  log_watch.py --dry-run   只看不推（验证用，绝不发消息）
  log_watch.py --status    打印游标与命中统计，不做任何判断
  log_watch.py --reset     把游标重置到当前文件末尾（首次部署/轮转后手工用）
  log_watch.py --log /tmp/x.log --cursor /tmp/y.json
                           指定日志与游标路径 —— **验证时用它把测试完全隔离到 /tmp**，
                           绝不要再往生产 run.log 里注入测试行（我 09-15 晚犯过这个错）。
"""
import os, sys, json, time, datetime

BASE = "/home/ubuntu/signal-bot"
RUN = BASE + "/v21"
DEFAULT_LOGF = RUN + "/run.log"
DEFAULT_CURSOR = RUN + "/watch_state.json"    # 本脚本自己的游标，不动机器人任何文件
NOTIFY_CFG = BASE + "/notify.json"


def _arg(flag, default):
    if flag in sys.argv:
        i = sys.argv.index(flag)
        if i + 1 < len(sys.argv):
            return sys.argv[i + 1]
    return default


LOGF = _arg("--log", DEFAULT_LOGF)
CURSOR_F = _arg("--cursor", DEFAULT_CURSOR)
STATE = RUN + "/state.json"                   # 固定指向生产状态文件（只读 getmtime）

CST = datetime.timezone(datetime.timedelta(hours=8))
STALE_HEARTBEAT_MIN = 10      # 心跳超过这个分钟数没更新 → 告警（机器人可能卡死）
STALE_STATE_MIN = 10          # state.json 超过这个分钟数没更新 → 告警
COOLDOWN_SEC = 1800           # 普通关键字：同类 30 分钟内只推一次，防轰炸
CRITICAL_COOLDOWN_SEC = 300   # 致命关键字：只冷 5 分钟
# ⚠️ 2026-09-15 B1 实战验证时发现的**真实缺陷**：原来冷却按整个 "fault" 类别计，
#    于是 19:49 一条「对账不一致」告警把 fault 冷却了 30 分钟 → 20:04 **浏览器整体死亡**
#    （BrowserContext）被静默吞掉，实际推送=无。这正是最不能漏的事件。
#    改成：**冷却按关键字分别计**，且致命关键字只冷 5 分钟。
CRITICAL_KW = ("BrowserContext", "熔断", "重建浏览器失败", "浏览器自动重启失败", "对账闸门")
MAX_SHOW = 3                  # 每条告警最多展示几行原文

# 只认这些"新增"行 —— 命中即告警
# ⚠️ 关键字要挑"真的出事"的，不能挑"正常也会打"的（否则就是我自己批评的告警疲劳）：
#    · 不收 "页面不存在/已关闭，正在重新打开…"：单个标签页被关后重开成功是小事，
#      真出事会由下一行的「重开失败」暴露（2026-09-15 事故里两行是成对出现的）。
#    · 不收 "差异"：影子模式下纸面有仓、交易所无仓本来就记为"差异"且属预期
#      （实测启动时打「差异 6 条 ｜ 结论=一致」）。只收 "结论=不一致"，
#      它才代表"对账发现异常"。
FAULT_KW = [
    "BrowserContext",              # 上下文死亡（B1 事故原串）
    "重开失败",                      # 重开失败（含"连续第 N 次"）
    "开页异常",
    "轮询异常",
    "熔断",                         # 连亏/当日亏损/总敞口熔断
    "对账闸门",                      # 启动对账不一致 → 阻止真实开仓
    "结论=不一致",
    "Traceback",
    "重建失败",                      # 轮询分支里的"页面重建失败：…"
    "重建浏览器失败",                 # B1 自愈里"重建浏览器失败（第 N 次）"——注意它不含"重建失败"子串
    "浏览器自动重启失败",             # B1 自愈 notify 文案（会以 [通知] 前缀落进 run.log）
]


def now_str():
    return datetime.datetime.now(CST).strftime("%Y-%m-%d %H:%M:%S")


def load_cursor():
    try:
        return json.load(open(CURSOR_F, encoding="utf-8"))
    except Exception:
        return {}


def save_cursor(d):
    try:
        os.makedirs(RUN, exist_ok=True)
        tmp = CURSOR_F + ".tmp"
        json.dump(d, open(tmp, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
        os.replace(tmp, CURSOR_F)
    except Exception as e:
        print("[warn] 游标落盘失败：%s" % e)


def push_feishu(text, dry=False):
    """推到飞书。文案必须以「【跟单机器人】」开头 —— 否则机器人会把自己的通知当信号重解析
    （交接文档第十一节第 4 条）。"""
    if dry:
        print("[dry-run] 本应推送飞书：\n" + text)
        return
    try:
        hook = json.load(open(NOTIFY_CFG, encoding="utf-8")).get("feishu_webhook")
    except Exception as e:
        print("[warn] 读 notify.json 失败：%s" % e)
        return
    if not hook:
        print("[warn] notify.json 里没有 feishu_webhook，跳过推送")
        return
    try:
        import requests
        r = requests.post(hook, json={"msg_type": "text", "content": {"text": text}},
                          timeout=20).json()
        print("webhook 响应：%s" % json.dumps(r, ensure_ascii=False)[:200])
        if r.get("code") not in (0, None):
            print("[warn] webhook 返回非 0：%s" % json.dumps(r, ensure_ascii=False)[:160])
    except Exception as e:
        print("[warn] webhook 失败：%s" % str(e)[:120])


def bot_process_alive():
    """扫 /proc 找机器人的主进程（只读）"""
    for d in os.listdir("/proc"):
        if not d.isdigit():
            continue
        try:
            cl = open("/proc/%s/cmdline" % d, "rb").read().decode("utf-8", "replace")
        except Exception:
            continue
        if "dryrun_bot2.py" in cl and "python" in cl:
            return int(d)
    return None


def last_heartbeat_ts(lines):
    """从尾部倒着找最后一条心跳的时间戳"""
    for l in reversed(lines):
        if "心跳" in l:
            try:
                return time.mktime(datetime.datetime.strptime(
                    l[:19], "%Y-%m-%d %H:%M:%S").timetuple())
            except Exception:
                return None
    return None


def main():
    dry = "--dry-run" in sys.argv
    status_only = "--status" in sys.argv
    do_reset = "--reset" in sys.argv

    if "--test-alert" in sys.argv:
        # 验证"外部巡检 → 飞书告警"这条链路。刻意把【测试】字样写在最前面，避免被误当成真告警。
        # ⚠️ 飞书 msg_type=text **不渲染 Markdown**：实测 `**` 会原样显示，所以这里不用 `**`。
        push_feishu(
            "【跟单机器人】🩺 【测试】外部巡检告警通道测试 —— 这不是真告警\n"
            "用途：验证「外部巡检 → 飞书」这条链路是通的。\n"
            "时间：%s\n"
            "说明：收到这条即代表 B2 告警通道正常。平时收不到巡检消息 = 系统一直健康，\n"
            "      只有发现【新增】故障行 / 心跳停摆 / 进程消失 / state.json 停更 时才会发。" % now_str())
        print("推送调用已返回（上面若出现 code 0 即成功）")
        return 0

    if not os.path.exists(LOGF):
        print("[FAIL] 日志不存在：%s" % LOGF)
        return 1
    size = os.path.getsize(LOGF)
    cur = load_cursor()

    if status_only:
        print("日志 %s  大小 %d 字节" % (LOGF, size))
        print("游标 offset=%s  last_run=%s" % (cur.get("offset"), cur.get("last_run")))
        print("冷却记录：%s" % json.dumps(cur.get("cooldown") or {}, ensure_ascii=False))
        return 0

    if do_reset or "offset" not in cur:
        # 首次部署 / 手工重置：只对齐到当前末尾，**不对历史告警**（否则一上来就炸一堆旧故障）
        cur["offset"] = size
        cur["last_run"] = now_str()
        save_cursor(cur)
        print("游标已初始化到文件末尾 offset=%d（历史故障不予告警）" % size)
        return 0

    off = int(cur.get("offset") or 0)
    rotated = False
    if size < off:
        # 日志被 copytruncate 轮转过 → 从头读
        print("[info] 检测到日志变小（%d < %d）→ 判定已轮转，游标归零" % (size, off))
        off = 0
        rotated = True

    new_lines = []
    # ⚠️ 只按**完整行**前进游标：如果最后一行还没写完（没有换行符），
    #    这一部分不消费，下次整行完整后再读。
    #    否则游标会落在行中间，下次 seek 到半行 → 关键字被截断 → 真故障漏报。
    #    （这是 09-15 隔离测试里实测暴露出来的问题，不是理论担心。）
    with open(LOGF, "rb") as f:
        f.seek(off)
        raw = f.read()
    nl = raw.rfind(b"\n")
    if nl < 0:
        complete, consumed = b"", 0
    else:
        complete, consumed = raw[:nl + 1], nl + 1
    new_lines = complete.decode("utf-8", "replace").splitlines()
    new_off = off + consumed

    hits = {}
    for l in new_lines:
        for kw in FAULT_KW:
            if kw in l:
                hits.setdefault(kw, []).append(l)
                break

    cand = []          # (冷却键, 正文, 冷却秒数)
    if hits:
        parts = []
        for kw in sorted(hits):
            ls = hits[kw]
            parts.append("· %s × %d\n  %s" % (kw, len(ls),
                                             "\n  ".join(x[:150] for x in ls[:MAX_SHOW])))
        cand.append(("fault:" + "|".join(sorted(hits)),
                     "🔴 新增故障行（共 %d 条）\n%s"
                     % (sum(len(v) for v in hits.values()), "\n".join(parts)),
                     CRITICAL_COOLDOWN_SEC if any(k in CRITICAL_KW for k in hits) else COOLDOWN_SEC))

    # 心跳停摆（机器人可能整体卡死，或进程被 pm2 拉起但没跑起来）
    hb = last_heartbeat_ts(new_lines) or last_heartbeat_ts(
        open(LOGF, encoding="utf-8", errors="replace").read().splitlines()[-400:])
    if hb and (time.time() - hb) > STALE_HEARTBEAT_MIN * 60:
        cand.append(("heartbeat",
                     "⏸️ 心跳停摆：最后一条心跳是 %s（%.1f 分钟前，阈值 %d 分钟）"
                     % (datetime.datetime.fromtimestamp(hb, CST).strftime("%Y-%m-%d %H:%M:%S"),
                        (time.time() - hb) / 60.0, STALE_HEARTBEAT_MIN), COOLDOWN_SEC))

    # 进程 / 状态文件
    pid = bot_process_alive()
    if not pid:
        cand.append(("proc", "💀 找不到机器人主进程（dryrun_bot2.py 不在 /proc 里）",
                     CRITICAL_COOLDOWN_SEC))
    try:
        sm = os.path.getmtime(STATE)
        if (time.time() - sm) > STALE_STATE_MIN * 60:
            cand.append(("state", "⏸️ state.json 已 %.1f 分钟没更新（阈值 %d 分钟）"
                         % ((time.time() - sm) / 60.0, STALE_STATE_MIN), COOLDOWN_SEC))
    except Exception:
        cand.append(("state", "💀 读不到 state.json", COOLDOWN_SEC))

    # 冷却：**按冷却键分别计**（不是按整个类别），致命项只冷 5 分钟
    cd = cur.get("cooldown") or {}
    fresh = [(k, m) for (k, m, cool) in cand
             if time.time() - float(cd.get(k, 0)) > cool]

    print("[%s] 新增 %d 行｜命中类别 %s｜候选告警 %d 项 %s｜实际推送 %d 项 %s"
          % (now_str(), len(new_lines), sorted(hits) or "无",
             len(cand), [k for k, _, _ in cand] or [],
             len(fresh), [k for k, _ in fresh] or []))

    rc = 0
    if fresh:
        rc = 1
        body = ["【跟单机器人】🩺 外部巡检告警（不依赖机器人进程）", ""]
        for k, m in fresh:
            body.append(m)
            cd[k] = time.time()
        body.append("")
        body.append("日志：%s" % LOGF)
        push_feishu("\n".join(body), dry=dry)

    cur["offset"] = new_off
    cur["last_run"] = now_str()
    cur["cooldown"] = cd
    if rotated:
        cur["rotated"] = now_str()
    save_cursor(cur)
    return rc


if __name__ == "__main__":
    sys.exit(main())
