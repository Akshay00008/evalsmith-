# lib/domains/general.py
# The base DomainProfile schema + the neutral "general" domain. Other
# domains are just instances of this with populated priors.

from __future__ import annotations
from pydantic import BaseModel, Field


class DomainPrior(BaseModel):
    """One specific hint the Strategist may cite. The `url_or_doi` is
    required because in breakthrough mode the Auditor refuses citations
    that don't trace to a real source — this discourages fabricated
    "domain knowledge"."""
    label: str
    note: str
    url_or_doi: str = ""    # may be a paper, blog, or internal-doc URL
    # Which technique families this prior is most relevant to. Lets the
    # Strategist filter for "priors relevant to my chosen arm".
    relevant_arms: list[str] = Field(default_factory=list)


class DomainProfile(BaseModel):
    """A bundle of priors + recommended starting points for one domain."""
    name: str
    description: str
    # Free-form priors. The Strategist surfaces these in prompts to the
    # model when planning the next variant.
    priors: list[DomainPrior] = Field(default_factory=list)
    # Recommended baseline system prompt skeleton. The /init pass seeds
    # the very first variant with this.
    seed_system_prompt: str = ""
    # Recommended initial model — the cost/latency floor varies by domain
    # and we want a sensible default rather than always starting at Haiku.
    seed_model: str = "claude-haiku-4-5"


# The neutral default.
GENERAL_DOMAIN = DomainProfile(
    name="general",
    description="Domain-neutral baseline. Adds no priors beyond the universal seeds.",
    priors=[],
    seed_system_prompt="You are a helpful, accurate assistant. Answer the user's question concisely.",
)
