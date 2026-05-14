# Chat — Streamlit chat UI that talks to the winning variant. Uses
# Streamlit's built-in st.chat_message / st.chat_input for the look-and-feel
# of a real chat product.

from __future__ import annotations
from pathlib import Path

import streamlit as st

from webui.lib_glue import ensure_lib_on_path, projects_dir, read_mission

ensure_lib_on_path()

st.title("💬 Chat with your assistant")

proj_name = st.session_state.get("active_project")
if not proj_name:
    st.warning("No active project. Pick one on the **📊 Dashboard** first.")
    st.stop()

project_dir = projects_dir() / proj_name
mission = read_mission(proj_name)
if not mission:
    st.error("This project has no Mission yet. Go to **🎯 Mission** first.")
    st.stop()


# Load the winning variant once per session ----------------------

from lib import chat as chat_mod
from lib import registry as model_registry

if "chat_variant" not in st.session_state or st.session_state.get("chat_variant_project") != proj_name:
    try:
        mission_obj, variant = chat_mod.load_winning_variant(project_dir)
        st.session_state["chat_variant"] = variant
        st.session_state["chat_variant_project"] = proj_name
        st.session_state["chat_messages"] = []   # reset on project switch
    except Exception as e:
        st.error(f"Could not load winning variant: {e}")
        st.stop()

variant = st.session_state["chat_variant"]
modality = mission.composition.task_modality

# Status banner about which variant is being used.
st.caption(
    f"Project: **{proj_name}** · Task: **{modality}** · Model: `{variant.generation.model}` · Variant id: `{variant.variant_id}`"
)

with st.expander("ℹ️ What is this screen?"):
    st.markdown(
        f"""
        This is a chat with the **winning variant** the optimizer selected for your project.

        Each message you type goes through the same pipeline that would run in production:
        - For **chatbot** projects → multi-turn conversation with memory.
        - For **RAG / QA** projects → retrieves chunks from `data/corpus.jsonl`, builds the prompt, generates an answer with citations.
        - For **NLQ** projects → generates SQL and (if `data/db.json` exists) executes it and shows the result table.

        Use this to **sanity-check the winner** before shipping. If you spot a bad answer, add the question + correct answer to your eval set and re-run the optimizer.

        All conversations are saved to `results/chat_log_<timestamp>.jsonl`.
        """
    )

st.divider()


# Chat UI -----------------------------------------------------------

# Per-page message buffer (cleared when project switches).
if "chat_messages" not in st.session_state:
    st.session_state["chat_messages"] = []

# Replay history.
for msg in st.session_state["chat_messages"]:
    with st.chat_message(msg["role"]):
        st.markdown(msg["content"])

# Input box pinned at the bottom.
user_input = st.chat_input("Type your message and press Enter")

