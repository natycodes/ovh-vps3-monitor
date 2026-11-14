# OVH VPS-3 monitor

Checks OVHcloud VPS-3 stock on **GitHub Actions every five minutes**, and sends **Telegram alerts** when a matching location becomes available. GitHub hosts the checks, so your PC can be switched off.

The structure follows [job-radar](https://github.com/natycodes/job-radar): scheduled Python checks, a manual run button, and persistent results committed to the repository. No OVH credentials or Python packages are needed.

## Add Telegram secrets securely

Open [Settings → Secrets and variables → Actions](https://github.com/natycodes/ovh-vps3-monitor/settings/secrets/actions), choose **New repository secret**, and add:

| Name | Value |
| --- | --- |
| `TELEGRAM_BOT_TOKEN` | Token from [@BotFather](https://t.me/BotFather) |
| `TELEGRAM_CHAT_ID` | ID of the private chat where your bot should send alerts |

Create a dedicated bot with `/newbot`, then send `/start` to your new bot. If you need your chat ID, run `run.ps1 --setup` locally: it finds your bot's recent private chat and prints the ID. Local `config.json` is gitignored. Copy its values into GitHub Secrets, never into a tracked file.

GitHub encrypts secrets and supplies them to selected workflow steps at runtime. Public readers cannot see their values, and they cannot be read back from Settings after saving. The workflow does not print them; the HTTP client omits token-containing URLs from errors. Official actions are pinned to verified commits.

Keep credentials out of source, Issues, commit messages and chat. Limit repository write access to people you trust: a person who can edit a trusted workflow can change how it uses its secrets. This workflow runs on the default branch's schedule, manual dispatches and trusted pushes to `main`; it has no pull-request trigger.

The public `data/state.json` holds only a keyed hash of the notification configuration and already announced plan/location/storage options. It contains no token or chat ID. Changing the bot token or recipient creates a new alert scope and may announce current stock again.

## Test and run

Open [Actions → Check OVH VPS-3 stock](https://github.com/natycodes/ovh-vps3-monitor/actions/workflows/monitor.yml), click **Run workflow**, and choose a mode:

| Mode | Behavior |
| --- | --- |
| `test-telegram` | Send a setup test without altering stock state |
| `monitor` | Check stock, announce new availability, and save state |
| `check` | Print stock without Telegram or state changes |

First run `test-telegram` and confirm the message arrives. Then run `monitor` for an immediate check. The five-minute schedule continues automatically.

Without both Telegram secrets, scheduled runs still check OVH and show stock in the run log. A notice explains which secrets to add. Notifications become active on the next run after both secrets are saved. Invalid configured credentials cause the notifying step to fail visibly in Actions.

GitHub can delay or drop scheduled runs under load; the five-minute schedule is a requested cadence, not an exact timing guarantee. The workflow must exist on the default branch. GitHub can disable schedules in a public repository after 60 days without repository activity; re-enable the workflow in Actions if that happens. Standard GitHub-hosted runners for public repositories are free under GitHub's standard Actions policy.

## Configure stock checks

Edit and commit `config/monitor.json`:

- `subsidiary`: `IE` by default, OVH's Worldwide storefront. Other examples: `UK`, `FR`, `DE`. This is the storefront you can use for checkout, not the datacenter country. The script uses OVH's European/global API; US accounts require its separate US API/storefront.
- `plan_codes`: `null` discovers standard `vps-YYYY-model3` products. On 14 November 2026 the catalogue included `vps-2025-model3` and `vps-2027-model3`; the current Worldwide VPS page linked to the latter. Set a one-element list such as `["vps-2027-model3"]` to watch just one.
- `os_family`: `linux` by default; accepts `windows` or `any`. Windows-only stock does not generate a Linux alert. A specific Linux image may still need checking at checkout.
- `storage`: `any` checks local and remote storage separately; accepts `local` or `remote`. Alerts identify the available storage option.
- `buy_url`: configurator URL for your storefront. The matching plan code is appended automatically. For the UK storefront use `https://www.ovhcloud.com/en-gb/vps/configurator/`.

Only exact API status `available` triggers an alert; preorders are excluded. Every returned location is checked. Select the stated location and storage option at checkout and check the price/specifications. API stock can change before checkout; this monitor does not reserve or buy a VPS.

To change the cloud interval, edit the cron expression in `.github/workflows/monitor.yml`. `interval_seconds` affects only the local continuous loop; Actions runs one check per job.

## State and failures

`data/state.json` is committed only when acknowledged alert state changes. It survives fresh runners without relying on an expiring cache. Persistent stock does not generate repeated alerts. A location that sells out and returns triggers a new alert. Concurrency prevents two monitor jobs running at once, and state commits do not trigger another check.

Provider failures and malformed responses retain previous alert state. Rejected Telegram deliveries remain eligible for a later run. Actions makes one attempt per job and retries on the next scheduled run. Lost acknowledgments or partly successful message chunks can cause a duplicate after retry. Failed jobs are visible in Actions.

## Run locally

Python 3.10+ is enough. On this Windows computer `run.ps1` uses Codex's bundled Python:

```powershell
.\run.ps1 --setup
.\run.ps1 --test-notification
.\run.ps1
```

The local loop requires the PC to stay awake. Use GitHub Actions for hosted checks. Local credentials/state live in gitignored `config.json` and `state.json`. Environment variables `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` override file values.

```text
python ovh_vps3_monitor.py --check --config config/monitor.json
python ovh_vps3_monitor.py --once --config config/monitor.json --state data/state.json
```

`--check` needs no credentials. `--once` needs Telegram credentials and exits after one notifying check. The local loop retries network failures with exponential delays up to 30 minutes and honors numeric provider retry delays.

## Verification and sources

The workflow runs offline tests before each check:

```text
python -m unittest discover -s tests -v
```

Tests use a captured public OVH response and local HTTP stand-ins. They verify plan discovery, OS/storage selection, preorder exclusion, restocks and persistence, rejected Telegram messages, malformed responses, retry delays, and credential-free checks. No real Telegram messages are sent by the tests.

Public endpoints:

```text
GET https://eu.api.ovh.com/1.0/order/catalog/public/vps?ovhSubsidiary=IE
GET https://eu.api.ovh.com/1.0/vps/order/rule/datacenter/aggregated
    ?ovhSubsidiary=IE&planCodes=vps-2025-model3,vps-2027-model3
```

Sources: [OVH API schema](https://eu.api.ovh.com/1.0/vps.json), [Worldwide VPS page](https://www.ovhcloud.com/en/vps/), [Telegram Bot API](https://core.telegram.org/bots/api#sendmessage), [GitHub scheduling behavior](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#schedule).
