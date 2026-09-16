#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
安全自检器（sandbox runner）—— 在 /tmp 里跑 dryrun_bot2.py 的自检，并【实测证明】没碰生产。

为什么需要它：
  · 自检分支自身会把 RUNTIME/TRADES/STATE/LOGF/IMGDIR/NOTIFY_CFG/RUN 重定向到 /tmp；
  · 但"代码说它隔离"不等于"真的隔离"。这个运行器在跑之前/之后对生产关键文件做指纹，
    有任何一处变了就判 FAIL 并返回非 0 —— 用测量说话，而不是用声明说话。
  · 顺带记录 Chromium 进程数：自检绝不允许拉起浏览器（API 模式不需要浏览器）。

用法（在服务器上，生产目录里）：
    venv/bin/python safe_selftest.py --selftest-imgmerge
    venv/bin/python safe_selftest.py --selftest-feishu
    venv/bin/python safe_selftest.py --selftest-approval
    venv/bin/python safe_selftest.py --selftest-parse --selftest-tp    # 多个也行
不带参数时默认跑一组核心自检。
"""
import os
import sys
import json
import hashlib
import subprocess

BASE = os.environ.get("SBX_BASE", "/home/ubuntu/signal-bot")
RUN = BASE + "/v21"
BOT = BASE + "/dryrun_bot2.py"
WORK = "/tmp/sbx_selftest"

DEFAULT_SUITE = ["--selftest-imgmerge", "--selftest-feishu"]

# 【硬比对】机器人自己不会改的文件：自检若动了它们 = 污染生产，直接 FAIL
HARD = [
    BOT,
    BASE + "/feishu_api.py",
    BASE + "/runtime_config.json",
    BASE + "/notify.json",
]

# 【软观察】机器人自己一直在写的文件（心跳/游标/已读记录）：只记录，不据此判 FAIL。
#   对它们的正确判据是"语义"：持仓数不许变、日志新增行不许来自自检。
SOFT = [
    RUN + "/state.json",
    RUN + "/run.log",
    RUN + "/trades_dryrun.jsonl",
]

WATCH = HARD + SOFT


def _md5(path):
    try:
        h = hashlib.md5()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
        return h.hexdigest()[:12]
    except Exception as e:
        return "ERR:%s" % type(e).__name__


def _size(path):
    try:
        return os.path.getsize(path)
    except Exception:
        return -1


def _lines(path):
    try:
        with open(path, "rb") as f:
            return sum(1 for _ in f)
    except Exception:
        return -1


def _open_positions():
    """state.json 里的持仓数（内容比对，而不是 mtime —— 机器人每轮都重写它）。"""
    try:
        d = json.load(open(RUN + "/state.json", encoding="utf-8")) or {}
        return len(d.get("open") or {})
    except Exception as e:
        return "ERR:%s" % type(e).__name__


def _seen_count():
    try:
        d = json.load(open(RUN + "/state.json", encoding="utf-8")) or {}
        return len(d.get("seen") or [])
    except Exception as e:
        return "ERR:%s" % type(e).__name__


def _imgs():
    try:
        names = sorted(os.listdir(RUN + "/imgs"))
        return len(names), hashlib.md5("\n".join(names).encode()).hexdigest()[:12]
    except Exception as e:
        return -1, "ERR:%s" % type(e).__name__


def _chrome():
    try:
        out = subprocess.run(["pgrep", "-fc", "chrome"], stdout=subprocess.PIPE,
                             stderr=subprocess.DEVNULL)
        return out.stdout.decode().strip() or "0"
    except Exception:
        return "?"


def _new_log_lines(before_lines):
    """返回 run.log 里新增的行（用于判断有没有自检产物漏进生产日志）。"""
    try:
        with open(RUN + "/run.log", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
        return lines[before_lines:] if before_lines >= 0 else []
    except Exception:
        return []


def fingerprint(label):
    n_img, h_img = _imgs()
    fp = {
        "hard_md5": {os.path.basename(p): _md5(p) for p in HARD},
        "soft": {os.path.basename(p): "%s/%s" % (_md5(p), _size(p)) for p in SOFT},
        "log_lines": _lines(RUN + "/run.log"),
        "open_positions": _open_positions(),
        "imgs": "%d/%s" % (n_img, h_img),
        "chrome": _chrome(),
    }
    print("---- 指纹[%s] ----" % label)
    print("  【硬】不该变的文件：%s" % json.dumps(fp["hard_md5"], ensure_ascii=False))
    print("  【软】机器人自己在写的文件（仅记录）：%s" % json.dumps(fp["soft"], ensure_ascii=False))
    print("  run.log 行数=%s ｜ 持仓=%s ｜ imgs=%s ｜ chrome 进程=%s"
          % (fp["log_lines"], fp["open_positions"], fp["imgs"], fp["chrome"]))
    return fp


def diff(before, after):
    """硬文件必须逐字节一致；软文件只看语义（机器人自身活动不算污染）。"""
    bad = []
    info = []
    for k, v in before["hard_md5"].items():
        if after["hard_md5"].get(k) != v:
            bad.append("【硬】%s 被改动：%s → %s" % (k, v, after["hard_md5"].get(k)))
    if after["open_positions"] != before["open_positions"]:
        bad.append("【语义】持仓数变了：%s → %s" % (before["open_positions"], after["open_positions"]))
    if after["imgs"] != before["imgs"]:
        bad.append("【语义】生产图片目录变了：%s → %s" % (before["imgs"], after["imgs"]))
    if after["chrome"] not in ("0", ""):
        bad.append("【硬】自检期间出现了 Chromium 进程（chrome=%s）—— 违反「API 模式不需要浏览器」"
                   % after["chrome"])

    leaked = [ln.strip() for ln in _new_log_lines(before["log_lines"])
              if ("自检" in ln) or ("selftest" in ln.lower())]
    if leaked:
        bad.append("【泄漏】自检输出漏进生产 run.log：%s" % leaked[:3])
    else:
        info.append("生产 run.log 新增 %d 行，全部来自机器人自身（无自检字样）"
                    % max(0, after["log_lines"] - before["log_lines"]))
    for k, v in before["soft"].items():
        if after["soft"].get(k) != v:
            info.append("%s 有变化（机器人自身活动，正常）：%s → %s"
                        % (k, v, after["soft"].get(k)))
    return bad, info


def main():
    flags = [a for a in sys.argv[1:] if a.startswith("--")]
    if not flags:
        flags = list(DEFAULT_SUITE)
    os.makedirs(WORK, exist_ok=True)
    print("=" * 78)
    print("安全自检器  参数=%s" % " ".join(flags))
    print("生产目录=%s ｜ 沙箱工作目录=%s（自检自己的 /tmp 重定向 + 本运行器指纹双重保险）" % (BASE, WORK))
    print("说明：每个自检分支都以 sys.exit 结束，所以逐个单独起进程跑；最后统一出隔离结论。")
    print("=" * 78)

    before = fingerprint("运行前")

    env = dict(os.environ)
    env.pop("DISPLAY", None)          # 不给浏览器留任何机会
    codes = {}
    for fl in flags:
        cmd = [sys.executable, BOT, fl]
        print("\n" + "-" * 78)
        print("---- 执行：%s（cwd=%s）----" % (" ".join(cmd), WORK))
        sys.stdout.flush()
        p = subprocess.run(cmd, cwd=WORK, env=env)
        codes[fl] = p.returncode
        print("---- %s 退出码：%s ----" % (fl, p.returncode))

    after = fingerprint("运行后")
    bad, info = diff(before, after)

    print("\n" + "=" * 78)
    for i in info:
        print("   · %s" % i)
    if bad:
        print("隔离检查：FAIL ❌ 生产被改动或自检越界：")
        for b in bad:
            print("   ✗ %s" % b)
        print("=" * 78)
        return 2
    print("隔离检查：PASS ✅ 不该变的文件逐字节一致；持仓数与图片目录未变；全程无浏览器；"
          "生产日志新增行里没有自检字样")
    failed = {k: v for k, v in codes.items() if v != 0}
    if failed:
        print("但有自检未通过：%s" % json.dumps(failed, ensure_ascii=False))
        print("=" * 78)
        return 1
    print("自检结果：%d 项全部通过 ✅  %s" % (len(codes), " ".join(codes)))
    print("=" * 78)
    return 0


if __name__ == "__main__":
    sys.exit(main())
