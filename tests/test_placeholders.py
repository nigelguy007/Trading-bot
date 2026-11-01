"""
Placeholder keys must behave as absent.

An unedited .env is the single most common first-run state. If a placeholder
reads as a real key, the bot tries it, gets a 401, and the user concludes the
software is broken — instead of falling back to the offline simulator.
"""
from __future__ import annotations

import pytest

from src.config import Settings, is_placeholder


def make(**kw) -> Settings:
    kw.setdefault("anthropic_api_key", "sk-ant-real")
    return Settings(_env_file=None, **kw)


@pytest.mark.parametrize(
    "value",
    [
        "your_anthropic_api_key_here",
        "your_paper_key",
        "your_paper_secret",
        "YOUR_KEY_HERE",
        "changeme",
        "  your_key_here  ",
        "TODO",
    ],
)
def test_template_values_are_placeholders(value):
    assert is_placeholder(value)


@pytest.mark.parametrize(
    "value",
    ["", "sk-ant-api03-abc123", "PKTEST1234567890", "a1b2c3", "yourstruly-real-key"],
)
def test_real_and_empty_values_are_not_placeholders(value):
    assert not is_placeholder(value)


def test_placeholder_broker_keys_count_as_absent():
    s = make(alpaca_api_key="your_paper_key", alpaca_secret_key="your_paper_secret")
    assert not s.has_broker_keys


def test_half_placeholder_broker_keys_still_count_as_absent():
    s = make(alpaca_api_key="PKREAL123", alpaca_secret_key="your_paper_secret")
    assert not s.has_broker_keys


def test_real_broker_keys_are_recognised():
    assert make(alpaca_api_key="PKREAL123", alpaca_secret_key="abcSECRET").has_broker_keys


def test_placeholder_llm_key_counts_as_missing():
    assert not make(anthropic_api_key="your_anthropic_api_key_here").has_llm_key


def test_real_llm_key_is_recognised():
    assert make(anthropic_api_key="sk-ant-real").has_llm_key


def test_openrouter_provider_checks_its_own_key():
    s = make(llm_provider="openrouter", openrouter_api_key="your_key_here")
    assert not s.has_llm_key
    assert make(llm_provider="openrouter", openrouter_api_key="sk-or-real").has_llm_key


def test_unedited_env_template_falls_back_to_the_simulator():
    """The whole point: a fresh clone runs offline rather than erroring."""
    from src.data.market_data import CSVMarketData, build_market_data

    s = make(alpaca_api_key="your_paper_key", alpaca_secret_key="your_paper_secret")
    assert isinstance(build_market_data(s), CSVMarketData)
