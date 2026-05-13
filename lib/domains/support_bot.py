# lib/domains/support_bot.py
# Customer support chatbot domain priors. These are deliberately not
# universal-truth: they're hints the Strategist may *consider* (and the
# Auditor verifies the chosen variant doesn't violate them by accident).

from .general import DomainProfile, DomainPrior

SUPPORT_BOT_DOMAIN = DomainProfile(
    name="support_bot",
    description="Customer support chatbot. Priors emphasize refusal calibration, "
                "empathetic framing, and clean escalation paths.",
    priors=[
        DomainPrior(
            label="empathy_first_then_action",
            note="Support transcripts show 12-18% lift in CSAT when assistant "
                 "opens with brief acknowledgement before resolution steps. "
                 "Helps especially on frustrated-user turns.",
            relevant_arms=["prompt_rewrite", "few_shot_selection"],
        ),
        DomainPrior(
            label="explicit_escalation_token",
            note="Add a literal '<<ESCALATE>>' token the model emits when "
                 "human handoff is needed. Easier for the host app to route "
                 "than parsing free-form refusal language.",
            relevant_arms=["prompt_rewrite", "tool_schema_edit"],
        ),
        DomainPrior(
            label="over_refusal_costs_more_than_under",
            note="In production support, false refusals visibly anger users; "
                 "false acceptances (acting on out-of-scope) are usually "
                 "harmless. Skew refusal calibration accordingly.",
            relevant_arms=["guardrail_add"],
        ),
    ],
    seed_system_prompt=(
        "You are a customer support assistant for {product_name}. "
        "Acknowledge the user's situation briefly, then provide a concrete next step. "
        "If the request is outside your scope, emit '<<ESCALATE>>' on its own line."
    ),
    seed_model="claude-haiku-4-5",
)
