"""AI providers behind one small interface.

    llm = get_llm()
    llm.json(system=..., context=..., prompt=..., schema=...)  -> dict
    llm.text(system=..., context=..., prompt=...)              -> str
    llm.vision(image_bytes, media_type, prompt)                -> str

`context` is the large, stable document text. Providers put it where it can be
cached (Anthropic prompt caching, OpenAI prefix caching) so drilling into many
slides of the same document stays fast and cheap.
"""
from __future__ import annotations

import base64
import json
import os
import re
import threading

from . import config


class LLMError(Exception):
    pass


def parse_json(text: str) -> dict:
    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start >= 0 and end > start:
            try:
                return json.loads(text[start:end + 1])
            except json.JSONDecodeError:
                pass
    raise LLMError("The AI returned malformed JSON. Try again.")


class BaseLLM:
    name = "none"
    model = ""
    context_chars = 0          # how much document text fits comfortably in one request
    supports_vision = False

    @property
    def available(self) -> bool:
        return self.name != "none"

    def json(self, *, system: str, context: str, prompt: str, schema: dict, max_tokens: int = 4096) -> dict:
        raise LLMError("No AI provider is configured.")

    def text(self, *, system: str, context: str, prompt: str, max_tokens: int = 1500) -> str:
        raise LLMError("No AI provider is configured.")

    def vision(self, image: bytes, media_type: str, prompt: str) -> str:
        raise LLMError("This AI provider can't read images.")

    def describe(self) -> dict:
        return {"provider": self.name, "model": self.model, "vision": self.supports_vision}


class NoLLM(BaseLLM):
    pass


class AnthropicLLM(BaseLLM):
    name = "anthropic"
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
        try:
            return self.client.messages.create(model=self.model, **kw)
        except self._anthropic.AuthenticationError:
            raise LLMError("Your Anthropic API key was rejected. Check it in Settings.")
        except self._anthropic.NotFoundError as e:
            raise LLMError(f"Model '{self.model}' was not found. Change ANTHROPIC_MODEL in Settings. ({e})")
        except self._anthropic.APIStatusError as e:
            raise LLMError(f"Anthropic API error {e.status_code}: {getattr(e, 'message', e)}")
        except self._anthropic.APIConnectionError:
            raise LLMError("Couldn't reach the Anthropic API. Check your internet connection.")

    def json(self, *, system, context, prompt, schema, max_tokens=4096):
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

    def text(self, *, system, context, prompt, max_tokens=1500):
        resp = self._call(max_tokens=max_tokens, system=self._system(system, context),
                          messages=[{"role": "user", "content": prompt}])
        return "".join(b.text for b in resp.content if b.type == "text").strip()

    def vision(self, image, media_type, prompt):
        resp = self._call(max_tokens=2500, messages=[{"role": "user", "content": [
            {"type": "image", "source": {"type": "base64", "media_type": media_type,
                                         "data": base64.b64encode(image).decode()}},
            {"type": "text", "text": prompt}]}])
        return "".join(b.text for b in resp.content if b.type == "text").strip()


class OpenAILLM(BaseLLM):
    name = "openai"
    context_chars = 160_000
    supports_vision = True

    def __init__(self, model: str, base_url: str | None):
        import openai

        self._openai = openai
        # A local OpenAI-compatible server (LM Studio, vLLM ...) may not need a real key.
        self.client = openai.OpenAI(api_key=os.getenv("OPENAI_API_KEY") or "not-needed",
                                    base_url=base_url, max_retries=3)
        self.model = model

    def _call(self, messages, max_tokens, json_mode):
        kw = {"model": self.model, "messages": messages, "max_completion_tokens": max_tokens}
        if json_mode:
            kw["response_format"] = {"type": "json_object"}
        try:
            r = self.client.chat.completions.create(**kw)
        except self._openai.AuthenticationError:
            raise LLMError("Your OpenAI API key was rejected. Check it in Settings.")
        except self._openai.APIStatusError as e:
            raise LLMError(f"OpenAI API error {e.status_code}: {getattr(e, 'message', e)}")
        except self._openai.APIConnectionError:
            raise LLMError("Couldn't reach the OpenAI API. Check your internet connection.")
        return (r.choices[0].message.content or "").strip()

    def json(self, *, system, context, prompt, schema, max_tokens=4096):
        sys = f"{system}\n\nRespond with ONE JSON object only, matching this JSON schema:\n{json.dumps(schema)}"
        user = (context + "\n\n" if context else "") + prompt
        return parse_json(self._call([{"role": "system", "content": sys}, {"role": "user", "content": user}],
                                     max_tokens * 2, True))

    def text(self, *, system, context, prompt, max_tokens=1500):
        user = (context + "\n\n" if context else "") + prompt
        return self._call([{"role": "system", "content": system}, {"role": "user", "content": user}],
                          max_tokens * 2, False)

    def vision(self, image, media_type, prompt):
        url = f"data:{media_type};base64,{base64.b64encode(image).decode()}"
        return self._call([{"role": "user", "content": [{"type": "text", "text": prompt},
                                                        {"type": "image_url", "image_url": {"url": url}}]}],
                          4000, False)


