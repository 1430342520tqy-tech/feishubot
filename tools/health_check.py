# -*- coding: utf-8 -*-
"""机器人自检：一条命令看清是否健康
用法：/home/ubuntu/signal-bot/venv/bin/python /home/ubuntu/signal-bot/health_check.py
"""
import json, os, re, subprocess, time

BASE = "/home/ubuntu/signal-bot"
V21 = os.path.join(BASE, "v21")
OK, BAD, WARN = "[OK]  ", "[!!]  ", "[?]   "
problems = []


def line(tag, msg):
    print(tag + msg)


def main():
    print("=" * 62)
    print("信号机器人自检  " + time.strftime("%Y-%m-%d %H:%M:%S"))
    print("=" * 62)

    # 1. pm2 进程
    print("\n--- 1. 进程 ---")
    try:
        out = subprocess.run(["pm2", "jlist"], capture_output=True, text=True,
                             timeout=30).stdout
        procs = {p["name"]: p for p in json.loads(out)}
        for name in ("dryrun-bot2", "liq-watch"):
            p = procs.get(name)
            if not p:
                line(BAD, f"{name}: 不存在"); problems.append(f"{name} 缺失")
                continue
            st = p["pm2_env"]["status"]
            up = (time.time() * 1000 - p["pm2_env"].get("pm_uptime", 0)) / 1000
            rl = p["pm2_env"].get("restart_time", 0)
            tag = OK if st == "online" else BAD
            if st != "online":
                problems.append(f"{name} 状态 {st}")
            line(tag, f"{name}: {st} | 已运行 {up/3600:.1f}h | 重启次数 {rl} "
                      f"| 内存 {p['monit']['memory']/1048576:.0f}MB")
    except Exception as e:
        line(BAD, "pm2 读取失败: " + str(e)[:100]); problems.append("pm2 读取失败")

    # 2. state.json
    print("\n--- 2. 持仓状态 state.json ---")
    try:
        st = json.load(open(os.path.join(V21, "state.json"), encoding="utf-8"))
        op = st.get("open", {})
        line(OK, f"JSON 合法，持仓 {len(op)} 笔")
        for c, t in op.items():
            line("      ", f"  {c} {t['dir']} 入场 {t['entry']} 止损 {t['sl']} "
                           f"止盈 {t.get('tps') or '未读'} 剩余 {t.get('remaining',1)*100:.0f}% "
                           f"来源 {t.get('group')}")
        # ⚠️ 2026-09-15 修：原来这里硬编码 5，而 max_open 是可用指令改的（当时已改成 7）
        #    → 会**永远误报**"持仓超过 5 笔上限"，把核验工具变成噪音源。
        #    现在读 runtime_config.json 的 max_open；读不到才退回代码默认 5。
        _mo = 5
        try:
            _mo = int(json.load(open(os.path.join(BASE, "runtime_config.json"),
                                     encoding="utf-8")).get("max_open") or 5)
        except Exception:
            pass
        if len(op) > _mo:
            problems.append(f"持仓 {len(op)} 笔 > 上限 {_mo}")
            line(BAD, f"持仓超过 {_mo} 笔上限（max_open={_mo}，可用「修改持仓上限 N」改）！")
        else:
            line(OK, f"持仓 {len(op)} 笔 ≤ 上限 {_mo} 笔")
        lm = st.get("last", {})
        line(OK, "进度游标: " + ", ".join(f"{k}" for k in lm))
        seen = st.get("seen") or []
        line(OK, f"已处理消息去重表: {len(seen)} 条（防重复开单）")
        if "黄金mansoor" not in lm:
            line(WARN, "黄金mansoor 还没有进度游标（说明该群还没有读到新消息）")
    except Exception as e:
        line(BAD, "state.json 损坏: " + str(e)[:150]); problems.append("state.json 损坏")

    # 3. 运行配置
    print("\n--- 3. 运行配置 ---")
    try:
        cfg = json.load(open(os.path.join(BASE, "runtime_config.json"), encoding="utf-8"))
        line(OK, f"监控群({len(cfg['groups'])}): " + "、".join(cfg["groups"]))
        line(OK, f"保证金 {cfg['margin']}U  杠杆 {cfg['leverage']}倍  "
                 f"名义 {cfg['margin']*cfg['leverage']}U  测试模式 {cfg['test_mode']}")
        line("      ", "  指令生效群: 开单记录 / 机器人开单通知")
        # 关键：配置里的群 与 日志里实际载入的群 是否一致
        log = open(os.path.join(V21, "run.log"), encoding="utf-8", errors="ignore").read()
        m = re.findall(r"已载入运行配置：监控群=([^ ]+)", log)
        if m:
            loaded = m[-1].split("、")
            if loaded != cfg["groups"]:
                line(BAD, f"配置与实际载入不一致！实际={loaded}")
                problems.append("runtime_config 与进程内不一致，需 pm2 restart dryrun-bot2")
            else:
                line(OK, "配置已生效（进程内 = 配置文件）")
    except Exception as e:
        line(BAD, "运行配置读取失败: " + str(e)[:120]); problems.append("运行配置读取失败")

    # 4. 日志健康度
    print("\n--- 4. 日志 ---")
    try:
        lines = open(os.path.join(V21, "run.log"), encoding="utf-8",
                     errors="ignore").read().splitlines()
        tail = lines[-400:]
        hb = [l for l in tail if "心跳" in l]
        err = [l for l in tail if ("错误" in l or "失败" in l or "Traceback" in l)]
        line(OK, f"最近 400 行中心跳 {len(hb)} 条")
        # 启动期（开 5 个群页面约需 4~5 分钟）不产生心跳，此时不算卡死
        try:
            up_s = (time.time() * 1000 - procs["dryrun-bot2"]["pm2_env"]["pm_uptime"]) / 1000
        except Exception:
            up_s = 1e9
        STARTUP_GRACE = 600          # 秒；开页阶段放宽
        if hb:
            line(OK, "最新心跳: " + hb[-1][:80])
            mm = re.search(r"(\d{2}):(\d{2}):(\d{2}) 心跳", hb[-1])
            if mm:
                now = time.localtime()
                sec_now = now.tm_hour * 3600 + now.tm_min * 60 + now.tm_sec
                sec_hb = int(mm.group(1)) * 3600 + int(mm.group(2)) * 60 + int(mm.group(3))
                gap = (sec_now - sec_hb) % 86400
                if up_s < STARTUP_GRACE:
                    line(OK, f"启动中（已运行 {up_s:.0f}s），开页阶段无心跳属正常，"
                             f"距上次心跳 {gap}s —— 跳过卡死判定")
                elif gap > 180:
                    line(BAD, f"心跳已停 {gap} 秒（>180秒），疑似卡死")
                    problems.append("心跳停滞")
                else:
                    line(OK, f"心跳新鲜（{gap} 秒前）")
        elif up_s >= STARTUP_GRACE:
            line(BAD, "日志中找不到心跳记录")
            problems.append("无心跳记录")
        else:
            line(OK, "启动中，等待首个心跳")
        if err:
            line(WARN, f"最近 400 行有 {len(err)} 条含'失败/错误'：")
            for l in err[-5:]:
                print("        " + l[:110])
        else:
            line(OK, "最近 400 行无报错")
    except Exception as e:
        line(BAD, "日志读取失败: " + str(e)[:100])

    # 5. 各群页面 & 最近信号
    print("\n--- 5. 各群最近信号 ---")
    try:
        log = open(os.path.join(V21, "run.log"), encoding="utf-8", errors="ignore").read()
        groups = json.load(open(os.path.join(BASE, "runtime_config.json"),
                                encoding="utf-8"))["groups"]
        for g in groups:
            cnt = len(re.findall(re.escape("[" + g + "]"), log))
            tag = OK if cnt else WARN
            line(tag, f"{g}: 日志中出现 {cnt} 次")
    except Exception as e:
        line(BAD, str(e)[:100])

    # 6. 纸面成交记录
    print("\n--- 6. 纸面交易记录 ---")
    p = os.path.join(V21, "trades_dryrun.jsonl")
    try:
        rows = [json.loads(l) for l in open(p, encoding="utf-8") if l.strip()]
        line(OK, f"共 {len(rows)} 条记录")
        for r in rows[-5:]:
            line("      ", "  " + json.dumps(r, ensure_ascii=False)[:130])
    except Exception as e:
        line(WARN, "记录读取失败: " + str(e)[:100])

    # 7. 磁盘/内存
    print("\n--- 7. 系统资源 ---")
    try:
        mem = open("/proc/meminfo").read()
        avail = int(re.search(r"MemAvailable:\s+(\d+)", mem).group(1)) / 1024
        tot = int(re.search(r"MemTotal:\s+(\d+)", mem).group(1)) / 1024
        sw = re.search(r"SwapTotal:\s+(\d+)", mem)
        swf = re.search(r"SwapFree:\s+(\d+)", mem)
        line(OK if avail > 250 else WARN,
             f"内存 {avail:.0f}MB 可用 / {tot:.0f}MB 总"
             + (f" | Swap {(int(sw.group(1))-int(swf.group(1)))/1024:.0f}MB 已用"
                f" / {int(sw.group(1))/1024:.0f}MB" if sw else " | 无Swap"))
        if avail < 250:
            problems.append("可用内存偏低")
        du = subprocess.run(["du", "-sh", V21], capture_output=True, text=True).stdout
        line(OK, "v21 目录占用 " + du.split()[0])
    except Exception as e:
        line(WARN, str(e)[:80])

    # 汇总
    print("\n" + "=" * 62)
    if problems:
        print("发现 %d 个问题：" % len(problems))
        for i, x in enumerate(problems, 1):
            print(f"  {i}. {x}")
    else:
        print("全部正常，无问题。")
    print("=" * 62)


if __name__ == "__main__":
    main()
