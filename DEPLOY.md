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

Each capture is one vision call (~$0.01 on the deep tier for a dense 1400px page); the
brief is one text call over all the notes; each question is one text call over the brief,
with the stable half of the prompt cached. A twenty-capture session lands around
$0.25–0.35. `/api/usage` and the app's own counter track it per day and all-time.
