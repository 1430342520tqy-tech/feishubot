#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""配置的唯一来源（密钥 / 路径）+ **启动前的必备项检查**。

## 规则

> **手写的配置一律走 `.env`（或真实环境变量）；机器人自己写的状态留在 json。**

| 类别 | 住哪 | 例子 |
|---|---|---|
| 手写的密钥/凭据/推送地址 | `.env` / 真实环境变量 | `BINANCE_API_KEY`、`FEISHU_WEBHOOK`、`DEEPSEEK_API_KEY` |
| 机器人自己写的状态（**不要手改**） | `runtime_config.json` | `live_trading`、`margin`、`groups`、`fetch_mode`、`chart_reader` |
| 机器人自己写的状态（**不要手改**） | `state.json` / `*.jsonl` | 持仓、游标、成交、审计 |
| 路径 | 环境变量 `SIGNAL_BOT_BASE`，默认生产路径 | — |

**只有这一个来源。** 变量名只在本文件里定义一次，模板是 `.env.example`。

## 必备项检查（`missing_required`）

缺了就在启动时**大声失败**（log + notify + 非零退出），不静默降级：
密钥缺失时默默退回空字符串、程序照跑不报错，是历史上真实出过的故障（全 AI 调用失败）。
"""
import os

# .env 由本模块统一加载 —— 任何入口（机器人、tools、对账脚本）只要 import config
# 就自动拿到 .env，不必各自写一遍。
try:
    import envload
    envload.load()
except Exception:
    pass

DEFAULT_BASE = "/home/ubuntu/signal-bot"

# ===== 所有环境变量名**只在这里定义一次** =====
E_BASE = "SIGNAL_BOT_BASE"
E_DEEPSEEK = "DEEPSEEK_API_KEY"
E_BINANCE_KEY = "BINANCE_API_KEY"
E_BINANCE_SEC = "BINANCE_API_SECRET"
E_FEISHU_WEBHOOK = "FEISHU_WEBHOOK"
E_FEISHU_APP_ID = "FEISHU_APP_ID"
E_FEISHU_APP_SEC = "FEISHU_APP_SECRET"
E_BITABLE_TOKEN = "BITABLE_APP_TOKEN"
E_BITABLE_TABLE = "BITABLE_TABLE_ID"


def base():
    """项目根目录：环境变量（含 `.env`）优先，否则用生产默认值。"""
    return _env(E_BASE) or DEFAULT_BASE


def paths(base_dir=None):
    """所有派生路径的唯一算法。

    ⚠️ 这些是**路径**（不是密钥），所以不强制塞进 `.env`；它们由 BASE 推出来。
    """
    b = base_dir or base()
    return {
        "BASE": b,
        "RUN": os.path.join(b, "v21"),
        "IMGDIR": os.path.join(b, "v21", "imgs"),
        "LOGF": os.path.join(b, "v21", "run.log"),
        "TRADES": os.path.join(b, "v21", "trades_dryrun.jsonl"),
        "STATE": os.path.join(b, "v21", "state.json"),
        "AUDITF": os.path.join(b, "v21", "real_orders.jsonl"),
        # 机器人自己写的状态（不手改）
        "RUNTIME": os.path.join(b, "runtime_config.json"),
    }


def _env(name):
    return (os.environ.get(name) or "").strip()


def secrets():
    """所有密钥/凭据 **只从环境变量（含 `.env`）读**。"""
    return {
        "deepseek_api_key": _env(E_DEEPSEEK),
        "binance_api_key": _env(E_BINANCE_KEY),
        "binance_api_secret": _env(E_BINANCE_SEC),
        "feishu_webhook": _env(E_FEISHU_WEBHOOK),
        "feishu_app_id": _env(E_FEISHU_APP_ID),
        "feishu_app_secret": _env(E_FEISHU_APP_SEC),
    }


def app_creds():
    """飞书自建应用凭据（供令牌续期用）。**只读环境变量。**"""
    return {"app_id": _env(E_FEISHU_APP_ID), "app_secret": _env(E_FEISHU_APP_SEC)}


def bitable():
    """飞书多维表格（成交统计用）。**只读环境变量。**未配置时返回空串，调用方跳过写入。"""
    return {"app_token": _env(E_BITABLE_TOKEN), "table_id": _env(E_BITABLE_TABLE)}


def missing_required(live=False, fetch_mode="api"):
    """启动前的**必备项检查**。返回缺失项清单（空 = 全齐）。

    分级（为什么这么分）：
      · `FEISHU_WEBHOOK` 必须 —— 没它则**所有告警都发不出去**。一个不能告警的跟单机器人
        等于静默运行，那是本项目最忌讳的状态（"绝不静默丢弃"）。
      · `DEEPSEEK_API_KEY` 必须 —— 解析信号全靠它，缺了等于收不到信号。
      · 币安密钥：**实盘时必须**；影子模式只是读仓位做对账，缺了会记日志、不阻断。
      · 飞书应用凭据：**API 取消息时必须**（用户令牌要自动续期）；
        回退浏览器模式时不需要。
    """
    miss = []
    if not _env(E_FEISHU_WEBHOOK):
        miss.append((E_FEISHU_WEBHOOK, "所有告警都发不出去（= 静默运行）"))
    if not _env(E_DEEPSEEK):
        miss.append((E_DEEPSEEK, "信号解析全部失效"))
    if live:
        if not _env(E_BINANCE_KEY):
            miss.append((E_BINANCE_KEY, "实盘下单要用"))
        if not _env(E_BINANCE_SEC):
            miss.append((E_BINANCE_SEC, "实盘下单要用"))
    if str(fetch_mode).lower() == "api":
        if not _env(E_FEISHU_APP_ID):
            miss.append((E_FEISHU_APP_ID, "飞书用户令牌自动续期要用（API 取消息模式）"))
        if not _env(E_FEISHU_APP_SEC):
            miss.append((E_FEISHU_APP_SEC, "飞书用户令牌自动续期要用（API 取消息模式）"))
    return miss


def fmt_missing(miss):
    """把缺失清单压成一段给人看的文字。"""
    if not miss:
        return ""
    return "\n".join("  · %s —— %s" % (k, why) for k, why in miss)
