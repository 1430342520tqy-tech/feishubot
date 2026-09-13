# -*- coding: utf-8 -*-
"""成交明细表生成器
trades_dryrun.jsonl 是「事件流」：开单/减仓/止盈/结单 每次状态变化都追加一条完整快照。
本工具按 (币种, 开单时间) 去重，只取最后一次快照 -> 得到一人一行、看得懂的结单表。

用法：
  python trade_table.py            # 打印表格
  python trade_table.py --push     # 打印 + 推送到飞书
"""
import json, os, sys, urllib.request

BASE = os.path.dirname(os.path.abspath(__file__))
V21 = os.path.join(BASE, "v21")
TRADES = os.path.join(V21, "trades_dryrun.jsonl")
NOTIFY_CFG = os.path.join(BASE, "notify.json")
NOTIONAL = 900.0     # 300U 保证金 x 3倍


def load_rows():
    if not os.path.exists(TRADES):
        return []
    rows = []
    for ln in open(TRADES, encoding="utf-8"):
        ln = ln.strip()
        if not ln:
            continue
        try:
            rows.append(json.loads(ln))
        except Exception:
            continue
    return rows


def load_live():
    """读当前真实持仓（state.json）——用来和事件流对账"""
    p = os.path.join(V21, "state.json")
    try:
        st = json.load(open(p, encoding="utf-8"))
        return st.get("open") or {}
    except Exception:
        return {}


def dedup(rows):
    """按 (coin, t_open) 取最后一条；把整个生命周期合并成一行"""
    latest = {}
    order = []
    for r in rows:
        key = (r.get("coin"), r.get("t_open"))
        if key not in latest:
            order.append(key)
        latest[key] = r
    return [latest[k] for k in order]


def reconcile(rows, live):
    """给每条记录判定真实归属，修正 status 显示
    返回 (rows, notes) —— notes 说明哪些行是重复/失效的
    """
    notes = []
    for r in rows:
        coin = r.get("coin")
        if r.get("status") == "CLOSED":
            r["_live"] = "closed"
            continue
        if coin not in live:
            r["_live"] = "stale"          # 不在当前持仓里 -> 历史遗留/已失效
            notes.append("%s（%s）不在当前持仓中，标记为『已失效』" % (coin, r.get("t_open")))
        elif live[coin].get("t_open") == r.get("t_open"):
            r["_live"] = "live"
        else:
            r["_live"] = "dup"            # 同一币种的另一次记录 -> 重复
            notes.append("%s（%s）与当前持仓的 %s 是同一信号的重复记录，标记为『重复』"
                         % (coin, r.get("t_open"), live[coin].get("t_open")))
    return rows, notes


def fmt_px(v):
    if v is None:
        return "-"
    if isinstance(v, (int, float)):
        s = ("%.8f" % v).rstrip("0").rstrip(".")
        return s
    return str(v)


def status_of(r):
    st = r.get("status", "OPEN")
    if st == "CLOSED":
        return "已结单"
    lv = r.get("_live")
    if lv == "live":
        rem = r.get("remaining", 1.0)
        return "持仓中 %.0f%%" % (rem * 100) if rem < 0.999 else "持仓中"
    if lv == "dup":
        return "⚠️重复记录"
    if lv == "stale":
        return "⚠️已失效"
    return "持仓中"


def render_md(rows):
    out = []
    out.append("| # | 币种 | 方向 | 开单时间 | 来源群 | 博主开仓价 | 我的入场价 | 止损 | "
               "止盈1 | 止盈2 | 止盈3 | 状态 | 平仓价 | 平仓原因 | 盈亏U |")
    out.append("|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|")
    for i, r in enumerate(rows, 1):
        tps = (r.get("tps") or []) + [None, None, None]
        pnl = r.get("pnl")
        out.append("| %d | %s | %s | %s | %s | %s | %s | %s | %s | %s | %s | %s | %s | %s | %s |" % (
            i,
            r.get("coin", "?"),
            "做多" if r.get("dir") == "LONG" else "做空",
            r.get("t_open", "-"),
            r.get("group", "-"),
            fmt_px(r.get("signal_entry")),
            fmt_px(r.get("entry")),
            fmt_px(r.get("sl")),
            fmt_px(tps[0]), fmt_px(tps[1]), fmt_px(tps[2]),
            status_of(r),
            fmt_px(r.get("exit")),
            r.get("exit_why") or "-",
            ("%+.1f" % pnl) if isinstance(pnl, (int, float)) else "-",
        ))
    return "\n".join(out)


