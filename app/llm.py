"""AI providers behind one small interface.

    llm = get_llm()
    llm.json(system=..., build=build, schema=...)   -> dict
    llm.text(system=..., build=build)               -> str
    llm.vision(image_bytes, media_type, prompt)     -> str
    llm.used_label()                                -> "groq:openai/gpt-oss-120b" (who answered)

`build(budget_chars)` returns `(context, prompt)` sized for the provider that
is about to answer: a provider with a small free allowance gets a shorter
document excerpt than one with a big context window. `context` is the large,
stable document text; providers put it where it can be cached.

Providers:
  anthropic  Claude API
  openai     OpenAI, or any OpenAI-compatible server (OPENAI_BASE_URL)
  ollama     a local model on this computer
  free       a chain of free services: Groq, then Gemini, then OpenRouter,
             then Ollama. When one reaches its free usage limit the next one
             takes over; it is used again once its limit resets.
"""
from __future__ import annotations

import base64
import json
import os
import re
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Callable, Optional, Tuple

from . import config

Build = Callable[[int], Tuple[str, str]]  # character budget -> (context, prompt)


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------
class LLMError(Exception):
    """A problem the learner should see on the slide."""


class RateLimited(LLMError):
    """The provider's usage limit was reached."""

    def __init__(self, message: str, *, retry_after: Optional[float] = None,
                 until: Optional[float] = None, daily: bool = False):
        super().__init__(message)
        self.retry_after = retry_after  # seconds, when the provider said so
        self.until = until              # epoch seconds when the limit should be lifted
        self.daily = daily


class TooLarge(LLMError):
    """This request is bigger than the provider accepts (try another provider)."""


class BadConfig(LLMError):
    """Key rejected, model missing, payment needed: retrying soon won't help."""


class Unreachable(LLMError):
    """Network problem, timeout or the service is down for now."""


class _DropOption(Exception):
    """Internal: the server rejected an optional request parameter; retry without it."""

    def __init__(self, option: str):
        super().__init__(option)
        self.option = option


def parse_json(text: str) -> dict:
    text = (text or "").strip()
    text = re.sub(r"<think>[\s\S]*?</think>", "", text).strip()  # local "thinking" models
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text)
    try:
        data = json.loads(text)
        if isinstance(data, dict):
            return data
    except json.JSONDecodeError:
        pass
    start, end = text.find("{"), text.rfind("}")
    if start >= 0 and end > start:
        try:
            data = json.loads(text[start:end + 1])
            if isinstance(data, dict):
                return data
        except json.JSONDecodeError:
            pass
    raise LLMError("The AI returned an answer that wasn't in the expected format.")


# ---------------------------------------------------------------------------
# Reading rate-limit hints
# ---------------------------------------------------------------------------
_DUR_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(ms|h|m|s)")
_DAILY_RE = re.compile(r"per[\s_-]?day|perday|\(rpd\)|\(tpd\)|daily|free-models-per-day", re.I)


def parse_duration(text: str) -> Optional[float]:
    """'1m26.4s' -> 86.4, '7.66s' -> 7.66, '250ms' -> 0.25, '2h' -> 7200, '30' -> 30."""
    if text is None:
        return None
    text = str(text).strip().lower()
    if re.fullmatch(r"\d+(\.\d+)?", text):
        return float(text)
    total, found = 0.0, False
    for num, unit in _DUR_RE.findall(text):
        found = True
        total += float(num) * {"ms": 0.001, "s": 1, "m": 60, "h": 3600}[unit]
    return total if found else None


def _next_midnight(tz_name: Optional[str]) -> float:
    """Epoch seconds of the next midnight in a time zone (UTC if unknown)."""
    tz = timezone.utc
    if tz_name:
        try:
            from zoneinfo import ZoneInfo

            tz = ZoneInfo(tz_name)
        except Exception:
            tz = timezone.utc
    now = datetime.now(tz)
    nxt = (now + timedelta(days=1)).replace(hour=0, minute=0, second=5, microsecond=0)
    return nxt.timestamp()


