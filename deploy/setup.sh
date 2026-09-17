#!/bin/bash
# feishubot 一键部署 / 卸载
#
# 作用：把「新机器从零跑起来」这件事做成一条命令 —— 以前做不到，因为
#   · 依赖没有清单（requirements.txt 是后补的）
#   · logrotate 只能手工 cp
#   · 巡检 unit 是从 /tmp 拷的（那个目录重启就空）
#   · pm2 配置不在仓库里（换机器要凭记忆敲）
#
# 用法（在仓库目录里跑）：
#   bash deploy/setup.sh              # 安装依赖 + logrotate + 巡检 timer + pm2
#   bash deploy/setup.sh --no-pm2     # 不动 pm2（机器人自己已经在跑）
#   bash deploy/setup.sh --uninstall  # 卸载 logrotate + 巡检 timer + pm2 里的机器人
#
# 幂等：可重复执行。
# 不动机器人数据：不碰 .env、不碰 runtime_config.json、不碰 v21/。
set -e

BASE="${SIGNAL_BOT_BASE:-/home/ubuntu/signal-bot}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/.." && pwd)"
PY="$BASE/venv/bin/python"

WATCH_UNIT=/etc/systemd/system/signalbot-watch.service
WATCH_TIMER=/etc/systemd/system/signalbot-watch.timer
LOGROTATE=/etc/logrotate.d/signal-bot

say() { echo "[setup] $*"; }
die() { echo "[FAIL] $*" >&2; exit 1; }

# ---------------------------------------------------------------- 卸载
if [ "$1" = "--uninstall" ]; then
  say "卸载外部巡检 timer"
  sudo systemctl disable --now signalbot-watch.timer 2>/dev/null || true
  sudo rm -f "$WATCH_UNIT" "$WATCH_TIMER"
  sudo systemctl daemon-reload
  say "卸载 logrotate 配置"
  sudo rm -f "$LOGROTATE"
  say "停掉 pm2 里的机器人"
  pm2 delete dryrun-bot2 2>/dev/null || true
  say "完成（.env / runtime_config.json / v21/ 一律没动）"
  exit 0
fi

# ---------------------------------------------------------------- 前提
[ -f "$REPO/requirements.txt" ] || die "找不到 $REPO/requirements.txt"
[ -d "$REPO/src" ] || die "找不到 $REPO/src —— 请在仓库目录里跑本脚本"
say "仓库=$REPO  运行根=$BASE"

# ---------------------------------------------------------------- 1) 依赖
if [ ! -x "$PY" ]; then
  say "1/5 建 venv"
  python3 -m venv "$BASE/venv"
fi
say "1/5 安装依赖（requirements.txt）"
"$BASE/venv/bin/pip" install -q -r "$REPO/requirements.txt"
say "      playwright 浏览器（只有回退 browser 模式才用）—— 装了不影响 API 模式"
"$BASE/venv/bin/playwright" install chromium >/dev/null 2>&1 || \
  say "     ⚠️ chromium 安装失败/跳过：只有回退 fetch_mode=browser 时会受影响"

# ---------------------------------------------------------------- 2) 配置自检
say "2/5 检查 .env"
if [ ! -f "$BASE/.env" ]; then
  die "缺少 $BASE/.env —— 先 cp .env.example .env 并填上密钥（缺必备项机器人会拒绝启动）"
fi
chmod 600 "$BASE/.env" 2>/dev/null || true
"$PY" - <<'EOF' || die "必备项检查未通过（见上面的缺失清单）"
import sys, os
sys.path.insert(0, os.path.join(os.environ.get("SIGNAL_BOT_BASE", "/home/ubuntu/signal-bot"), "src"))
import config
miss = config.missing_required(live=True, fetch_mode="api")
for k, why in miss:
    print("   缺 %-22s %s" % (k, why))
sys.exit(1 if miss else 0)
EOF

# ---------------------------------------------------------------- 3) logrotate
say "3/5 安装 logrotate（run.log 轮转）"
sed "s#/home/ubuntu/signal-bot#$BASE#g" "$REPO/deploy/logrotate-signal-bot" | sudo tee "$LOGROTATE" >/dev/null
sudo chmod 644 "$LOGROTATE"
sudo logrotate -d "$LOGROTATE" >/dev/null 2>&1 && say "     干跑通过" || say "     ⚠️ 干跑有警告，请手工看：sudo logrotate -d $LOGROTATE"

# ---------------------------------------------------------------- 4) 巡检 timer
say "4/5 安装外部巡检 timer（每 5 分钟）"
sudo cp "$REPO/deploy/signalbot-watch.service" "$WATCH_UNIT"
sudo cp "$REPO/deploy/signalbot-watch.timer"   "$WATCH_TIMER"
sudo chmod 644 "$WATCH_UNIT" "$WATCH_TIMER"
sudo systemctl daemon-reload
"$PY" "$REPO/tools/log_watch.py" --reset || true     # 游标对齐到当前日志末尾
sudo systemctl enable --now signalbot-watch.timer
sudo systemctl start signalbot-watch.service || true
say "     最近一次巡检结果: $(systemctl show -p Result --value signalbot-watch.service)"

# ---------------------------------------------------------------- 5) pm2
if [ "$1" = "--no-pm2" ]; then
  say "5/5 跳过 pm2（--no-pm2）"
else
  say "5/5 配置 pm2（崩溃自启 + 开机自启）"
  command -v pm2 >/dev/null 2>&1 || die "没装 pm2：npm i -g pm2"
  mkdir -p "$BASE/v21"
  pm2 delete dryrun-bot2 2>/dev/null || true
  pm2 start "$REPO/deploy/ecosystem.config.js"
  pm2 save
  say "     开机自启（需要一次 sudo）"
  sudo env PATH="$PATH" pm2 startup systemd -u "$(whoami)" --hp "$HOME" >/dev/null 2>&1 || \
    say "     ⚠️ 开机自启没配上，请手工跑：pm2 startup"
fi

say "============================"
say "完成。验证："
say "  pm2 list                                  # 机器人是否 online"
say "  tail -20 $BASE/v21/run.log                 # 看启动日志（必备项检查在这里）"
say "  systemctl list-timers signalbot-watch.timer"
say "  $PY $REPO/tools/verify_api_mode.py         # 取消息层一跳校验"
