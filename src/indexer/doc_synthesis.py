"""Convert a normalized OpenMetadata table into:

    1. A payload dict (stored in Qdrant)
    2. A text document (fed to the embedding model)

The payload is what /v1/retrieve returns — keep it intentionally small.
The doc text is what the embedder sees — fold in column descriptions so
column-level intent contributes to retrieval signal even though we don't
emit per-column points.
"""

from __future__ import annotations

from typing import Any, Dict, Tuple


def build_payload(table: Dict[str, Any]) -> Dict[str, Any]:
    """Shape-stable payload for /v1/retrieve consumers."""
    return {
        "name": table["name"],
        "domain": table.get("domain"),
        "description": table.get("description") or "",
        "columns": [
            {
                "name": c["name"],
                "description": c.get("description") or "",
            }
            for c in (table.get("columns") or [])
        ],
        "owner_team": table.get("owner_team"),
    }


def build_doc_text(table: Dict[str, Any]) -> str:
    """Concatenate table + column descriptions into embedder input."""
    parts = []
    name = table.get("name") or ""
    parts.append(f"Table: {name}")
    if table.get("domain"):
        parts.append(f"Domain: {table['domain']}")
    description = (table.get("description") or "").strip()
    if description:
        parts.append(f"Description: {description}")

    cols = table.get("columns") or []
    if cols:
        col_lines = []
        for c in cols:
            cname = c.get("name") or ""
            cdesc = (c.get("description") or "").strip()
            if cdesc:
                col_lines.append(f"- {cname}: {cdesc}")
            else:
                col_lines.append(f"- {cname}")
        parts.append("Columns:\n" + "\n".join(col_lines))

    return "\n".join(parts)


def synthesize(table: Dict[str, Any]) -> Tuple[Dict[str, Any], str]:
    """Convenience: returns (payload, doc_text)."""
    return build_payload(table), build_doc_text(table)
