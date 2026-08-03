"""
The prompt library.

The ten USER_* templates are the guide's prompts, kept word-for-word so what
the bot sends is what you'd paste into Claude yourself. The bracket
placeholders became `{fields}`; nothing else was reworded.

Every analysis prompt ends by asking for the risks — that is the point of the
whole exercise, and it is the easiest thing to accidentally optimise away.
"""
from __future__ import annotations

# ====================================================================== #
#  System prompts — the standing instructions wrapped around each call
# ====================================================================== #

ANALYST_SYSTEM = """You are a disciplined trading analyst supporting a retail trader.

Ground rules:
- You are an analyst, not an oracle. Say plainly what you do not know.
- "No trade" is a valid and frequently correct answer. A weak setup is worse
  than no setup, because it costs real money to discover it was weak.
- Every idea must have an entry, a stop, a target, and a stated invalidation
  BEFORE it is worth discussing. No stop means no trade.
- Never claim certainty about direction. You are estimating probabilities.
- Distinguish what the data shows from what you are inferring.
- Flag anything reckless, including in your own reasoning.

The trader reviews every output before acting on it. Write for that reader."""

RISK_SYSTEM = """You are a risk manager. Your job is to find the way this trade
loses money, not to help it get placed.

Assume the setup thesis is wrong and reason from there. Quantify risk in both
percent-of-account and dollar terms. Be blunt about position size, correlation
with existing exposure, and the realistic worst case — not the average case.
If something is reckless, say so in the first sentence."""

JOURNAL_SYSTEM = """You are a trading coach reviewing a trader's executed trades.

Judge the PROCESS, not the outcome. A winning trade taken against the rules is
a bad trade; a losing trade taken correctly is a good trade. Be specific and
concrete — "size was 2.4% of account against a 1% rule" beats "manage risk
better". End with exactly one process fix, the highest-leverage one."""

DEBATE_SYSTEM = {
    "bull": """You are the bull. Argue the strongest honest case FOR the long side
using only the supplied data. Cite specific levels and figures. Do not
manufacture conviction you cannot support — if the bull case is weak, say it is
weak and explain why.""",
    "bear": """You are the bear. Argue the strongest honest case AGAINST the trade,
and for the short side if one exists, using only the supplied data. Cite
specific levels and figures. Attack the bull thesis at its weakest joint.""",
    "risk": """You are the risk manager and you have the final word. You have read the
bull and bear cases. Decide: take it, or pass. You are not required to find a
trade — passing is free, being wrong is not. If you take it, specify entry,
stop, target, and what would prove you wrong. If either side's argument rests
on data that isn't present, discard that argument.""",
}


# ====================================================================== #
#  The ten prompts from the guide (§6)
# ====================================================================== #

USER_MARKET_ANALYSIS = """Analyze {ticker} using this data: {data}.
Give me the trend, the setup if any, and 3 things that would invalidate it. Be
blunt about what you don't know."""

USER_SWING_TRADING = """Based on the daily chart data for {ticker}, is there a swing setup with a 3-10
day horizon? Give entry, stop, target, and the reasoning. Skip it if the setup
is weak.

Daily chart data:
{data}

Recent news context:
{news}"""

USER_DAY_TRADING = """Given this intraday data for {ticker}, identify a same-day setup if one
exists. Entry, stop, target, and the exact invalidation. Say 'no trade' if
it's not clean.

Intraday data:
{data}

Recent news context:
{news}"""

USER_RISK_ANALYSIS = """For this proposed trade {trade}, calculate the risk/reward,
the % of account at risk, and the worst realistic outcome. Flag anything
reckless.

Account equity: ${equity}
Existing open positions: {positions}"""

USER_POSITION_SIZING = """My account is ${equity}. I risk max {risk_pct}% per trade. For entry {entry} and stop {stop},
tell me the exact share/contract size and the dollar risk."""

USER_PORTFOLIO_REVIEW = """Here are my open positions {positions}. Assess concentration, correlation, and
total risk exposure. What would you trim first and why?

Account equity: ${equity}
Total open risk: ${open_risk}"""

USER_TRADE_JOURNALING = """Log this trade {trade}. What did I do well, what did I do wrong,
and what's the one process fix for next time?"""

USER_STRATEGY_OPTIMIZATION = """Here are {count} trades from strategy {strategy} {data}. What's actually working vs noise?
Suggest 2 concrete rule changes and how you'd test them.

Aggregate metrics:
{metrics}"""

USER_NEWS_ANALYSIS = """Summarize how these headlines {headlines} could affect {ticker} short-term.
Separate real catalysts from noise. Rate impact low/med/high."""

USER_CHART_BREAKDOWN = """Break down structure, key levels, and the cleanest setup if
any. Tell me where you'd be wrong.

{ticker} price structure:
{data}"""


# ====================================================================== #
#  JSON schemas for the calls whose output the bot acts on
# ====================================================================== #

SETUP_SCHEMA = {
    "type": "object",
    "properties": {
        "has_setup": {
            "type": "boolean",
            "description": "False if there is no clean setup. Prefer false when unsure.",
        },
        "direction": {"type": "string", "enum": ["long", "short", "none"]},
        "entry": {"type": "number", "description": "Entry price. 0 if has_setup is false."},
        "stop": {"type": "number", "description": "Stop loss price. 0 if has_setup is false."},
        "target": {"type": "number", "description": "Primary target price. 0 if has_setup is false."},
        "confidence": {
            "type": "number",
            "description": "0.0-1.0 confidence in the setup, not in the direction of the market.",
        },
        "reasoning": {"type": "string", "description": "Why this setup, in 2-4 sentences."},
        "invalidation": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Three concrete things that would prove this setup wrong.",
        },
        "risks": {"type": "string", "description": "What you don't know, and the worst realistic case."},
        "no_trade_reason": {
            "type": "string",
            "description": "If has_setup is false, why. Empty string otherwise.",
        },
    },
    "required": [
        "has_setup",
        "direction",
        "entry",
        "stop",
        "target",
        "confidence",
        "reasoning",
        "invalidation",
        "risks",
        "no_trade_reason",
    ],
    "additionalProperties": False,
}

NEWS_SCHEMA = {
    "type": "object",
    "properties": {
        "impact": {"type": "string", "enum": ["low", "medium", "high"]},
        "summary": {"type": "string", "description": "Two sentences on the short-term read."},
        "catalysts": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Headlines that genuinely move price.",
        },
        "noise": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Headlines that look important but are not.",
        },
        "direction_bias": {"type": "string", "enum": ["bullish", "bearish", "neutral"]},
    },
    "required": ["impact", "summary", "catalysts", "noise", "direction_bias"],
    "additionalProperties": False,
}

DEBATE_VERDICT_SCHEMA = {
    "type": "object",
    "properties": {
        "verdict": {"type": "string", "enum": ["take", "pass"]},
        "direction": {"type": "string", "enum": ["long", "short", "none"]},
        "entry": {"type": "number"},
        "stop": {"type": "number"},
        "target": {"type": "number"},
        "confidence": {"type": "number"},
        "reasoning": {"type": "string"},
        "invalidation": {"type": "array", "items": {"type": "string"}},
        "strongest_counterargument": {
            "type": "string",
            "description": "The best point from the losing side, kept on the record.",
        },
    },
    "required": [
        "verdict",
        "direction",
        "entry",
        "stop",
        "target",
        "confidence",
        "reasoning",
        "invalidation",
        "strongest_counterargument",
    ],
    "additionalProperties": False,
}
