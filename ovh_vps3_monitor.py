#!/usr/bin/env python3
"""Poll OVHcloud's public VPS-3 stock API and send Telegram alerts.

Python 3.10+, standard library only. Run --setup, then --test-notification,
then run without flags. --check is read-only; --once is suitable for cron.
"""

import argparse
import getpass
import hashlib
import hmac
import http.client
import json
import logging
import os
from pathlib import Path
import random
import re
import socket
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

ROOT = Path(__file__).resolve().parent
DEFAULTS = {
    "subsidiary": "IE",
    "plan_codes": None,
    "os_family": "linux",
    "storage": "any",
    "interval_seconds": 300,
    "timeout_seconds": 20,
    "telegram_bot_token": "",
    "telegram_chat_id": "",
    "buy_url": "https://www.ovhcloud.com/en/vps/configurator/",
}
OVH_BASE = "https://eu.api.ovh.com/1.0"
TELEGRAM_BASE = "https://api.telegram.org"
STATUSES = {"available", "out-of-stock", "out-of-stock-preorder-allowed"}
LOG = logging.getLogger("ovh-vps3")


class MonitorError(Exception):
    def __init__(self, message, retry_after=0):
        super().__init__(message)
        self.retry_after = retry_after


def request_json(url, timeout, payload=None):
    headers = {"User-Agent": "OVH-VPS3-stock-monitor/1.0", "Accept": "application/json"}
    data = None
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=data, headers=headers)
    # Never include the URL in errors: Telegram embeds the bot token in it.
    provider = "Telegram" if "/bot" in url else "OVH"
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.load(response)
    except urllib.error.HTTPError as exc:
        retry_after = 0
        try:
            retry_after = max(0, int(exc.headers.get("Retry-After", "0")))
            body = json.loads(exc.read())
            if isinstance(body, dict) and isinstance(body.get("parameters"), dict):
                retry_after = max(retry_after, int(body["parameters"].get("retry_after", 0)))
        except (ValueError, TypeError, OSError):
            pass
        raise MonitorError(f"{provider} returned HTTP {exc.code}", retry_after) from None
    except (urllib.error.URLError, TimeoutError, socket.timeout, OSError, http.client.HTTPException):
        raise MonitorError(f"{provider} connection failed or timed out") from None
    except (ValueError, UnicodeError):
        raise MonitorError(f"{provider} returned invalid JSON") from None


def atomic_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    # Restrict permissions on Unix, including the token-containing config.
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        json.dump(value, stream, indent=2)
        stream.write("\n")
    temporary.replace(path)


def load_config(path):
    config = DEFAULTS.copy()
    if path.exists():
        try:
            value = json.loads(path.read_text(encoding="utf-8-sig"))
            if not isinstance(value, dict):
                raise ValueError
            config.update(value)
        except (ValueError, UnicodeError):
            raise MonitorError("Config must be a valid JSON object") from None
    config["telegram_bot_token"] = os.getenv("TELEGRAM_BOT_TOKEN") or config["telegram_bot_token"]
    config["telegram_chat_id"] = os.getenv("TELEGRAM_CHAT_ID") or config["telegram_chat_id"]
    if not isinstance(config["subsidiary"], str) or not re.fullmatch(r"[A-Z]{2}", config["subsidiary"]):
        raise MonitorError("subsidiary must be an OVH storefront code, such as IE, UK or FR")
    if config["os_family"] not in ("linux", "windows", "any"):
        raise MonitorError("os_family must be linux, windows or any")
    if config["storage"] not in ("any", "local", "remote"):
        raise MonitorError("storage must be any, local or remote")
    for key, minimum in (("interval_seconds", 60), ("timeout_seconds", 1)):
        if type(config[key]) not in (int, float) or not minimum <= config[key] <= 86400:
            raise MonitorError(f"{key} must be a number between {minimum} and 86400")
    plans = config["plan_codes"]
    if plans is not None and (
        not isinstance(plans, list) or not plans
        or not all(isinstance(p, str) and re.fullmatch(r"vps-20\d{2}-model3", p) for p in plans)
    ):
        raise MonitorError("plan_codes must be null (auto-discover) or a list of VPS-3 codes")
    for key in ("telegram_bot_token", "telegram_chat_id", "buy_url"):
        if not isinstance(config[key], str):
            raise MonitorError(f"{key} must be a string")
    if not config["buy_url"].startswith("https://"):
        raise MonitorError("buy_url must start with https://")
    return config


def require_telegram(config):
    if not re.fullmatch(r"\d+:[A-Za-z0-9_-]+", config["telegram_bot_token"]):
        raise MonitorError("Set your Telegram bot token with --setup or TELEGRAM_BOT_TOKEN")
    if not config["telegram_chat_id"].strip():
        raise MonitorError("Set your Telegram chat ID with --setup or TELEGRAM_CHAT_ID")


