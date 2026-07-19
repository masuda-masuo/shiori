---
name: mcp-tools
description: >
  Conventions and implementation patterns for adding/modifying MCP tools in shiori.
  Covers tool naming, description style, parameter design, error messages,
  and annotations (readOnlyHint / destructiveHint / idempotentHint).
  Read before adding new tools; also reference when reviewing existing tools.
---

# MCP Tools — Implementation Conventions

shiori uses `mcp.server.fastmcp.FastMCP`. All tools are
defined in `src/shiori/mcp_server.py` (2283 lines).

## Tool Naming

```
@mcp.tool(name="shiori_<domain>_<action>")
```

- Prefix `shiori_` is required for all tools.
- Domain may be omitted (e.g. `shiori_search` / `shiori_status`). Domains with multiple tools include a subdomain (e.g. `shiori_pr_diff` / `shiori_pr_changes` / `shiori_pr_review_comments`).
- `<action>` is a verb or noun + verb (`search`, `read_file`, `list_tree`, `pr_changes`).
- Python function names are descriptive; the `name=` argument in the decorator is the actual public name.

### Adding a New Tool

```
shiori_<domain>_<action>
```

Check for namespace collisions with existing tools. There are already 4 `shiori_read_*` tools, so verify no role overlap before adding a new read tool.

## docstring Structure

Tool descriptions are written as Python docstrings. FastMCP reads them automatically as the tool description.

```
"""One-line purpose (the what).

Optional detailed behavior (the how).
May include issue references like (issue #98).

param_name: description of how param works
repo: "owner/name" or short name.
      Omit for default configured repo.
"""
```

### Rules

- MCP tool descriptions are read by LLMs, so **write them in English**.
- Line 1: One-sentence summary of the tool's purpose.
- Lines 2+: Detailed behavior, issue references (`(issue #NNN)`).
- Parameter descriptions go at the end of the docstring in `param: description` format.
- Use the uniform `repo` parameter description format across all tools.

## Error Handling

FastMCP converts standard Python exceptions to MCP errors.

| Exception | When to Use |
|---|---|
| `ValueError` | Invalid parameter, unsupported value, not indexed |
| `FileNotFoundError` | Missing clone, missing file |
| `RuntimeError` | System failure (rg/ctags unavailable, git failure) |

### Writing Error Messages

```
ValueError: what is wrong + how to fix it
```

```python
# OK — shows valid values
raise ValueError(
    f"Invalid source_type: '{source_type}'."
    f" Valid values: {', '.join(sorted(_VALID_SOURCE_TYPES))}"
)

# OK — shows how to fix
raise ValueError(f"#{number} is not indexed (run CLI ingest first)")

# NG — lacks information
raise ValueError("invalid parameter")
```

When partial success occurs, include a `status: "error"` field in the result rather than failing entirely (see `shiori_read_issue`'s `numbers` parameter for a reference pattern).

## Annotations

Existing shiori tools do not use annotations (`readOnlyHint` / `destructiveHint` / `idempotentHint`). When adding a new tool, use the following criteria:

```python
@mcp.tool(
    name="shiori_<name>",
    annotations={
        "readOnlyHint": True,    # does not modify state (most tools)
        "destructiveHint": False, # does not destroy data (most tools)
        "idempotentHint": True,  # safe to call multiple times
    }
)
```

| annotation | Criteria |
|---|---|
| `readOnlyHint: True` | No side effects. All search/read tools. |
| `readOnlyHint: False` | Modifies state (e.g. cache clear, config change). |
| `destructiveHint: True` | Deletes or modifies data. Prompts caller to confirm. |
| `idempotentHint: True` | Safe to call multiple times (all search tools; upsert requires judgment). |

Since almost all shiori tools are read-only, the default for new tools is to set all three annotations as shown above. Adjust individually when adding a tool that modifies state.

## Related Files

- `src/shiori/mcp_server.py` — tool definition source
- `docs/design/06_mcp_server_and_tool_design.md` — design document
