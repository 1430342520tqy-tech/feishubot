#!/bin/bash
# B2 安装脚本：外部日志巡检 timer
# 作用：每 5 分钟跑一次 log_watch.py（只读巡检），发现**新增**故障行 / 心跳停摆 /
#       进程消失 / state.json 停更 → 推飞书告警。不依赖机器人进程活着。
#
# 幂等：可重复执行。
# 不动机器人：不重启 pm2、不改 config.json / runtime_config.json / notify.json。
#
# 用法：bash deploy/install-watch.sh            （在本机仓库目录里跑，需 scp 到服务器）
#       bash deploy/install-watch.sh --uninstall
set -e
PY=/home/ubuntu/signal-bot/venv/bin/python
LW=/home/ubuntu/signal-bot/log_watch.py
UNIT=/etc/systemd/system/signalbot-watch.service
TIMER=/etc/systemd/system/signalbot-watch.timer

if [ "$1" = "--uninstall" ]; then
  sudo systemctl disable --now signalbot-watch.timer || true
  sudo rm -f "$UNIT" "$TIMER"
  sudo systemctl daemon-reload
  echo "已卸载 signalbot-watch.timer"
  exit 0
fi

for f in "$LW"; do
  [ -f "$f" ] || { echo "[FAIL] 缺少 $f"; exit 1; }
done
[ -f /tmp/signalbot-watch.service ] || { echo "[FAIL] 缺少 /tmp/signalbot-watch.service"; exit 1; }
[ -f /tmp/signalbot-watch.timer ]   || { echo "[FAIL] 缺少 /tmp/signalbot-watch.timer"; exit 1; }

echo "--- 1) 语法自检 ---"
$PY -m py_compile "$LW" && echo "  py_compile OK"

echo "--- 2) 把游标对齐到当前日志末尾（避免刚启用就把历史故障当成新增）---"
$PY "$LW" --reset

echo "--- 3) 安装 unit ---"
sudo cp /tmp/signalbot-watch.service "$UNIT"
sudo cp /tmp/signalbot-watch.timer   "$TIMER"
sudo chmod 644 "$UNIT" "$TIMER"
sudo systemctl daemon-reload

echo "--- 4) 启用并立即触发一次（验证能跑通）---"
sudo systemctl enable --now signalbot-watch.timer
sudo systemctl start signalbot-watch.service
sleep 3
echo "  service 结果: $(systemctl show -p Result --value signalbot-watch.service)  (success=正常)"
echo "  最近日志:"
sudo journalctl -u signalbot-watch.service --no-pager | tail -5

echo "--- 5) 定时器状态 ---"
systemctl list-timers signalbot-watch.timer --no-pager
echo
echo "完成。手动排查命令："
echo "  sudo systemctl start signalbot-watch.service      # 立刻跑一次"
echo "  journalctl -u signalbot-watch.service -n 30       # 看输出"
echo "  $PY $LW --status                                   # 看游标/冷却"
echo "  $PY $LW --log /tmp/x.log --cursor /tmp/y.json --dry-run   # 隔离验证"
