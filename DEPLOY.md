# Deploying Study Buddy to Railway

One service, Dockerfile-detected, HTTPS out of the box (HTTPS is what lets the browser
grant screen sharing at all).

## 1. Create the service

Railway → New Project → Deploy from GitHub repo → `JoPhuzz/Study_Buddy`.
It detects the Dockerfile. Railway injects `$PORT` — do **not** set PORT or HOST yourself.

## 2. Variables

| Variable | Value |
|---|---|
| `ANTHROPIC_API_KEY` | the only model key it needs |
| `ACCESS_PASSWORD` | the one password that gates the whole app — set it, this is the open internet |
| `KNOWLEDGE_REPO` | `https://github.com/JoPhuzz/buddies-backups.git` |
| `KNOWLEDGE_TOKEN` | fine-grained PAT, **Contents: Read AND Write**, scoped to that one repo |
| `KNOWLEDGE_SUBPATH` | `study-web/latest.tar.gz` (the default — its own brain, separate from the other buddies) |
| `AUTO_PUSH_SECONDS` | `600` (default) — how often a dirty brain is pushed back |

Optional: `DEEP_MODEL` / `FAST_MODEL` (no date suffixes), and `READ_TIER`.

**`READ_TIER` is the one worth understanding.** Reading a capture is the load-bearing
call: its notes are the only record that survives, and the brief and every answer inherit
whatever it missed. It defaults to `deep`. Setting `READ_TIER=fast` cuts a long
scroll-capture to roughly a fifth of the cost, at the price of a less careful reading —
reasonable for skimming a blog post, a bad trade for a contract or a dense table.

## 2b. Answering from your own model (optional)

The answering half — questions against a sealed brief — is text-only, and it is the half
that happens over and over. It can run on a model of your own for nothing:

| Variable | Value |
|---|---|
| `LOCAL_LLM_URL` | where the server is — the base (`https://mini.example.com`), its `/v1`, or the full `/v1/chat/completions` path; all three work |
| `LOCAL_LLM_MODEL` | the model name as the server knows it (`qwen3:14b`, whatever LM Studio calls yours) |
| `LOCAL_LLM_KEY` | the bearer token the server or its tunnel wants — leave unset if nothing does |
| `LOCAL_BRIEFS` | `1` to compact briefs there too; default is to keep that on Sonnet |

Anything speaking the OpenAI-style chat protocol works: Ollama, LM Studio, llama.cpp,
vLLM, LiteLLM.

**What moves and what doesn't.** Questions move. Reading a capture never does — that is
the vision model's job and the one call every answer inherits, and the app will refuse
rather than send an image to a model that can't see. Compacting the brief stays on the
vision model by default because it happens once per subject and a lossy brief costs you
detail in every answer after; `LOCAL_BRIEFS=1` moves it if you want zero Anthropic text
calls.

**It has to be reachable from Railway.** `localhost`, `127.0.0.1` and `.local` names mean
the Railway container, not your Mac. Put the Mac behind a tunnel (Cloudflare Tunnel,
Tailscale Funnel, ngrok) and give the tunnel's URL. If the tunnel or a proxy in front of
the model wants a token, that is `LOCAL_LLM_KEY`.

**Thinking models.** Qwen3 and friends reason in a `<think>` block first, and that
reasoning is billed out of the same token budget as the answer — the identical trap
Sonnet's adaptive thinking set. The reasoning is stripped from what you see, but a small
budget (Answer mode is 500 tokens) can be spent entirely on thinking, and you'll get a
clear error saying so. Turn thinking off on the server (Ollama: `think: false`, or a
non-thinking variant) rather than raising budgets.

**Checking it took.** `/api/health` → `config.models` shows which model is on which job:

```json
"models": {"read": "claude-sonnet-5", "brief": "claude-sonnet-5", "answer": "qwen3:14b"}
```

If `answer` still says `claude-sonnet-5`, the variables didn't land (Railway sometimes
needs a manual redeploy after adding them). If it says the local model but the first
question errors, the error names the URL it tried and what came back — a `401` points at
the key, a `404` at the URL or model name, "can't reach" at the tunnel or a sleeping Mac.
Hovering an answer in the app shows which model wrote it and what it cost.

**Slow is fine, but it is slower.** A 14B model on a Mac Mini takes tens of seconds on a
long brief where Sonnet took three. The server no longer freezes while it waits — captures
and the health check keep working — and the request timeout is five minutes.

## 3. Gotchas (inherited from the sibling buddies, all learned the hard way)

- **Railway won't always redeploy when you add variables** — trigger a redeploy manually.
- **A fine-grained PAT outside its repo list returns 404, not 403** — a "missing brain"
  that never pushes usually means the token isn't scoped to `buddies-backups`. Check the
  sync line in `/api/health`, or the dot in the top-right of the app.
- **No volumes needed** — the container is ephemeral by design; the brain lives in the
  repo. Don't run two instances against the same `KNOWLEDGE_SUBPATH`: this head assumes
  it is the only writer, and a push that finds the remote has moved refuses rather than
  overwriting.
- The brain pushes at most every `AUTO_PUSH_SECONDS` and again on shutdown (SIGTERM). A
  hard kill inside that window loses at most that window's captures.
- **Screen sharing needs HTTPS and a real user gesture.** It works on Chrome and Edge
  desktop. Safari's `getDisplayMedia` is more restricted; iOS has no screen share at all,
  so on a phone the URL-paste path is the way in.

## 4. First boot

No brain in the repo yet → it starts fresh and creates `study-web/latest.tar.gz` on the
first push. Nothing to import: this buddy's archive format is its own single SQLite file,
not the shared snapshot shape the Gaming/Program buddies pass between themselves.

## 5. Cost, so a long session isn't a surprise

Measured on Sonnet 5 against a dense 1400px page, not estimated:

| Call | When | Cost |
|---|---|---|
| read a capture | once per press | **$0.006** |
| compact the brief | once per Done | scales with the notes (~$0.005 for two captures) |
| a question, first of a session | cache write | **$0.004** |
| a question, thereafter | cache read | **$0.0012** |

So a twenty-capture session with twenty questions is roughly **$0.20**, and it is the
*capturing* that dominates, not the asking (and with a local answering model the
questions are free, leaving only the capturing) — questions after the first cost about a
third of it, because the brief rides in the cached half of the prompt and a session is a
long run of questions against one unchanging document.

`READ_TIER=fast` cuts the top line by roughly four fifths and is the only lever that
meaningfully moves the total; it is also the one that trades away accuracy in the place
you can least afford it. `/api/usage` tracks the real figures per day and all-time.