def classify_rate_limit(text: str, headers=None, *, daily_reset_tz: Optional[str] = None) -> RateLimited:
    """Turn a 429 response (message + headers) into a RateLimited with a sensible lift time."""
    text = text or ""
    h = {str(k).lower(): str(v) for k, v in (dict(headers or {})).items()}
    now = time.time()
    daily = bool(_DAILY_RE.search(text))
    wait: Optional[float] = None
    until: Optional[float] = None

    # 1. explicit hints, most specific first
    m = re.search(r"try again in\s+([0-9hms.\s]+)", text, re.I)          # Groq
    if m:
        wait = parse_duration(m.group(1))
    if wait is None:
        m = re.search(r"retry in\s+([0-9.]+\s*(?:ms|s|m|h)?)", text, re.I)   # Gemini message
        if m:
            wait = parse_duration(m.group(1) if re.search(r"[a-z]", m.group(1)) else m.group(1) + "s")
    if wait is None:
        m = re.search(r'retryDelay["\']?\s*[:=]\s*["\']?([0-9.]+s)', text)  # Gemini details
        if m:
            wait = parse_duration(m.group(1))
    reset = h.get("x-ratelimit-reset")
    if reset is None:
        m = re.search(r'X-RateLimit-Reset["\']?\s*[:=]\s*["\']?(\d{10,13})', text, re.I)  # OpenRouter metadata
        reset = m.group(1) if m else None
    if reset and re.fullmatch(r"\d{10,13}", reset):
        val = float(reset)
        until = val / 1000 if val > 1e12 else val
    if wait is None and h.get("retry-after"):
        wait = parse_duration(h["retry-after"])
    if wait is None:
        key = "x-ratelimit-reset-requests" if daily else "x-ratelimit-reset-tokens"
        wait = parse_duration(h.get(key)) if h.get(key) else None

    # 2. decide when to try this provider again
    if until is None and wait is not None:
        until = now + max(wait, 1.0)
    if daily:
        # Some services report a short retry delay even for a daily cap; never trust less than an hour
        # unless they gave a precise reset time.
        if until is None or (until - now < 3600 and not re.search(r"try again in", text, re.I) and not reset):
            until = _next_midnight(daily_reset_tz) if daily_reset_tz is not None else now + 3600
    if until is None:
        until = now + 60
    retry_after = None if daily else (wait if wait is not None else max(0.0, until - now))
    return RateLimited(text[:300] or "Usage limit reached", retry_after=retry_after, until=until, daily=daily)


# ---------------------------------------------------------------------------
# Providers
# ---------------------------------------------------------------------------
class BaseLLM:
    name = "none"
    label = "None"
    model = ""
    context_chars = 0          # how much document text fits comfortably in one request
    supports_vision = False

    @property
    def available(self) -> bool:
        return self.name != "none"

    def json(self, *, system: str, build: Build, schema: dict, max_tokens: int = 4096) -> dict:
        raise LLMError("No AI provider is configured.")

    def text(self, *, system: str, build: Build, max_tokens: int = 1500) -> str:
        raise LLMError("No AI provider is configured.")

    def vision(self, image: bytes, media_type: str, prompt: str) -> str:
        raise LLMError("This AI provider can't read images.")

    def used_label(self) -> str:
        return f"{self.name}:{self.model}" if self.model else self.name

    def describe(self) -> dict:
        return {"provider": self.name, "model": self.model, "vision": self.supports_vision}


class NoLLM(BaseLLM):
    pass


