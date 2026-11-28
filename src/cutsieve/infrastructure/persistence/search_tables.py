"""Immutable query files and the rebuildable SQLite evidence index."""

import re

from sqlalchemy import BigInteger, Column, JSON, String, Table, text

from cutsieve.application.sparse_retrieval import normalize_text

from .sqlalchemy import Base


FTS_TABLE = "media_evidence_fts"
FTS_CREATE = f"""
CREATE VIRTUAL TABLE IF NOT EXISTS {FTS_TABLE} USING fts5(
    workspace UNINDEXED, media_revision_id UNINDEXED, evidence_id UNINDEXED,
    scope_key, raw_text, tokenized_text, tokenize='unicode61'
)
"""
CJK_OR_WORD = re.compile(r"[\u3400-\u9fff]+|[^\W\u3400-\u9fff]+", re.UNICODE)


def fts_scope(run_id):
    return "run" + run_id.replace("-", "")


def fts_terms(value):
    for word in CJK_OR_WORD.findall(normalize_text(value)):
        if "\u3400" <= word[0] <= "\u9fff":
            yield word, list(word) + [word[i:i + 2] for i in range(len(word) - 1)]
        else:
            yield word, [word]


def fts_query(value):
    terms = []
    for word, tokens in fts_terms(value):
        if len(tokens) == 1:
            terms.append(word)
        else:
            # Contiguous bigram phrases cover short phrases exactly. Longer instructions
            # can still retrieve useful evidence through their individual bigrams.
            bigrams = tokens[len(word):]
            terms.append(" ".join(bigrams))
            if len(word) > 4:
                terms.extend(bigrams)
    return " OR ".join(f'"{term}"' for term in list(dict.fromkeys(terms))[:64])


def write_fts(db, workspace, revision, run_id, items):
    db.connection().exec_driver_sql(FTS_CREATE)
    scope_key = fts_scope(run_id)
    db.execute(text(f"DELETE FROM {FTS_TABLE} WHERE {FTS_TABLE} MATCH :scope AND workspace=:workspace"),
               dict(scope=f'scope_key:"{scope_key}"', workspace=workspace))
    # The empty sentinel marks complete generations, including sources with no dialogue.
    rows = [dict(evidence_id="", raw="", tokenized="")]
    for item in items:
        if item["source"] == "sampled-frame":
            continue
        ev = item["evidence"]
        rows.append(dict(evidence_id=ev["evidence_id"], raw=ev["claim"],
                         tokenized=" ".join(term for _, tokens in fts_terms(ev["claim"]) for term in tokens)))
    db.execute(text(f"""
        INSERT INTO {FTS_TABLE} (workspace, media_revision_id, evidence_id, scope_key, raw_text, tokenized_text)
        VALUES (:workspace, :revision, :evidence_id, :scope_key, :raw, :tokenized)
    """), [dict(row, workspace=workspace, revision=revision, scope_key=scope_key) for row in rows])


def remove_fts(db, workspace, revision):
    conn = db.connection()
    if conn.dialect.name == "sqlite" and conn.dialect.has_table(conn, FTS_TABLE):
        db.execute(text(f"DELETE FROM {FTS_TABLE} WHERE workspace=:workspace AND media_revision_id=:revision"),
                   dict(workspace=workspace, revision=revision))


artifacts = Table(
    "vm_search_artifacts",
    Base.metadata,
    Column("id", String(36), primary_key=True),
    Column("workspace", String(36), nullable=False, index=True),
    Column("project", String(36), nullable=False),
    Column("kind", String(24), nullable=False),
    Column("digest", String(64), nullable=False),
    Column("mime", String(80), nullable=False),
    Column("size", BigInteger, nullable=False),
    Column("media_ids", JSON, nullable=False),
)

runs = Table(
    "vm_query_scans",
    Base.metadata,
    Column("run_id", String(36), primary_key=True),
    Column("workspace", String(36), nullable=False),
    Column("project", String(36), nullable=False),
    Column("request_hash", String(64), nullable=False),
    Column("snapshot", JSON, nullable=False),
    Column("answer", JSON(none_as_null=True)),
)
