# Upload Data — the page where the user adds:
#   1. Eval cases (CSV / Excel / JSONL)
#   2. PDFs (optional, for RAG)
#   3. Database connection (optional, for NLQ)
# All three are independently optional but at least an eval set is needed
# before /plan and /run.

from __future__ import annotations
from pathlib import Path
import json
import io

import streamlit as st
import pandas as pd

from webui.lib_glue import ensure_lib_on_path, projects_dir

ensure_lib_on_path()

st.title("📁 Upload Data")

proj_name = st.session_state.get("active_project")
if not proj_name:
    st.warning("No active project. Pick one on the **📊 Dashboard** first.")
    st.stop()

project_dir = projects_dir() / proj_name
data_dir = project_dir / "data"
data_dir.mkdir(parents=True, exist_ok=True)

st.markdown(f"Active project: **{proj_name}**")
st.markdown("Upload your test questions / data here. Most projects only need step **1**.")

# Tabs let the user focus on whichever upload they need.
tab_eval, tab_pdfs, tab_db = st.tabs(["1️⃣ Eval questions (required)", "2️⃣ PDFs (RAG only)", "3️⃣ Database (NLQ only)"])


# ---------------------------------------------------------------------------
# Tab 1: Eval set upload
# ---------------------------------------------------------------------------

with tab_eval:
    st.markdown(
        "Upload a list of **questions you want the assistant to answer well**, "
        "along with the **gold/correct answers**. The optimizer will use these to "
        "score every variant it tries."
    )
    st.markdown(
        "Accepted formats: **CSV**, **Excel (.xlsx)**, or **JSON Lines (.jsonl)**.\n\n"
        "Required columns:\n"
        "- `case_id` — a unique id per row (any short string)\n"
        "- `input` — the question or input\n"
        "- `expected` — the correct/gold answer (leave blank if the question should be refused)\n\n"
        "Optional columns:\n"
        "- `tags` — comma-separated tags like `policy,short_answer`\n"
        "- `relevant_doc_ids` — comma-separated chunk ids the answer should cite (RAG only)"
    )

    uploaded = st.file_uploader(
        "Drop a CSV / XLSX / JSONL file here",
        type=["csv", "xlsx", "jsonl", "json"],
    )

    if uploaded is not None:
        try:
            # Parse depending on extension.
            ext = Path(uploaded.name).suffix.lower()
            if ext == ".csv":
                df = pd.read_csv(uploaded)
            elif ext == ".xlsx":
                df = pd.read_excel(uploaded)
            else:
                # JSONL — parse line by line.
                content = uploaded.read().decode("utf-8")
                rows = [json.loads(line) for line in content.splitlines() if line.strip()]
                df = pd.DataFrame(rows)

            st.markdown(f"**Preview ({len(df)} rows):**")
            st.dataframe(df.head(10), use_container_width=True)

            # Basic validation: required columns present?
            missing = {"case_id", "input"} - set(df.columns)
            if missing:
                st.error(f"Missing required columns: {missing}")
            elif len(df) < 20:
                st.warning(f"Only {len(df)} rows. The optimizer requires at least **20** to lock a Mission.")
            else:
                if st.button(f"Save as eval set ({len(df)} cases)", type="primary"):
                    # Coerce to the JSONL shape the framework expects.
                    out_path = data_dir / "eval_set.jsonl"
                    with out_path.open("w", encoding="utf-8") as f:
                        for _, row in df.iterrows():
                            case = {
                                "case_id": str(row["case_id"]),
                                "input": row["input"] if pd.notna(row["input"]) else "",
                                "expected": row["expected"] if "expected" in df.columns and pd.notna(row.get("expected")) else None,
                                "tags": _split_csv(row.get("tags")) if "tags" in df.columns else [],
                            }
                            if "relevant_doc_ids" in df.columns and pd.notna(row.get("relevant_doc_ids")):
                                case["relevant_doc_ids"] = _split_csv(row["relevant_doc_ids"])
                            f.write(json.dumps(case, ensure_ascii=False, default=str) + "\n")
                    st.success(f"✅ Saved {len(df)} cases to `data/eval_set.jsonl`. You can now go to **🎯 Mission**.")

        except Exception as e:
            st.error(f"Could not parse file: {e}")
            st.caption("Tip: make sure the header row matches the required column names.")

    # Show current eval set status if one already exists.
    eval_path = data_dir / "eval_set.jsonl"
    if eval_path.exists():
        n_cases = sum(1 for line in eval_path.read_text(encoding="utf-8").splitlines() if line.strip())
        st.info(f"Current eval set: **{n_cases} cases** at `data/eval_set.jsonl`.")
        if st.button("🗑 Delete current eval set"):
            eval_path.unlink()
            st.rerun()


# ---------------------------------------------------------------------------
# Tab 2: PDF upload + ingestion
# ---------------------------------------------------------------------------

