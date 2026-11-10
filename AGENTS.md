# Agent operating rules for this repository

- This is a research prototype. Do not place live or paper orders based solely on its current output. No automated order path exists yet.
- Never embed or commit Nightwatch keys, Discord webhooks, Robinhood credentials, account numbers, or execution logs with sensitive data. Read secrets from environment or an approved secret manager.
- Nightwatch REST base: `https://api.yehangshe.com`. Call `GET /v1/discover` before a live data session and verify permissions and `quota.monthly_remaining`. Do not infer a live quota from prior reports.
- Use `SPX` Heatmap data for SPX analysis; never scale SPY data into SPX. Public Heatmap contains Top nodes, not a full strike grid. A missing cell is unknown, not zero.
- Nightwatch snapshot buckets are labeled with their beginning and only available after completion. Do not use future data in replay. Snapshot reads no faster than once per second; respect account-wide 60 RPM, quota weights, and 429 `Retry-After`.
- Price bars, option quotes, and orders need explicit timestamps. Stale or incomplete inputs mean no new trade. Any already open position requires urgent operator attention when data fails.
- Strategy risk sleeve is $5,000 maximum with a $1,000 reserve. Initial design is at most one spread at a time and one contract. No averaging down, same-day revenge reversal, or guaranteed daily profit target.
- `spx_vertical.py` is a signal evaluator and `spread_exit.py` is an exit decision function. Neither is a broker executor. Before adding live execution, implement durable state, idempotent order reconciliation, full spread orders, fill confirmation, disconnect recovery, and integration tests with real broker previews.
- Publish source only after secret scan. Cloning this repository never grants API or brokerage credentials.
- Discord delivery is opt-in through `DISCORD_SPX_WEBHOOK_URL` and `discord_notifier.py --send`. Never label a signal as a fill without a broker order ID. Keep the ignored `state/discord_events.sqlite3` when running to prevent duplicate messages.
