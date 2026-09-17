# -*- coding: utf-8 -*-
"""飞书官方 API 取消息层（用户身份令牌）—— 用来替代"爬飞书网页"。

为什么要有它（2026-09-15/16 的真实教训）：
  · 爬网页依赖飞书自己的前端：9/15 首页 93 分钟打不开、9/16 前端 `web-framework.getNavigations`
    直接抛未捕获异常、开页失败、改版即全量失效 —— 一共栽了三次。
  · 官方 API 以**用户本人**的身份读**你自己的**群：机器人不需要被拉进群（那些群是别人的，
    拉不进去），也不需要浏览器、不需要 DOM、不受前端改版影响。
  · 实测（2026-09-16）：官方 API 拿到的图与网页 blob 抓下来的**字节完全相同**
    （1365x2560 / 191555 字节），读图结果一致（止损 6.396、止盈 7.18/8.216/9.289）。

令牌：authorization_code → user_access_token（2 小时）+ refresh_token（7 天，可自动续期）。
      刷新失败**不静默**：返回 None，由调用方告警提醒用户重新授权（重新授权只需点一条链接）。

对外接口：
    set_logger(fn)                              # 注入机器人的 log()
    health()                                    # 令牌 + 列群自检，返回 (ok, 说明)
    resolve_chat_ids(names)                     # 群名 → chat_id（模糊匹配，缓存）
    fetch_new(chat_id, since_ms, img_dir)       # 拉新消息，返回与网页版同结构的行
    msg_text_of(item)                           # 从任意消息（文本/卡片/post/图片）抽文字
"""
import os, json, time, hashlib
from urllib.parse import quote as urlencode
import requests

API = "https://open.feishu.cn/open-apis"
_HERE = os.path.dirname(os.path.abspath(__file__))

# .env 由 config 统一加载（见 config.py 文件头）
import config

TOKEN_PATH = os.environ.get("FEISHU_TOKEN_PATH") or os.path.join(_HERE, "feishu_user_token.json")
# ⚠️ 原来这里还有一行 `CFG_PATH = ... config.find_cfg()` —— 已删：
#    配置改成**只从环境变量（含 .env）读**后，config.json 已整个退役，
#    这行既没人用、又在 `find_cfg()` 被删后变成会崩的死代码。
REFRESH_MARGIN = 600          # 剩余有效期 < 10 分钟就提前刷新
# 重新授权用：控制台「安全设置 → 重定向 URL」里登记过的地址（不需要真的能打开）
REDIRECT_URI = "https://localhost:8765/callback"
SCOPE = ("im:chat:readonly im:message:readonly im:message.group_msg:get_as_user "
         "im:resource offline_access")

_log = lambda m: print(m, flush=True)
_TOKEN = {}                   # 内存缓存
_CHAT_CACHE = {}


def authorize_url(state="dsh"):
    """生成**用户点一下就能重新授权**的链接（令牌失效时随告警一起发给他）。
    返回 (主链接, 备用链接)：主链接带 scope（能拿到 offline_access 长期令牌），
    备用链接不带 scope（万一主链接报"权限不足"，它会授权该应用已批准的全部用户权限）。"""
    _q = urlencode
    main = ("https://accounts.feishu.cn/open-apis/authen/v1/authorize?client_id=%s&redirect_uri=%s"
            "&scope=%s&state=%s" % (_cfg().get("app_id", ""), _q(REDIRECT_URI, safe=""), _q(SCOPE), state))
    fallback = ("https://accounts.feishu.cn/open-apis/authen/v1/authorize?client_id=%s&redirect_uri=%s"
                "&state=%s2" % (_cfg().get("app_id", ""), _q(REDIRECT_URI, safe=""), state))
    return main, fallback


def reauth_hint():
    """给告警用的三行文字：告诉他怎么恢复（用户 2026-09-16 要求：刷新失败要提醒重新授权）。"""
    m, f = authorize_url()
    return ("重新授权步骤：\n"
            "1) 点开这条链接、用你的飞书账号登录并同意授权：\n%s\n"
            "2) 浏览器会跳到 https://localhost:8765/callback?code=xxxx（页面打不开是正常的）\n"
            "3) 把地址栏那一整条发给 AI（我换好长期令牌就恢复，不用再管 7 天）\n"
            "（如果第 1 步报「权限不足」，用这条不带 scope 的：%s）" % (m, f))


def set_logger(fn):
    global _log
    _log = fn


def _cfg():
    """飞书自建应用凭据 —— 从环境变量（含 `.env`）读：`FEISHU_APP_ID` / `FEISHU_APP_SECRET`。"""
    return config.app_creds()