def telegram(config, method, payload=None):
    url = f'{TELEGRAM_BASE}/bot{config["telegram_bot_token"]}/{method}'
    result = request_json(url, config["timeout_seconds"], payload)
    if not isinstance(result, dict) or result.get("ok") is not True:
        raise MonitorError("Telegram did not acknowledge the request")
    return result.get("result")


def send_message(config, message):
    telegram(config, "sendMessage", {
        "chat_id": config["telegram_chat_id"], "text": message,
        "disable_web_page_preview": True,
    })


def discover_plans(config):
    if config["plan_codes"] is not None:
        return sorted(set(config["plan_codes"]))
    query = urllib.parse.urlencode({"ovhSubsidiary": config["subsidiary"]})
    catalog = request_json(f"{OVH_BASE}/order/catalog/public/vps?{query}", config["timeout_seconds"])
    if not isinstance(catalog, dict) or not isinstance(catalog.get("plans"), list):
        raise MonitorError("Unexpected OVH catalogue format")
    plans = sorted({
        p["planCode"] for p in catalog["plans"]
        if isinstance(p, dict) and isinstance(p.get("planCode"), str)
        and re.fullmatch(r"vps-20\d{2}-model3", p["planCode"])
    })
    if not plans:
        raise MonitorError("No VPS-3 plans in this storefront's catalogue; check subsidiary")
    return plans


def check_stock(config, plans):
    query = urllib.parse.urlencode({"ovhSubsidiary": config["subsidiary"], "planCodes": ",".join(plans)})
    result = request_json(f"{OVH_BASE}/vps/order/rule/datacenter/aggregated?{query}", config["timeout_seconds"])
    if not isinstance(result, dict) or not isinstance(result.get("models"), list):
        raise MonitorError("Unexpected OVH stock format; retaining previous alert state")
    field = {"linux": "linuxStatus", "windows": "windowsStatus", "any": "status"}[config["os_family"]]
    found = set()
    available = {}
    location_count = 0
    for model in result["models"]:
        if not isinstance(model, dict) or model.get("planCode") not in plans:
            raise MonitorError("Unexpected plan in OVH stock response")
        plan = model["planCode"]
        if plan in found or not isinstance(model.get("datacenters"), list):
            raise MonitorError("Unexpected OVH datacenter list")
        found.add(plan)
        for dc in model["datacenters"]:
            if (not isinstance(dc, dict) or not isinstance(dc.get(field), str) or dc[field] not in STATUSES
                or not isinstance(dc.get("code"), str) or not dc["code"]
                or not isinstance(dc.get("datacenter"), str)
                or type(dc.get("remoteStorage")) is not bool):
                raise MonitorError("Unexpected OVH datacenter fields; retaining previous alert state")
            storage = "remote" if dc["remoteStorage"] else "local"
            if config["storage"] not in ("any", storage):
                continue
            location_count += 1
            if dc[field] == "available":
                key = f'{plan}|{dc["code"]}|{storage}'
                available[key] = {"plan": plan, "storage": storage, **dc}
    if found != set(plans):
        raise MonitorError("OVH omitted a requested plan; retaining previous alert state")
    return available, location_count


def buy_link(config, plan):
    url = urllib.parse.urlsplit(config["buy_url"])
    query = dict(urllib.parse.parse_qsl(url.query))
    query["planCode"] = plan
    return urllib.parse.urlunsplit(url._replace(query=urllib.parse.urlencode(query)))


def alert_text(config, entries):
    lines = [f'OVHcloud VPS-3 available! ({config["subsidiary"]}, {config["os_family"]})']
    for entry in entries:
        lines.append(f'- {entry["datacenter"]} ({entry["code"]}), {entry["storage"]} storage, {entry["plan"]}')
    for plan in sorted({e["plan"] for e in entries}):
        lines.extend(("", buy_link(config, plan)))
    lines.extend(("", "Stock can change before checkout. Verify the location and storage option when ordering."))
    return "\n".join(lines)


def load_state(path):
    if not path.exists():
        return {}
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(state, dict) or not all(
            isinstance(v, list) and all(isinstance(k, str) for k in v) for v in state.values()
        ):
            raise ValueError
        return state
    except (ValueError, UnicodeError):
        raise MonitorError("Invalid state file. Rename it to start fresh (may repeat an alert)") from None


def state_scope(config):
    """Identify an alert recipient without publishing the Telegram chat ID."""
    metadata = json.dumps([config["subsidiary"], config["os_family"], config["storage"], config["telegram_chat_id"]])
    digest = hmac.new(config["telegram_bot_token"].encode(), metadata.encode(), hashlib.sha256).hexdigest()
    return "v2:" + digest


def process_stock(config, state, scope, available, state_path):
    previous = set(state.get(scope, []))
    new_keys = sorted(set(available) - previous)
    # Chunk before Telegram's 4096-character limit. Save only after all chunks
    # are acknowledged; a failed/ambiguous delivery is retried next poll.
    for offset in range(0, len(new_keys), 8):
        entries = [available[k] for k in new_keys[offset:offset + 8]]
        send_message(config, alert_text(config, entries))
    updated = {**state, scope: sorted(available)}
    atomic_json(state_path, updated)
    state.clear()
    state.update(updated)
    if new_keys:
        LOG.info("Telegram alert delivered for %d new availability options", len(new_keys))


