# Running the bot somewhere that isn't your laptop

The bot is a single long-running process with a SQLite journal. That shapes
every choice below.

## Read this part first

**Set `DASHBOARD_PASSWORD` before the bot is reachable from the internet.**

Every endpoint can move money or expose the account: approving a signal
sends an order to your broker, `/api/bot/start` switches the bot on, and
`/api/portfolio` reads out your positions and equity.

The default fails closed: with no password set the server answers **only
clients on the same machine** and returns 403 to everything else. So a
deploy that forgets the password refuses everybody rather than handing out
the account. It is still a thing to get right deliberately, not to rely on.

```bash
openssl rand -base64 24
```

Two more things that are easy to get wrong:

**The process must stay up.** The scan loop is an in-process scheduler, not
a cron job. Anything that sleeps idle containers, scales to zero, or runs
request-driven functions will simply stop trading. That rules out most
serverless platforms.

**The journal needs a real disk.** `data/journal.db` holds your trade
history, the equity curve, and the day's opening equity — which is what the
daily-loss circuit breaker measures against. On an ephemeral container
filesystem it is wiped on every deploy, and the breaker silently loses its
reference point. Mount a volume and point `DATA_DIR` at it.

---

## Option A — Fly.io

Always-on small machines, volumes, and HTTPS included. `fly.toml` is in the
repo; change `app` and `primary_region` first.

```bash
fly launch --no-deploy --copy-config --name your-app-name
fly volumes create trader_data --size 1 --region lhr

fly secrets set \
  ANTHROPIC_API_KEY=sk-ant-... \
  ALPACA_API_KEY=... \
  ALPACA_SECRET_KEY=... \
  TRADINGVIEW_WEBHOOK_SECRET=... \
  DASHBOARD_PASSWORD="$(openssl rand -base64 24)"

fly deploy
fly open
```

Secrets go through `fly secrets`, never into `fly.toml` or the image —
`.dockerignore` excludes `.env` for the same reason.

`fly.toml` already sets `auto_stop_machines = false` and
`min_machines_running = 1`. Leave both alone: Fly's default stops a machine
when no HTTP request arrives, which for this app means it stops scanning.

Keep it to **one** machine. Two means two schedulers: double scanning,
double the Claude bill, and in auto mode duplicate orders.

```bash
fly logs
fly ssh console -C "ls -la /data"     # the journal should be here
```

## Option B — any box with Docker

A £4/month VPS is plenty.

```bash
cp .env.example .env          # fill it in, including DASHBOARD_PASSWORD
docker compose -f docker-compose.trader.yml up -d
docker compose -f docker-compose.trader.yml logs -f
```

The compose file binds to `127.0.0.1:8080` deliberately. Put Caddy or nginx
in front for TLS rather than exposing Basic auth over plain HTTP, where the
password crosses the network in base64 on every request. With Caddy that is
two lines:

```
desk.example.com {
    reverse_proxy 127.0.0.1:8080
}
```

## Option C — Render, Railway, Koyeb and similar

The same `Dockerfile.trader` works. Whatever you pick, check:

- a **persistent disk** mounted at `/data`, with `DATA_DIR=/data`
- **no scale-to-zero** and no idle sleep
- **one** instance, never two
- secrets set as environment variables, including `DASHBOARD_PASSWORD`
- health check path `/api/live`

---

## After it is up

```bash
curl -fsS https://your-app/api/live                      # {"status":"ok"}
curl -u trader:YOUR_PASSWORD https://your-app/api/bot    # running?
```

Open the dashboard, sign in, and check the **The bot** panel says
*Working*. With `AUTOSTART_SCANNER=true` it starts scanning on boot, which
is what you want on a host where nobody is there to press the button.

The build shown in the header tells you which commit is actually serving,
so you can confirm a deploy landed.

## Going live with real money

Nothing about deploying changes the gates. Real orders still need all three
of:

```
TRADING_MODE=live
ALPACA_PAPER=false          # or IG_DEMO=false / T212_DEMO=false
CONFIRM_LIVE=I_UNDERSTAND_THE_RISK
```

Think hard before combining live money with `EXECUTION_MODE=auto` on an
unattended host: the bot will place orders with nobody watching. `alert`
mode with Telegram approval gives you the same always-on scanning and still
puts a human in front of every order.

## Keeping your money safe

- Use a **paper** account until you have watched it run for a few weeks.
- Broker keys should be **trade-only**. Never enable withdrawals.
- Rotate `DASHBOARD_PASSWORD` if you ever paste a URL with credentials.
- `fly secrets` and the host's env store are the only places secrets go.
  `.env` is gitignored and excluded from the image; keep it that way.
- Back up the journal: `fly ssh sftp get /data/journal.db`.