def token_info():
    """给「状态」指令用：令牌剩余时间、refresh 剩余时间、scope。"""
    t = _load()
    now = time.time()
    return {"has_token": bool(t.get("access_token")),
            "access_left_s": int((t.get("expires_at") or 0) - now),
            "refresh_left_s": int((t.get("refresh_expires_at") or 0) - now),
            "has_refresh": bool(t.get("refresh_token")),
            "scope": t.get("scope"), "saved_at": t.get("saved_at")}


def _load():
    if _TOKEN:
        return _TOKEN
    try:
        _TOKEN.update(json.load(open(TOKEN_PATH, encoding="utf-8")))
    except Exception:
        pass
    return _TOKEN


def _save(d):
    _TOKEN.clear(); _TOKEN.update(d)
    try:
        os.umask(0o077)
        json.dump(d, open(TOKEN_PATH, "w", encoding="utf-8"), ensure_ascii=False)
        os.chmod(TOKEN_PATH, 0o600)
    except Exception as e:
        _log("   ⚠️ 令牌落盘失败：%s" % str(e)[:100])


def _refresh():
    """用 refresh_token 换新令牌。失败返回 None（调用方负责告警）。"""
    t = _load()
    rt = t.get("refresh_token")
    if not rt:
        _log("   ❌ 没有 refresh_token，需要重新授权")
        return None
    app = _cfg()
    try:
        r = requests.post(API + "/authen/v2/oauth/token",
                          json={"grant_type": "refresh_token", "client_id": app.get("app_id"),
                                "client_secret": app.get("app_secret"), "refresh_token": rt},
                          timeout=25).json()
    except Exception as e:
        _log("   ⚠️ 刷新令牌网络异常：%s" % str(e)[:100])
        return None
    if not r.get("access_token"):
        _log("   ❌ 刷新令牌被拒：%s" % json.dumps(r, ensure_ascii=False)[:200])
        return None
    d = dict(t)
    d.update({"access_token": r["access_token"],
              "refresh_token": r.get("refresh_token") or rt,
              "expires_at": time.time() + int(r.get("expires_in") or 7200),
              "refresh_expires_at": time.time() + int(r.get("refresh_token_expires_in") or 604800),
              "scope": r.get("scope") or t.get("scope"),
              "refreshed_at": time.strftime("%Y-%m-%d %H:%M:%S")})
    _save(d)
    _log("   ♻️ 飞书令牌已自动刷新（access 有效至 %s）"
         % time.strftime("%H:%M:%S", time.localtime(d["expires_at"])))
    return d["access_token"]


def access_token():
    """拿可用令牌（快过期就自动刷新）。拿不到返回 None。"""
    t = _load()
    if not t.get("access_token"):
        return None
    if (t.get("expires_at") or 0) - time.time() < REFRESH_MARGIN:
        return _refresh()
    return t["access_token"]


def _get(path, params=None, _retry=True):
    """GET 封装：401/令牌过期自动刷新重试一次。返回 (code, data, msg)。"""
    tok = access_token()
    if not tok:
        return (-1, None, "没有可用令牌（需要重新授权）")
    try:
        r = requests.get(API + path, params=params,
                         headers={"Authorization": "Bearer " + tok}, timeout=30).json()
    except Exception as e:
        return (-2, None, "网络异常：%s" % str(e)[:80])
    code = r.get("code")
    if code in (99991677, 99991668, 401) and _retry:      # 令牌过期/无效
        if _refresh():
            return _get(path, params, _retry=False)
    return (code, r.get("data"), r.get("msg") or "")


# ---------------- 群 ----------------
def list_chats(page_size=100, max_pages=10):
    """列出"我"所在的群/会话。返回 [{name, chat_id, external, chat_mode}]。"""
    out, page = [], None
    for _ in range(max_pages):
        p = {"page_size": page_size}
        if page:
            p["page_token"] = page
        code, data, msg = _get("/im/v1/chats", p)
        if code != 0:
            _log("   ⚠️ 列群失败 code=%s msg=%s" % (code, msg))
            break
        out += (data or {}).get("items") or []
        page = (data or {}).get("page_token")
        if not (data or {}).get("has_more"):
            break
    return [{"name": (c.get("name") or "").strip(), "chat_id": c.get("chat_id"),
             "external": c.get("external"), "chat_mode": c.get("chat_mode")} for c in out]


def resolve_chat_ids(names, force=False):
    """群名 → chat_id。模糊匹配（群名互相包含时优先完全相等）。返回 {群名: chat_id}。"""
    global _CHAT_CACHE
    if force or not _CHAT_CACHE:
        try:
            _CHAT_CACHE = {c["name"]: c["chat_id"] for c in list_chats()}
        except Exception:
            _CHAT_CACHE = {}
    found = {}
    for n in names:
        n = (n or "").strip()
        if not n:
            continue
        if n in _CHAT_CACHE:
            found[n] = _CHAT_CACHE[n]
            continue
        for k, v in _CHAT_CACHE.items():
            if n.lower() in (k or "").lower() or (k or "").lower() in n.lower():
                found[n] = v
                break
    return found