def retry_delay(interval, failures, retry_after=0):
    return max(retry_after, min(1800, interval * 2 ** min(failures - 1, 8))) + random.uniform(0, 5)


def setup(config_path):
    config = load_config(config_path)
    print("Create a Telegram bot with @BotFather using /newbot, then send /start to your bot.")
    token = getpass.getpass("Bot token (hidden; Enter keeps existing): ").strip()
    if token:
        config["telegram_bot_token"] = token
    if not re.fullmatch(r"\d+:[A-Za-z0-9_-]+", config["telegram_bot_token"]):
        raise MonitorError("A valid Telegram bot token is required")
    bot = telegram(config, "getMe")
    if not isinstance(bot, dict):
        raise MonitorError("Unexpected Telegram bot details")
    print(f'Connected to @{bot.get("username", "your bot")}')
    chat = input("Chat ID (Enter to find your recent private /start message): ").strip()
    if not chat:
        updates = telegram(config, "getUpdates")
        if not isinstance(updates, list):
            raise MonitorError("Unexpected Telegram update format")
        chats = {}
        for update in updates:
            if not isinstance(update, dict):
                continue
            message = update.get("message", {})
            if not isinstance(message, dict):
                continue
            item = message.get("chat", {})
            if not isinstance(item, dict):
                continue
            if item.get("type") == "private":
                chats[str(item["id"])] = item.get("first_name", "Private chat")
        if len(chats) == 1:
            chat, name = next(iter(chats.items()))
            print(f"Found chat: {name} ({chat})")
        else:
            for chat_id, name in chats.items():
                print(f"  {chat_id}: {name}")
            chat = input("Send /start to the bot if needed, then enter your chat ID: ").strip()
    if not re.fullmatch(r"-?\d+", chat):
        raise MonitorError("No chat ID found. Send /start to your bot and rerun --setup")
    config["telegram_chat_id"] = chat
    subsidiary = input(f'OVH storefront code [{config["subsidiary"]}; IE=Worldwide, UK=UK, FR=France]: ').strip().upper()
    if subsidiary:
        config["subsidiary"] = subsidiary
    atomic_json(config_path, config)
    load_config(config_path)
    print(f"Saved {config_path}. Run --test-notification, then run without flags.")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--config", type=Path, default=ROOT / "config.json")
    parser.add_argument("--state", type=Path, default=ROOT / "state.json")
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--setup", action="store_true", help="configure Telegram interactively")
    modes.add_argument("--check", action="store_true", help="one OVH check, no Telegram or state changes")
    modes.add_argument("--once", action="store_true", help="check and alert once, then exit (cron)")
    modes.add_argument("--test-notification", action="store_true", help="send a Telegram setup test")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        if args.setup:
            setup(args.config)
            return 0
        config = load_config(args.config)
        if not args.check:
            require_telegram(config)
        if args.test_notification:
            send_message(config, "OVH VPS-3 monitor test: Telegram notifications are working.")
            print("Test delivered. Start the monitor without flags.")
            return 0
        state = {} if args.check else load_state(args.state)
        if not args.check:
            atomic_json(args.state, state)  # Verify state storage before sending.
        scope = state_scope(config)
        plans, refreshed, failures = [], 0, 0
        LOG.info("Checking all datacenters; storefront=%s OS=%s storage=%s interval=%ss",
                 config["subsidiary"], config["os_family"], config["storage"], config["interval_seconds"])
        while True:
            try:
                if not plans or time.monotonic() - refreshed >= 21600:
                    plans = discover_plans(config)
                    refreshed = time.monotonic()
                    LOG.info("Watching plans: %s", ", ".join(plans))
                available, count = check_stock(config, plans)
                LOG.info("%d available options / %d checked", len(available), count)
                if args.check:
                    print(alert_text(config, list(available.values())) if available else "All checked locations are out of stock.")
                    return 0
                process_stock(config, state, scope, available, args.state)
                if args.once:
                    return 0
                failures = 0
                delay = config["interval_seconds"] + random.uniform(0, 5)
            except (MonitorError, OSError) as exc:
                if args.once or args.check:
                    raise
                failures += 1
                delay = retry_delay(config["interval_seconds"], failures, getattr(exc, "retry_after", 0))
                LOG.error("%s; retry in %.0fs (failure %d)", str(exc) if isinstance(exc, MonitorError)
                          else "Unable to save local state", delay, failures)
            time.sleep(delay)
    except KeyboardInterrupt:
        print("\nMonitor stopped.")
        return 0
    except (MonitorError, OSError) as exc:
        LOG.error("%s", str(exc) if isinstance(exc, MonitorError) else "Unable to read/write a local file")
        return 1


if __name__ == "__main__":
    sys.exit(main())
