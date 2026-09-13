# -*- coding: utf-8 -*-
"""对 dryrun_bot2.py 打补丁：
  1) 已处理消息 id 去重（持久化到 state.json.seen）——防重启/游标回退后重复处理旧信号
  2) 已有同币种持仓时不重复开单——防真钱阶段重复下单 & 仓位跟踪丢失
幂等：已打过补丁再次运行会提示并跳过。
"""
import io, os, re, shutil, sys, time

TARGET = "/home/ubuntu/signal-bot/dryrun_bot2.py"

PATCHES = []


def add(name, old, new, expect=1):
    PATCHES.append((name, old, new, expect))


# ---------- 1. 全局：SEEN 集合 ----------
add(
    "全局 SEEN 集合",
    "STATE_DIRTY = [False]",
    '''STATE_DIRTY = [False]

# ===== 消息级去重：防止重启/游标回退后把旧信号当新信号重复开单 =====
SEEN = set()
SEEN_MAX = 1500

def mark_seen(mid):
    try:
        SEEN.add(int(mid))
    except Exception:
        return
    if len(SEEN) > SEEN_MAX:                      # 只保留最近的，避免无限膨胀
        for x in sorted(SEEN)[:len(SEEN) - SEEN_MAX]:
            SEEN.discard(x)
''',
)

# ---------- 2. 启动时载入 SEEN ----------
add(
    "载入 SEEN",
    """            for k, v in (sv.get("last") or {}).items():
                last_id[k] = int(v)""",
    """            for k, v in (sv.get("last") or {}).items():
                last_id[k] = int(v)
            for _x in (sv.get("seen") or []):
                try:
                    SEEN.add(int(_x))
                except Exception:
                    pass
            if SEEN:
                log("已载入已处理消息 %d 条（防重复开单）" % len(SEEN))""",
)

# ---------- 3. 保存 SEEN（两处 state 写入，缩进不同，分别处理）----------
add(
    "保存 SEEN（启动时，8空格缩进）",
    """        json.dump({"open": open_pos, "last": last_id, "ts": datetime.datetime.now(CST).strftime("%Y-%m-%d %H:%M:%S")},
                  open(STATE, "w"), ensure_ascii=False, indent=1)""",
    """        json.dump({"open": open_pos, "last": last_id, "seen": sorted(SEEN)[-800:],
                   "ts": datetime.datetime.now(CST).strftime("%Y-%m-%d %H:%M:%S")},
                  open(STATE, "w"), ensure_ascii=False, indent=1)""",
    expect=1,
)

add(
    "保存 SEEN（主循环，12空格缩进）",
    """            json.dump({"open": open_pos, "last": last_id, "ts": datetime.datetime.now(CST).strftime("%Y-%m-%d %H:%M:%S")},
                      open(STATE, "w"), ensure_ascii=False, indent=1)""",
    """            json.dump({"open": open_pos, "last": last_id, "seen": sorted(SEEN)[-800:],
                       "ts": datetime.datetime.now(CST).strftime("%Y-%m-%d %H:%M:%S")},
                      open(STATE, "w"), ensure_ascii=False, indent=1)""",
    expect=1,
)

# ---------- 4. 主循环：消息级去重 ----------
add(
    "主循环去重",
    """                        log("[%s] 发现新消息 | 发出=%s | %s" % (g, when, txt[:110]))
                        low = txt.lower()""",
    """                        log("[%s] 发现新消息 | 发出=%s | %s" % (g, when, txt[:110]))
                        if int(mid) in SEEN:
                            log("   ↳ 该消息此前已处理过，跳过（防重复开单）")
                            continue
                        mark_seen(mid)
                        STATE_DIRTY[0] = True
                        low = txt.lower()""",
)

# ---------- 5. 出单前：同币种已有持仓则跳过 ----------
add(
    "同币种持仓防护",
    """        dirc = dirc0
        tm = {"detect": p["t_found"] - p["first_ts"],""",
    """        if coin in open_pos:
            _ex = open_pos[coin]
            notify("【信号·跳过】%s 已有持仓，不重复开单\\n现有：%s 入场 %.8g · 止损 %.8g · 剩余 %.0f%%\\n本次信号原文：%s"
                   % (coin, _ex.get("dir"), _ex.get("entry") or 0, _ex.get("sl") or 0,
                      (_ex.get("remaining", 1.0) * 100),
                      (p["texts"][0][:120] if p["texts"] else "")))
            log("   ↳ %s 已有持仓，跳过本次信号（防重复开单）" % coin)
            PENDING.pop(coin, None)
            continue
        dirc = dirc0
        tm = {"detect": p["t_found"] - p["first_ts"],""",
)


def main():
    src = io.open(TARGET, encoding="utf-8").read()

    # 幂等检查
    done = [n for n, o, _, _ in PATCHES if o not in src]
    if "SEEN = set()" in src:
        print("检测到已打过补丁（SEEN 已存在）。是否重复执行？")
        for n, o, _, _ in PATCHES:
            if o not in src:
                print("  已应用: " + n)
        return 1

    shutil.copy(TARGET, TARGET + ".bak_" + time.strftime("%Y%m%d_%H%M%S"))
    print("已备份原文件")

    ok = True
    for name, old, new, expect in PATCHES:
        cnt = src.count(old)
        if cnt != expect:
            print("  [!!] %s: 期望匹配 %d 处，实际 %d 处 —— 跳过" % (name, expect, cnt))
            ok = False
            continue
        src = src.replace(old, new)
        print("  [OK] %s（替换 %d 处）" % (name, cnt))

    if not ok:
        print("\n有补丁未能应用，未写入。请检查锚点。")
        return 2

    io.open(TARGET, "w", encoding="utf-8").write(src)
    print("\n已写入 " + TARGET)
    return 0


if __name__ == "__main__":
    sys.exit(main())
