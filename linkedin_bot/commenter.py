"""Generate human-sounding comment drafts.

Two prompt variants:
  * `draft_basic_comment` — Level 2: post text only.
  * `draft_contextual_comment` — Level 3: post text + author profile context.

The system prompt is intentionally opinionated: it bans the AI tells that
make LinkedIn comments feel synthetic ("Great post!", "Thanks for sharing!",
em-dash-stuffed compound sentences, hashtag confetti). The post-processor
runs cheap regex checks and re-prompts once if a forbidden phrase slips in.
"""

from __future__ import annotations

import logging
import re

from .llm import LLMProvider
from .models import AuthorContext, CommentDraft, Post
from .ranker import RankedPost

log = logging.getLogger(__name__)


SYSTEM_PROMPT = """\
You write LinkedIn comments that read like a senior practitioner replying
in a hurry between meetings — specific, grounded, and short.

Hard rules:
- 1 to 3 sentences. 60–280 characters total. No exceptions.
- No filler openers: never start with "Great post", "Love this", "Thanks for sharing",
  "Couldn't agree more", "This is gold", "100%", "Spot on", "Insightful", or any
  variation. Open with the substantive idea.
- No emoji. No hashtags. No @mentions. No links.
- No em dashes (—). Use a period or a comma.
- Don't summarise the post back to the author. Add one thing: a counter-example,
  a follow-up question, a concrete detail from your own experience, or a sharper
  reframing.
- If you ask a question, make it specific enough that the author could actually
  answer it in one comment.

Return only the comment body — no quotes, no prefix, no explanation.
"""


_BANNED_PATTERNS = [
    r"^\s*great post",
    r"^\s*love this",
    r"^\s*thanks for sharing",
    r"^\s*couldn'?t agree more",
    r"^\s*this is gold",
    r"^\s*spot on",
    r"^\s*100%",
    r"^\s*insightful",
    r"#\w+",         # hashtags
    r"@\w+",         # @mentions
    r"https?://",    # links
    r"—",            # em dash
]


def _looks_synthetic(comment: str) -> str | None:
    for pat in _BANNED_PATTERNS:
        if re.search(pat, comment, flags=re.IGNORECASE):
            return pat
    if len(comment) > 320:
        return "too_long"
    if len(comment) < 40:
        return "too_short"
    return None


def _build_basic_prompt(post: Post, reason: str) -> str:
    return (
        f"Post by {post.author_name} ({post.author_headline or 'no headline'}):\n"
        f"\"\"\"\n{post.text[:1200]}\n\"\"\"\n\n"
        f"Why this post was selected to comment on: {reason or 'High substance.'}\n\n"
        "Write the comment now."
    )


def _build_contextual_prompt(
    post: Post, reason: str, author: AuthorContext
) -> str:
    ctx_lines = ["Author context gathered from their profile:"]
    if author.headline:
        ctx_lines.append(f"- Headline: {author.headline}")
    if author.current_role:
        role = author.current_role + (f" @ {author.company}" if author.company else "")
        ctx_lines.append(f"- Current role: {role}")
    if author.location:
        ctx_lines.append(f"- Location: {author.location}")
    if author.mutual_connections:
        ctx_lines.append(f"- Mutual connections note: {author.mutual_connections}")
    if author.about:
        ctx_lines.append(f"- About (truncated): {author.about[:400]}")
    if author.recent_post_snippets:
        ctx_lines.append("- Recent post snippets:")
        for s in author.recent_post_snippets[:3]:
            ctx_lines.append(f"    * {s[:200]}")
    if len(ctx_lines) == 1:
        ctx_lines.append("- (No additional profile context could be scraped.)")

    return (
        f"Post by {post.author_name}:\n"
        f"\"\"\"\n{post.text[:1200]}\n\"\"\"\n\n"
        + "\n".join(ctx_lines)
        + f"\n\nWhy this post was selected to comment on: {reason or 'High substance.'}\n\n"
        "Use the author's role and recent themes to make the comment feel like "
        "it comes from someone who actually knows their work. Do not name-drop "
        "details that would be creepy; you can reference at most one factual "
        "anchor from the profile (their domain, a recurring theme, their company "
        "type) — and only if it strengthens the point.\n\n"
        "Write the comment now."
    )


def _generate(provider: LLMProvider, system: str, user: str) -> str:
    raw = provider.complete(system, user, max_tokens=350).strip()
    # The model sometimes wraps the answer in quotes.
    if raw.startswith('"') and raw.endswith('"'):
        raw = raw[1:-1].strip()
    return raw


def _generate_with_retry(provider: LLMProvider, user: str) -> str:
    comment = _generate(provider, SYSTEM_PROMPT, user)
    problem = _looks_synthetic(comment)
    if problem is None:
        return comment

    log.info("Comment violated rule %s — retrying once with feedback.", problem)
    retry_user = (
        user
        + f"\n\nYour previous draft was rejected (rule violated: {problem}). "
        "Rewrite it from scratch and respect every rule above. Do not apologise; "
        "just write the new comment."
    )
    comment = _generate(provider, SYSTEM_PROMPT, retry_user)
    return comment


def draft_basic_comment(
    provider: LLMProvider, ranked: RankedPost
) -> CommentDraft:
    body = _generate_with_retry(
        provider, _build_basic_prompt(ranked.post, ranked.llm_reason)
    )
    return CommentDraft(
        post_id=ranked.post.post_id,
        author_name=ranked.post.author_name,
        selection_reason=ranked.llm_reason or "Heuristic top pick.",
        comment_body=body,
        used_profile_context=False,
    )


def draft_contextual_comment(
    provider: LLMProvider,
    ranked: RankedPost,
    author: AuthorContext,
) -> CommentDraft:
    body = _generate_with_retry(
        provider,
        _build_contextual_prompt(ranked.post, ranked.llm_reason, author),
    )
    return CommentDraft(
        post_id=ranked.post.post_id,
        author_name=ranked.post.author_name,
        selection_reason=ranked.llm_reason or "Heuristic top pick.",
        comment_body=body,
        used_profile_context=True,
    )