class AnthropicLLM(BaseLLM):
    name = "anthropic"
    label = "Claude"
    context_chars = 160_000
    supports_vision = True

    def __init__(self, model: str):
        import anthropic

        self._anthropic = anthropic
        self.client = anthropic.Anthropic(max_retries=3)
        self.model = model

    def _system(self, system: str, context: str):
        blocks = [{"type": "text", "text": system}]
        if context:
            blocks.append({"type": "text", "text": context, "cache_control": {"type": "ephemeral"}})
        return blocks

    def _call(self, **kw):
        a = self._anthropic
        try:
            return self.client.messages.create(model=self.model, **kw)
        except a.AuthenticationError:
            raise BadConfig("Your Anthropic API key was rejected. Check it in Settings.")
        except a.NotFoundError as e:
            raise BadConfig(f"Model '{self.model}' was not found. Change ANTHROPIC_MODEL in Settings. ({e})")
        except a.RateLimitError as e:
            raise classify_rate_limit(str(e), getattr(getattr(e, "response", None), "headers", None))
        except a.APIStatusError as e:
            raise LLMError(f"Anthropic API error {e.status_code}: {getattr(e, 'message', e)}")
        except a.APIConnectionError:
            raise Unreachable("Couldn't reach the Anthropic API. Check your internet connection.")

    def json(self, *, system, build, schema, max_tokens=4096):
        context, prompt = build(self.context_chars)
        resp = self._call(
            max_tokens=max_tokens,
            system=self._system(system, context),
            messages=[{"role": "user", "content": prompt}],
            tools=[{"name": "respond", "description": "Return the slide content.", "input_schema": schema}],
            tool_choice={"type": "tool", "name": "respond"},
        )
        for block in resp.content:
            if block.type == "tool_use":
                return dict(block.input)
        raise LLMError("The AI did not return structured output.")

    def text(self, *, system, build, max_tokens=1500):
        context, prompt = build(self.context_chars)
        resp = self._call(max_tokens=max_tokens, system=self._system(system, context),
                          messages=[{"role": "user", "content": prompt}])
        return "".join(b.text for b in resp.content if b.type == "text").strip()

    def vision(self, image, media_type, prompt):
        resp = self._call(max_tokens=2500, messages=[{"role": "user", "content": [
            {"type": "image", "source": {"type": "base64", "media_type": media_type,
                                         "data": base64.b64encode(image).decode()}},
            {"type": "text", "text": prompt}]}])
        return "".join(b.text for b in resp.content if b.type == "text").strip()


