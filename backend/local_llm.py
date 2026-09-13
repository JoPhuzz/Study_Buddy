"""The local adapter — your own model, over the chat protocol every local server speaks.

Ollama, LM Studio, llama.cpp, vLLM and LiteLLM all serve the OpenAI-style
`/v1/chat/completions` endpoint, so one adapter covers them. It presents the same
`ask()` as the Anthropic adapter and the engine cannot tell them apart — which is the
point: answering is routed here by configuration, reading is not, and neither side
knows about the other.

What it will NOT do is read an image. Reading a capture is the load-bearing call — the
note it produces is the only record that survives — and it stays on the vision model
whatever this is set to. An image arriving here is a bug in the router, so it is
refused loudly rather than silently sent to a model that would describe it badly.

And, as with the other adapter, there is no tool path in this file. A closed world is a
property of what the code cannot do.
"""
from __future__ import annotations

import json
import re
import urllib.error
import urllib.request

from .llm import TOO_LONG, Answer, LLMError

REQUEST_TIMEOUT_S = 300.0     # a 14B model on a Mac Mini reading a 12k-token brief takes its time

# Thinking models (Qwen3 among them) put their reasoning in a <think> block ahead of the
# answer. It is billed out of the same max_tokens as the answer — the exact trap Sonnet's
# adaptive thinking set, in a different costume — so a small budget can come back as
# nothing but an unfinished thought.
_THINK_RE = re.compile(r"<think>.*?</think>\s*", re.S)
_OPEN_THINK_RE = re.compile(r"<think>(?!.*</think>)", re.S)


def endpoint(url: str) -> str:
    """Accept the base, the /v1 base, or the full path — people paste all three."""
    u = (url or "").strip().rstrip("/")
    if not u:
        return ""
    if u.endswith("/chat/completions"):
        return u
    if u.endswith("/v1"):
        return u + "/chat/completions"
    return u + "/v1/chat/completions"


def strip_thinking(text: str) -> tuple[str, bool]:
    """(answer, spent_it_all_thinking)."""
    text = text or ""
    if _OPEN_THINK_RE.search(text):
        return "", True
    return _THINK_RE.sub("", text).strip(), False


class LocalLLM:
    def __init__(self, url: str, key: str, model: str) -> None:
        if not url:
            raise RuntimeError("LOCAL_LLM_URL is not set.")
        if not model:
            raise RuntimeError("LOCAL_LLM_MODEL is not set.")
        self.url = endpoint(url)
        self.key = key or ""
        self.model = model
        # The engine asks its adapter which model to use by tier. There is one here.
        self.deep_model = model
        self.fast_model = model

    def ask(self, *, user: str, system: str = "", cached_system: str = "",
            images: list[bytes] | None = None, history: list[dict] | None = None,
            model: str | None = None, max_tokens: int = 800) -> Answer:
        if images:
            raise LLMError("The local model doesn't read images — captures are read by the "
                           "vision model. This is a routing bug; please report it.")
        used = model or self.model
        # No prompt cache on this side, so the two halves simply become one system message.
        sys_text = "\n\n".join(p for p in (cached_system, system) if p).strip()
        messages: list[dict] = []
        if sys_text:
            messages.append({"role": "system", "content": sys_text})
        for turn in (history or []):
            q, a = (turn.get("question") or "").strip(), (turn.get("answer") or "").strip()
            if q and a:
                messages.append({"role": "user", "content": q})
                messages.append({"role": "assistant", "content": a})
        messages.append({"role": "user", "content": user})

        body = {"model": used, "messages": messages, "max_tokens": max_tokens,
                "stream": False}
        text, finish, usage = self._post(body)
        truncated = finish == "length"
        if truncated and text:
            # Same answer, same budget, about half the length — never a bigger budget,
            # which would silently override the length the user chose.
            retry = dict(body)
            retry["messages"] = [dict(m) for m in messages]
            if retry["messages"][0]["role"] == "system":
                retry["messages"][0]["content"] += TOO_LONG
            else:
                retry["messages"].insert(0, {"role": "system", "content": TOO_LONG.strip()})
            try:
                text2, finish2, usage2 = self._post(retry)
            except LLMError:
                text2 = ""
            if text2:
                text, finish, usage = text2, finish2, usage2
                truncated = finish == "length"
        if not text:
            raise LLMError(f"{used} used its whole budget without producing a reply. "
                           "Try a longer answer length.")
        return Answer(text=text, model=used,
                      prompt_tokens=int(usage.get("prompt_tokens") or 0),
                      completion_tokens=int(usage.get("completion_tokens") or 0),
                      truncated=truncated, cost=0.0)

    def _post(self, body: dict) -> tuple[str, str, dict]:
        """One request. Returns (answer text, finish_reason, usage)."""
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        if self.key:
            headers["Authorization"] = f"Bearer {self.key}"
        req = urllib.request.Request(self.url, data=json.dumps(body).encode("utf-8"),
                                     headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT_S) as r:
                raw = r.read()
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                detail = exc.read(600).decode("utf-8", "replace").strip()
            except Exception:
                pass
            hint = {401: " — check LOCAL_LLM_KEY", 403: " — check LOCAL_LLM_KEY",
                    404: " — check LOCAL_LLM_URL and LOCAL_LLM_MODEL"}.get(exc.code, "")
            raise LLMError(f"Local model returned {exc.code}{hint}. {detail}"[:400]) from exc
        except TimeoutError as exc:
            raise LLMError(f"The local model took longer than {REQUEST_TIMEOUT_S:.0f}s. "
                           "It may be loading, or the question may need a shorter mode.") from exc
        except urllib.error.URLError as exc:
            reason = getattr(exc, "reason", exc)
            if isinstance(reason, TimeoutError) or "timed out" in str(reason).lower():
                raise LLMError(f"The local model took longer than {REQUEST_TIMEOUT_S:.0f}s. "
                               "It may be loading, or the question may need a shorter "
                               "mode.") from exc
            raise LLMError(f"Can't reach the local model at {self.url}: {reason}. Is the "
                           "Mac awake and the tunnel up?") from exc
        except Exception as exc:  # noqa: BLE001
            raise LLMError(f"Local model request failed: {exc}") from exc

        try:
            data = json.loads(raw.decode("utf-8", "replace"))
        except ValueError as exc:
            raise LLMError("The local model replied with something that isn't JSON — is "
                           "LOCAL_LLM_URL pointing at a chat-completions server?") from exc
        if isinstance(data, dict) and data.get("error"):
            err = data["error"]
            raise LLMError("Local model error: "
                           + str(err.get("message") if isinstance(err, dict) else err)[:300])
        try:
            choice = data["choices"][0]
            msg = choice.get("message") or {}
            content = msg.get("content") or ""
            finish = choice.get("finish_reason") or ""
        except (KeyError, IndexError, TypeError) as exc:
            raise LLMError("The local model's reply had no choices in it.") from exc

        text, all_thinking = strip_thinking(content)
        if all_thinking:
            raise LLMError(f"{body.get('model')} spent its whole budget thinking and never "
                           "answered. Turn thinking off on the server (Ollama: set think "
                           "to false, or use a non-thinking model) — the budget here is "
                           "meant for the answer.")
        return text, finish, (data.get("usage") or {})
