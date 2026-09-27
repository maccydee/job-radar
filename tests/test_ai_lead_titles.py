"""Titles of the form "AI Lead" are dropped by AI phrases that are all two words.

A config whose AI terms are phrases like "ai enablement", "ai adoption" or
"head of ai" cannot match a bare "AI Lead", however loosely. The loose matcher
(`screen.title_matches_loosely`) needs every word of a configured phrase to be
present in the title, and "AI Lead" carries none of "enablement", "adoption",
"head" or "director". The strict path needs the phrase as a substring, which
also fails. So the role is dropped on the title gate, upstream of screening and
of storage, and never reaches the database at all.

The whole family goes the same way: "AI Automation Lead", "AI & Automation
Lead", "Enterprise AI Lead", "Agentic AI Ops Lead".

One term fixes all of them. Adding "ai lead" works without a code change,
because the loose matcher already tolerates words sitting between the two, so
long as the interrupting word is not one that changes the job.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from jobradar.config import Config  # noqa: E402
from jobradar.models import Job     # noqa: E402
from jobradar.screen import match   # noqa: E402

# A config shaped like the problem: engineering terms, plus AI terms that are
# all two-word phrases. Deliberately small and synthetic.
TWO_WORD_AI_TERMS = [
    "engineering manager",
    "head of engineering",
    "ai enablement",
    "ai transformation",
    "ai adoption",
    "head of ai",
    "director of ai",
    "ai strategy",
]

# The fix: one term added, nothing removed or reworded.
WITH_AI_LEAD = TWO_WORD_AI_TERMS + ["ai lead"]

AI_LEAD_FAMILY = [
    "AI Lead",
    "AI Automation Lead",
    "AI & Automation Lead",
    "Enterprise AI Lead",
    "Agentic AI Ops Lead",
]


def _matches(title, terms):
    cfg = Config()
    cfg.titles_include = list(terms)
    job = Job(
        company="Acme",
        title=title,
        url="https://example.invalid/1",
        platform="ashby",
        location="London",
        description="We are hiring.",
    )
    return match(job, cfg)[0]


def test_two_word_ai_terms_drop_the_whole_ai_lead_family():
    """Reproduces the gap: none of these match, strictly or loosely."""
    for title in AI_LEAD_FAMILY:
        assert not _matches(title, TWO_WORD_AI_TERMS), (
            f"{title!r} matched a config with no 'ai lead' term, so this test "
            "no longer reproduces the gap it documents"
        )


def test_adding_ai_lead_catches_the_whole_family():
    """The same titles, with one term added and no code change."""
    for title in AI_LEAD_FAMILY:
        assert _matches(title, WITH_AI_LEAD), title


def test_ai_lead_still_requires_both_words():
    """The added term must not become a catch-all for anything AI or any lead."""
    for title in (
        "AI Engineer",
        "Data Scientist, AI",
        "Warehouse Team Lead",
        "Team Lead, Customer Support",
        "AI Trainer",
    ):
        assert not _matches(title, WITH_AI_LEAD), title


def test_a_title_carrying_head_of_ai_matched_before_the_fix():
    """An AI-Lead-shaped title that also says "Head of ... AI" already matched
    via "head of ai", so the fix is additive rather than a behaviour change."""
    title = "AI Lead - Head of Data and AI"
    assert _matches(title, TWO_WORD_AI_TERMS), (
        "the loose matcher changed; the claim that this fix is purely "
        "additive needs re-checking"
    )