class OllamaLLM(BaseLLM):
    name = "ollama"
    supports_vision = True  # only works with vision models (llava, llama3.2-vision, qwen2.5vl ...)

    def __init__(self, model: str, url: str, num_ctx: int):
        self.model, self.url, self.num_ctx = model, url.rstrip("/"), num_ctx
        self.context_chars = max(8000, int(num_ctx * 2.2))

    def _chat(self, messages, fmt=None) -> str:
        import httpx

        body = {"model": self.model, "messages": messages, "stream": False,
                "options": {"num_ctx": self.num_ctx}}
        if fmt is not None:
            body["format"] = fmt
        try:
            r = httpx.post(f"{self.url}/api/chat", json=body, timeout=600)
        except httpx.HTTPError:
            raise LLMError(f"Couldn't reach Ollama at {self.url}. Is the Ollama app running?")
        if r.status_code == 404:
            raise LLMError(f"Ollama model '{self.model}' isn't installed. Run: ollama pull {self.model}")
        if r.status_code >= 400:
            raise LLMError(f"Ollama error {r.status_code}: {r.text[:300]}")
        return (r.json().get("message") or {}).get("content", "").strip()

    def json(self, *, system, context, prompt, schema, max_tokens=4096):
        sys = f"{system}\n\nRespond with ONE JSON object matching this schema:\n{json.dumps(schema)}"
        user = (context + "\n\n" if context else "") + prompt
        return parse_json(self._chat([{"role": "system", "content": sys}, {"role": "user", "content": user}],
                                     fmt=schema))

    def text(self, *, system, context, prompt, max_tokens=1500):
        user = (context + "\n\n" if context else "") + prompt
        return self._chat([{"role": "system", "content": system}, {"role": "user", "content": user}])

    def vision(self, image, media_type, prompt):
        return self._chat([{"role": "user", "content": prompt, "images": [base64.b64encode(image).decode()]}])


_lock = threading.Lock()
_cached: tuple[tuple, BaseLLM] | None = None


def get_llm() -> BaseLLM:
    """Build (and cache) the provider described by the current settings."""
    global _cached
    s = config.llm_settings()
    key = (tuple(sorted(s.items())), os.getenv("ANTHROPIC_API_KEY", "")[-6:], os.getenv("OPENAI_API_KEY", "")[-6:])
    with _lock:
        if _cached and _cached[0] == key:
            return _cached[1]
        provider = s["provider"]
        if provider == "auto":
            if os.getenv("ANTHROPIC_API_KEY"):
                provider = "anthropic"
            elif os.getenv("OPENAI_API_KEY"):
                provider = "openai"
            else:
                provider = "none"
        try:
            if provider == "anthropic" and os.getenv("ANTHROPIC_API_KEY"):
                llm: BaseLLM = AnthropicLLM(s["anthropic_model"])
            elif provider == "openai" and (os.getenv("OPENAI_API_KEY") or s["openai_base_url"]):
                llm = OpenAILLM(s["openai_model"], s["openai_base_url"])
            elif provider == "ollama":
                llm = OllamaLLM(s["ollama_model"], s["ollama_url"], s["ollama_num_ctx"])
            else:
                llm = NoLLM()
        except ImportError as e:
            raise LLMError(f"The '{e.name}' package is missing. Run: pip install -r requirements.txt")
        _cached = (key, llm)
        return llm
