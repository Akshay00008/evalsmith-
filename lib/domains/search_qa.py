# lib/domains/search_qa.py
# Open-domain search QA domain priors. Applies to rag_qa, research_agent
# over web/wiki corpora, and search_engine.

from .general import DomainProfile, DomainPrior

SEARCH_QA_DOMAIN = DomainProfile(
    name="search_qa",
    description="Open-domain QA priors. Emphasizes recency-awareness, multi-hop "
                "decomposition, and citation requirement.",
    priors=[
        DomainPrior(
            label="multi_hop_decomposition",
            note="Multi-hop questions answered as a single retrieve-and-generate "
                 "score ~15pt below decomposed-then-stitched approaches (HotpotQA, "
                 "MuSiQue). Consider sub-question planning.",
            url_or_doi="https://arxiv.org/abs/1809.09600",
            relevant_arms=["prompt_rewrite", "tool_schema_edit"],
        ),
        DomainPrior(
            label="hyde_for_underspecified",
            note="For sparse queries, generating a hypothetical answer first and "
                 "embedding *that* often improves recall@k by 4-8pt vs embedding "
                 "the raw query (HyDE).",
            url_or_doi="https://arxiv.org/abs/2212.10496",
            relevant_arms=["retriever_change"],
        ),
        DomainPrior(
            label="rerank_high_payoff",
            note="Cross-encoder rerankers consistently lift NDCG@10 by 5-12pt "
                 "vs bi-encoder retrieval alone — usually worth the latency.",
            relevant_arms=["rerank_add_or_change"],
        ),
    ],
    seed_system_prompt=(
        "You answer questions using the provided context. Cite the source doc id "
        "for each claim in the form [doc_id]. If the context is insufficient, "
        "say so explicitly rather than guessing."
    ),
    seed_model="claude-haiku-4-5",
)
