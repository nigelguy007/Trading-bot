"""
The LLM transport.

Anthropic is the default path. A few deliberate choices:

* `thinking` is never sent. Fable 5 rejects any explicit thinking config, and
  Opus 5 / Sonnet 5 run adaptive thinking by default anyway — so omitting the
  parameter is the one form that is correct on every current model.
* No `temperature` / `top_p` / `top_k`. Current models reject them; steering
  happens in the prompt.
* `stop_reason` is checked before `content` is read. A safety refusal returns
  HTTP 200 with an empty content list, and indexing into it blindly is a crash.
* Refusal fallbacks are requested when the endpoint supports them, and the
  client silently degrades through progressively plainer request shapes so an
  older SDK still works.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Optional

import httpx

from src.config import Settings
from src.logging_setup import get_logger

log = get_logger(__name__)

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"


class LLMError(RuntimeError):
    pass


@dataclass
class LLMResponse:
    text: str
    model: str = ""
    parsed: Optional[dict[str, Any]] = None
    refused: bool = False
    refusal_category: str = ""
    usage: dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return not self.refused and bool(self.text or self.parsed)


class LLMClient:
    """One `complete()` call, whichever provider is configured."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.provider = settings.llm_provider
        self._request_mode = "beta_fallbacks"  # degrades on first incompatibility
        self._anthropic = None
        self._http: Optional[httpx.Client] = None

        if self.provider == "anthropic":
            if not settings.anthropic_api_key:
                raise LLMError(
                    "ANTHROPIC_API_KEY is not set. Add it to .env — Claude is the "
                    "analysis brain and nothing downstream works without it."
                )
            import anthropic  # imported lazily so `doctor` can report a missing package

            self._anthropic = anthropic.Anthropic(api_key=settings.anthropic_api_key)
            self._errors = anthropic
        else:
            if not settings.openrouter_api_key:
                raise LLMError("LLM_PROVIDER=openrouter but OPENROUTER_API_KEY is empty.")
            self._http = httpx.Client(
                timeout=120.0,
                headers={
                    "Authorization": f"Bearer {settings.openrouter_api_key}",
                    "Content-Type": "application/json",
                },
            )

    # ------------------------------------------------------------------ #
    def close(self) -> None:
        if self._http is not None:
            self._http.close()

    @property
    def model(self) -> str:
        return (
            self.settings.claude_model
            if self.provider == "anthropic"
            else self.settings.openrouter_model
        )

    def complete(
        self,
        system: str,
        user: str,
        *,
        schema: Optional[dict] = None,
        max_tokens: Optional[int] = None,
        effort: Optional[str] = None,
    ) -> LLMResponse:
        """Send one prompt. With `schema`, the reply is validated JSON."""
        max_tokens = max_tokens or self.settings.claude_max_tokens
        effort = effort or self.settings.claude_effort

        if self.provider == "anthropic":
            return self._complete_anthropic(system, user, schema, max_tokens, effort)
        return self._complete_openrouter(system, user, schema, max_tokens)

    # ------------------------------------------------------------------ #
    # Anthropic
    # ------------------------------------------------------------------ #
    def _complete_anthropic(
        self, system: str, user: str, schema: Optional[dict], max_tokens: int, effort: str
    ) -> LLMResponse:
        output_config: dict[str, Any] = {"effort": effort}
        if schema:
            output_config["format"] = {"type": "json_schema", "schema": schema}

        base = {
            "model": self.settings.claude_model,
            "max_tokens": max_tokens,
            "system": system,
            "messages": [{"role": "user", "content": user}],
        }

        # Try the richest request shape first, then degrade. Whatever works is
        # remembered so we only pay the discovery cost once per process.
        order = ["beta_fallbacks", "plain", "bare"]
        start = order.index(self._request_mode)
        last_error: Exception | None = None

        for mode in order[start:]:
            try:
                response = self._send_anthropic(base, output_config, mode)
                self._request_mode = mode
                return self._parse_anthropic(response, schema)
            except self._errors.BadRequestError as exc:
                message = str(exc)
                if "retention" in message.lower():
                    raise LLMError(
                        f"{self.settings.claude_model} requires 30-day data retention on "
                        "your organisation and your org does not meet it. Either change "
                        "the retention setting or set CLAUDE_MODEL=claude-opus-5."
                    ) from exc
                log.debug("llm.shape_rejected", mode=mode, error=message[:200])
                last_error = exc
            except TypeError as exc:
                # Installed SDK predates a parameter used by this shape.
                log.debug("llm.shape_unsupported_by_sdk", mode=mode, error=str(exc)[:200])
                last_error = exc
            except self._errors.AuthenticationError as exc:
                raise LLMError(
                    "Anthropic rejected the API key (401). Check ANTHROPIC_API_KEY in "
                    ".env — a trailing space is the usual culprit."
                ) from exc
            except self._errors.RateLimitError as exc:
                raise LLMError(
                    "Anthropic rate limit (429). Raise SCAN_INTERVAL_MINUTES, shorten "
                    "WATCHLIST, or lower CLAUDE_EFFORT."
                ) from exc
            except self._errors.APIConnectionError as exc:
                raise LLMError(f"Could not reach the Anthropic API: {exc}") from exc

        raise LLMError(f"Anthropic request failed in every supported shape: {last_error}")

    def _send_anthropic(self, base: dict, output_config: dict, mode: str):
        if mode == "beta_fallbacks":
            # Safety classifiers can decline a request; `fallbacks: "default"`
            # re-runs it server-side on Anthropic's recommended substitute.
            return self._anthropic.beta.messages.create(
                **base,
                output_config=output_config,
                betas=["server-side-fallback-2026-07-01"],
                extra_body={"fallbacks": "default"},
            )
        if mode == "plain":
            return self._anthropic.messages.create(**base, output_config=output_config)
        return self._anthropic.messages.create(**base, extra_body={"output_config": output_config})

    def _parse_anthropic(self, response, schema: Optional[dict]) -> LLMResponse:
        usage = {}
        if getattr(response, "usage", None):
            usage = {
                "input_tokens": getattr(response.usage, "input_tokens", 0),
                "output_tokens": getattr(response.usage, "output_tokens", 0),
            }

        # Check stop_reason before touching content — a refusal has none.
        if getattr(response, "stop_reason", None) == "refusal":
            details = getattr(response, "stop_details", None)
            category = getattr(details, "category", "") or "unspecified"
            log.warning("llm.refused", category=category, model=getattr(response, "model", ""))
            return LLMResponse(
                text="",
                model=getattr(response, "model", ""),
                refused=True,
                refusal_category=category,
                usage=usage,
            )

        text = "".join(
            block.text
            for block in (response.content or [])
            if getattr(block, "type", "") == "text" and getattr(block, "text", "")
        ).strip()

        return LLMResponse(
            text=text,
            model=getattr(response, "model", self.settings.claude_model),
            parsed=_extract_json(text) if schema else None,
            usage=usage,
        )

    # ------------------------------------------------------------------ #
    # OpenRouter
    # ------------------------------------------------------------------ #
    def _complete_openrouter(
        self, system: str, user: str, schema: Optional[dict], max_tokens: int
    ) -> LLMResponse:
        payload: dict[str, Any] = {
            "model": self.settings.openrouter_model,
            "max_tokens": max_tokens,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        }
        if schema:
            payload["response_format"] = {
                "type": "json_schema",
                "json_schema": {"name": "response", "strict": True, "schema": schema},
            }

        try:
            resp = self._http.post(OPENROUTER_URL, json=payload)
        except httpx.HTTPError as exc:
            raise LLMError(f"Could not reach OpenRouter: {exc}") from exc

        if resp.status_code == 401:
            raise LLMError("OpenRouter rejected the key (401). Check OPENROUTER_API_KEY.")
        if resp.status_code == 429:
            raise LLMError("OpenRouter rate limit (429). Slow the scan loop down.")
        if resp.status_code >= 400:
            raise LLMError(f"OpenRouter {resp.status_code}: {resp.text[:300]}")

        text = (resp.json()["choices"][0]["message"].get("content") or "").strip()
        return LLMResponse(
            text=text,
            model=self.settings.openrouter_model,
            parsed=_extract_json(text) if schema else None,
        )


def _extract_json(text: str) -> Optional[dict]:
    """
    Structured outputs return bare JSON, but a degraded path or a provider
    without schema support may wrap it in a fence or prose. Recover both.
    """
    if not text:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    if "```" in text:
        segment = text.split("```")[1]
        if segment.startswith("json"):
            segment = segment[4:]
        try:
            return json.loads(segment.strip())
        except json.JSONDecodeError:
            pass

    start, end = text.find("{"), text.rfind("}")
    if 0 <= start < end:
        try:
            return json.loads(text[start : end + 1])
        except json.JSONDecodeError:
            pass

    log.warning("llm.json_parse_failed", preview=text[:200])
    return None
