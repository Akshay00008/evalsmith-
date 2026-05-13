# lib/domains/code_assistant.py
# Code-assistant domain priors. Applies to capabilities like rag_qa over
# code, research_agent that browses repos, and chatbots tuned for coding.

from .general import DomainProfile, DomainPrior

CODE_ASSISTANT_DOMAIN = DomainProfile(
    name="code_assistant",
    description="Coding assistant priors. Emphasizes file:line citations, "
                "test-first reasoning, and conservatism around destructive ops.",
    priors=[
        DomainPrior(
            label="file_line_citation",
            note="Code answers should cite file:line so reviewers can verify. "
                 "Hallucinated file paths are the #1 failure mode — guard for them.",
            relevant_arms=["prompt_rewrite", "guardrail_add"],
        ),
        DomainPrior(
            label="test_first_planning",
            note="On fix/feature requests, explicit 'identify failing test or "
                 "write a new one first' planning improves correctness by "
                 "~10pt on SWE-bench-style evals.",
            url_or_doi="https://arxiv.org/abs/2310.06770",
            relevant_arms=["prompt_rewrite", "few_shot_selection"],
        ),
        DomainPrior(
            label="conservative_on_irreversible",
            note="`rm -rf`, `git push --force`, DB migrations — confirm before "
                 "executing. Provoker should red-team these.",
            relevant_arms=["guardrail_add", "tool_schema_edit"],
        ),
    ],
    seed_system_prompt=(
        "You are a coding assistant. Cite file:line for any specific reference. "
        "Before suggesting changes, identify the relevant tests; before destructive "
        "operations (rm, force-push, drop table), confirm explicitly."
    ),
    seed_model="claude-sonnet-4-6",
)
