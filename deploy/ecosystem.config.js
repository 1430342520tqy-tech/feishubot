module.exports = {
  apps: [
    {
      name: "dryrun-bot2",
      cwd: "/home/ubuntu/signal-bot",
      // 用 venv 里的 python 直接跑脚本（不经 pm2 的 node 解释器）
      script: "./venv/bin/python",
      args: "-u src/dryrun_bot2.py",
      interpreter: "none",
      // 崩溃自动拉起
      autorestart: true,
      restart_delay: 5000,
      // 启动前必备项检查失败会 exit(2)：如果反复失败，说明配置有问题，
      // 不要让 pm2 无限重启刷日志（20 次后停下来，让外部巡检报警）
      max_restarts: 20,
      // 日志交给机器人自己的 v21/run.log；pm2 的另存一份短的便于看崩溃原因
      output: "/home/ubuntu/signal-bot/v21/pm2.out.log",
      error: "/home/ubuntu/signal-bot/v21/pm2.err.log",
      merge_logs: true,
      time: true,
      env: {
        SIGNAL_BOT_BASE: "/home/ubuntu/signal-bot",
        // 不给浏览器留机会；API 模式本来就不需要浏览器
        DISPLAY: "",
      },
    },
  ],
};
