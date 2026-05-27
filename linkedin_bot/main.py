"""Top-level pipeline.

Orchestrates: login → collect feed → like → rank → (Level 3) scrape author
profiles → draft comments → write reports. The function `run()` is the
single entry point; `python -m linkedin_bot` and `run.py` both call into it.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

from rich.console import Console
from rich.logging import RichHandler
from rich.table import Table

from .commenter import draft_basic_comment, draft_contextual_comment
from .config import Settings
from .linkedin import linkedin_session
from .llm import build_provider
from .models import AuthorContext, CommentDraft, Post
from .ranker import RankedPost, llm_rerank, rank_posts
from .reporter import (
    write_comments_markdown,
    write_feed_summary_markdown,
    write_results_json,
)

console = Console()


def _setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(message)s",
        handlers=[RichHandler(console=console, markup=False, rich_tracebacks=True)],
    )


def _print_feed_table(posts: list[Post]) -> None:
    table = Table(title="Top feed posts", show_lines=False)
    table.add_column("#", style="cyan", no_wrap=True)
    table.add_column("Author")
    table.add_column("React.")
    table.add_column("Comm.")
    table.add_column("Outcome")
    table.add_column("Preview (200ch)")
    for i, p in enumerate(posts, 1):
        table.add_row(
            str(i),
            p.author_name,
            str(p.reactions),
            str(p.comments),
            p.like_outcome,
            (p.preview[:120] + "…") if len(p.preview) > 120 else p.preview,
        )
    console.print(table)


def run() -> int:
    _setup_logging()
    log = logging.getLogger("linkedin_bot")

    settings = Settings.load()
    log.info(
        "Run level=%d | top_n=%d | top_k_for_comments=%d | like=%s | provider=%s",
        settings.run_level,
        settings.top_n_posts,
        settings.top_k_for_comments,
        settings.like_posts,
        settings.llm_provider,
    )

    posts: list[Post] = []
    author_contexts: dict[str, AuthorContext] = {}
    comments: list[CommentDraft] = []
    ranked_picks: list[RankedPost] = []
    screenshots_dir = settings.output_dir / "screenshots"

    with linkedin_session(settings) as client:
        client.login()
        client.screenshot(screenshots_dir / "01_after_login.png", "after login")

        log.info("Collecting top %d feed posts (like_each=%s)…",
                 settings.top_n_posts, settings.like_posts)
        posts = client.collect_feed_posts(
            settings.top_n_posts, like_each=settings.like_posts
        )
        log.info("Collected %d posts from /feed.", len(posts))

        # Empty-feed handling. A brand-new account lands on /feed with only
        # recommendations and ads — nothing to engage with. We do at most
        # two recovery steps: (1) follow N recommended people so the feed
        # populates, (2) fall back to a hashtag search feed if still empty.
        if not posts and settings.warmup_follows > 0:
            followed = client.warmup_follow(target=settings.warmup_follows)
            log.info(
                "Warmup followed %d sources — waiting for the feed to populate.",
                followed,
            )
            client.page.wait_for_timeout(4000)
            posts = client.collect_feed_posts(
                settings.top_n_posts, like_each=settings.like_posts
            )
            log.info("After warmup: collected %d posts.", len(posts))

        if not posts and settings.fallback_hashtag:
            # The /feed/hashtag/ URL redirects to /search/results/content/ in
            # the current LinkedIn UI, so target the search route directly.
            fallback_url = (
                f"https://www.linkedin.com/search/results/content/"
                f"?keywords=%23{settings.fallback_hashtag}"
            )
            log.warning(
                "Home feed still empty — falling back to hashtag-search feed %s. "
                "Documented in README as a fresh-account compromise.",
                fallback_url,
            )
            posts = client.collect_feed_posts(
                settings.top_n_posts,
                source_url=fallback_url,
                like_each=settings.like_posts,
            )
            log.info("Hashtag fallback: collected %d posts.", len(posts))

        if not settings.like_posts:
            for p in posts:
                p.like_outcome = "skipped (LIKE_POSTS=false)"

        client.screenshot(screenshots_dir / "02_feed_collected.png", "feed scrolled")
        for p in posts:
            log.info("  %s → %s", p.author_name, p.like_outcome)

        if not posts:
            log.error(
                "No posts available to engage with even after warmup + fallback. "
                "Likely the account has no followed sources and the hashtag "
                "fallback is disabled or blocked. Skipping like / comment phases."
            )

        # Likes happen during collection (collect_feed_posts(like_each=...)),
        # so this stage is just an extra screenshot of the post-engagement state.
        client.screenshot(screenshots_dir / "03_after_likes.png", "after likes")

        if settings.run_level >= 2:
            log.info("Ranking and picking top %d for comments…", settings.top_k_for_comments)
            provider = build_provider(settings)
            heuristic = rank_posts(posts)
            ranked_picks = llm_rerank(heuristic, settings.top_k_for_comments, provider)

            if settings.run_level >= 3:
                log.info("Scraping author profiles for %d picks…", len(ranked_picks))
                for r in ranked_picks:
                    ctx = client.fetch_author_context(r.post)
                    author_contexts[r.post.post_id] = ctx

            log.info("Drafting comments…")
            for r in ranked_picks:
                if settings.run_level >= 3:
                    ctx = author_contexts.get(
                        r.post.post_id, AuthorContext(profile_url=r.post.author_profile_url or "")
                    )
                    draft = draft_contextual_comment(provider, r, ctx)
                else:
                    draft = draft_basic_comment(provider, r)
                comments.append(draft)
                log.info("  Draft for %s: %s", draft.author_name, draft.comment_body[:90])

    # ----- writeout --------------------------------------------------------
    posts_by_id = {p.post_id: p for p in posts}
    feed_path = write_feed_summary_markdown(settings.output_dir, posts)
    json_path = write_results_json(
        settings.output_dir,
        level=settings.run_level,
        posts=posts,
        ranked_picks=ranked_picks,
        author_contexts=author_contexts,
        comments=comments,
    )
    log.info("Wrote %s", feed_path.relative_to(settings.project_root))
    log.info("Wrote %s", json_path.relative_to(settings.project_root))
    if comments:
        md_path = write_comments_markdown(
            settings.output_dir,
            level=settings.run_level,
            comments=comments,
            posts_by_id=posts_by_id,
            author_contexts=author_contexts,
        )
        log.info("Wrote %s", md_path.relative_to(settings.project_root))

    console.print()
    _print_feed_table(posts)
    if comments:
        console.print()
        console.rule("[bold]Comment drafts (NOT posted)[/bold]")
        for c in comments:
            console.print(f"\n[bold]{c.author_name}[/bold] — {c.selection_reason}")
            console.print(f"  {c.comment_body}")

    return 0


def cli() -> None:
    try:
        sys.exit(run())
    except KeyboardInterrupt:
        console.print("\n[red]Interrupted.[/red]")
        sys.exit(130)


if __name__ == "__main__":
    cli()
