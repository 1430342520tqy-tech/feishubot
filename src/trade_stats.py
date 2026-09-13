#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
成交统计：一单结单 → 往飞书【多维表格】写一行（用户 2026-09-13 定的方案）

设计要点（按用户要求）：
  1. **只统计已结单的**：持仓中的不写，结单那一刻才写一行。
  2. **整单一一行**：多档止盈也合成一行；结单时间 = 最后一档平完的时刻。
  3. 收益率 = **净盈亏 ÷ 保证金**（含 3 倍杠杆）。
  4. **手续费计入**：开仓/止盈/止损/手动平仓分别按真实费率 maker 万2 / taker 万5 累计。
  5. 落地只到**飞书多维表格**一个地方（用户明确只需要一个地方能看见）。

放在机器人**同一个进程里调用**（结单时直接调），不新起进程、不常驻 →
对这台 2C4G 的额外内存开销≈0，不影响开单。

配置（服务器 config.json）：
  "bitable": { "app_token": "<多维表格 URL 里 /base/ 后面那串>",
               "table_id":  "<URL 里 table= 后面那串 tblXXXX>" }
未配置时只写日志、不报错，机器人照常运行。
"""
import os
import json
import time
import datetime
import urllib.parse
import urllib.request
import urllib.error

BASE = os.environ.get("SIGNAL_BOT_BASE", "/home/ubuntu/signal-bot")
CFG = os.path.join(BASE, "config.json")
FEISHU = "https://open.feishu.cn/open-apis"
CST = datetime.timezone(datetime.timedelta(hours=8))

DEFAULT_MARGIN = 300.0

_TOKEN = {"v": None, "exp": 0.0}


def _cfg():
    try:
        c = json.load(open(CFG, encoding="utf-8"))
        return c.get("bitable") or {}, c
    except Exception:
        return {}, {}


# 字段定义：名称 -> Bitable 类型（1=文本 2=数字 5=日期）
FIELDS = [
    ("币种", 1), ("方向", 1),
    ("开单时间", 5), ("结单时间", 5), ("持仓时长", 1), ("持仓小时", 2),
    ("保证金U", 2), ("杠杆", 2), ("名义U", 2),
    ("入场价", 2), ("止损价", 2), ("止盈1", 2), ("止盈2", 2), ("止盈3", 2),
    ("止损点数%", 2),
    ("结单方式", 1), ("平仓价", 2),
    ("毛盈亏U", 2), ("手续费U", 2), ("净盈亏U", 2), ("净收益率%", 2),
    ("来源群", 1), ("记录类型", 1), ("备注", 1),
]


def _token():
    """tenant_access_token，缓存到过期前 5 分钟"""
    if _TOKEN["v"] and time.time() < _TOKEN["exp"] - 300:
        return _TOKEN["v"]
    _, c = _cfg()
    app = c.get("feishu_app") or {}
    aid, sec = (app.get("app_id") or "").strip(), (app.get("app_secret") or "").strip()
    if not aid or not sec:
        return None
    body = json.dumps({"app_id": aid, "app_secret": sec}).encode()
    req = urllib.request.Request(FEISHU + "/auth/v3/tenant_access_token/internal",
                                data=body, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            d = json.loads(r.read().decode())
        if d.get("code") == 0:
            _TOKEN["v"] = d["tenant_access_token"]
            _TOKEN["exp"] = time.time() + int(d.get("expire", 7200))
            return _TOKEN["v"]
        return None
    except Exception:
        return None


def _api(method, path, payload=None, token=None, timeout=20):
    tk = token or _token()
    if not tk:
        return None, "没有可用的 tenant_access_token（config.json 缺 feishu_app 或换取失败）"
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(FEISHU + path, data=data, method=method,
                                headers={"Authorization": "Bearer " + tk,
                                         "Content-Type": "application/json; charset=utf-8"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode()), None
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
        try:
            raw = json.loads(raw)
        except Exception:
            pass
        return None, "HTTP %s: %s" % (e.code, raw)
    except Exception as e:
        return None, "%s: %s" % (type(e).__name__, e)


def ensure_fields(log=print):
    """把缺的列自动建出来（表里默认字段不用管，留着空即是）"""
    bt, _ = _cfg()
    app_token, table_id = (bt.get("app_token") or "").strip(), (bt.get("table_id") or "").strip()
    if not app_token or not table_id:
        log("[统计] 未配置 bitable.app_token / table_id → 跳过建列")
        return False
    d, err = _api("GET", "/bitable/v1/apps/%s/tables/%s/fields?page_size=200" % (app_token, table_id))
    if err:
        log("[统计] 读字段失败：%s" % err)
        return False
    have = {f.get("field_name") for f in (d.get("data") or {}).get("items", [])}
    made = []
    for name, ftype in FIELDS:
        if name in have:
            continue
        _, e2 = _api("POST", "/bitable/v1/apps/%s/tables/%s/fields" % (app_token, table_id),
                     {"field_name": name, "type": ftype})
        if e2:
            log("[统计] 建列 %s 失败：%s" % (name, e2))
        else:
            made.append(name)
    log("[统计] 字段就绪（新建 %d 列：%s）" % (len(made), "、".join(made) or "无"))
    return True


def _hold_text(sec):
    try:
        sec = int(max(0, sec))
    except Exception:
        return "-"
    d, r = divmod(sec, 86400)
    h, r = divmod(r, 3600)
    m = r // 60
    if d:
        return "%d天%d小时" % (d, h)
    if h:
        return "%d小时%d分" % (h, m)
    return "%d分钟" % m


def _exit_kind(why):
    """归类结单方式。⚠️ 顺序有讲究：先判『手动/指令』再判止盈/保本/止损 ——
    否则『手动平仓(全部平仓指令，TP1后移保本)』会因为含『保本』二字被误归成保本止损。"""
    w = str(why or "")
    if "手动" in w or "指令" in w:
        return "手动平仓"
    if "止盈" in w:
        return "止盈"
    if "保本" in w:
        return "保本止损"
    if "止损" in w:
        return "止损"
    return w or "-"


def open_ts(tr):
    """取开仓时刻（Unix 秒）。
    新记录有 t_open_ts；旧记录只有 "09-12 22:23:52" 这种**无年份**字符串 → 按当年补全。"""
    v = tr.get("t_open_ts")
    if v:
        try:
            return int(v)
        except Exception:
            pass
    s = tr.get("t_open")
    if s:
        try:
            return int(datetime.datetime.strptime(
                "%d-%s" % (datetime.datetime.now(CST).year, s),
                "%Y-%m-%d %H:%M:%S").replace(tzinfo=CST).timestamp())
        except Exception:
            pass
    return None


def build_row(tr, margin=None):
    """把一笔已结单的记录变成多维表格的一行（整单一一行）"""
    margin = float(margin or tr.get("margin") or DEFAULT_MARGIN)
    lev = int(tr.get("lev") or 3)
    notional = float(tr.get("notional") or margin * lev)
    entry = tr.get("entry") or 0.0
    t_open = open_ts(tr)
    t_close = tr.get("t_close_ts") or int(time.time())
    try:
        hold = int(t_close) - int(t_open) if t_open else 0
    except Exception:
        hold = 0
    gross = float(tr.get("realized") or 0.0)
    fee = float(tr.get("fee") or 0.0)
    net = gross - fee
    tps = list(tr.get("tps") or [])[:3]
    sl = tr.get("sl")
    sl_pts = None
    if sl and entry:
        sl_pts = round(abs(float(sl) - float(entry)) / float(entry) * 100, 4)
    row = {
        "币种": str(tr.get("coin") or ""),
        "方向": ("做多" if str(tr.get("dir")).upper() == "LONG" else "做空"),
        "开单时间": int(t_open) * 1000 if t_open else None,
        "结单时间": int(t_close) * 1000,
        "持仓时长": _hold_text(hold),
        "持仓小时": round(hold / 3600.0, 3),
        "保证金U": round(margin, 2),
        "杠杆": lev,
        "名义U": round(notional, 2),
        "入场价": float(entry) if entry else None,
        "止损价": float(sl) if sl else None,
        "止损点数%": sl_pts,
        "结单方式": _exit_kind(tr.get("exit_why")),
        "平仓价": float(tr["exit"]) if isinstance(tr.get("exit"), (int, float)) else None,
        "毛盈亏U": round(gross, 4),
        "手续费U": round(-fee, 4),
        "净盈亏U": round(net, 4),
        "净收益率%": round(net / margin * 100, 4) if margin else None,
        "来源群": str(tr.get("group") or ""),
        "记录类型": ("实盘" if str(tr.get("real_layer")) == "实盘" else "纸面"),
        "备注": "%s ｜ 入场来源 %s ｜ 止损来源 %s ｜ 原文 %s" % (
            tr.get("exit_why") or "-", tr.get("entry_src") or "-", tr.get("stop_src") or "-",
            (tr.get("text") or "")[:120]),
    }
    for i in range(3):
        row["止盈%d" % (i + 1)] = float(tps[i]) if i < len(tps) and isinstance(tps[i], (int, float)) else None
    return {k: v for k, v in row.items() if v is not None}


def push_close(tr, log=print):
    """结单时调用：往多维表格写一行。未配置就只记日志，绝不影响交易。"""
    bt, _ = _cfg()
    app_token, table_id = (bt.get("app_token") or "").strip(), (bt.get("table_id") or "").strip()
    if not app_token or not table_id:
        log("[统计] 未配置多维表格 → 本单不写表（结单时间已存进本地记录，可事后补录）")
        return False
    row = build_row(tr)
    d, err = _api("POST", "/bitable/v1/apps/%s/tables/%s/records" % (app_token, table_id),
                  {"fields": row})
    if err:
        log("[统计] ❌ 写多维表格失败：%s" % err)
        return False
    log("[统计] ✅ 已写入多维表格：%s %s 净%.2fU（%.2f%%）持仓%s"
        % (row.get("币种"), row.get("方向"), row.get("净盈亏U") or 0,
           row.get("净收益率%") or 0, row.get("持仓时长")))
    return True


if __name__ == "__main__":
    import sys
    if "--ensure" in sys.argv:
        print("检查/新建多维表格列…")
        ensure_fields()
    elif "--test" in sys.argv:
        demo = {"coin": "BTC", "dir": "SHORT", "entry": 77000.0, "sl": 77500.0,
                "tps": [74500.0, 70500.0], "realized": 29.2, "fee": 0.63,
                "t_open_ts": int(time.time()) - 98000, "t_close_ts": int(time.time()),
                "exit": 74500.0, "exit_why": "全部止盈", "group": "机器人开单通知",
                "entry_src": "市价成交", "stop_src": "消息文字", "text": "测试行"}
        print(json.dumps(build_row(demo), ensure_ascii=False, indent=1))
        print("\n推送测试行：")
        push_close(demo)
    else:
        print(__doc__)
