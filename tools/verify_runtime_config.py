#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""验收：`runtime_config.json` 保存时**不许丢键**（用 local_selftest 的桩/沙箱跑真代码）。

## 为什么要有这个脚本

实测抓到的 bug：
`save_runtime()` 原写法是「白名单 out + 逐个手动补键」，会把**本函数不认识的键**
整文件重写抹掉。实测后果：`fetch_mode`（取消息方式的回滚开关）被「修改金额」这类
无关指令悄悄删掉 —— 内存里还生效，**一重启就回退到默认 api，主动回滚的选择丢失**。

当时的修法是改成 `out = dict(_old)` 打底、只覆盖程序管理的键。
这个脚本把当时的一次性复核**固化成可重复的判据** —— 以后再改 `save_runtime()`
（哪怕只是加一个开关），跑一次就知道有没有把老毛病带回来。

## 用法

    python3 tools/verify_runtime_config.py

退出码 0 = 全部通过；非 0 = 有条失败（会打印是哪条）。
"""
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)          # local_selftest 在同目录
sys.path.insert(0, os.path.join(ROOT, "src"))

import local_selftest as ls       # noqa: E402

# 文件里放：全部 17 个配置键 + 1 个代码完全不认识的键
SEED = {
    "groups": ["G"], "margin": 300, "leverage": 3, "max_open": 5, "max_consec_loss": 1,
    "daily_loss_limit": 100, "max_total_margin": 2000, "require_approval": True,
    "ai_first_parse": True, "loss_limit_pct": 20, "test_mode": False,
    "live_trading": False, "strict_limit_groups": ["G"], "silence_alert_hours": 6,
    "fetch_mode": "browser", "chart_reader": "llm", "chart_llm_reads": 2,
    "my_custom_key": "别动我",          # ← 代码不认识的键，也不许丢
}

# 程序不管、保存后值必须原样不动的键
MUST_KEEP = [("fetch_mode", "browser"), ("chart_reader", "llm"), ("chart_llm_reads", 2),
             ("live_trading", False), ("silence_alert_hours", 6),
             ("my_custom_key", "别动我")]

_results = []


def chk(name, cond, detail=""):
    _results.append(bool(cond))
    print("  %s %s%s" % ("✅" if cond else "❌", name, ("  ← " + str(detail)) if not cond else ""))


def main():
    ls.build(force=True)                       # 造桩 + /tmp 沙箱
    os.environ["SIGNAL_BOT_BASE"] = ls.SANDBOX
    sys.path.insert(0, ls.STUBS)
    import dryrun_bot2 as bot

    json.dump(SEED, open(bot.RUNTIME, "w", encoding="utf-8"), ensure_ascii=False)
    bot.load_runtime()
    bot.MARGIN = 500.0                          # 模拟「修改金额 500」
    bot.save_runtime()
    got = json.load(open(bot.RUNTIME, encoding="utf-8"))

    print("① 无键丢失（%d 进 → %d 出）" % (len(SEED), len(got)))
    lost = sorted(set(SEED) - set(got))
    chk("键数 = %d" % len(SEED), len(got) == len(SEED), "实际 %d，丢了 %s" % (len(got), lost))

    print("② 程序管理的键被更新为当前值")
    chk("margin 300 → 500", got.get("margin") == 500.0, "实际 %s" % got.get("margin"))

    print("③ 程序不管的键，值原样不动")
    for k, v in MUST_KEEP:
        chk("%s = %r" % (k, v), got.get(k) == v, "实际 %r" % got.get(k))

    print("④ 重复保存幂等")
    bot.save_runtime()
    got2 = json.load(open(bot.RUNTIME, encoding="utf-8"))
    chk("第二次保存后键数不变", len(got2) == len(SEED), "实际 %d" % len(got2))
    chk("fetch_mode 仍在", got2.get("fetch_mode") == "browser", "实际 %r" % got2.get("fetch_mode"))

    bad = _results.count(False)
    print("\n%s" % ("全部通过 ✅（%d 条）" % len(_results) if not bad
                    else "有 %d 条失败 ❌" % bad))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
