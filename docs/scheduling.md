# Deriving beliefs on a schedule (without an always-on computer)

`memware derive` reads every transcript turn indexed since its last run, asks a model for
the durable facts in them, and files the ones that pass a deterministic check into the
belief ledger. It is **incremental and idempotent**: it keeps a watermark beside the
database, so running it twice does no extra work and running it late catches up. That
property is what makes scheduling it easy — you do not need a machine that is on at
03:00; you need it to run *sometime* when the machine is on.

Nothing to configure for the default provider. It runs the extraction through the Claude
Code CLI on your own subscription (`claude -p`, Haiku by default), so if `claude` is on
your PATH you can try it right now:

```bash
memware derive --plan     # no network: every excerpt a run would send, and where
memware derive            # dry run: sends the excerpts to the model, writes nothing
memware derive --apply    # files the candidates, advances the watermark
```

The dry run still sends every excerpt to the provider; it skips only the write and the
watermark. If your transcripts must clear an egress review first, start with `--plan`: it
calls no model and writes nothing, and it works before `claude` or `OPENAI_*` is set up.

Pick the option below that matches your machine. Only one is needed.

## 1. Let Claude Code run it (recommended if you use the plugin)

The memware plugin already runs on every session start. Turn on the derive step and it
runs in the background, at most once a day, whenever you open Claude Code — which is
exactly when your laptop is on. `memware setup` is where you switch it on: it says where the
excerpts go and points at `memware derive --plan` before it asks. The same switch, set
directly:

```bash
memware config derive.auto true
```

That is the whole setup. The hook calls `memware derive --apply --auto --if-stale 24
--quiet`: `--if-stale 24` skips when the last run is younger than a day, `--auto` makes
the hook a silent no-op until you switch it on, `--quiet` keeps it out of your terminal.
Turn it off the same way (`memware config derive.auto false`). If you installed the
plugin before this option existed, reinstall it to pick up the new hook
(`claude plugin install memware@memware`).

## 2. macOS: a LaunchAgent that catches up after sleep

`launchd` runs a missed `StartCalendarInterval` job at the next wake, so a laptop that was
asleep at 03:00 runs it when you open the lid. Save this as
`~/Library/LaunchAgents/com.memware.derive.plist` (adjust the `memware` path to the output
of `which memware`):

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>com.memware.derive</string>
  <key>ProgramArguments</key>
  <array><string>/Users/you/.local/bin/memware</string><string>derive</string><string>--apply</string><string>--quiet</string></array>
  <key>StartCalendarInterval</key><dict><key>Hour</key><integer>3</integer><key>Minute</key><integer>0</integer></dict>
  <key>StandardOutPath</key><string>/tmp/memware-derive.log</string>
  <key>StandardErrorPath</key><string>/tmp/memware-derive.log</string>
</dict></plist>
```

```bash
launchctl load ~/Library/LaunchAgents/com.memware.derive.plist    # once
launchctl start com.memware.derive                                # run it now to check
tail /tmp/memware-derive.log
```

A machine that was *off* (not asleep) at 03:00 does not run a calendar job for that day —
if that describes your laptop, use option 1, or swap `StartCalendarInterval` for
`<key>StartInterval</key><integer>21600</integer>` (every six hours while awake) and add
`--if-stale 24` to the arguments.

## 3. Linux: a systemd user timer with `Persistent=true`

`Persistent=true` runs a missed timer at the next boot or login. Two files under
`~/.config/systemd/user/`:

```ini
# memware-derive.service
[Unit]
Description=memware derive
[Service]
Type=oneshot
ExecStart=%h/.local/bin/memware derive --apply --quiet
```

```ini
# memware-derive.timer
[Unit]
Description=memware derive, daily, catching up after downtime
[Timer]
OnCalendar=*-*-* 03:00:00
Persistent=true
[Install]
WantedBy=timers.target
```

```bash
systemctl --user daemon-reload
systemctl --user enable --now memware-derive.timer
systemctl --user list-timers | grep memware      # next/last run
```

`loginctl enable-linger $USER` lets user timers run while you are logged out.

## 4. Plain cron

Only if the machine is always on. cron does not catch up: a missed 03:00 is skipped
until the next one.

```
0 3 * * * /home/you/.local/bin/memware derive --apply --quiet >> /tmp/memware-derive.log 2>&1
```

## Using a different model

`--provider openai` sends the extraction to any OpenAI-compatible chat endpoint. Set
`OPENAI_BASE_URL`, `OPENAI_MODEL` and `OPENAI_API_KEY` in the environment or in
`<memware home>/.env` (usually `~/.memware/.env`):

```
OPENAI_BASE_URL=https://api.example.com/v1
OPENAI_MODEL=some-small-fast-model
OPENAI_API_KEY=...
```

`memware config derive.provider openai` makes it the default. There is no fallback
chain on purpose: a model that was not chosen does not get to write into the ledger.

## What to expect

- Most excerpts are rejected. That is the design: the model must copy the value verbatim
  from the excerpt, and a value with a word the excerpt does not contain is dropped
  before it reaches the ledger.
- Derived beliefs carry reliability 0.5, below anything a human stated, so when one
  contradicts an existing belief it lands in `memware review`, never on top of yours.
- Exit codes: `0` done or nothing new; `2` not configured (no `claude` on PATH, or the
  openai settings missing); `4` the provider is unavailable right now (rejected key,
  usage limit) — nothing was written and the watermark did not move, so the next run
  picks up where this one stopped.
