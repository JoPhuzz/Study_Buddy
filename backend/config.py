"""Configuration, read once from the environment (.env).

Study Buddy is a closed-world reader. You show it something across a series of
deliberate captures; it reads each one as it lands, compacts them into a brief when
you say you're done, and then answers ONLY from that brief.

Note what is NOT configurable here: there is no search provider, no embedding model,
no research toggle. The promise this buddy makes is that it will not tell you
anything you did not show it, and the way that promise is kept is that no code in
this repository can reach the open web except `fetch.py`, which pulls a page YOU
named and files it as another capture.
"""
from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import load_dotenv

load_dotenv()


def _get(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


@dataclass(frozen=True)
class Config:
    app_name: str = _get("APP_NAME", "Study Buddy")

    anthropic_api_key: str = _get("ANTHROPIC_API_KEY")

    # Two tiers. READING a capture is the load-bearing call — the note it produces is
    # the only record that survives, so everything downstream inherits its mistakes.
    # It defaults to the good model on purpose; READ_MODEL=fast trades accuracy for
    # about a fifth of the cost on a long scroll-capture.
    deep_model: str = _get("DEEP_MODEL", "") or "claude-sonnet-5"
    fast_model: str = _get("FAST_MODEL", "") or "claude-haiku-4-5"
    read_tier: str = (_get("READ_TIER", "deep") or "deep").lower()

    # A local model for the answering half. Any server speaking the OpenAI-style
    # chat-completions protocol (Ollama, LM Studio, llama.cpp, vLLM). URL and MODEL
    # switch it on; the key is whatever the server or its tunnel wants, or empty.
    # Reading captures NEVER moves here — that is the vision model's job whatever this
    # says. Briefs stay on the vision model too unless LOCAL_BRIEFS=1: compaction is
    # the one text job where a weaker model silently costs you detail for every answer
    # after, and it happens once per subject rather than once per question.
    local_llm_url: str = _get("LOCAL_LLM_URL")
    local_llm_key: str = _get("LOCAL_LLM_KEY")
    local_llm_model: str = _get("LOCAL_LLM_MODEL")
    local_briefs: bool = _get("LOCAL_BRIEFS", "").lower() in ("1", "true", "yes", "on")

    # Data — the working copy of the brain.
    data_dir: str = _get("DATA_DIR", "webdata")
    db_path: str = _get("DB_PATH", "") or os.path.join(_get("DATA_DIR", "webdata"), "study.db")

    # The brain repo (persistence across ephemeral Railway containers). Its OWN file:
    # a captured pricing page must not land in another buddy's memory.
    knowledge_repo: str = _get("KNOWLEDGE_REPO", "https://github.com/JoPhuzz/buddies-backups.git")
    knowledge_token: str = _get("KNOWLEDGE_TOKEN")
    knowledge_subpath: str = _get("KNOWLEDGE_SUBPATH", "study-web/latest.tar.gz")
    auto_push_seconds: int = int(_get("AUTO_PUSH_SECONDS", "600") or "600")

    # Auth — one password gates the whole app (it's on the open internet). Empty = open (dev).
    access_password: str = _get("ACCESS_PASSWORD")

    # Server
    host: str = _get("HOST", "127.0.0.1")
    port: int = int(_get("PORT", "8080") or "8080")

    def read_model(self) -> str:
        return self.fast_model if self.read_tier == "fast" else self.deep_model

    def local_llm_wanted(self) -> bool:
        """Either half set means they meant to set it up; a half-set one should fail
        loudly at boot rather than quietly answer from Anthropic."""
        return bool(self.local_llm_url or self.local_llm_model)


config = Config()
