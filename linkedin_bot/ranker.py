"""Rank feed posts and pick the most compelling ones for commenting.

We blend a cheap, deterministic heuristic with an LLM-as-judge step:

  1. Heuristic score = log(reactions) + 2·log(comments) + length/quality bonuses.
     This is order-stable, never makes an API call, and survives if the LLM
     is unavailable.
  2. LLM rerank: we hand the top heuristic candidates to the model and ask
     it to choose the K with the highest comment-worthy substance, returning
     reasoning per pick. The LLM only re-orders; it never invents posts.

The split exists because "most reactions" and "most compelling to comment on"
are different objectives — a viral hot take racks up reactions but rewards
shallow replies, while a thoughtful technical write-up earns a real comment.
"""

from __future__ import annotations

import json
import logging
import math
import re
from dataclasses import dataclass

from .llm import LLMProvider
from .models import Post

log = logging.getLogger(__name__)


@dataclass
class RankedPost:
    post: Post
    heuristic_score: float
    llm_reason: str = ""


def _heuristic_score(p: Post) -> float:
    reactions = math.log1p(p.reactions)
    comments = 2.0 * math.log1p(p.comments)
    reposts = 0.5 * math.log1p(p.reposts)

    # Substance: longer text & question marks tend to invite better replies.
    length_bonus = min(len(p.text), 1500) / 500.0
    question_bonus = 0.4 if "?" in p.text else 0.0

    # Penalise pure promo / hiring posts — they rarely reward thoughtful comments.
    promo_penalty = 0.0
    promo_signals = ("we are hiring", "we're hiring", "apply now", "dm me", "link in bio")
    if any(s in p.text.lower() for s in promo_signals):
        promo_penalty = -1.0

    return reactions + comments + reposts + length_bonus + question_bonus + promo_penalty


def rank_posts(posts: list[Post]) -> list[RankedPost]:
    ranked = [RankedPost(post=p, heuristic_score=_heuristic_score(p)) for p in posts]
    ranked.sort(key=lambda r: r.heuristic_score, reverse=True)
    return ranked


SYSTEM_PROMPT = (
    "You are helping a thoughtful professional decide which LinkedIn posts "
    "are worth a substantive comment. You value substance over virality. "
    "You return strict JSON only — no prose around it."
)


def _build_user_prompt(candidates: list[RankedPost], k: int) -> str:
    lines = [
        f"Pick the {k} posts most worth commenting on. Criteria:",
        "- Invites a real reply (poses a question, makes a claim, shares lessons).",
        "- Author seems to write from experience rather than recycling clichés.",
        "- Topic has enough depth that a one-line reply would feel lazy.",
        "",
        "Posts:",
    ]
    for i, r in enumerate(candidates):
        p = r.post
        lines.append(
            f"[{i}] author={p.author_name!r} headline={p.author_headline!r} "
            f"reactions={p.reactions} comments={p.comments} media={p.media_type}"
        )
        lines.append(f"     text: {p.text[:600]!r}")
        lines.append("")
    lines.append(
        'Return JSON: {"picks":[{"index":int,"reason":"<one sentence>"}]} '
        f"with exactly {k} items, ordered best-first."
    )
    return "\n".join(lines)


def _safe_parse_picks(raw: str) -> list[dict]:
    # The model occasionally wraps JSON in ``` fences.
    cleaned = re.sub(r"^```(?:json)?|```$", "", raw.strip(), flags=re.MULTILINE).strip()
    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError:
        # Last-ditch — grab the first {...} blob.
        m = re.search(r"\{.*\}", cleaned, re.DOTALL)
        if not m:
            raise
        data = json.loads(m.group(0))
    return data.get("picks", [])


def llm_rerank(
    candidates: list[RankedPost],
    k: int,
    provider: LLMProvider,
) -> list[RankedPost]:
    """Ask the LLM to pick the K best from the heuristic top.

    Falls back to the heuristic order if the LLM call or parse fails.
    """
    if not candidates:
        return []
    if k <= 0:
        return []
    pool = candidates[: max(k * 3, 6)]  # give the model some headroom
    user = _build_user_prompt(pool, k)
    try:
        raw = provider.complete(SYSTEM_PROMPT, user, max_tokens=600)
        picks = _safe_parse_picks(raw)
    except Exception as exc:  # noqa: BLE001
        log.warning("LLM rerank failed (%s); falling back to heuristic order.", exc)
        return candidates[:k]

    result: list[RankedPost] = []
    seen: set[int] = set()
    for pick in picks:
        try:
            idx = int(pick["index"])
        except (KeyError, TypeError, ValueError):
            continue
        if idx in seen or idx < 0 or idx >= len(pool):
            continue
        seen.add(idx)
        rp = pool[idx]
        rp.llm_reason = str(pick.get("reason", "")).strip()
        result.append(rp)
        if len(result) >= k:
            break

    # If the model returned fewer than k usable picks, top up from the heuristic order.
    if len(result) < k:
        for rp in candidates:
            if rp.post.post_id not in {x.post.post_id for x in result}:
                if not rp.llm_reason:
                    rp.llm_reason = "Filled by heuristic fallback — LLM returned fewer picks than requested."
                result.append(rp)
                if len(result) >= k:
                    break
    return result
