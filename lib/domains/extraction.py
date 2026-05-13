# lib/domains/extraction.py
# Structured extraction domain priors. Applies to insight_agent and
# nlq_to_query.

from .general import DomainProfile, DomainPrior

EXTRACTION_DOMAIN = DomainProfile(
    name="extraction",
    description="Structured extraction priors. Emphasizes schema strictness, "
                "explicit empty-field handling, and grounded evidence spans.",
    priors=[
        DomainPrior(
            label="explicit_null_over_omitted",
            note="Models silently omitting fields is the dominant schema-validity "
                 "failure. Require explicit `null` in the schema, with a sentence "
                 "in the prompt forbidding omission.",
            relevant_arms=["prompt_rewrite", "tool_schema_edit"],
        ),
        DomainPrior(
            label="evidence_span_grounding",
            note="Requiring the model to emit `evidence_span` (verbatim quote) "
                 "alongside each extracted value cuts hallucination by ~30% on "
                 "DocBench-style evals.",
            relevant_arms=["prompt_rewrite", "tool_schema_edit"],
        ),
        DomainPrior(
            label="sliding_window_long_docs",
            note="For docs exceeding context window, sliding window with "
                 "section-boundary alignment beats fixed-stride chunking by "
                 "~5-10pt on insight recall.",
            relevant_arms=["chunking_change"],
        ),
    ],
    seed_system_prompt=(
        "Extract the requested fields from the document. If a field is absent, "
        "output `null` — never omit. For each extracted value, include the "
        "verbatim `evidence_span` from the source."
    ),
    seed_model="claude-haiku-4-5",
)
