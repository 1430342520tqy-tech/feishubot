#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""零依赖的 .env 加载器 —— 把 `KEY=value` 文件读进进程环境变量。

为什么不用 `python-dotenv`：
  本项目**没有 requirements.txt**，装依赖全靠手工；历史上已经因为"以为配好了、其实没配"
  栽过一次（提交 33c649b：脱敏后服务器无环境变量 → 所有 AI 调用失败）。
  再加一个"必须 pip install 才有"的依赖，就是同一个坑的翻版 → 所以这里只用标准库。

规则（与 `python-dotenv` 一致）：
  · **真实环境变量优先**：进程里已存在的同名变量**绝不覆盖**。
    这样 systemd 的 `Environment=`、pm2 的 `ecosystem.config.js` 永远赢过 `.env`。
  · 支持 `KEY=value`、`export KEY=value`、`# 注释`、空行、值两侧的引号。
  · **找不到 .env 就静默跳过** —— 不报错、不影响启动。
    （当前生产上没有 .env，所以本模块在线上是**纯空操作**，行为与改动前完全一致。）

用法（必须在读 BASE / 读密钥**之前** import 并调用）：
    import envload
    envload.load()
"""
import os

_LOADED = []


def _candidates():
    """.env 的查找顺序：**只认两处**，不做“到处翻”。
       ① `$SIGNAL_BOT_BASE/.env`（生产；也是本文件的默认值）
       ② `<仓库根>/.env`（仓库 `src/` 布局：`src/../.env`）

    ⚠️ 已删两个旧候选（未上线，无历史包袱）：
       · `here/.env` —— 仓库布局下它等于 `src/.env`，那不是配置目录；生产下与①重复。
       · `cwd/.env` —— 依赖“从哪里启动”，不可预测 → 反而可能误读别的目录的配置。
    """
    here = os.path.dirname(os.path.abspath(__file__))
    out = []
    b = os.environ.get("SIGNAL_BOT_BASE")
    if b:
        out.append(os.path.join(b, ".env"))
    out.append(os.path.join(os.path.dirname(here), ".env"))   # 仓库布局：src/../.env
    return out


def _parse_line(line):
    s = line.strip()
    if not s or s.startswith("#"):
        return None
    if s[:7].lower() == "export ":
        s = s[7:].lstrip()
    if "=" not in s:
        return None
    k, v = s.split("=", 1)
    k, v = k.strip(), v.strip()
    if len(v) >= 2 and v[0] == v[-1] and v[0] in ("'", '"'):
        v = v[1:-1]
    else:
        # 行尾注释：`#` 前面必须是空白才算注释 —— 免得把 URL 里的 # 当注释截断
        for i, ch in enumerate(v):
            if ch == "#" and i > 0 and v[i - 1] in " \t":
                v = v[:i].rstrip()
                break
    return (k, v) if k else None


def load(paths=None, override=False):
    """加载 .env，返回真正被读到的文件路径列表（可能为空列表）。

    override=False（默认）：不覆盖进程里已存在的变量 —— systemd / pm2 / 手动 export 永远优先。
    """
    got = []
    for p in (paths if paths is not None else _candidates()):
        try:
            if not p or not os.path.isfile(p):
                continue
            with open(p, encoding="utf-8") as f:
                lines = f.read().splitlines()
        except Exception:
            continue
        n = 0
        for ln in lines:
            kv = _parse_line(ln)
            if not kv:
                continue
            k, v = kv
            if not override and k in os.environ:
                continue
            os.environ[k] = v
            n += 1
        got.append(p)
        _LOADED.append((p, n))
    return got
