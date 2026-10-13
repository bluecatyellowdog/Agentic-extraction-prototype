# Writing extraction profiles

A profile is a YAML file that tells the pipeline which tables to build and how the LLM and the validator should treat each column. Everything specific to a domain lives here. The Python code stays generic.

Load a built-in profile by name (`--profile novel`) or any file by path (`--profile path/to/profile.yaml`).

## Top level

| Field | Required | Meaning |
|-------|----------|---------|
| `name` | yes | Identifier. Stored in `run_metadata` and part of the resume key, so resuming with a different profile is refused. |
| `description` | yes | One sentence on what to extract. It opens the system prompt. |
| `instructions` | no | List of general rules for the LLM. |
| `tables` | yes | One or more tables (below). The table name `notes` is reserved. |

## Tables

| Field | Default | Meaning |
|-------|---------|---------|
| `name` | required | Snake-case table name. |
| `description` | required | What one row represents. Sent to the LLM. |
| `key` | none | Column that identifies an entity. Rows with the same key are merged across chunks. Leave it out for an **append-only** table: every row is stored separately and gets an auto `id` plus the `chunk_index` it came from. |
| `columns` | required | Column definitions (below). |
| `instructions` | `[]` | Extra rules for this table only. |
| `seed_keys` | `[]` | Rows created before extraction starts, e.g. a fixed set of categories. Keyed tables only. |
| `blocklist` | `[]` | Key values that are always dropped (case-insensitive). Useful for recurring false positives. Keyed tables only. |

## Columns

| Field | Default | Meaning |
|-------|---------|---------|
| `name` | required | Snake-case column name. |
| `description` | required | What the column holds. Sent to the LLM as part of the schema. |
| `type` | `VARCHAR` | `VARCHAR`, `INTEGER`, `DOUBLE`, `BOOLEAN` or `DATE` (ISO format). A value that can't be converted drops the row. |
| `required` | `false` | Drop the row if this column is empty. Key columns are always required. |
| `allowed_values` | none | Closed set of values. Matching ignores case and treats spaces, hyphens and underscores as equivalent. The value is stored in the spelling given here. Anything else drops the row. |
| `grounding` | none | `exact`: the value must appear verbatim (case-insensitive) in the chunk. `any_token`: at least one significant word must appear, which suits names ("Mr. Holmes" vs "Sherlock Holmes"). |
| `merge` | `replace` | How a later value combines with the stored one. `replace` overwrites, and an empty later value never erases data. `append` keeps distinct fragments joined by `separator`. |
| `separator` | `", "` | Separator for `merge: append` columns. |
| `track_conflicts` | `false` | When a later chunk gives a different non-empty value, log a dispute with both claims and their source text. Only for `replace` columns. |

## Choosing merge and conflict settings

- **Facts that should have one true value** (occupation, birthplace, a category): use `merge: replace` with `track_conflicts: true`. The latest claim is stored, and earlier ones stay reviewable in `fact_disputes`.
- **Facts that accumulate** (aliases, members, descriptions): use `merge: append`.
- **Things that happen** (events, transactions, quotes): use a table without a `key`. Each row keeps its `chunk_index`, so you can join it to `source_chunks` for the original passage.

## Example

[`examples/profiles/wizarding_world.yaml`](../examples/profiles/wizarding_world.yaml) uses most of these features: closed value sets, seeded rows, a blocklist, an integer column and an append-only event table.