class OpenAICompatLLM(BaseLLM):
    """OpenAI itself, or any OpenAI-compatible service (Groq, Gemini, OpenRouter, LM Studio ...)."""

    def __init__(self, *, name: str, label: str, model: str, api_key: Optional[str],
                 base_url: Optional[str] = None, context_chars: int = 160_000, vision: bool = True,
                 json_mode: bool = True, max_tokens: Optional[int] = None, out_factor: float = 1.0,
                 token_param: str = "max_tokens", extra: Optional[dict] = None,
                 headers: Optional[dict] = None, retries: int = 3, timeout: float = 600,
                 daily_reset_tz: Optional[str] = None):
        import openai

        self._openai = openai
        self.name, self.label, self.model = name, label, model
        self.context_chars, self.supports_vision = context_chars, vision
        self.json_mode = json_mode
        self.max_tokens_cap, self.out_factor, self.token_param = max_tokens, out_factor, token_param
        self.extra = dict(extra or {})
        self.daily_reset_tz = daily_reset_tz
        # A local OpenAI-compatible server (LM Studio, vLLM ...) may not need a real key.
        self.client = openai.OpenAI(api_key=api_key or "not-needed", base_url=base_url, max_retries=retries,
                                    timeout=timeout, default_headers=headers or None)

    # -- one request, with error mapping -------------------------------------------------
    def _request(self, messages, max_tokens: int, json_mode: bool, extra: dict) -> str:
        o = self._openai
        n = int(max_tokens * self.out_factor)
        if self.max_tokens_cap:
            n = min(n, self.max_tokens_cap)
        kw = {"model": self.model, "messages": messages, self.token_param: n}
        if json_mode:
            kw["response_format"] = {"type": "json_object"}
        if extra:
            kw["extra_body"] = dict(extra)
        try:
            r = self.client.chat.completions.create(**kw)
        except o.RateLimitError as e:
            raise classify_rate_limit(_err_text(e), _err_headers(e), daily_reset_tz=self.daily_reset_tz)
        except o.APIStatusError as e:
            raise self._map_status(e, json_mode, extra)
        except o.APITimeoutError:
            raise Unreachable(f"{self.label} took too long to answer.")
        except o.APIConnectionError:
            raise Unreachable(f"Couldn't reach {self.label}. Check your internet connection.")
        if not getattr(r, "choices", None):
            raise LLMError(f"{self.label} returned an empty answer.")
        choice = r.choices[0]
        content = (choice.message.content or "").strip()
        if not content:
            if getattr(choice, "finish_reason", "") == "length":
                raise LLMError(f"{self.label}'s answer was cut off before it finished.")
            raise LLMError(f"{self.label} returned an empty answer.")
        return content

    def _map_status(self, e, json_mode: bool, extra: dict) -> Exception:
        status = getattr(e, "status_code", 0) or 0
        text = _err_text(e)
        low = text.lower()
        if status == 413 or re.search(r"too large|too long|context[ _]length|context window|maximum context|"
                                      r"reduce (the|your) (message|prompt|input)|exceeds the maximum", low):
            return TooLarge(f"{self.label}: this request is too large for its limits.")
        if status in (400, 404, 422):
            if json_mode and re.search(r"json_validate_failed|failed to generate json", low):
                return _DropOption("json-once")  # the model fumbled JSON mode this time; retry without it
            if json_mode and re.search(r"response_format|json[_ ]mode|json_object|structured|"
                                       r"requested parameters", low):
                return _DropOption("json")
            for opt in list(extra):
                if opt.replace("_", " ") in low or opt in low:
                    return _DropOption(opt)
        if status in (401, 403):
            return BadConfig(f"Your {self.label} API key was rejected. Check it in Settings.")
        if status == 402:
            return BadConfig(f"{self.label} says this model needs paid credits.")
        if status == 404:
            return BadConfig(f"{self.label} doesn't have the model '{self.model}'. Change it in .env.")
        if status == 429:
            return classify_rate_limit(text, _err_headers(e), daily_reset_tz=self.daily_reset_tz)
        if status >= 500 or status in (408, 498):
            return Unreachable(f"{self.label} is temporarily unavailable ({status}).")
        return LLMError(f"{self.label} error {status}: {text[:300]}")

    def _call(self, messages, max_tokens: int, want_json: bool) -> str:
        json_mode = want_json and self.json_mode
        extra = dict(self.extra)
        for _ in range(4):  # drop rejected optional parameters one at a time
            try:
                return self._request(messages, max_tokens, json_mode, extra)
            except _DropOption as d:
                if d.option in ("json", "json-once") and json_mode:
                    json_mode = False
                    if d.option == "json":
                        self.json_mode = False  # remember: this server doesn't do JSON mode
                elif d.option in extra:
                    extra.pop(d.option)
                    self.extra.pop(d.option, None)
                else:
                    raise LLMError(f"{self.label} rejected the request.")
        raise LLMError(f"{self.label} rejected the request.")

    # -- interface ----------------------------------------------------------------------
    def json(self, *, system, build, schema, max_tokens=4096):
        context, prompt = build(self.context_chars)
        sys = f"{system}\n\nRespond with ONE JSON object only, matching this JSON schema:\n{json.dumps(schema)}"
        user = (context + "\n\n" if context else "") + prompt
        return parse_json(self._call([{"role": "system", "content": sys}, {"role": "user", "content": user}],
                                     max_tokens, True))

    def text(self, *, system, build, max_tokens=1500):
        context, prompt = build(self.context_chars)
        user = (context + "\n\n" if context else "") + prompt
        out = self._call([{"role": "system", "content": system}, {"role": "user", "content": user}],
                         max_tokens, False)
        return re.sub(r"<think>[\s\S]*?</think>", "", out).strip()

    def vision(self, image, media_type, prompt):
        if not self.supports_vision:
            raise LLMError(f"{self.label} can't read images with model '{self.model}'.")
        url = f"data:{media_type};base64,{base64.b64encode(image).decode()}"
        return self._call([{"role": "user", "content": [{"type": "text", "text": prompt},
                                                        {"type": "image_url", "image_url": {"url": url}}]}],
                          2500, False)


def _err_text(e) -> str:
    body = getattr(e, "body", None)
    parts = [getattr(e, "message", "") or str(e)]
    if body is not None:
        try:
            parts.append(json.dumps(body))
        except (TypeError, ValueError):
            parts.append(str(body))
    return " ".join(p for p in parts if p)