with tab_pdfs:
    st.markdown(
        "Drop PDFs your assistant should answer questions about. After uploading, "
        "click **Ingest** to extract text and prepare them for retrieval."
    )
    st.caption("Only relevant for RAG, search, or research-agent projects.")

    raw_pdf_dir = data_dir / "raw_pdfs"
    raw_pdf_dir.mkdir(exist_ok=True)

    pdf_files = st.file_uploader(
        "Drop PDF files here (multiple allowed)",
        type=["pdf"],
        accept_multiple_files=True,
    )
    if pdf_files:
        for pdf in pdf_files:
            (raw_pdf_dir / pdf.name).write_bytes(pdf.read())
        st.success(f"✅ Uploaded {len(pdf_files)} file(s) to `data/raw_pdfs/`")

    existing_pdfs = sorted(raw_pdf_dir.glob("*.pdf"))
    if existing_pdfs:
        st.markdown(f"**PDFs in this project ({len(existing_pdfs)}):**")
        for p in existing_pdfs:
            size_kb = p.stat().st_size / 1024
            st.markdown(f"- `{p.name}` ({size_kb:.0f} KB)")

        chunk_size = st.slider(
            "Chunk size (characters per chunk)",
            min_value=400, max_value=3000, value=1500, step=100,
            help="Smaller chunks → more precise retrieval but answers may straddle boundaries.",
        )
        overlap = st.slider(
            "Overlap (characters)",
            min_value=0, max_value=500, value=200, step=50,
            help="Bigger overlap → safer chunk boundaries but more chunks total.",
        )

        if st.button("⚙️ Ingest PDFs into searchable corpus", type="primary"):
            # Use the same logic as tools/ingest_pdfs.py without subprocess.
            try:
                import sys
                from pathlib import Path as _Path
                sys.path.insert(0, str(_Path(__file__).resolve().parent.parent.parent))
                from tools.ingest_pdfs import ingest_project
                with st.spinner("Extracting text + chunking..."):
                    n = ingest_project(project_dir, chunk_size_chars=chunk_size, overlap_chars=overlap, dry_run=False)
                st.success(f"✅ Ingested {n} chunks into `data/corpus.jsonl`.")
            except Exception as e:
                st.error(f"Ingestion failed: {e}")
                st.info("Tip: make sure `pypdf` is installed. Run `pip install pypdf`.")

    corpus_path = data_dir / "corpus.jsonl"
    if corpus_path.exists() and corpus_path.stat().st_size > 0:
        n_chunks = sum(1 for line in corpus_path.read_text(encoding="utf-8").splitlines() if line.strip())
        st.info(f"Current corpus: **{n_chunks} chunks** at `data/corpus.jsonl`.")


# ---------------------------------------------------------------------------
# Tab 3: DB connection
# ---------------------------------------------------------------------------

with tab_db:
    st.markdown(
        "Connect a database so the optimizer can actually run the SQL it generates "
        "(execution equivalence instead of string match)."
    )
    st.caption("Only relevant for NLQ projects. Use a **read-only** DB role.")

    st.markdown("**Pick your database type:**")
    db_type = st.selectbox(
        "Database",
        ["SQLite (file on disk)", "PostgreSQL", "MySQL / MariaDB", "Oracle", "MS SQL Server"],
    )

    # Each db_type drives a different URL template. We hide SQLAlchemy
    # URL knowledge from the user.
    if db_type == "SQLite (file on disk)":
        sqlite_path = st.text_input("Path to .db file", value=str(data_dir / "sample.db"))
        url = f"sqlite:///{sqlite_path}"
    elif db_type == "PostgreSQL":
        host = st.text_input("Host", value="localhost")
        port = st.text_input("Port", value="5432")
        dbname = st.text_input("Database name")
        user = st.text_input("Username")
        password = st.text_input("Password", type="password")
        url = f"postgresql+psycopg://{user}:{password}@{host}:{port}/{dbname}"
    elif db_type == "MySQL / MariaDB":
        host = st.text_input("Host", value="localhost")
        port = st.text_input("Port", value="3306")
        dbname = st.text_input("Database name")
        user = st.text_input("Username")
        password = st.text_input("Password", type="password")
        url = f"mysql+pymysql://{user}:{password}@{host}:{port}/{dbname}"
    elif db_type == "Oracle":
        host = st.text_input("Host", value="localhost")
        port = st.text_input("Port", value="1521")
        service = st.text_input("Service name / SID")
        user = st.text_input("Username")
        password = st.text_input("Password", type="password")
        url = f"oracle+oracledb://{user}:{password}@{host}:{port}/?service_name={service}"
    else:  # MSSQL
        host = st.text_input("Host", value="localhost")
        dbname = st.text_input("Database name")
        user = st.text_input("Username")
        password = st.text_input("Password", type="password")
        url = f"mssql+pyodbc://{user}:{password}@{host}/{dbname}?driver=ODBC+Driver+18+for+SQL+Server"

    timeout_ms = st.number_input("Query timeout (ms)", min_value=500, max_value=120_000, value=5000, step=500)
    max_rows = st.number_input("Max rows returned per query", min_value=10, max_value=100_000, value=1000, step=100)

    if st.button("🔌 Test connection + introspect schema", type="primary"):
        cfg_path = data_dir / "db.json"
        cfg_path.write_text(json.dumps({
            "url": url,
            "query_timeout_ms": int(timeout_ms),
            "max_rows": int(max_rows),
            "read_only": True,
        }, indent=2), encoding="utf-8")

        try:
            from lib import db as db_mod
            cfg = db_mod.DBConfig.model_validate_json(cfg_path.read_text(encoding="utf-8"))
            with st.spinner("Connecting + introspecting schema..."):
                schema = db_mod.introspect_schema(cfg)
            if not schema:
                st.warning("Connected, but no tables found (check permissions / `allowed_tables`).")
            else:
                st.success(f"✅ Connected. Found **{len(schema)} table(s)**.")
                text = db_mod.schema_to_prompt(schema)
                (data_dir / "schema.txt").write_text(text, encoding="utf-8")
                with st.expander("View schema dump"):
                    st.code(text, language="sql")
        except Exception as e:
            st.error(f"Connection failed: {e}")
            st.info("Common fixes: install the right driver (psycopg, pymysql, oracledb, pyodbc); check credentials; ensure the DB is reachable from this machine.")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _split_csv(value) -> list[str]:
    """Parse a comma-separated tag/id field tolerantly."""
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return []
    return [s.strip() for s in str(value).split(",") if s.strip()]
