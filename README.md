# SPX 0DTE 垂直价差研究原型

当前代码**只输出候选信号和退出判断，不会连接 Robinhood 下单**。尚未完成真实行情与订单接口验收，也没有可证明的胜率或日收益。`$100–200/天`不是系统保证或仓位目标。

## 策略版本 0.2

- 只研究 `SPX` 的当日到期 `SPXW`，美东 10:00–11:30 ET 入场，13:00 ET 退出。
- 用户提供的 A–E 是看涨突破结构；F 仅适用于个股，SPX 禁用。A–E 通过时否决 Call Credit。Put Credit 还需两个已完成 5 分钟 K 线形成向上确认及回踩，且卖出腿在回踩低点和现价一倍 5 分钟波幅以下。
- 5 分钟波幅按参考图定义为最近 14 根 5 分钟 K 线高低差的**中位数**，不是 Wilder ATR。
- Heatmap 的占比按当前可见的 0DTE Top 节点绝对 GEX 作分母，30 分钟变化只对两次快照共有节点计算。共同节点覆盖任何一次快照绝对 GEX 不到 80% 时不交易。这个测量仍受 Top 截断偏差影响，不能当作全市场占比。
- 正负 GEX 是对冲方式估计，不单独决定价格方向。A–E 是未验证的研究假设。
- 初始实盘设计上限：每次一组 5/10 点价差，整个策略同时只允许一组；单组最大理论亏损不超过 $500，账户内至少保留 $1,000 的策略专用现金。日内亏损达到 $250 停止新仓；不补仓、不滚仓、不反手追损。
- 卖方价差使用实际可执行报价计算：开仓净 credit = 短腿 bid − 长腿 ask；平仓净 debit = 短腿 ask − 长腿 bid。价差浮亏达每组 $125、现货失效、回购价格降至开仓 credit 的 50%，或 13:00 ET 时触发退出判断。滑点与跳价可能使实际亏损超过预设线。

## 运行测试

```bash
python3 -m unittest -v test_strategy.py
python3 spx_vertical.py /path/to/fixture.json
```

`fixture.json` 包含 `bars`（至少 15 根、每根含 `end_at/open/high/low/close`）、`session_high_usd`（当日真实最高价）、`heatmap_30m_ago`、`heatmap_now`、`side`、`quote`、`sleeve_equity_usd`、`day_pnl_usd`。快照使用 Nightwatch `GET /v1/derived/heatmap/SPX/snapshot` 格式。测试文件 `fixture()` 是最小完整样例。所有时间必须有时区。

`nightwatch_client.py` 是只读 REST 客户端，用 `YEHANGSHE_API_KEY` 环境变量鉴权，支持 `discover` 与单次 Heatmap snapshot，处理 429 `Retry-After`。仓库不保存 Key。当前桌面运行环境没有该变量，尚未完成实测连通和配额读取。

## Discord 通知

`discord_notifier.py` 从进程环境读取 `DISCORD_SPX_WEBHOOK_URL`，只接受 Discord 官方 Webhook URL。输入 JSON 事件的 `event_type` 支持 `plan`、`signal`、`submitted`、`filled`、`exit_signal`、`exit_fill`、`fault`、`report`；每条需要 `session_date_et` 和 `signal_id`。`filled` 与 `exit_fill` 还必须提供 `broker_order_id`，以免把信号写成成交。

```bash
python3 discord_notifier.py /path/to/event.json            # 只预览消息
python3 discord_notifier.py /path/to/event.json --send     # 实际发送
python3 discord_notifier.py --test-connection              # 在本机终端隐藏粘贴 Webhook，发送一条无交易测试
```

实际发送前，在运行该命令的进程环境中安全设置 `DISCORD_SPX_WEBHOOK_URL`；不要把完整 URL 放入命令历史、仓库或日志。通知状态保存在被 Git 忽略的 `state/discord_events.sqlite3`，同一交易日、信号 ID、事件类型最多自动发送一次。超时会标记为“发送结果不确定”，需要先检查 Discord 频道，再人工决定是否重发。Discord 429 按返回的 `Retry-After` 等待后，由上层调度器重试。当前没有后台调度器，因此不会自动监控或自动发布交易信号。

若 Discord 明确返回 4xx，状态会记录为 `rejected_状态码`，再次手动运行测试可重试；网络超时或 5xx 仍视为结果不确定。错误输出只显示 HTTP 状态与 Discord 数字错误码，不显示 Webhook 或原始响应。Discord `40333` 通常指 Cloudflare 拒绝请求；`50001` 为缺少访问权限；`50027` 为无效 Webhook token。先依据错误码处理，再重试，避免反复触发无效请求限制。

## 实盘启动门槛

1. 在实际运行器验收 SPX 0DTE Heatmap 的有效到期、时间戳与 SPX 5 分钟 K 线数据源。
2. 验证 Nightwatch Key 的 `derived:read` 报价权限；历史先回放，随后至少一个观察期只发信号。数据 API 未公开 Nightwatch 模拟下单接口。
3. 验证 Robinhood 可交易账户、组合单预览、开平仓订单回报、未成交订单查询和紧急平仓。订单应提交完整价差，不单腿平仓。
4. 验证持久运行器、断线报警、Discord 消息去重、仓位与日亏损持久化。没有这些条件，不能无人值守自动开仓。

本仓库不包含 Key、Webhook 或账户数据。克隆仓库的人必须自行配置凭证及交易权限；克隆代码本身不会授权下单。

## 参考

- [Nightwatch Heatmap API](https://docs.yehangshe.com/api/dealer-heatmap-snapshot)
- [Nightwatch options 报价与权限](https://docs.yehangshe.com/api/domains/options)
- [Nightwatch 配额与限速](https://docs.yehangshe.com/api/quota-and-rate-limits)
- [Robinhood 价差平仓说明](https://robinhood.com/us/en/support/articles/advanced-options-strategies/)