def _err_headers(e) -> dict:
    resp = getattr(e, "response", None)
    try:
        return dict(resp.headers) if resp is not None else {}
    except Exception:
        return {}


class OllamaLLM(BaseLLM):
    name = "ollama"
    label = "Ollama"
    supports_vision = True  # works with vision models (qwen3.5, gemma3/4, llava ...)

    def __init__(self, model: str, url: str, num_ctx: int):
        self.model, self.url, self.num_ctx = model, url.rstrip("/"), num_ctx
        self.context_chars = max(8000, int(num_ctx * 2.2))
        self._think_ok = True

    def _chat(self, messages, fmt=None) -> str:
        import httpx

        body = {"model": self.model, "messages": messages, "stream": False,
                "options": {"num_ctx": self.num_ctx}}
        if fmt is not None:
            body["format"] = fmt
        if self._think_ok:
            body["think"] = False  # faster, and keeps the JSON clean on "thinking" models
        try:
            r = httpx.post(f"{self.url}/api/chat", json=body, timeout=900)
        except httpx.HTTPError:
            raise Unreachable(f"Couldn't reach Ollama at {self.url}. Is the Ollama app running?")
        if r.status_code == 400 and "think" in r.text.lower() and self._think_ok:
            self._think_ok = False
            return self._chat(messages, fmt)
        if r.status_code == 404:
            raise BadConfig(f"Ollama model '{self.model}' isn't installed. Run: ollama pull {self.model}")
        if r.status_code >= 400:
            raise LLMError(f"Ollama error {r.status_code}: {r.text[:300]}")
        return (r.json().get("message") or {}).get("content", "").strip()

    def json(self, *, system, build, schema, max_tokens=4096):
        context, prompt = build(self.context_chars)
        sys = f"{system}\n\nRespond with ONE JSON object matching this schema:\n{json.dumps(schema)}"
        user = (context + "\n\n" if context else "") + prompt
        return parse_json(self._chat([{"role": "system", "content": sys}, {"role": "user", "content": user}],
                                     fmt=schema))

    def text(self, *, system, build, max_tokens=1500):
        context, prompt = build(self.context_chars)
        user = (context + "\n\n" if context else "") + prompt
        out = self._chat([{"role": "system", "content": system}, {"role": "user", "content": user}])
        return re.sub(r"<think>[\s\S]*?</think>", "", out).strip()

    def vision(self, image, media_type, prompt):
        return self._chat([{"role": "user", "content": prompt, "images": [base64.b64encode(image).decode()]}])


# ---------------------------------------------------------------------------
# The free chain
# ---------------------------------------------------------------------------
def _when(until: float) -> str:
    left = until - time.time()
    if left <= 90:
        return f"back in {max(1, int(left))} s"
    if left <= 3600:
        return f"back in about {int(round(left / 60))} min"
    dt = datetime.fromtimestamp(until)
    clock = dt.strftime("%I:%M %p").lstrip("0")
    day = "" if dt.date() == datetime.now().date() else " tomorrow" if \
        (dt.date() - datetime.now().date()).days == 1 else dt.strftime(" on %b %d")
    return f"back around {clock}{day}"


