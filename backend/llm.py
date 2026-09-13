"""The Anthropic adapter — vision and text, and nothing else.

Note what is missing: there is no web_search tool anywhere in this file, and no code
path that could add one. That absence IS the product. A closed-world promise enforced
by a paragraph in a prompt is a promise the model can talk itself out of; a promise
enforced by the tool simply not existing cannot be.

Three lessons carried over from Program Buddy, each of which cost a real session to
find:
  * text blocks are joined with a blank line, never "" — a response that brackets a
    tool call comes back as several blocks, and gluing them welds two sentences into
    one ("...each item does:Nice — I can read every field...");
  * an answer cut off at max_tokens is retried for the SAME answer, complete but more
    concise, in the SAME budget — tripling the budget silently overrides the length
    the user chose;
  * the stable half of a system prompt is cached, because it is re-sent on every call
    of a session and a cache read costs a tenth of a fresh one.
"""
from __future__ import annotations

import base64
from dataclasses import dataclass, field

import anthropic

REQUEST_TIMEOUT_S = 180.0

# $ per million tokens (input, output). Cache writes bill at 1.25x input, reads at 0.1x.
# Verified against the published table, not recalled — two of these were wrong when
# written from memory, and reading a capture is the call whose cost decides whether a
# forty-shot scroll is affordable.
PRICES: dict[str, tuple[float, float]] = {
    "claude-fable-5-1": (10.0, 50.0),
    "claude-fable-5": (10.0, 50.0),
    "claude-opus-5": (5.0, 25.0),
    "claude-opus-4-8": (5.0, 25.0),
    "claude-opus-4-7": (5.0, 25.0),
    "claude-opus-4-6": (5.0, 25.0),
    "claude-sonnet-5": (2.0, 10.0),
    "claude-sonnet-4-6": (3.0, 15.0),
    "claude-haiku-4-5": (1.0, 5.0),
}
_DEFAULT_PRICE = (3.0, 15.0)


class LLMError(RuntimeError):
    """Something the user should see, in words they can act on."""


@dataclass
class Answer:
    text: str
    model: str
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cache_read: int = 0
    cache_write: int = 0
    cost: float = 0.0
    truncated: bool = False
    blocks: list = field(default_factory=list)


def price(model: str, prompt: int, completion: int, cache_read: int = 0,
          cache_write: int = 0) -> float:
    pin, pout = PRICES.get(model, _DEFAULT_PRICE)
    return round(
        (prompt * pin + completion * pout + cache_write * pin * 1.25 + cache_read * pin * 0.1)
        / 1_000_000.0, 6)


def _media_type(data: bytes) -> str:
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:3] == b"GIF":
        return "image/gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return "image/jpeg"


def _image_block(data: bytes) -> dict:
    return {"type": "image", "source": {"type": "base64", "media_type": _media_type(data),
                                        "data": base64.b64encode(data).decode("ascii")}}


def text_of(resp) -> str:
    """Join a response's text blocks with a blank line.

    NEVER "". A response whose blocks sit either side of something else comes back as
    separate paragraphs, and gluing them produces a welded, broken line. This is the
    exact bug that shipped in Program Buddy.
    """
    parts = [(getattr(b, "text", "") or "").strip()
             for b in (getattr(resp, "content", None) or [])]
    return "\n\n".join(p for p in parts if p).strip()


# Sonnet 5 runs adaptive thinking when `thinking` is omitted, and thinking is billed out
# of the SAME max_tokens as the answer. That makes every mode's budget mean "answer
# length minus however much the model decided to think" — invisibly, and only on the hard
# questions, which are exactly the ones you wanted a full answer to. A comparison over an
# 18k-character brief spent all 450 tokens and came back cut off mid-sentence; a tighter
# budget came back as thinking with no answer attached at all.
#
# Here the budget should mean the answer, so thinking is off. (The documented misbehaviour
# with thinking disabled — tool calls written into visible text — is an Opus 5 problem,
# and this app has no tools at all.) A model that rejects the parameter is remembered and
# retried without it.
_NO_THINKING = {"type": "disabled"}
_thinking_unsupported: set[str] = set()


def _is_thinking_param_error(exc) -> bool:
    msg = (getattr(exc, "message", None) or str(exc)).lower()
    return "thinking" in msg and ("unsupported" in msg or "not supported" in msg
                                 or "invalid" in msg or "unexpected" in msg)


