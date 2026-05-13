# lib/domains/__init__.py
# Domain modules inject *priors* — the kind of hints a human domain expert
# would offer the Strategist on day one. Same idea as the source repo's
# domains (`manufacturing`, `forecasting_demand`) but oriented at GenAI:
#
#   * `general`           — neutral baseline (no priors beyond seeds)
#   * `support_bot`       — customer support priors (refusal patterns,
#                           empathy framing, escalation handling)
#   * `code_assistant`    — code generation/review priors (test-first,
#                           citing files)
#   * `search_qa`         — open-domain QA priors (citation requirement,
#                           recency, multi-hop)
#   * `extraction`        — structured extraction priors (schema strictness,
#                           empty-field handling)

from .general import GENERAL_DOMAIN
from .support_bot import SUPPORT_BOT_DOMAIN
from .code_assistant import CODE_ASSISTANT_DOMAIN
from .search_qa import SEARCH_QA_DOMAIN
from .extraction import EXTRACTION_DOMAIN

DOMAINS: dict[str, "DomainProfile"] = {
    "general": GENERAL_DOMAIN,
    "support_bot": SUPPORT_BOT_DOMAIN,
    "code_assistant": CODE_ASSISTANT_DOMAIN,
    "search_qa": SEARCH_QA_DOMAIN,
    "extraction": EXTRACTION_DOMAIN,
}

# Forward import — pydantic class declared in domains/general.py.
from .general import DomainProfile  # noqa: E402, F401


def get_domain(name: str) -> "DomainProfile":
    """Resolve a domain by name. Unknown -> general (we intentionally do
    not raise, because /plan may pass a free-form domain string and we want
    to degrade gracefully)."""
    return DOMAINS.get(name, GENERAL_DOMAIN)
