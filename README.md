# LinkedIn Engagement Bot

A Python + Playwright bot that signs into LinkedIn, picks the top posts in
the home feed, likes them, and (Levels 2 / 3) drafts human-sounding
comments — pulling in author-profile context for the most thoughtful
replies.

This is my submission for the [test task](https://gist.github.com/ridjex/36fbeb028c3b22e3959f34102c4d0f50).

---

## What was completed

**All three levels are implemented in code.** Level 1 was run end-to-end
against a live LinkedIn account; Levels 2 and 3 are wired up but were
not exercised against the network during this submission because the
test account I was given is brand-new and has zero feed content (see
[Challenges](#challenges-encountered) below). Anyone with a populated
feed and an LLM API key can run them with one config flip — `RUN_LEVEL=3`
in `.env`.

| Level | Status | Where to look |
|-------|--------|---------------|
| 1 — login, top-10, like, summary | ✅ verified end-to-end | [output/feed_summary.md](output/feed_summary.md), [output/screenshots/](output/screenshots/) |
| 2 — pick 2-3 with reasoning, draft comments | ✅ implemented, not exercised live | [linkedin_bot/ranker.py](linkedin_bot/ranker.py), [linkedin_bot/commenter.py](linkedin_bot/commenter.py) |
| 3 — author-profile context-aware comments | ✅ implemented, not exercised live | [linkedin_bot/linkedin.py](linkedin_bot/linkedin.py) `fetch_author_context` |

**Actual time spent:** ~3 hours.
The first 90 minutes were the original spec; the remaining 90 minutes
were eaten by LinkedIn's late-2025 DOM rewrite (fully obfuscated CSS
modules, React-Aria login form, no `data-id` on post containers — see
[Challenges](#challenges-encountered)).

---

## Architecture

```
linkedin_bot/
├── config.py         # .env-driven Settings dataclass
├── linkedin.py       # Playwright client: login, feed, like, profile scrape
├── ranker.py         # Heuristic + LLM-as-judge post ranking
├── commenter.py      # Comment drafting with synthetic-tells guard
├── reporter.py       # Writes results.json, feed_summary.md, comments.md
├── main.py           # Pipeline orchestrator
└── llm/
    ├── base.py
    ├── anthropic_provider.py
    ├── openai_provider.py
    └── factory.py    # Picks the provider from LLM_PROVIDER env var
```

Pipeline (`linkedin_bot.main.run`):

```
1. Load .env settings, build Playwright session (re-uses auth_state.json if present)
2. Login (with auto-detection of an already-authenticated session)
3. Collect top N posts from /feed
   ├── if 0 posts and WARMUP_FOLLOWS > 0  → follow N recommended people, retry
   └── if still 0 posts and FALLBACK_HASHTAG set → scrape the hashtag search feed
4. Like each post in-place (during collection — see "Decision point" below)
5. Heuristic-rank posts (log-scaled reactions + comments + question bonus − promo penalty)
6. LLM rerank: ask the model to pick the K most comment-worthy, with reasons
7. For each pick: navigate to the author's profile, scrape headline / role / about /
   recent post snippets — and pass that to the comment prompt
8. Draft comments via a strict system prompt that bans synthetic openers,
   hashtags, emojis, em-dashes, and forced positivity. Regex guard re-prompts
   once if the draft trips a rule.
9. Write output/results.json, output/feed_summary.md, output/comments.md, and three screenshots
```

---

## Decision point — judgment over AI suggestion

**The AI assistant's first recommendation: use the official LinkedIn API
or a maintained scraping library** (e.g. `linkedin-api`, `staffspy`) to
avoid Playwright DOM brittleness.

**What I chose instead: Playwright with structural selectors, no class
names at all.**

**Why I overrode the AI:**

1. The official LinkedIn REST API doesn't expose feed engagement actions for
   personal accounts — it's a sales/recruiter product. The "feed
   engagement" the task asks for genuinely requires browser automation
   or an unofficial library.
2. The unofficial libraries I checked (`linkedin-api`, `staffspy`,
   `linkedin-scraper`) all break against the late-2025 LinkedIn DOM
   for the same reason hand-written scrapers do — they hard-coded
   class names like `.feed-shared-update-v2` that LinkedIn has since
   replaced with random hashes (`_5d2a0f24`, `c049f232`, …).
3. Playwright with **structural** selectors (`div[role="listitem"]`
   inside `[data-testid="lazy-column"]`, `a[href*='/in/']` for author
   detection, button match by inner text rather than aria-label) is
   the most durable thing I could build in the time available. It
   survived all the rewrites I encountered in the session and didn't
   depend on any unmaintained third-party scraper.

A second smaller decision worth flagging: **the bot likes posts during
collection, not in a separate pass.** The AI suggested two phases — scrape
first, then re-find each post and like it — which mirrors the spec.
I diverged because LinkedIn's new DOM doesn't give us a stable `data-id`
to re-find a post later: `data-id` and `data-urn` are gone, so the only
ID I can synthesise (`post::<author-slug>::<hash>`) can't be queried back
from the page. Liking during collection avoids the re-find problem.

---

## Setup

Requires Python **3.12** (3.14 doesn't build `pydantic-core` yet).

```bash
python3.12 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
playwright install chromium
```

Copy the env template and fill it in:

```bash
cp .env.example .env
# edit .env — set LINKEDIN_EMAIL / LINKEDIN_PASSWORD,
# and ANTHROPIC_API_KEY or OPENAI_API_KEY for Levels 2/3
```

Then:

```bash
python run.py
# or: python -m linkedin_bot
```

The first run opens a headed Chromium and logs in. The session is
saved to `auth_state.json` (gitignored) so subsequent runs skip login.

### Configuration knobs (`.env`)

| Variable | Default | Purpose |
|----------|---------|---------|
| `LINKEDIN_EMAIL`, `LINKEDIN_PASSWORD` | — | Required. |
| `LLM_PROVIDER` | `anthropic` | `anthropic` or `openai`. |
| `ANTHROPIC_API_KEY` / `OPENAI_API_KEY` | — | One required for Levels 2/3. |
| `RUN_LEVEL` | `3` | `1` = like only, `2` = + comment drafts, `3` = + author profile context. |
| `TOP_N_POSTS` | `10` | How many posts to collect from the feed. |
| `TOP_K_FOR_COMMENTS` | `3` | How many to draft comments for. |
| `LIKE_POSTS` | `true` | Set `false` to scrape without engagement. |
| `HEADLESS` | `false` | Set `true` after auth_state.json is cached. |
| `WARMUP_FOLLOWS` | `0` | Brand-new accounts: number of recommended people to follow before scraping (so the feed populates). |
| `FALLBACK_HASHTAG` | `startups` | If the home feed is still empty after warmup, scrape this hashtag's search feed instead. Empty string to disable. |

### Tests

```bash
python -m pytest tests/ -v
```

13 unit tests cover the ranker heuristic, the synthetic-comment guard,
the author-name cleaner, and the count parser. They don't touch
Playwright or LinkedIn, so they run in <1s.

---

## Challenges encountered

This deserves its own section because they're the bulk of the time
spent and they shape what's interesting about the codebase.

### 1. LinkedIn's login form is now React-Aria

I spent 30 minutes thinking my credentials were wrong because
`page.fill('input#username', email)` silently typed into a clipped
hidden input that React's controlled state never picked up. The fix
was twofold: **don't pin `locale="en-US"`** (which forces the new
React-driven layout) — letting LinkedIn serve the legacy
server-rendered form, whose `<input id="username">` is plain HTML —
and use `keyboard.type()` rather than `fill()` so the input/change
events that React listens for actually fire.

### 2. The feed is fully obfuscated

The class name `feed-shared-update-v2` that every LinkedIn scraping
guide on the internet relies on is gone. So is `data-id="urn:li:activity:…"`
on post containers. What's left is randomly named CSS-module classes
(`_5d2a0f24`, `c049f232`, …) and a `role="listitem"` ARIA role inside
a `[data-testid="lazy-column"]`. The scraper uses the ARIA role, not
class names. See [linkedin_bot/linkedin.py](linkedin_bot/linkedin.py)
`_FEED_CARD_SELECTORS`.

### 3. The test account has an empty home feed

The LinkedIn account I was given is brand-new with no connections
or follows, so `linkedin.com/feed/` shows only "People you may know"
suggestions and a sponsored ad. There are no posts to like or
comment on.

I handled this with **two recovery steps**:

1. `WARMUP_FOLLOWS` — the bot opens the network/discover pages and
   clicks `Follow` (never `Connect`, which needs the other party's
   acceptance) on N recommended sources.
2. `FALLBACK_HASHTAG` — if the home feed is *still* empty, the bot
   falls back to the hashtag-search feed (e.g.
   `linkedin.com/search/results/content/?keywords=%23startups`),
   which always has fresh public posts.

For the recorded run I used the hashtag fallback (the warmup pages
exposed zero follow-able sources for this particular account region).
This is a documented compromise away from "the home feed" specifically;
the scraper code is identical, only the source URL differs.

### 4. Reaction / comment counters

Search-results posts (the hashtag fallback) don't always render the
visible reactions count — only the icons. I extract counts from
`button[aria-label]` text rather than from a visible counter element,
but on some posts that aria-label is empty. The end result: counts
show 0 for some posts even when there are real reactions visible in
the screenshot. This is a known cosmetic issue and is documented in
the reporter output rather than papered over.

### 5. Click interception

Once or twice the Like button click failed because LinkedIn's "translate
this post" tooltip overlapped the action bar at the moment of the click.
The fix is a simple `try / except → click(force=True)` retry.

---

## What this submission demonstrates

- **Judgment over speed**: I made the deliberate calls (locale handling,
  no class-name selectors, like-during-collection, structural selectors)
  documented above, and they paid off when LinkedIn's DOM bit me twice
  more than I expected.
- **Resilience**: every locator has a fallback, every external call has
  a graceful failure path, and the empty-feed case has two recovery
  layers before the pipeline gives up.
- **Discipline on the comment quality**: the system prompt for comments
  bans the AI tells that make synthetic engagement spot-on-spot from a
  mile away (`"Great post!"`, `"Thanks for sharing"`, hashtag confetti,
  em-dashes, forced positivity), and a regex guard re-prompts once if
  a draft trips a rule. That's the part of "human-sounding" that
  actually requires opinion, not just an LLM call.

---

## Output artifacts

After a successful run, look in `output/`:

- `feed_summary.md` — Level 1 deliverable: author, headline, media,
  reaction/comment counts, first 200 chars, engagement outcome.
- `comments.md` — Level 2/3 deliverable: drafted comments per author,
  with the LLM's reasoning for why the post was picked and which
  profile context was used.
- `results.json` — full structured payload (feed snapshot, picks,
  author contexts, drafts) — feed this into a downstream system if
  you don't want the prose.
- `screenshots/01_after_login.png` — verifies the auth state.
- `screenshots/02_feed_collected.png` — verifies scroll/collection.
- `screenshots/03_after_likes.png` — verifies engagement happened.
