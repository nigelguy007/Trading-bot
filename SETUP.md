# Getting the bot running in the cloud

Four steps. Do them in order. About 15 minutes.

You need two browser tabs open: **supabase.com** and **railway.app**.

---

## Step 1 — Copy your database address from Supabase

1. Go to **supabase.com** and open the project called **trading-bot**.
2. Click the **Connect** button at the top of the page.
3. Choose the **Session pooler** option.
   (Not "Direct connection" — most hosts cannot reach that one.)
4. Copy the long line that starts with `postgresql://`
5. It has `[YOUR-PASSWORD]` in the middle. Replace that with your database
   password, including the square brackets.

   Don't know the password? On the same page: **Settings → Database →
   Reset database password**. Set a new one and use that.

Keep this line somewhere for the next step. It is a password — don't
paste it into a chat or commit it.

---

## Step 2 — Create the app on Railway

1. Go to **railway.app** and sign in with GitHub.
2. Click **New Project** → **Deploy from GitHub repo**.
3. Choose **Trading-bot**.
4. It will start building and will probably fail the first time. That is
   expected — it has no settings yet. Carry on to step 3.

---

## Step 3 — Paste in your settings

In Railway, open your project → **Variables** → **Raw Editor**, and paste
this in, filling in the four `<...>` parts:

```
ANTHROPIC_API_KEY=<your Claude key, starts sk-ant->
ALPACA_API_KEY=<your Alpaca key>
ALPACA_SECRET_KEY=<your Alpaca secret>
ALPACA_PAPER=true
JOURNAL_BACKEND=postgres
DATABASE_URL=<the postgresql:// line from Step 1>
DASHBOARD_PASSWORD=<make up a long password>
AUTOSTART_SCANNER=true
WEBHOOK_HOST=0.0.0.0
```

Your Alpaca and Claude keys are in the `.env` file on your Mac. Open it
with:

```
cd ~/Trading-bot && open -e .env
```

Save the variables. Railway rebuilds on its own.

Then go to **Settings → Networking → Generate Domain**. That gives you a
web address like `trading-bot-production.up.railway.app`.

---

## Step 4 — Open it and switch the bot on

1. Open the address Railway gave you.
2. It asks for a username and password.
   Username: `trader`
   Password: the `DASHBOARD_PASSWORD` you made up.
3. You land on the opening page. Click **Enter the trading desk**.
4. Find **The bot** panel. It should already say **Working**, because
   `AUTOSTART_SCANNER=true` starts it on boot.

That's it. You can close your laptop.

---

## Checking it is really working

- The panel says **Working** and shows a "next scan" time.
- After the first scan, **last run** appears with what it found.
- In Supabase → **Table editor** → schema **trading** → table **equity**,
  rows appear as it runs. That is your history, safe from the host.

## If something goes wrong

In Railway, click **Deployments** → the latest one → **View logs**. The
error is in there. Paste it to me and I'll sort it.

## Important

`ALPACA_PAPER=true` means fake money. Leave it that way until you have
watched it run for a few weeks. Real trading needs three more settings
changed on purpose, which is covered in `docs/deploy.md`.