class FallbackLLM(BaseLLM):
    """Try providers in order; skip one while its usage limit is in effect."""

    name = "free"
    label = "Free models"

    def __init__(self, providers: list, missing: list):
        self.providers = providers               # ready to use, in order
        self.missing = missing                   # [(name, label)] skipped because no key is set
        self._state = {p.name: {"until": 0.0, "reason": "", "kind": ""} for p in providers}
        self._lock = threading.Lock()
        self._local = threading.local()
        self.context_chars = max((p.context_chars for p in providers), default=0)
        self.supports_vision = any(p.supports_vision for p in providers)
        self.model = " → ".join(p.label for p in providers)

    @property
    def available(self) -> bool:
        return bool(self.providers)

    # -- state ----------------------------------------------------------------------------
    def _block(self, p: BaseLLM, until: float, reason: str, kind: str) -> None:
        with self._lock:
            self._state[p.name] = {"until": until, "reason": reason, "kind": kind}

    def _clear(self, p: BaseLLM) -> None:
        with self._lock:
            self._state[p.name] = {"until": 0.0, "reason": "", "kind": ""}

    def _blocked(self, p: BaseLLM) -> Optional[str]:
        with self._lock:
            st = dict(self._state[p.name])
        if st["until"] > time.time():
            return f"{st['reason']}, {_when(st['until'])}"
        return None

    def current(self) -> Optional[BaseLLM]:
        return next((p for p in self.providers if not self._blocked(p)), None)

    # -- running a request ----------------------------------------------------------------
    def _run(self, op, need_vision: bool = False):
        notes = []
        last = self.providers[-1] if self.providers else None
        for p in self.providers:
            if need_vision and not p.supports_vision:
                continue
            blocked = self._blocked(p)
            if blocked:
                notes.append(f"{p.label}: {blocked}")
                continue
            for attempt in (0, 1):
                try:
                    out = op(p)
                except RateLimited as e:
                    short = (not e.daily and e.retry_after is not None
                             and e.retry_after <= config.FREE_WAIT_SECONDS)
                    if attempt == 0 and short:
                        time.sleep(e.retry_after + 0.5)  # a per-minute blip: wait instead of switching
                        continue
                    reason = "daily free limit reached" if e.daily else "free usage limit reached"
                    self._block(p, e.until or time.time() + 60, reason, "limit")
                    notes.append(f"{p.label}: {reason}, {_when(e.until or time.time() + 60)}")
                    break
                except TooLarge:
                    notes.append(f"{p.label}: this request is too big for its free allowance")
                    break
                except BadConfig as e:
                    if p is not last:
                        self._block(p, time.time() + 600, str(e).rstrip("."), "config")
                    notes.append(f"{p.label}: {e}")
                    break
                except Unreachable as e:
                    if p is not last:
                        self._block(p, time.time() + 30, "not responding", "down")
                    notes.append(f"{p.label}: {e}")
                    break
                except LLMError as e:
                    notes.append(f"{p.label}: {e}")
                    break
                else:
                    self._clear(p)
                    self._local.used = p
                    return out
        if not notes:
            raise LLMError("None of the free services set up here can read images.")
        raise LLMError("None of the free AI services could answer right now. "
                       + "; ".join(notes) + ". Try again later, or check Settings.")

    def json(self, *, system, build, schema, max_tokens=4096):
        return self._run(lambda p: p.json(system=system, build=build, schema=schema, max_tokens=max_tokens))

    def text(self, *, system, build, max_tokens=1500):
        return self._run(lambda p: p.text(system=system, build=build, max_tokens=max_tokens))

    def vision(self, image, media_type, prompt):
        return self._run(lambda p: p.vision(image, media_type, prompt), need_vision=True)

    def used_label(self) -> str:
        p = getattr(self._local, "used", None)
        return p.used_label() if p else "free"

    def describe(self) -> dict:
        chain = []
        cur = self.current()
        for p in self.providers:
            blocked = self._blocked(p)
            with self._lock:
                st = dict(self._state[p.name])
            chain.append({"name": p.name, "label": p.label, "model": p.model,
                          "state": st["kind"] if blocked else ("active" if p is cur else "ready"),
                          "detail": blocked or ("in use" if p is cur else "standing by"),
                          "until": st["until"] if blocked else None})
        for name, label in self.missing:
            chain.append({"name": name, "label": label, "model": "", "state": "missing",
                          "detail": "no key yet, skipped", "until": None})
        return {"provider": "free", "model": cur.label if cur else "none available",
                "vision": self.supports_vision, "chain": chain}


FREE_LABELS = {"groq": "Groq", "gemini": "Gemini", "openrouter": "OpenRouter", "ollama": "Ollama"}


