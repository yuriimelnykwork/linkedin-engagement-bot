"""Serialize run results to disk."""

from __future__ import annotations

import json
from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .models import AuthorContext, CommentDraft, Post
from .ranker import RankedPost


def _dump(obj: Any) -> Any:
    if is_dataclass(obj):
        return {k: _dump(v) for k, v in asdict(obj).items()}
    if isinstance(obj, list):
        return [_dump(x) for x in obj]
    if isinstance(obj, dict):
        return {k: _dump(v) for k, v in obj.items()}
    return obj


def write_results_json(
    output_dir: Path,
    *,
    level: int,
    posts: list[Post],
    ranked_picks: list[RankedPost],
    author_contexts: dict[str, AuthorContext],
    comments: list[CommentDraft],
) -> Path:
    payload = {
        "run_level": level,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "feed_top_posts": [
            {
                "post_id": p.post_id,
                "author": p.author_name,
                "author_headline": p.author_headline,
                "author_profile_url": p.author_profile_url,
                "preview": p.preview,
                "media_type": p.media_type,
                "reactions": p.reactions,
                "comments": p.comments,
                "reposts": p.reposts,
                "permalink": p.permalink,
                "engagement_outcome": p.like_outcome,
                "liked": p.liked,
            }
            for p in posts
        ],
        "selected_for_comment": [
            {
                "post_id": r.post.post_id,
                "author": r.post.author_name,
                "heuristic_score": round(r.heuristic_score, 3),
                "selection_reason": r.llm_reason,
            }
            for r in ranked_picks
        ],
        "author_contexts": {pid: _dump(ctx) for pid, ctx in author_contexts.items()},
        "comment_drafts": [_dump(c) for c in comments],
    }
    path = output_dir / "results.json"
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    return path


def write_comments_markdown(
    output_dir: Path,
    *,
    level: int,
    comments: list[CommentDraft],
    posts_by_id: dict[str, Post],
    author_contexts: dict[str, AuthorContext],
) -> Path:
    lines: list[str] = [
        f"# Comment drafts (Level {level})",
        "",
        "_These are drafts — none of them have been posted to LinkedIn._",
        "",
    ]
    for c in comments:
        post = posts_by_id.get(c.post_id)
        ctx = author_contexts.get(c.post_id)
        lines.append(f"## {c.author_name}")
        if post and post.permalink:
            url = post.permalink if post.permalink.startswith("http") else f"https://www.linkedin.com{post.permalink}"
            lines.append(f"- Permalink: {url}")
        if post:
            lines.append(f"- Preview: {post.preview!r}")
        lines.append(f"- Why this post: {c.selection_reason}")
        if ctx and c.used_profile_context:
            chips: list[str] = []
            if ctx.headline:
                chips.append(f"headline: {ctx.headline}")
            if ctx.current_role:
                role = ctx.current_role + (f" @ {ctx.company}" if ctx.company else "")
                chips.append(f"role: {role}")
            if ctx.location:
                chips.append(f"location: {ctx.location}")
            if chips:
                lines.append(f"- Author context used: {' | '.join(chips)}")
            if ctx.scrape_notes:
                lines.append(f"- Scrape notes: {'; '.join(ctx.scrape_notes)}")
        lines.append("")
        lines.append("> " + c.comment_body.replace("\n", "\n> "))
        lines.append("")
    path = output_dir / "comments.md"
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def write_feed_summary_markdown(output_dir: Path, posts: list[Post]) -> Path:
    """Level 1's required output: author, first 200 chars, engagement outcome."""
    lines = ["# Top feed posts (Level 1 summary)", ""]
    for i, p in enumerate(posts, 1):
        lines.extend([
            f"## {i}. {p.author_name}",
            f"- Headline: {p.author_headline or '(none)'}",
            f"- Media: {p.media_type} | reactions={p.reactions} comments={p.comments} reposts={p.reposts}",
            f"- Engagement outcome: **{p.like_outcome}**",
            "",
            f"> {p.preview}",
            "",
        ])
    path = output_dir / "feed_summary.md"
    path.write_text("\n".join(lines), encoding="utf-8")
    return path
