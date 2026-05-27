"""Environment-driven configuration."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(PROJECT_ROOT / ".env")


def _bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "y", "on"}


def _int(name: str, default: int) -> int:
    raw = os.getenv(name)
    return int(raw) if raw else default


def _require(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise RuntimeError(
            f"Missing required env var: {name}. Copy .env.example to .env and fill it in."
        )
    return value


@dataclass(frozen=True)
class Settings:
    linkedin_email: str
    linkedin_password: str
    llm_provider: str
    anthropic_api_key: str | None
    anthropic_model: str
    openai_api_key: str | None
    openai_model: str
    headless: bool
    top_n_posts: int
    top_k_for_comments: int
    like_posts: bool
    run_level: int
    warmup_follows: int
    fallback_hashtag: str
    project_root: Path
    output_dir: Path
    auth_state_path: Path

    @classmethod
    def load(cls) -> "Settings":
        output_dir = PROJECT_ROOT / "output"
        output_dir.mkdir(parents=True, exist_ok=True)
        (output_dir / "screenshots").mkdir(parents=True, exist_ok=True)
        return cls(
            linkedin_email=_require("LINKEDIN_EMAIL"),
            linkedin_password=_require("LINKEDIN_PASSWORD"),
            llm_provider=os.getenv("LLM_PROVIDER", "anthropic").lower(),
            anthropic_api_key=os.getenv("ANTHROPIC_API_KEY"),
            anthropic_model=os.getenv("ANTHROPIC_MODEL", "claude-sonnet-4-5"),
            openai_api_key=os.getenv("OPENAI_API_KEY"),
            openai_model=os.getenv("OPENAI_MODEL", "gpt-4o"),
            headless=_bool("HEADLESS", False),
            top_n_posts=_int("TOP_N_POSTS", 10),
            top_k_for_comments=_int("TOP_K_FOR_COMMENTS", 3),
            like_posts=_bool("LIKE_POSTS", True),
            run_level=_int("RUN_LEVEL", 3),
            warmup_follows=_int("WARMUP_FOLLOWS", 0),
            fallback_hashtag=os.getenv("FALLBACK_HASHTAG", "startups"),
            project_root=PROJECT_ROOT,
            output_dir=output_dir,
            auth_state_path=PROJECT_ROOT / "auth_state.json",
        )
