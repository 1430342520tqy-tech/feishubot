#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""验收：`.env.example` 里**每个变量名都必须有代码真的在读**，且**代码要的变量一个都没漏**。

## 为什么要有这个脚本

模板与代码之间**没有任何强制对齐** —— 历史上就因此真出过事：
模板里写着 `"app": {app_id, app_secret}`，而代码读的是 `"feishu_app"`，
照模板填会让飞书令牌续期**静默失败**；另一次更严重：模板 9 个顶层键里只有 3 个真被代码读，
`bitable`（被读）模板里反而没有。

这个脚本把「模板 ↔ 代码」的对齐固化成判据：往 `.env.example` 加一行、
或往 `config.py` 加一个变量名，跑一次就知道两边是否还一致。

## 用法

    python3 tools/verify_config_template.py

退出码 0 = 全部通过。
"""
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "src"))
TPL = os.path.join(ROOT, ".env.example")

# 这几个变量**不在 config.py 的 E_* 常量里**，由别处直接读：
#   DISPLAY          —— dryrun_bot2 的 os.environ.setdefault（浏览器虚拟屏）
#   FEISHU_TOKEN_PATH—— feishu_api 直接读（令牌文件位置）
#   SIGNALBOT_SILENT —— notifier.resolve_webhook 读（自检/工具静默开关）
EXTRA_ALLOWED = {"DISPLAY", "FEISHU_TOKEN_PATH", "SIGNALBOT_SILENT"}

# 必备项：这几条必须出现在模板里（缺了会导致启动失败或静默运行）
MUST_BE_DOCUMENTED = {"FEISHU_WEBHOOK", "DEEPSEEK_API_KEY",
                      "BINANCE_API_KEY", "BINANCE_API_SECRET",
                      "FEISHU_APP_ID", "FEISHU_APP_SECRET"}

_results = []


def chk(name, cond, detail=""):
    _results.append(bool(cond))
    print("  %s %s%s" % ("✅" if cond else "❌", name,
                         ("  ← " + str(detail)) if not cond else ""))


def env_names(path):
    """从 `.env.example` 提取变量名。

    同时收**未注释**（`NAME=`）与**被注释掉的**（`# NAME=`）两种 ——
    后者是「可选/备选」记录，也算模板的一部分。
    """
    out = set()
    pat = re.compile(r"^\s*#?\s*([A-Z][A-Z0-9_]{2,})\s*=")
    for ln in open(path, encoding="utf-8"):
        m = pat.match(ln)
        if m:
            out.add(m.group(1))
    return out


def main():
    if not os.path.exists(TPL):
        print("❌ 找不到模板：%s" % TPL)
        return 1
    import config
    code_vars = {getattr(config, n) for n in dir(config)
                 if n.startswith("E_") and isinstance(getattr(config, n), str)}
    tpl_vars = env_names(TPL)
    print("模板变量 %d 个 ｜ 代码常量 %d 个\n" % (len(tpl_vars), len(code_vars)))

    allowed = code_vars | EXTRA_ALLOWED
    print("① 模板里每个变量都必须有代码在读")
    extra = sorted(tpl_vars - allowed)
    chk("无死变量（写了没人读）", not extra, "死变量：%s" % extra)

    print("② 代码要的变量一个都没漏（否则照模板配就会启动失败）")
    missing = sorted(code_vars - tpl_vars)
    chk("无漏项", not missing, "模板里缺：%s" % missing)

    print("③ 必备项必须在模板里写明")
    chk("必备项齐全", not (MUST_BE_DOCUMENTED - tpl_vars),
        "缺：%s" % sorted(MUST_BE_DOCUMENTED - tpl_vars))

    print("④ 说清了「哪些东西不住在这里」")
    txt = open(TPL, encoding="utf-8").read()
    for kw in ("runtime_config", "机器人自己写的状态"):
        chk("注释提到 %s" % kw, kw in txt)

    print("⑤ 说清了「缺了会失败」（33c649b 的教训）")
    chk("提到缺项会启动失败", "启动失败" in txt or "启动时会" in txt)

    bad = _results.count(False)
    print("\n%s" % ("全部通过 ✅（%d 条）" % len(_results) if not bad
                    else "有 %d 条失败 ❌" % bad))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