if user_input:
    # Show the user's message immediately.
    st.session_state["chat_messages"].append({"role": "user", "content": user_input})
    with st.chat_message("user"):
        st.markdown(user_input)

    # Dispatch to the right modality handler. Reuses lib/chat.py turn
    # handlers so behavior matches the CLI REPL exactly.
    with st.chat_message("assistant"):
        with st.spinner("Thinking..."):
            try:
                # We can't use the CLI's turn-handlers directly because they
                # return a single formatted string with telemetry; for the
                # web UI we want the bot text separate from the diagnostics.
                if modality == "chatbot":
                    turns = [
                        {"role": m["role"], "content": m["content"]}
                        for m in st.session_state["chat_messages"]
                    ]
                    call = model_registry.chat_call(
                        system=variant.prompt.system,
                        turns=turns,
                        generation=variant.generation,
                        few_shots=variant.prompt.few_shots,
                    )
                    response_text = call.text
                    diag = f"_{call.input_tokens}+{call.output_tokens} tok · ${call.cost_usd:.4f} · {int(call.latency_ms)}ms_"
                elif modality == "rag_qa":
                    retrieved = model_registry.retrieve(
                        query=user_input, config=variant.retrieval, corpus_dir=project_dir,
                    )
                    ctx_block = "\n\n".join(f"[{d['doc_id']}] {d['text']}" for d in retrieved)
                    rendered = (
                        variant.prompt.user_template.format(input=user_input, context=ctx_block)
                        if "{context}" in variant.prompt.user_template
                        else f"Context:\n{ctx_block}\n\nQuestion: {user_input}"
                    )
                    call = model_registry.model_call(
                        system=variant.prompt.system,
                        user=rendered,
                        generation=variant.generation,
                        few_shots=variant.prompt.few_shots,
                    )
                    cite_str = ", ".join(d["doc_id"] for d in retrieved[:3]) if retrieved else "(no retrieved chunks)"
                    response_text = call.text
                    diag = f"_retrieved: {cite_str} · {call.input_tokens}+{call.output_tokens} tok · ${call.cost_usd:.4f}_"
                elif modality == "nlq_to_query":
                    from lib.capabilities.nlq import _extract_sql
                    call = model_registry.model_call(
                        system=variant.prompt.system,
                        user=variant.prompt.user_template.format(input=user_input),
                        generation=variant.generation,
                        few_shots=variant.prompt.few_shots,
                    )
                    sql = _extract_sql(call.text)
                    out_md = f"**SQL:**\n```sql\n{sql}\n```\n"
                    db_cfg_path = project_dir / "data" / "db.json"
                    if db_cfg_path.exists():
                        from lib import db as db_mod
                        cfg = db_mod.DBConfig.model_validate_json(db_cfg_path.read_text(encoding="utf-8"))
                        res = db_mod.safe_execute(cfg, sql)
                        if res.ok:
                            import pandas as pd
                            preview = pd.DataFrame(res.rows[:20], columns=res.columns)
                            out_md += "\n**Result:**\n"
                            response_text = out_md
                            st.markdown(response_text)
                            st.dataframe(preview, use_container_width=True, hide_index=True)
                            response_text += "\n\n" + preview.to_markdown(index=False)
                        else:
                            response_text = out_md + f"\n_Execution failed ({res.error_kind}): {res.error_message}_"
                    else:
                        response_text = out_md + "\n_(No `data/db.json` configured — SQL not executed.)_"
                    diag = f"_{call.input_tokens}+{call.output_tokens} tok · ${call.cost_usd:.4f}_"
                else:
                    # Fallback for other capabilities.
                    call = model_registry.model_call(
                        system=variant.prompt.system,
                        user=variant.prompt.user_template.format(input=user_input),
                        generation=variant.generation,
                        few_shots=variant.prompt.few_shots,
                    )
                    response_text = call.text
                    diag = f"_{call.input_tokens}+{call.output_tokens} tok · ${call.cost_usd:.4f}_"

                if modality != "nlq_to_query":
                    st.markdown(response_text)
                st.caption(diag)
                st.session_state["chat_messages"].append({
                    "role": "assistant",
                    "content": response_text + "\n\n" + diag,
                })
            except Exception as e:
                st.error(f"Chat call failed: {e}")
                st.session_state["chat_messages"].append({
                    "role": "assistant",
                    "content": f"_Error: {e}_",
                })


# Side controls ----------------------------------------------------

with st.sidebar:
    st.subheader("Chat controls")
    if st.button("🗑 Reset conversation"):
        st.session_state["chat_messages"] = []
        st.rerun()
    if st.button("💾 Save transcript"):
        # Hand off to lib/chat.py's session save logic — we manually pack
        # the messages into the same row shape.
        import json, time
        out = project_dir / "results" / f"chat_log_{int(time.time())}.jsonl"
        out.parent.mkdir(parents=True, exist_ok=True)
        rows = []
        msgs = st.session_state.get("chat_messages", [])
        # The CLI saves user/assistant *pairs* per turn; reconstruct.
        for i in range(0, len(msgs) - 1, 2):
            if msgs[i]["role"] == "user" and i + 1 < len(msgs):
                rows.append({"turn": i // 2, "user": msgs[i]["content"], "assistant": msgs[i+1]["content"]})
        with out.open("w", encoding="utf-8") as f:
            for r in rows:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
        st.success(f"Saved to {out.relative_to(projects_dir().parent)}")