# ---------------- 消息内容 ----------------
def _walk_card(node, texts, imgs, depth=0):
    """递归抠卡片/富文本里的可见文字与图片 key（保持出现顺序）。"""
    if depth > 14:
        return
    if isinstance(node, dict):
        t = node.get("tag")
        if t == "text" and isinstance(node.get("text"), str) and node["text"].strip():
            texts.append(node["text"])
        elif t in ("a",) and isinstance(node.get("text"), str) and node["text"].strip():
            texts.append(node["text"])
        elif t == "img" and node.get("image_key"):
            imgs.append(node["image_key"])
        elif t == "button" and isinstance(node.get("text"), str):
            texts.append(node["text"])
        for k, v in node.items():
            if k in ("text", "tag") and isinstance(v, str):
                continue
            _walk_card(v, texts, imgs, depth + 1)
    elif isinstance(node, list):
        for x in node:
            _walk_card(x, texts, imgs, depth + 1)


def msg_text_of(item):
    """从任意消息抽文字（文本/卡片/post/图片说明）。"""
    mt = item.get("msg_type")
    raw = (item.get("body") or {}).get("content") or ""
    try:
        cj = json.loads(raw)
    except Exception:
        return raw if isinstance(raw, str) else ""
    if mt == "text":
        return cj.get("text") or ""
    if mt == "post":
        texts, imgs = [], []
        _walk_card(cj.get("content") or cj, texts, imgs)
        head = cj.get("title") or ""
        return (head + "\n" if head else "") + "".join(texts)
    if mt == "interactive":
        texts, imgs = [], []
        _walk_card(cj, texts, imgs)
        return "".join(texts)
    if mt == "image":
        return ""                                   # 纯图片：没有文字（由图片通道处理）
    texts, imgs = [], []
    _walk_card(cj, texts, imgs)
    return "".join(texts)


def msg_images_of(item):
    """从任意消息抽图片 key（保持顺序）。"""
    raw = (item.get("body") or {}).get("content") or ""
    try:
        cj = json.loads(raw)
    except Exception:
        return []
    if item.get("msg_type") == "image":
        k = cj.get("image_key")
        return [k] if k else []
    texts, imgs = [], []
    _walk_card(cj, texts, imgs)
    return imgs


_IMG_MAGIC = ((b"\xff\xd8\xff", ".jpg"), (b"\x89PNG", ".png"), (b"GIF8", ".gif"), (b"RIFF", ".webp"))


def download_image(message_id, image_key, img_dir):
    """下载消息里的图片**原图**。返回落盘路径，失败返回 None。
    ⚠️ 必须用 /im/v1/messages/{mid}/resources/{key}?type=image ——
       /im/v1/images/{key} 会报 14049（那个接口只认本应用自己上传的图）。"""
    if not message_id or not image_key:
        return None
    tok = access_token()
    if not tok:
        return None
    try:
        r = requests.get("%s/im/v1/messages/%s/resources/%s" % (API, message_id, image_key),
                         params={"type": "image"},
                         headers={"Authorization": "Bearer " + tok}, timeout=45)
    except Exception as e:
        _log("   ⚠️ 下载图片异常：%s" % str(e)[:80])
        return None
    if r.status_code != 200 or len(r.content) < 800:
        _log("   ⚠️ 下载图片失败 HTTP %s ｜ %d 字节" % (r.status_code, len(r.content)))
        return None
    ext = ".png"
    for magic, e in _IMG_MAGIC:
        if r.content.startswith(magic):
            ext = e
            break
    try:
        os.makedirs(img_dir, exist_ok=True)
        fn = os.path.join(img_dir, "%s_%s%s" % (message_id[-10:], image_key[-6:], ext))
        with open(fn, "wb") as f:
            f.write(r.content)
        return fn
    except Exception as e:
        _log("   ⚠️ 图片落盘失败：%s" % str(e)[:80])
        return None


def _stable_id(create_ms, message_id):
    """把消息变回"整数 id"给现有游标/去重逻辑用：时间在前、同毫秒用消息 id 哈希区分。"""
    h = int(hashlib.md5((message_id or "").encode()).hexdigest()[:4], 16) % 1000
    return int(create_ms) * 1000 + h


