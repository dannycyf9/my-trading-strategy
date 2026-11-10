"""Discord delivery for SPX strategy events; no secret is stored in this repo.

Use DISCORD_SPX_WEBHOOK_URL in the process environment. Sending requires the
explicit CLI --send flag. Network failures are treated as uncertain delivery
and are not blindly retried.
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import re
import sqlite3
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo


ALLOWED_EVENTS = {
    "connection_test": "连接测试｜无交易",
    "plan": "盘前计划",
    "signal": "候选信号｜未下单",
    "shadow_open": "本地模拟开仓｜假设成交，非平台订单",
    "shadow_close": "本地模拟平仓｜假设成交，非平台订单",
    "submitted": "订单已提交｜待成交",
    "filled": "券商确认成交",
    "exit_signal": "退出信号｜待执行",
    "exit_fill": "券商确认平仓",
    "fault": "数据或执行故障",
    "report": "盘后报告",
}
WEBHOOK_RE = re.compile(r"https://discord\.com/api/webhooks/[0-9]+/[A-Za-z0-9_-]+\Z")


class DeliveryError(RuntimeError):
    pass


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _open_no_redirect(request, timeout):
    return urllib.request.build_opener(_NoRedirect).open(request, timeout=timeout)


def _discord_error_code(error: urllib.error.HTTPError) -> int | None:
    try:
        data = json.loads(error.read(4096).decode("utf-8"))
        code = data.get("code")
        return code if isinstance(code, int) else None
    except (ValueError, UnicodeError, AttributeError):
        return None
    finally:
        error.close()


def build_message(event: dict) -> str:
    kind = event.get("event_type")
    if kind not in ALLOWED_EVENTS:
        raise ValueError("unknown event_type")
    signal_id = str(event.get("signal_id", ""))
    session_date = str(event.get("session_date_et", ""))
    if not signal_id or not re.fullmatch(r"20\d{2}-\d{2}-\d{2}", session_date):
        raise ValueError("signal_id and session_date_et required")
    if kind in ("filled", "exit_fill") and not event.get("broker_order_id"):
        raise ValueError("broker_order_id required for claimed fills")
    lines = [f"【SPX 研究策略｜{ALLOWED_EVENTS[kind]}】",
             f"交易日（ET）：{session_date}", f"信号 ID：{signal_id}"]
    for key, label in (
        ("strategy_version", "版本"), ("data_at", "数据时间"),
        ("direction", "方向"), ("expiration", "到期日"),
        ("short_leg", "短腿"), ("long_leg", "长腿"),
        ("quantity", "组数"), ("credit_usd", "净权利金/股"),
        ("debit_usd", "回购价/股"), ("max_loss_usd", "最大理论亏损"),
        ("realized_pnl_usd", "本地模拟盈亏/美元"),
        ("invalidation_spot", "现货失效价"), ("reason", "原因"),
        ("broker_order_id", "券商订单 ID"),
    ):
        value = event.get(key)
        if value is not None:
            lines.append(f"{label}：{value}")
    message = "\n".join(lines)
    if len(message) > 2000:
        raise ValueError("Discord message exceeds 2000 characters")
    return message


def _webhook_url() -> str:
    url = os.environ.get("DISCORD_SPX_WEBHOOK_URL", "")
    if not WEBHOOK_RE.fullmatch(url):
        raise DeliveryError("DISCORD_SPX_WEBHOOK_URL is missing or invalid")
    return url


def deliver(event: dict, *, db_path: str = "state/discord_events.sqlite3",
            opener=_open_no_redirect) -> str:
    message = build_message(event)
    url = _webhook_url()
    key = f"{event['session_date_et']}:{event['signal_id']}:{event['event_type']}"
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS deliveries "
                     "(event_key TEXT PRIMARY KEY, status TEXT NOT NULL, updated_at TEXT NOT NULL)")
        prior = conn.execute("SELECT status FROM deliveries WHERE event_key = ?", (key,)).fetchone()
        if prior and not prior[0].startswith("rejected_"):
            # 'sending' may mean the prior request reached Discord before a crash.
            return "already_recorded_or_uncertain"
        if prior:
            conn.execute("UPDATE deliveries SET status = 'sending', updated_at = ? WHERE event_key = ?",
                         (datetime.now(timezone.utc).isoformat(), key))
        else:
            conn.execute("INSERT INTO deliveries VALUES (?, 'sending', ?)",
                         (key, datetime.now(timezone.utc).isoformat()))
        conn.commit()
        body = json.dumps({"content": message, "allowed_mentions": {"parse": []}},
                          ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(url + "?wait=true", data=body,
                                         headers={"Content-Type": "application/json",
                                                  "User-Agent": "DiscordBot (https://github.com/dannycyf9/my-trading-strategy, 0.3)"},
                                         method="POST")
        try:
            with opener(request, timeout=20) as response:
                reply = json.load(response)
            if not reply.get("id"):
                raise DeliveryError("Discord response lacks message ID; delivery uncertain")
        except urllib.error.HTTPError as exc:
            code = _discord_error_code(exc)
            # Explicit client rejection means no message was accepted. Reuse this
            # event key only after an operator deliberately runs the command again.
            if 400 <= exc.code < 500:
                conn.execute("UPDATE deliveries SET status = ?, updated_at = ? WHERE event_key = ?",
                             (f"rejected_{exc.code}", datetime.now(timezone.utc).isoformat(), key))
                conn.commit()
            if exc.code == 429:
                raise DeliveryError("Discord rate-limited; respect Retry-After: " +
                                    str(exc.headers.get("Retry-After", "unknown"))) from None
            suffix = f" (Discord code {code})" if code is not None else " (no Discord JSON code)"
            if 400 <= exc.code < 500:
                raise DeliveryError(f"Discord HTTP {exc.code}{suffix}; request rejected") from None
            raise DeliveryError(f"Discord HTTP {exc.code}{suffix}; delivery uncertain") from None
        except (urllib.error.URLError, TimeoutError, ValueError) as exc:
            raise DeliveryError("Discord delivery uncertain; inspect channel before retrying") from None
        conn.execute("UPDATE deliveries SET status = 'sent', updated_at = ? WHERE event_key = ?",
                     (datetime.now(timezone.utc).isoformat(), key))
        conn.commit()
    return "sent"


def main() -> int:
    parser = argparse.ArgumentParser(description="Send or preview one SPX strategy event")
    parser.add_argument("event_json", nargs="?", help="UTF-8 JSON event file")
    parser.add_argument("--send", action="store_true", help="actually send to Discord")
    parser.add_argument("--test-connection", action="store_true",
                        help="prompt for webhook invisibly and send one no-trade test")
    args = parser.parse_args()
    try:
        if args.test_connection:
            if args.event_json:
                raise ValueError("event_json is not used with --test-connection")
            if not os.environ.get("DISCORD_SPX_WEBHOOK_URL"):
                os.environ["DISCORD_SPX_WEBHOOK_URL"] = getpass.getpass("Discord Webhook URL (hidden): ")
            today = datetime.now(ZoneInfo("America/New_York")).date().isoformat()
            event = {"event_type": "connection_test", "signal_id": "connection-test",
                     "session_date_et": today, "reason": "连接测试；无交易、无订单"}
            print(deliver(event))
            return 0
        if not args.event_json:
            raise ValueError("event_json is required unless --test-connection is used")
        with open(args.event_json, encoding="utf-8") as handle:
            event = json.load(handle)
        if args.send:
            print(deliver(event))
        else:
            print(build_message(event))
    except EOFError:
        print("Hidden input unavailable; run --test-connection in an interactive Terminal.",
              file=sys.stderr)
        return 1
    except (ValueError, DeliveryError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
