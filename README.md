# feishubot — 飞书 KOL 信号 → 币安合约 自动跟单机器人

从飞书群里的 KOL 消息（**文字 + K 线图**）自动解析出「币种 / 方向 / 入场 / 加仓 / 止损 / 多档止盈」，
按固定资金规则生成下单计划，并推送到飞书。**当前为 dryRun（纸面）模式，不会真实下单。**

---

## 核心能力

| 能力 | 说明 |
|---|---|
| **消息级抓取** | 遍历飞书消息 DOM 节点逐条取，不再把整页文字倒出来（旧做法会串群、丢时间戳） |
| **精确时间戳** | 飞书 message-id 高 32 位 = Unix 秒：`时间 = id >> 32`，与页面显示逐条一致 |
| **图表抓取** | 图片惰性加载 → 等 `naturalWidth>0` 再抓；blob 图直接 `fetch → base64` 存 PNG |
| **图表读取** | 像素级定位色块 → 逐块放大 OCR → 按区域规则判类型（见下） |
| **只抓开单信号** | 闲聊一律跳过；博主管理指令（平仓/减仓/移保本）单独处理 |
| **全链路计时** | 信号发出 → 发现 → 抓图 → 解析 → 读图 → 下单 → 推送，逐段计时并随通知推送 |
| **多群稳定监控** | 每个群一个独立标签页，开完不再切换会话，杜绝"读错群" |

## 读图规则（关键）

KOL 在 TradingView 图上画的是：
- **红色横线 / 暗红区间** = 止损位（在开仓价下方）
- **开仓价** = 止损上方第一个价格标签
- **止盈** = 开仓价上方的**实线**横线（不限颜色），按你给的规则**只取前 3 档**，第 4 档忽略

判定"是不是止盈线"用的是**横线像素覆盖率**：
- 止盈线是实线 → 覆盖率 ≈ 90~100%
- 价格轴数字 / 当前价虚线 / 开仓标记 → 覆盖率 < 60%

这条判据能把「坐标轴数字」「当前价格线」这些干扰项自动剔除——这是早期读错止盈的主要根因。

## 交易规则（可配）

- 单笔 **保证金 300 USDT × 3 倍 = 名义 900 USDT**
- 博主给了加仓价 → 头仓 1/3 市价 + 加仓 2/3 挂在加仓价；没给 → 一次性满仓
- 开仓门槛：博主点位与现价差 **>1% 就等**，最多 30 分钟，始终不进则不开
- 止盈每档平 1/3；**TP1 成交后止损移到开仓价（保本）**
- 最多同时 3 笔

## 目录结构

```
src/
  dryrun_bot2.py      当前使用的机器人（每群独立标签页 + 全链路计时）
  dryrun_bot_v1.py    早期版本（单页面切换会话，供对比参考）
tools/
  read_chart_final.py 图表读取（像素定位 + 逐块 OCR + 覆盖率判据）
  tags_v26.py         色块矩形检测 + 分块识别的算法原型
  scrape_v19.py       群历史抓取（消息级，含图表下载）
  scrape_v16.py       抓取算法原型（媒体元素扫描）
  calib_tags.py       色块颜色标定工具
config/
  config.example.json 配置模板（API Key、通知 Webhook、监控群）
docs/
  交接文档.md          项目背景、已完成/未完成、坑与结论
```

## 部署

```bash
# 1) 依赖
python3 -m venv venv && ./venv/bin/pip install playwright ccxt requests pillow
./venv/bin/playwright install chromium

# 2) 配置
cp config/config.example.json config.json   # 填入 API Key / Webhook / 监控群
export DEEPSEEK_API_KEY=sk-xxx              # 或写进 config.json

# 3) 首次登录飞书（服务器上开一次浏览器扫码/登录，登录态存到 profile 目录）
./venv/bin/python tools/scrape_v19.py

# 4) 启动机器人（纸面模式）
pm2 start ./venv/bin/python --name dryrun-bot -- -u src/dryrun_bot2.py
pm2 logs dryrun-bot
```

## 运行环境注意事项

- 服务器 **2C4G**，5 个飞书标签页约占 3GB 内存 → **务必加 swap**（否则页面会崩）
  ```bash
  sudo fallocate -l 4G /swapfile && sudo chmod 600 /swapfile
  sudo mkswap /swapfile && sudo swapon /swapfile
  echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab
  ```
- 一个浏览器 profile 只能被一个进程使用，别同时跑两个抓取脚本
- 飞书网页自动化用的是**真实账号**，有风控风险

## 已知限制

1. **真实下单层尚未实现**（当前 dryRun，只生成计划与推送）
2. 博主"规则止损"（如 4H 收盘跌破 X）目前按该价位的硬止损近似处理
3. 同一张图上的价格标签 OCR 偶有 0.0x% 级误差（挂单取整后通常同价）
4. 群历史抓取需要滚动翻页，图片依赖懒加载，个别老图可能抓不到

## 免责声明

本项目仅用于个人自动化研究。自动跟单有真实亏损风险，请先在纸面模式验证。
