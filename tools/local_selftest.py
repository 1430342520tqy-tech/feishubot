#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""本机自检台 —— **不用服务器、不用网络** 就能跑 dryrun_bot2 的自检分支。

为什么需要它：
  · `safe_selftest.py` 要求在**生产目录**（`/home/ubuntu/signal-bot`）里跑；
  · 而改一行代码想验证一下，不该动生产机 —— 更不该等一次部署；
  · 所以这里用"桩模块"替掉本机没装的第三方库（requests / PIL / playwright / ccxt），
    再把所有路径重定向到 `/tmp` 沙箱，让主程序能在**任意机器**上 import 起来并跑自检。

用法（在仓库根目录）：
    python3 tools/local_selftest.py                 # 跑全部能跑的分支
    python3 tools/local_selftest.py parse tp b5     # 只跑指定分支
    python3 tools/local_selftest.py --verbose       # 连自检的完整输出一起打

⚠️ 它能证明什么 / 不能证明什么（别误读）：
  能：改完代码后「账本 / 执行状态 / 直读 / 解析 / 止盈 / B5 / B1 / 审批」这几条逻辑没行为变化。
  不能：**不代表生产能跑**，也**不验证隔离**。第三方库是桩、图片是桩、网络被拦；
       `--selftest-feishu`（要真调飞书）与 `--selftest-imgmerge`（要真实图片）这两个分支
       在本机**跑不了**，必须上服务器跑。

⚠️⚠️ **为什么“不验证隔离”这句很重要**（这是踩过的坑）：
  本工具把 `SIGNAL_BOT_BASE` 指向 `/tmp` 沙箱 —— 于是**自检分支自己没做路径重定向的 bug
  会被完全掩盖**（日志写到沙箱而不是生产，看起来一切干净）。
  实际就出过：`--selftest-ledger` / `--selftest-exec` 第一版**没重定向生产路径**，
  在服务器上会写生产 `run.log`、**真发飞书消息**、写生产 `real_orders.jsonl`。
  → 所以：**本机通过 ≠ 隔离合格**；隔离这件事只能靠服务器上的 `safe_selftest.py`（指纹比对）来证。
