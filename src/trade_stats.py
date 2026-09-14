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


# 字段定义：名称 -> (Bitable 类型, property)  —— 顺序即表里的从左到右顺序
# 用户 2026-09-13 要求：
#   · 开单/结单时间精确到秒（日期字段不一定能显示秒 + 数字列位数整列统一 → 时间/价格用文本，显示绝对精确）
#   · 删掉 持仓小时 / 名义U / 结单方式 / 备注
#   · 杠杆改叫「杠杆倍数」，不带小数点
#   · 所有「xx率」带 % 且不要小数
#   · 价格要能直出原值（如 0.8521），不能被截成 0.9
MONEY = {"formatter": "0.00"}
INTFMT = {"formatter": "0"}

FIELDS = [
    # ⚠️ 第 1 列在飞书多维表格里是【主列】，建表时就得定好、之后不能删、不能换位置
    #    → 用户 2026-09-14 要求把「来源群」放第一列，因此本表是按此顺序新建的
    ("来源群", 1, None),
    ("币种", 1, None),
    ("方向", 1, None),
    ("开单时间", 1, None),
    ("结单时间", 1, None),
    ("持仓时长", 1, None),
    ("保证金U", 2, MONEY),
    ("杠杆倍数", 2, INTFMT),
    ("入场价", 1, None),
    ("止损价", 1, None),
    ("止损点数%", 1, None),
    ("止盈1", 1, None),
    ("止盈2", 1, None),
    ("止盈3", 1, None),
    ("平仓价", 1, None),
    ("毛盈亏U", 2, MONEY),
    ("手续费U", 2, MONEY),
    ("净盈亏U", 2, MONEY),
    ("净收益率%", 1, None),
    ("记录类型", 1, None),
]


def ensure_fields(log=print):
    """把缺的列按 FIELDS 的顺序建出来（已存在的跳过）"""
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
    for name, ftype, prop in FIELDS:
        if name in have:
            continue
        payload = {"field_name": name, "type": ftype}
        if prop:
            payload["property"] = prop
        _, e2 = _api("POST", "/bitable/v1/apps/%s/tables/%s/fields" % (app_token, table_id), payload)
        if e2:
            log("[统计] 建列 %s 失败：%s" % (name, e2))
        else:
            made.append(name)
    log("[统计] 字段就绪（新建 %d 列：%s）" % (len(made), "、".join(made) or "无"))
    return True


def _price_text(v):
    """价格转文本：保留原值、去掉多余的 0（0.8521 就是 0.8521；77000 就是 77000）"""
    if v is None or v == "":
        return None
    try:
        s = ("%.10f" % float(v)).rstrip("0")
        if s.endswith("."):
            s = s[:-1]
        return s
    except Exception:
        return str(v)


def _time_text(ts):
    if not ts:
        return None
    try:
        return datetime.datetime.fromtimestamp(int(ts), CST).strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return None


def _pct_text(v, digits=0):
    """收益率/点数 -> '835%' 这种（用户要求带 % 且不要小数）"""
    if v is None:
        return None
    try:
        return ("%." + str(digits) + "f%%") % float(v)
    except Exception:
        return None



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
        # 时间用文本 -> 保证精确到秒、显示无误差（用户明确要求）
        "开单时间": _time_text(t_open),
        "结单时间": _time_text(t_close),
        "持仓时长": _hold_text(hold),
        "保证金U": round(margin, 2),
        "杠杆倍数": lev,
        # 价格用文本 -> 0.8521 就显示 0.8521，不会被列的显示位数截成 0.9
        "入场价": _price_text(entry) if entry else None,
        "止损价": _price_text(sl) if sl else None,
        "止损点数%": _pct_text(sl_pts) if sl_pts is not None else None,
        "平仓价": _price_text(tr["exit"]) if isinstance(tr.get("exit"), (int, float)) else None,
        "毛盈亏U": round(gross, 2),
        "手续费U": round(-fee, 2),
        "净盈亏U": round(net, 2),
        # 收益率带 % 且不要小数（用户要求）
        "净收益率%": _pct_text(net / margin * 100) if margin else None,
        "来源群": str(tr.get("group") or ""),
        "记录类型": ("实盘" if str(tr.get("real_layer")) == "实盘" else "纸面"),
    }
    for i in range(3):
        row["止盈%d" % (i + 1)] = (_price_text(tps[i])
                                   if i < len(tps) and isinstance(tps[i], (int, float)) else None)
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
    log("[统计] ✅ 已写入多维表格：%s %s 净%+.2fU（%s）持仓%s"
        % (row.get("币种"), row.get("方向"), row.get("净盈亏U") or 0,
           row.get("净收益率%") or "-", row.get("持仓时长")))
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
