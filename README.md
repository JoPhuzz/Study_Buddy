# Study Buddy

**Show it something, then ask about it — and it answers from what you showed it, or
says it doesn't know.**

Share a window. Every time you're looking at something worth knowing about, press
**Capture**. Page one, then page two, then page three. Or scroll a long page and shoot
as you go. Or circle a model in Blender and take four angles. Each capture is read the
moment it lands, so the reading happens while you carry on clicking.

When you've shown it everything, press **Done**. It compacts every capture into one
brief — stitching the scrolled halves back into whole pages, keeping the tables as
tables and the prices to the penny — and from then on that brief is the entire world it
can answer from.

## Why the closed world is the point

Every other assistant fills gaps. Ask about a page and you get an answer blended from
what's on it and what the model already believed, with no seam showing. That is worse
than useless when you're trying to find out what *this* contract, *this* pricing page or
*this* build actually says, because you can't tell which half is which.

So Study Buddy doesn't fill gaps:

> **What is their refund policy?**
> You didn't show me that. Neither capture includes a refund policy — you'd need to
> capture a page covering their terms, FAQ, or refund/cancellation policy to find that.

And it tells the two kinds of silence apart: *the material covers this and says nothing*
is a different fact from *you never captured that page*, and it will say which.

The guarantee is kept structurally, not by asking nicely. There is no search tool in the
adapter to reach for, no retrieval over anything but the current subject, and no path
that answers a question with no brief behind it. A test asserts that no tool declaration
exists anywhere in the backend, because a rule that lives only in a prompt is a rule the
model can talk itself out of.

## The parts

- **Capture** — one press, one frame, read immediately. A label is optional ("annual
  tab") and reaches both the reader and the record. Spacebar works, so your hands stay
  on the thing you're showing.
- **Continuity** — each capture is told what the one before it showed, so it can tell a
  scrolled continuation of one page from a genuinely new screen, and record only what's
  new.
- **Done** — compaction. Merge, de-overlap, preserve every number and exact phrase, and
  note what was cut off or unreadable.
- **Paste a URL instead** — for a web page this is strictly better than a screenshot:
  every line, in order, nothing lost at the fold, no OCR mistakes on a dense table, no
  twenty presses to get to the bottom. Screenshots are for programs; URLs are for pages.
- **Paste a YouTube link** — it takes the transcript, not the page, split into
  ten-minute captures labelled by time. Timestamps are kept in the text, so "where does
  he explain the call stack?" comes back as "4:21 to 7:41" rather than a paraphrase with
  no way back to it. Costs nothing: it is text, so no vision call.
- **Drop a file on it** — PDF, image (PNG/JPEG/GIF/WebP) or text (txt, md, csv, json…).
  A PDF becomes one capture per page, in page order, so the reader treats page 4 as
  following page 3. One rule decides how each is handled: **if the file already carries
  text, it is extracted** — free, exact, and better than reading a picture of the same
  words. Only pages with no text layer go through the vision reader, and the app tells
  you when that happened and what it cost.
- **Add more later** — capture again against the same subject and press Done; the brief
  merges rather than starting over. Until you do, answers say the brief predates your
  latest captures.
- **Modes** — Answer, Quote (the source's exact words), Summary, Compare, Direct. Every
  one of them is closed-world; a mode changes tone, never sources.

## Two models, if you want

Reading a capture is the load-bearing call and stays on Claude's vision. Answering
questions is text-only and happens over and over — so it can run on a model of your own
(Ollama, LM Studio, anything speaking the OpenAI-style chat protocol) for nothing. Set
`LOCAL_LLM_URL` and `LOCAL_LLM_MODEL`; see `DEPLOY.md`. Routing changes who answers,
never what they answer from: the local model gets exactly the brief and the
conversation, the same as Claude would, and a test asserts it.

## What it deliberately doesn't have

No web search. No voice. No embeddings or vector store — the brief goes into context
whole, which is simpler and lossless, and if briefs ever outgrow that, retrieval can be
added then. Frames are never stored, only the notes taken from them: cheap to re-compact,
no image-count ceiling, and a brain small enough to sync.

## Run it locally

```bash
./dev-run.sh   # serves on :8092
```

Or with your own env: copy `.env.example` → `.env`, `pip install -r requirements.txt`,
`python -m backend.main`. Tests are offline and need no key: `./run-tests.sh`.

## Where it sits

The fifth buddy, after Gaming Buddy and Bench Buddy (on the Pi), Gaming Buddy Mobile and
Program Buddy (on Railway). It shares their shape — one password, a GitHub-backed brain,
one container — but not their code: Program Buddy is built to reach for the web when it
doesn't know, and this one must never, so it was written fresh rather than forked.

Deploy: see [DEPLOY.md](DEPLOY.md).