def _create(client, kwargs: dict):
    """One request, retrying without the thinking parameter if this model won't take it."""
    model = kwargs.get("model", "")
    if model in _thinking_unsupported:
        return client.messages.create(**kwargs)
    try:
        return client.messages.create(**dict(kwargs, thinking=_NO_THINKING))
    except anthropic.APIError as exc:
        if not _is_thinking_param_error(exc):
            raise
        _thinking_unsupported.add(model)
        return client.messages.create(**kwargs)


TOO_LONG = (
    "\n\nYour previous attempt ran past the space available and was cut off mid-sentence. "
    "Give the SAME answer in ABOUT HALF the length and make sure the final sentence is "
    "finished. Keep every fact that changes the answer; cut the elaboration, the examples "
    "and the caveats. Merge related points into one line rather than listing them "
    "separately, and drop any preamble entirely. Finishing matters more than completeness: "
    "a short whole answer beats a long one that stops mid-sentence."
)


class LLM:
    def __init__(self, api_key: str, deep_model: str, fast_model: str) -> None:
        if not api_key:
            raise RuntimeError("ANTHROPIC_API_KEY is not set.")
        self.client = anthropic.Anthropic(api_key=api_key, timeout=REQUEST_TIMEOUT_S)
        self.deep_model = deep_model
        self.fast_model = fast_model

    # --- the one call everything goes through --------------------------------------
    def ask(self, *, user: str, system: str = "", cached_system: str = "",
            images: list[bytes] | None = None, history: list[dict] | None = None,
            model: str | None = None, max_tokens: int = 800) -> Answer:
        """One request.

        `cached_system` is the half that is identical on every call of a session and is
        marked for caching; `system` is the part that changes. Splitting them explicitly
        beats hiding a sentinel in the string — the caller knows which half is stable,
        and nothing has to be stripped back out before the model sees it.
        """
        used = model or self.deep_model
        content: list[dict] = []
        for img in (images or []):
            content.append(_image_block(img))
        content.append({"type": "text", "text": user})

        messages: list[dict] = []
        for turn in (history or []):
            q, a = (turn.get("question") or "").strip(), (turn.get("answer") or "").strip()
            if q and a:
                messages.append({"role": "user", "content": q})
                messages.append({"role": "assistant", "content": a})
        messages.append({"role": "user", "content": content})

        sys_param: object
        if cached_system and system:
            sys_param = [
                {"type": "text", "text": cached_system,
                 "cache_control": {"type": "ephemeral"}},
                {"type": "text", "text": system},
            ]
        elif cached_system:
            sys_param = [{"type": "text", "text": cached_system,
                          "cache_control": {"type": "ephemeral"}}]
        else:
            sys_param = system

        kwargs = dict(model=used, system=sys_param, messages=messages, max_tokens=max_tokens)
        try:
            resp = _create(self.client, kwargs)
            truncated = getattr(resp, "stop_reason", "") == "max_tokens"
            if truncated and text_of(resp):
                resp = self._retry_shorter(kwargs, resp)
                truncated = getattr(resp, "stop_reason", "") == "max_tokens"
        except anthropic.APITimeoutError as exc:
            raise LLMError(
                f"The model took longer than {REQUEST_TIMEOUT_S:.0f}s to answer. Try again."
            ) from exc
        except anthropic.APIError as exc:
            raise LLMError(
                f"Anthropic API error: {getattr(exc, 'message', None) or exc}") from exc

        text = text_of(resp)
        if not text:
            raise LLMError(
                f"{used} used its whole budget without producing a reply. "
                "Try a longer answer length.")
        u = getattr(resp, "usage", None)
        pt = getattr(u, "input_tokens", 0) or 0
        ct = getattr(u, "output_tokens", 0) or 0
        cr = getattr(u, "cache_read_input_tokens", 0) or 0
        cw = getattr(u, "cache_creation_input_tokens", 0) or 0
        return Answer(text=text, model=used, prompt_tokens=pt, completion_tokens=ct,
                      cache_read=cr, cache_write=cw, truncated=truncated,
                      cost=price(used, pt, ct, cr, cw))

    def _retry_shorter(self, kwargs: dict, first):
        """Ask again for the same answer in the same budget. Never returns worse."""
        retry = dict(kwargs)
        sysv = retry.get("system")
        if isinstance(sysv, list):
            blocks = [dict(b) for b in sysv]
            # onto the TAIL — appending to the cached block would change the prefix and
            # throw the cache away on every retry
            blocks[-1] = dict(blocks[-1])
            blocks[-1]["text"] = (blocks[-1].get("text") or "") + TOO_LONG
            retry["system"] = blocks
        else:
            retry["system"] = (sysv or "") + TOO_LONG
        try:
            second = _create(self.client, retry)
        except anthropic.APIError:
            return first
        return second if text_of(second) else first