"""
import json
import os
import shutil
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "src")
H = "/tmp/local_selftest"
STUBS = H + "/stubs"
SANDBOX = H + "/sandbox"

# 能跑的分支（feishu 要真网络、imgmerge 要真实图片 → 排除）
BRANCHES = ["ledger", "exec", "chartllm", "parse", "tp", "b5", "b1", "approval"]
KEYS = ("自检：", "通过 ✅", "全部通过")

# 币种表：生产机上由单独的抓取脚本生成，本机给一份够用的
SYMS = ("BTC ETH SOL BNB XRP DOGE ADA AVAX LINK DOT MATIC LTC UNI ATOM NEAR APT ARB OP SUI TIA SEI INJ "
        "LSK VELVET ON AT THE IN SO AAVE ETC FIL ICP STX RUNE GALA SAND MANA AXS GMT ENS CRV MKR SNX "
        "ZEC DASH EOS TRX XLM ALGO VET THETA FTM GRT IMX WLD JUP STRK BLUR ORDI WIF BONK FLOKI "
        "TAO RENDER FET PENDLE LDO ETHFI ENA ZK ZRO BLAST XAU XAUT PAXG TSLA MSTR PLTR COIN NVDA").split()

STUB_FILES = {
    "requests.py": '''def post(*a, **k): raise RuntimeError("自检台：网络被拦截（requests.post）")
def get(*a, **k): raise RuntimeError("自检台：网络被拦截（requests.get）")
''',
    "PIL/__init__.py": "",
    "PIL/Image.py": '''LANCZOS = 1
class _Im:
    size = (100, 100)
    def convert(self, *a): return self
    def load(self): return lambda x, y: (0, 0, 0)
    def crop(self, *a): return self
    def resize(self, *a, **k): return self
    def copy(self): return self
    def save(self, *a, **k): pass
def open(*a, **k): raise RuntimeError("自检台：不读真实图片")
def new(*a, **k): return _Im()
''',
    "playwright/__init__.py": "",
    "playwright/sync_api.py": 'def sync_playwright(*a, **k): raise RuntimeError("自检台：不启动浏览器")\n',
    "ccxt.py": 'def binance(*a, **k): raise RuntimeError("自检台：不连交易所")\nclass Exchange: pass\n',
}


def build(force=False):
    """造桩模块 + /tmp 沙箱（每次重建，保证干净）。"""
    if force and os.path.isdir(H):
        shutil.rmtree(H, ignore_errors=True)
    for rel, body in STUB_FILES.items():
        p = os.path.join(STUBS, rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8") as f:
            f.write(body)
    os.makedirs(SANDBOX + "/v21/imgs", exist_ok=True)
    json.dump(sorted({s + "USDT" for s in SYMS if s and not s.isdigit()}),
              open(SANDBOX + "/fapi_symbols.json", "w"), ensure_ascii=False)
    for rel, body in (("v21/run.log", "2026-01-01 00:00:00 心跳：运行中 | 持仓 0 笔（-）\n"),
                      ("v21/state.json", '{"open":{},"last":0,"seen":[]}'),
                      ("runtime_config.json", "{}"),
                      # 🆕 配置现在只走 .env —— 沙箱里也给它一份，好让自检真的走 env 这条路。
                      #    ⚠️ 故意**不设 FEISHU_WEBHOOK**：自检一律不发飞书（双重保险）。
                      (".env", "DEEPSEEK_API_KEY=selftest_ds\nBINANCE_API_KEY=selftest_bk\n"
                               "BINANCE_API_SECRET=selftest_bs\nFEISHU_APP_ID=selftest_ai\n"
                               "FEISHU_APP_SECRET=selftest_as\n"),
                      # 自检的隔离复核会读这两个文件的指纹，所以要存在
                      ("dryrun_bot2.py", "# 沙箱占位（真身由 src/ 提供）\n"),
                      ("feishu_api.py", "# 沙箱占位\n")):
        full = os.path.join(SANDBOX, rel)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "w", encoding="utf-8") as f:
            f.write(body)


def run_one(src_dir, branch, timeout=90, verbose=False):
    env = dict(os.environ)
    env["SIGNAL_BOT_BASE"] = SANDBOX
    env["PYTHONPATH"] = src_dir + ":" + STUBS
    env["SIGNALBOT_SILENT"] = "1"      # 保证一条飞书都不发（配置改走 .env 后的统一开关）
    try:
        r = subprocess.run([sys.executable, os.path.join(src_dir, "dryrun_bot2.py"),
                            "--selftest-" + branch],
                           capture_output=True, text=True, timeout=timeout, env=env, cwd="/tmp")
    except subprocess.TimeoutExpired:
        return ("超时", "(超时)")
    out = (r.stdout or "") + (r.stderr or "")
    if verbose:
        print(out)
    tail = [l.strip() for l in out.splitlines() if any(k in l for k in KEYS)]
    return (r.returncode, tail[-1] if tail else "(没有汇总行，可能崩了)")


def label(rc):
    return {0: "✅ 通过"}.get(rc, "❌ 失败(rc=%s)" % rc)


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    branches = args or BRANCHES
    build(force=True)
    print("自检台就绪：桩模块 %s ｜ 沙箱 %s\n" % (STUBS, SANDBOX))

    if "--verbose" in sys.argv:
        for b in branches:
            print("=" * 20, b)
            print(run_one(SRC, b, verbose=True)[1])

    print("%-10s %-12s %s" % ("分支", "结果", "汇总行"))
    print("-" * 78)
    bad = 0
    for b in branches:
        rc, line = run_one(SRC, b)
        bad += 0 if rc == 0 else 1
        print("%-10s %-12s %s" % (b, label(rc), line))
    print("-" * 78)
    print("共 %d 个分支，%d 个失败" % (len(branches), bad))
    print("\n⚠️ 本机跑不了：--selftest-feishu（要真调飞书）、--selftest-imgmerge（要真实图片）")
    print("⚠️ 这里通过 ≠ 生产能跑：第三方库是桩、网络被拦。上线前仍须在服务器跑 safe_selftest.py")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