def render_plain(rows):
    L = ["成交明细（共 %d 单）" % len(rows), ""]
    for i, r in enumerate(rows, 1):
        tps = (r.get("tps") or [])
        d = 1 if r.get("dir") == "LONG" else -1
        L.append("%d) %s %s ｜ %s ｜ 来源 %s" % (
            i, r.get("coin"), "做多" if d == 1 else "做空", r.get("t_open"), r.get("group")))
        L.append("   博主开仓 %s → 我的入场 %s（%s）" % (
            fmt_px(r.get("signal_entry")), fmt_px(r.get("entry")), r.get("entry_src") or "-"))
        L.append("   止损 %s（读到方式：%s）" % (fmt_px(r.get("sl")), r.get("stop_src") or "-"))
        if tps:
            for j, t in enumerate(tps[:3], 1):
                if t is None:
                    continue
                gross = (t - r["entry"]) * d / r["entry"] * 100
                L.append("   止盈%d %s → 含3倍杠杆 %+.2f%% ｜ 该档约 %+.1fU"
                         % (j, fmt_px(t), gross * 3,
                            (t - r["entry"]) * d / r["entry"] * NOTIONAL / max(len(tps), 1)))
        else:
            L.append("   止盈：未读到（该信号图文里都没有可读的止盈位）")
        if r.get("status") == "CLOSED":
            pnl = r.get("pnl")
            L.append("   ✅ 已结单：%s @%s ｜ 盈亏 %s"
                     % (r.get("exit_why"), fmt_px(r.get("exit")),
                        ("%+.1fU" % pnl) if isinstance(pnl, (int, float)) else "-"))
        else:
            filled = r.get("filled") or []
            lv = r.get("_live")
            if lv == "stale":
                L.append("   ⚠️ 已失效：该记录不在当前持仓中（历史遗留，可能是状态恢复时被覆盖）")
            elif lv == "dup":
                L.append("   ⚠️ 重复记录：同一信号的另一次记录，真实持仓见上面同名币种那一行")
            else:
                L.append("   ⏳ 持仓中：剩余 %.0f%% ｜ 已成交档位 %s"
                         % (r.get("remaining", 1.0) * 100,
                            ("/".join("TP%d" % (x + 1) for x in filled) or "无")))
        L.append("")
    return "\n".join(L)


def push(text):
    try:
        cfg = json.load(open(NOTIFY_CFG, encoding="utf-8"))
        url = cfg["feishu_webhook"]
    except Exception as e:
        print("读 webhook 失败: " + str(e)[:80])
        return
    body = json.dumps({"msg_type": "text", "content": {"text": text}}).encode()
    req = urllib.request.Request(url, data=body,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            print("推送结果: " + r.read().decode()[:120])
    except Exception as e:
        print("推送失败: " + str(e)[:120])


def main():
    raw = load_rows()
    rows = dedup(raw)
    if not rows:
        print("暂无成交记录")
        return
    live = load_live()
    rows, notes = reconcile(rows, live)

    n_live = sum(1 for r in rows if r.get("status") != "CLOSED" and r.get("_live") == "live")
    n_closed = sum(1 for r in rows if r.get("status") == "CLOSED")
    closed_pnl = sum(r.get("pnl") or 0 for r in rows if r.get("status") == "CLOSED")

    print(render_plain(rows))
    print("-" * 70)
    print("汇总：当前持仓 %d 单 ｜ 已结单 %d 单 ｜ 已结单累计盈亏 %+.1fU"
          % (n_live, n_closed, closed_pnl))
    if notes:
        print("\n对账提示（记录表与真实持仓的差异）：")
        for x in notes:
            print("  · " + x)
    print("=" * 70)
    print(render_md(rows))
    if "--push" in sys.argv:
        tail = "汇总：当前持仓 %d 单 ｜ 已结单 %d 单 ｜ 已结单累计盈亏 %+.1fU" % (
            n_live, n_closed, closed_pnl)
        push("【成交明细】\n\n" + render_plain(rows) + "\n" + tail)


if __name__ == "__main__":
    main()