def fetch_messages(chat_id, start_time=None, end_time=None, page_size=50, max_pages=4, asc=True):
    """取某群消息（默认按时间升序）。start_time/end_time 为**秒级**时间戳。"""
    out, page = [], None
    for _ in range(max_pages):
        p = {"container_id_type": "chat", "container_id": chat_id, "page_size": page_size,
             "sort_type": "ByCreateTimeAsc" if asc else "ByCreateTimeDesc"}
        if start_time:
            p["start_time"] = str(int(start_time))
        if end_time:
            p["end_time"] = str(int(end_time))
        if page:
            p["page_token"] = page
        code, data, msg = _get("/im/v1/messages", p)
        if code != 0:
            return out, "code=%s %s" % (code, msg)
        out += (data or {}).get("items") or []
        page = (data or {}).get("page_token")
        if not (data or {}).get("has_more"):
            break
    out.sort(key=lambda it: (int(it.get("create_time") or 0), it.get("message_id") or ""))
    return out, None


def fetch_new(chat_id, since_ms, img_dir, want_images=True, max_msgs=60, skip_ids=None):
    """拉"比 since_ms 新"的消息，产出与网页版 SCAN_JS **同结构**的行：
        {"id": int, "t_sig": int(秒), "text": str, "nimg": n, "loaded": n, "nblob": n,
         "_imgs": [已下载的原图路径], "msg_id": str, "msg_type": str,
         "sender_type": "user"/"app"/None, "sender_id": str/None, "sender_id_type": str/None}
    这样下游的解析/审批/下单逻辑一行都不用改。

    🆕 2026-09-17：把**发送者**一并带出来（自环防护用，实测数据在下面这段注释里）。

    ⚠️ 实测（2026-09-17，生产真数据，别凭印象推翻）：
      · 机器人自己的通知（自定义机器人 webhook 发的）→ {"sender_type": "app",
          "id": "cli_c08abc...（脱敏）", "id_type": "app_id"}
      · 用户本人发的消息                              → {"sender_type": "user",
          "id": "ou_ef47…", "id_type": "open_id"}
      · **KOL 群里的博主信号同样是 "app"**（黄金mansoor / UA-nurseneil2 的卡片全是 app），
        而且 app_id **和我们自己的 webhook 一模一样**（同一个平台应用），只有 tenant_key 不同。
        ⇒ 所以绝对不能用"凡是 app 发的就跳过"来防自环 —— 那样会把**真信号全部杀光**。
          正确做法：只在**我们自己的群**（CMD_GROUPS）里把 app 消息当成"自己的通知"（见
          dryrun_bot2._is_self_app_row）。
    
    skip_ids：已经处理过的消息 id 集合（传 SEEN 进来）——**在下载图片之前就跳过**，
              否则那 5 秒回看窗口里的图每轮都会被重复下载。
    """
    since_s = int(since_ms / 1000) - 5 if since_ms else None          # 往回多取 5 秒，避免边界丢消息
    items, err = fetch_messages(chat_id, start_time=since_s)
    if err:
        return [], err
    rows = []
    for it in items[-max_msgs:]:
        cms = int(it.get("create_time") or 0)
        if since_ms and cms <= since_ms:
            continue
        mid = it.get("message_id") or ""
        if it.get("deleted"):
            continue
        _sid = _stable_id(cms, mid)
        if skip_ids and _sid in skip_ids:
            continue
        txt = msg_text_of(it)
        keys = msg_images_of(it) if want_images else []
        imgs = []
        for ik in keys[:4]:                                            # 一条消息最多取 4 张图
            p = download_image(mid, ik, img_dir)
            if p:
                imgs.append(p)
        _sd = it.get("sender") or {}
        rows.append({"id": _sid, "t_sig": cms // 1000, "text": txt,
                     "nimg": len(keys), "loaded": len(imgs), "nblob": len(imgs),
                     "_imgs": imgs, "msg_id": mid, "msg_type": it.get("msg_type"),
                     # 发送者：id 是**字符串**（app 时是 app_id、user 时是 open_id），id_type 说明是哪种
                     "sender_type": _sd.get("sender_type") or None,
                     "sender_id": _sd.get("id") or None,
                     "sender_id_type": _sd.get("id_type") or None,
                     "sender_tenant": _sd.get("tenant_key") or None})
    return rows, None


def health():
    """自检：令牌可用 + 能列群。返回 (ok: bool, 说明: str)。"""
    ti = token_info()
    if not ti["has_token"]:
        return False, "没有令牌（需要在服务器上完成一次授权）"
    chats = list_chats(max_pages=2)
    if not chats:
        return False, "令牌在但不能列群（scope 或网络问题）→ %s" % json.dumps(ti, ensure_ascii=False)
    return True, ("令牌正常（access 剩 %d 分钟 / refresh 剩 %d 小时）｜ 能列到 %d 个群"
                  % (ti["access_left_s"] // 60, ti["refresh_left_s"] // 3600, len(chats)))