def build_free_chain(s: dict) -> FallbackLLM:
    providers, missing = [], []
    timeout = config.FREE_TIMEOUT
    for name in s["free_chain"]:
        if name == "groq":
            key = os.getenv("GROQ_API_KEY")
            if not key:
                missing.append((name, "Groq"))
                continue
            model = s["groq_model"]
            providers.append(OpenAICompatLLM(
                name="groq", label="Groq", model=model, api_key=key,
                base_url=os.getenv("GROQ_BASE_URL", "https://api.groq.com/openai/v1"),
                context_chars=s["groq_input_chars"], vision=bool(re.search(r"llama-4|vision|scout|maverick", model)),
                max_tokens=2800, extra={"reasoning_effort": "low"} if "gpt-oss" in model else {},
                retries=0, timeout=timeout))
        elif name == "gemini":
            key = os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")
            if not key:
                missing.append((name, "Gemini"))
                continue
            providers.append(OpenAICompatLLM(
                name="gemini", label="Gemini", model=s["gemini_model"], api_key=key,
                base_url=os.getenv("GEMINI_BASE_URL", "https://generativelanguage.googleapis.com/v1beta/openai/"),
                context_chars=s["gemini_input_chars"], vision=True, max_tokens=8192, out_factor=2.0,
                extra={"reasoning_effort": "low"}, retries=0, timeout=timeout,
                daily_reset_tz="America/Los_Angeles"))  # Gemini daily quotas reset at midnight Pacific
        elif name == "openrouter":
            key = os.getenv("OPENROUTER_API_KEY")
            if not key:
                missing.append((name, "OpenRouter"))
                continue
            providers.append(OpenAICompatLLM(
                name="openrouter", label="OpenRouter", model=s["openrouter_model"], api_key=key,
                base_url=os.getenv("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1"),
                context_chars=s["openrouter_input_chars"], vision=True, max_tokens=4096,
                headers={"HTTP-Referer": "https://github.com/regmi-sijan/LearnDeck", "X-Title": "LearnDeck"},
                retries=0, timeout=timeout, daily_reset_tz="UTC"))  # free-model days are UTC days
        elif name == "ollama":
            providers.append(OllamaLLM(s["ollama_model"], s["ollama_url"], s["ollama_num_ctx"]))
    return FallbackLLM(providers, missing)


# ---------------------------------------------------------------------------
# Choosing the provider
# ---------------------------------------------------------------------------
_lock = threading.Lock()
_cached: Optional[tuple] = None
_KEY_VARS = ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GROQ_API_KEY", "GEMINI_API_KEY", "GOOGLE_API_KEY",
             "OPENROUTER_API_KEY")


def get_llm() -> BaseLLM:
    """Build (and cache) the provider described by the current settings."""
    global _cached
    s = config.llm_settings()
    key = (tuple(sorted((k, tuple(v) if isinstance(v, list) else v) for k, v in s.items())),
           tuple(os.getenv(k, "")[-6:] for k in _KEY_VARS))
    with _lock:
        if _cached and _cached[0] == key:
            return _cached[1]
        provider = s["provider"]
        if provider == "auto":
            if os.getenv("ANTHROPIC_API_KEY"):
                provider = "anthropic"
            elif os.getenv("OPENAI_API_KEY"):
                provider = "openai"
            elif any(os.getenv(k) for k in ("GROQ_API_KEY", "GEMINI_API_KEY", "GOOGLE_API_KEY", "OPENROUTER_API_KEY")):
                provider = "free"
            else:
                provider = "none"
        try:
            if provider == "anthropic" and os.getenv("ANTHROPIC_API_KEY"):
                llm: BaseLLM = AnthropicLLM(s["anthropic_model"])
            elif provider == "openai" and (os.getenv("OPENAI_API_KEY") or s["openai_base_url"]):
                base = s["openai_base_url"]
                llm = OpenAICompatLLM(name="openai", label="OpenAI", model=s["openai_model"],
                                      api_key=os.getenv("OPENAI_API_KEY"), base_url=base,
                                      out_factor=2.0,
                                      token_param="max_completion_tokens" if not base else "max_tokens")
            elif provider == "ollama":
                llm = OllamaLLM(s["ollama_model"], s["ollama_url"], s["ollama_num_ctx"])
            elif provider == "free":
                llm = build_free_chain(s)
            else:
                llm = NoLLM()
        except ImportError as e:
            raise LLMError(f"The '{e.name}' package is missing. Run: pip install -r requirements.txt")
        _cached = (key, llm)
        return llm
