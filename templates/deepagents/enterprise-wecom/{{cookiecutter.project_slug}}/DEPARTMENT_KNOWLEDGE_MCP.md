---
title: Department Knowledge MCP Contract
type: reference
audience: [A2, A3, A4]
runs: no
verified_on: 2026-07-14
sources:
  - digital_employees/industry-report/profile.yaml
  - src/{{ cookiecutter.project_slug }}/department_knowledge/mcp_server.py
  - src/{{ cookiecutter.project_slug }}/department_knowledge/repository.py
  - scripts/import_department_knowledge.py
---

# Department Knowledge MCP Contract

## Boundary

| Item | Contract |
| --- | --- |
| Owner | Strategic Development Department (`战略发展部`). |
| Consumer | `industry-report` DigitalEmployeeProfile. |
| Provider | MCP server named `department-knowledge`. |
| Collection | Server-owned `strategic-development`; callers cannot override scope. |
| Production implementation | External department knowledge platform implementing contract version 1. |
| Local implementation | PostgreSQL 17 with `pgvector` and `pg_trgm`; development and acceptance only. |
| Embedding | bge-m3 ONNX dense vector, 1024 dimensions by default. |
| Mutation | No write MCP tools. Trusted documents enter through an administrator import command. |
| Employee uploads | Request-scoped files only; never promoted automatically to department knowledge. |
| External sources | Gildata and Tavily remain configured. M2-02 requires explicit employee permission before use. |

## Profile Reference

| Field | Value |
| --- | --- |
| `provider` | `mcp` |
| `server` | `department-knowledge` |
| `collection` | `strategic-development` |
| `owning_org` | `战略发展部` |
| `contract_version` | `1` |
| `retrieval_modes` | `keyword`, `semantic`, `hybrid` |
| `default_mode` | `hybrid` |

## Tools

### `knowledge_list_documents`

| Input | Type | Constraint |
| --- | --- | --- |
| `limit` | integer | 1 to 20; default 20. |

Output: fixed organization and collection identifiers plus document metadata.
Document content is not returned.

### `knowledge_search`

| Input | Type | Constraint |
| --- | --- | --- |
| `query` | string | Non-blank. |
| `search_mode` | enum | `keyword`, `semantic`, or `hybrid`; default `hybrid`. |
| `top_k` | integer | 1 to 20; default 8. |

Output: ranked excerpts, `document_id`, `chunk_id`, headings, and component scores.
Hybrid mode combines keyword and semantic rankings with reciprocal rank fusion.

### `knowledge_read_chunks`

| Input | Type | Constraint |
| --- | --- | --- |
| `chunk_ids` | string array | Up to 20 IDs returned by `knowledge_search`. |

Output: full text for selected chunks only. Unknown or cross-collection IDs are omitted.

## Retrieval Order

```text
knowledge_list_documents (when collection discovery is useful)
-> knowledge_search(search_mode=hybrid)
-> knowledge_read_chunks(selected chunk IDs)
-> identify evidence gaps
-> request employee permission before Gildata or Tavily
```

## Storage Tables

| Table | Purpose |
| --- | --- |
| `department_knowledge_documents` | Source digest, title, classification, metadata, and chunk count. |
| `department_knowledge_chunks` | Scoped text chunks, heading, ordinal, and vector embedding. |

Both tables use `tenant_id + collection_id` predicates for every read.
The local server obtains these values from environment settings, not MCP arguments.

## Replacement Rule

An external platform replaces the local simulator by retaining the MCP server name,
the three tool names, input schemas, result fields, and contract version. The
DigitalEmployeeProfile and report workflow remain unchanged.
