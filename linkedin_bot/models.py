"""Plain dataclasses used as the contract between modules."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


@dataclass
class Post:
    """A single feed post collected from LinkedIn."""

    post_id: str
    author_name: str
    author_profile_url: Optional[str]
    author_headline: Optional[str]
    text: str
    media_type: str  # "text" | "image" | "video" | "article" | "document" | "repost"
    reactions: int
    comments: int
    reposts: int
    permalink: Optional[str]
    liked: bool = False
    like_outcome: str = "skipped"  # "liked" | "already_liked" | "skipped" | "failed: ..."

    @property
    def preview(self) -> str:
        return self.text[:200]


@dataclass
class AuthorContext:
    """Extra context scraped from the author's profile (Level 3)."""

    profile_url: str
    full_name: Optional[str] = None
    headline: Optional[str] = None
    location: Optional[str] = None
    about: Optional[str] = None
    current_role: Optional[str] = None
    company: Optional[str] = None
    mutual_connections: Optional[str] = None
    recent_post_snippets: list[str] = field(default_factory=list)
    scrape_notes: list[str] = field(default_factory=list)


@dataclass
class CommentDraft:
    post_id: str
    author_name: str
    selection_reason: str
    comment_body: str
    used_profile_context: bool
