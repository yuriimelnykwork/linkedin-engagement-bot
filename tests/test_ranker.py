"""Tests for the heuristic ranker and the synthetic-comment guard.

These cover the pure-Python logic that doesn't touch Playwright or LinkedIn,
so they run in <1s in CI and don't depend on network or an LLM key.
"""

from __future__ import annotations

from linkedin_bot.commenter import _looks_synthetic
from linkedin_bot.linkedin import _clean_author_name, _parse_count
from linkedin_bot.models import Post
from linkedin_bot.ranker import _heuristic_score, rank_posts


def make_post(**kw) -> Post:
    defaults = dict(
        post_id="p", author_name="A", author_profile_url=None,
        author_headline=None, text="", media_type="text",
        reactions=0, comments=0, reposts=0, permalink=None,
    )
    defaults.update(kw)
    return Post(**defaults)


class TestRanker:
    def test_substantive_post_outranks_promo(self):
        substantive = make_post(
            post_id="a",
            text="What's the best way to scale postgres beyond 10TB? "
                 "Curious how others have handled WAL bloat.",
            reactions=120, comments=45,
        )
        promo = make_post(
            post_id="b",
            text="We are hiring senior engineers. Apply now! DM me.",
            reactions=200, comments=5,
        )
        ranked = rank_posts([substantive, promo])
        assert ranked[0].post.post_id == "a"

    def test_question_bonus_applies(self):
        with_q = make_post(text="x" * 200 + "?", reactions=10, comments=10)
        without_q = make_post(text="x" * 200, reactions=10, comments=10)
        assert _heuristic_score(with_q) > _heuristic_score(without_q)

    def test_promo_penalty_applies(self):
        normal = make_post(text="x" * 200, reactions=50, comments=10)
        promo = make_post(
            text="We are hiring " + "x" * 200,
            reactions=50, comments=10,
        )
        assert _heuristic_score(normal) > _heuristic_score(promo)


class TestSyntheticGuard:
    def test_rejects_filler_opener(self):
        assert _looks_synthetic("Great post! Thanks for sharing.") is not None
        assert _looks_synthetic("Love this — really insightful.") is not None
        assert _looks_synthetic("100% agree, exactly my view.") is not None

    def test_rejects_hashtags_and_mentions(self):
        body = "Solid point about #startups and @someone — totally agree about churn."
        assert _looks_synthetic(body) is not None

    def test_rejects_too_short(self):
        assert _looks_synthetic("Cool.") == "too_short"

    def test_accepts_substantive_reply(self):
        body = (
            "I tried a similar approach but ran into WAL bloat at 4TB; "
            "a logical-replication cutover saved us."
        )
        assert _looks_synthetic(body) is None


class TestAuthorNameClean:
    def test_strips_degree_marker(self):
        assert _clean_author_name("Gbenga Nureni • 3-й+") == "Gbenga Nureni"
        assert _clean_author_name("Darren Dixon · 3rd+") == "Darren Dixon"
        assert _clean_author_name("Yash Khandelwal • 3-й") == "Yash Khandelwal"

    def test_leaves_clean_names_alone(self):
        assert _clean_author_name("Vasily Alekseenko") == "Vasily Alekseenko"
        assert _clean_author_name("Michael J. Thomas") == "Michael J. Thomas"

    def test_safety_net_cuts_at_digit(self):
        # Even if a stray separator is missed, the digit-cut grabs it.
        assert _clean_author_name("Brandon Maier — 2nd") == "Brandon Maier"


class TestParseCount:
    def test_plain_numbers(self):
        assert _parse_count("123 reactions") == 123
        assert _parse_count("1,234 reactions") == 1234

    def test_suffixes(self):
        assert _parse_count("1.2K reactions") == 1200
        assert _parse_count("3M comments") == 3_000_000

    def test_empty(self):
        assert _parse_count(None) == 0
        assert _parse_count("") == 0
