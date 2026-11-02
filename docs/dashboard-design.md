# Dashboard design

The dashboard is a single self-contained HTML page served by
`src/webhook_server.py`. No build step, no external scripts or stylesheets,
no CDN. It runs on localhost and draws its own charts, so it works with the
internet off.

## Where the look comes from

Two references, mixed deliberately rather than copied:

**Tradeo** (onixtheme.com) supplied the surface treatment — a near-black
background with a blue-violet cast, surfaces that step up in tone rather
than using borders alone, a violet accent, and headings at weight 500
instead of bold.

**Alpha Ledger** (Eloqwnt, Dribbble) supplied the layout discipline — a thin
icon rail, a greeting header, big light numerals with small muted captions,
and a dense two-column metric table. Its restraint is the part worth
stealing: colour belongs to data, nothing else.

Neither one's markup, CSS or assets were used. This is an original page
informed by both.

## Tokens

```
--bg        #060610     page
--surface-1 #0b0b15     panels, tables, rail
--surface-2 #11121c     table headers, chips
--surface-3 #191928     hover, tags, the keyed panel
--stroke-1  #171720     hairlines
--stroke-2  #252537     raised borders
--accent    #af60ff     actions only
--text      #ffffff
--muted     #a9a9ac     labels and captions
--pos       #46d19e     money up
--neg       #ff6b6b     money down
--warn      #e3b341     banners
```

Type is Inter at weight 500, falling back to the system stack. Figures use
`font-variant-numeric: tabular-nums` so digits do not jitter on refresh.

## The one rule

**Colour belongs to data.** Violet marks what you can act on — the Approve
button and the paper/live badge. Green and red mean money moved. Everything
else is a shade of near-black or muted grey. That is why a losing day is
visible from across the room: it is the only coloured thing on the screen.

## Layout

| Region | What it carries |
|---|---|
| Icon rail | Section jumps. Becomes a top strip under 720px — 68px is a lot of a 390px screen |
| Header | Mode badge, venue, size unit |
| Hero | Equity, day P&L, open risk — the three figures worth seeing from across the room |
| Equity curve | Inline SVG from `/api/equity`, which the journal had been recording all along with nothing to show it |
| Risk limits | Each figure against the line that stops trading: "-0.8% of -3%" rather than a bare "-0.8%" |
| Tables | Approvals, positions, venues (hidden on one broker), signals |
| Performance | Closed-trade metrics |

## Phone behaviour

Pending approvals stop being a table under 720px and become cards. As a
table the action column sat inside a sideways-scrolling box, so reaching
Approve meant swiping the table first — on the one interaction that alert
mode exists for. As cards the buttons are full width, and the columns the
table had to drop (direction, target, R:R, size) come back, because you want
the whole setup in front of you before approving a trade on a phone.

Verified at 1440, 768 and 390px: no horizontal overflow, no console errors,
Approve and Deny fully on screen at every width. `tests/test_api_smoke.py`
pins the parts that silently regress.

---

## A note on the launcher

`start.sh` must parse under **bash 3.2**. Apple froze bash there in 2007
over the GPL3 licence change, so every Mac runs a shell older than most of
bash's modern syntax — while CI and most Linux boxes run bash 5.

That difference has already cost one broken release. A heredoc inside a
`$(...)` command substitution parses fine in bash 5; bash 3.2 scans the
substitution for its closing paren with the ordinary quote lexer and does
not know the heredoc body is literal, so one apostrophe in an embedded
Python comment made the whole script unparseable. `bash -n`, the full test
suite and three live launches all passed before it shipped.

Two rules, both enforced by `tests/test_start_script.py`:

1. **No heredoc inside `$(...)`.** Put embedded interpreters in files —
   `scripts/pick_port.py` is the precedent. They are testable there too.
2. **No bash 4+ syntax**: `${VAR^^}`, `${VAR,,}`, `declare -A`, `mapfile`,
   `readarray`, `&>>`, `|&`, `coproc`.

To check against the real thing:

```
curl -O https://ftp.gnu.org/gnu/bash/bash-3.2.tar.gz
tar xzf bash-3.2.tar.gz && cd bash-3.2
./configure --without-bash-malloc && make
./bash -n ../start.sh
```

The test suite picks that binary up automatically from `/tmp/b32/bash-3.2/bash`
or a `bash-3.2` on PATH, and skips when neither exists.
