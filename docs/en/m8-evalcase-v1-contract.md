# M8 EvalCase v1 contract

[中文](../m8-evalcase-v1-contract.md) | **English**

- Status: maintainer contract for the first public eval slice
- Parent roadmap: #30
- Parent capability: #9
- Coordinates: #13 fixtures/checks, #15 schema validation
- Contract version: `EvalCase schema_version = 1`

## 1. Why this contract exists

Issue #13 is intentionally contributor-sized and issue #15 will independently implement schema
validation. They must not invent two different evaluation formats.

This document freezes the **smallest shared contract** needed for those tasks. It is not an
evaluator implementation and it does not require an AgentRuntime, model provider, baseline runner,
or semantic judge.

The boundary is:

```text
EvalCase definition
        +
Evaluation candidate
        ↓
provider-independent deterministic check
        ↓
AssertionResult evidence
```

Case definitions never contain provider credentials, hidden Prompt material, runtime secrets, or
answers from a test set that is prohibited from Prompt composition.

## 2. Directory ownership

The target layout is:

```text
evals/i18n-prompt/
├── manifest.yaml
├── cases/
│   ├── dev/
│   │   ├── protocol.jsonl
│   │   └── placeholders.jsonl
│   └── locked/
├── rubrics/
└── baselines/
```

For #13, only `cases/dev/protocol.jsonl` and `cases/dev/placeholders.jsonl` are required.

`locked/` means **excluded from Prompt/background/few-shot composition paths**. It does not imply
that a file committed to this public repository is secret. Code that builds Prompt material must
not read locked cases.

`rubrics/` and `baselines/` belong to later #9 work and are not part of #13 or #15 implementation
scope.

## 3. EvalCase v1

A public case is one JSON object per JSONL line.

### 3.1 Required fields

| Field | Type | Contract |
| --- | --- | --- |
| `schema_version` | integer | Must be exactly `1` for this contract. |
| `id` | string | Stable lowercase case ID; see §3.3. |
| `source_locale` | string | Canonical BCP-47-style locale tag such as `en-US`. |
| `target_locale` | string | Canonical BCP-47-style locale tag such as `zh-Hans`. |
| `domain` | string | Short stable domain such as `game-ui`; do not encode a real private project name. |
| `source` | string | Source text presented to the translation system. |
| `assertions` | array | One or more assertion objects from the v1 vocabulary in §4. |
| `provenance` | object | Redistribution/provenance metadata in §3.4. |

### 3.2 Optional fields

| Field | Type | Contract |
| --- | --- | --- |
| `context` | object | Non-secret context needed to evaluate the case, e.g. screen role or length budget. |
| `required_tokens` | array[string] | Literal tokens that must survive or appear when an assertion uses them. Default `[]`. |
| `references` | array[string] | Accepted references when reference scoring is meaningful. Default `[]`; #13 does not need to score them. |
| `difficulty` | string | `basic`, `intermediate`, or `advanced`. |
| `tags` | array[string] | Stable lowercase discovery/filter tags. |

Unknown future fields must not silently change the meaning of a v1 field. #15 may either reject
unknown fields in its public schema or explicitly document a forward-compatible extension point;
it must not reinterpret existing fields.

### 3.3 Stable IDs

Use lowercase ASCII segments separated by `.`. The recommended shape is:

```text
<dimension>.<subtype>.<three-digit-sequence>
```

Examples:

```text
placeholder.printf.001
placeholder.python.004
protocol.item-count.002
protocol.terminator.003
```

Once merged, a case ID is an identity and must not be recycled for a semantically different case.
If the case meaning changes materially, add a new ID.

### 3.4 Provenance

`provenance` must contain:

```json
{
  "kind": "synthetic",
  "license": "GPL-3.0-or-later"
}
```

Allowed `kind` values for v1:

- `synthetic`
- `public-domain`
- `compatible-licensed`
- `authorized-regression`

`license` must be an explicit SPDX-style license expression or another unambiguous redistribution
statement accepted by maintainers. Do not write `unknown`, `fair-use`, or a blank value.

External/derived material should additionally record a `source` identifier/URL and, when relevant,
a short `transformation` description. `authorized-regression` cases must be de-identified and must
not expose restricted game/project text.

## 4. Assertion vocabulary v1

Assertions are objects, not bare strings:

```json
{
  "name": "placeholder_integrity",
  "severity": "hard",
  "params": {
    "syntax": "printf",
    "allow_reorder": true
  }
}
```

`severity` is `hard` or `report`. Deterministic protocol/placeholder regressions used by #13 are
`hard`.

### 4.1 Assertions required by #13

#### `protocol_complete`

Checks the structural response protocol independently of translation quality. Its parameters may
include:

- `expected_items`: positive integer;
- `terminator`: optional literal terminator when the protocol uses one;
- `allow_extra_items`: boolean, default `false`.

A pass requires the expected structure to be complete. Missing items, extra items when disallowed,
malformed item boundaries, or a required missing/malformed terminator are failures.

#### `placeholder_integrity`

Checks **identity and multiplicity**, not merely set membership. This is deliberately named
`placeholder_integrity` rather than `placeholder_set`: `%s %s` becoming `%s` must fail.

Parameters:

- `syntax`: `auto`, `printf`, `python`, `icu`, or `custom`;
- `allow_reorder`: boolean, default `true`.

A pass requires every expected placeholder/token occurrence to be preserved without mutation,
loss, or unintended duplication. Reordering is allowed only when `allow_reorder` is true and the
placeholder syntax supports it.

### 4.2 Reserved deterministic names for #9/#15

The following names are valid v1 vocabulary but #13 is **not required to implement them**:

- `number_fidelity`
- `required_tokens`
- `forbidden_renderings`
- `max_chars`

Their executable semantics belong to #9. #15 may validate their shape, but the first #13 PR should
stay focused on protocol and placeholders.

Adding a new assertion name is an additive contract change and must be documented before fixtures
rely on it. Existing assertion names must never be silently redefined.

## 5. Case definition is not candidate output

Public case files describe **what should be evaluated**. They do not persist a model/provider run
as the canonical answer.

A deterministic checker receives a separate candidate object conceptually equivalent to:

```json
{
  "case_id": "placeholder.printf.001",
  "text": "Welcome, %s! You have %d coins.",
  "raw_response": null
}
```

- `text` is the decoded candidate translation for content assertions.
- `raw_response` is optional and is used when `protocol_complete` must inspect the provider's raw
  batch/protocol response.

#13 may use small local test vectors for candidate outputs. Those vectors are test inputs, not a
replacement for the public EvalCase schema.

## 6. Deterministic result contract

Every check returns structured evidence. A result has at least:

```json
{
  "case_id": "placeholder.printf.001",
  "assertion": "placeholder_integrity",
  "passed": false,
  "severity": "hard",
  "code": "placeholder_missing",
  "message": "Expected placeholder %d is missing.",
  "evidence": {
    "expected": ["%s", "%d"],
    "actual": ["%s"]
  }
}
```

Required result fields:

- `case_id`
- `assertion`
- `passed`
- `severity`
- `code`
- `message`
- `evidence` (object; may be empty on a clean pass)

`code` must be machine-stable. Human-readable `message` may improve without changing result
identity. Evidence should contain the smallest deterministic facts needed to understand/reproduce
the result and must not include credentials or unrelated source material.

Suggested #13 failure codes include:

```text
protocol_missing_item
protocol_extra_item
protocol_malformed_item
protocol_missing_terminator
placeholder_missing
placeholder_extra
placeholder_mutated
placeholder_count_mismatch
```

These codes are guidance for #13; if implementation reveals a clearer minimal partition, maintainers
may adjust them before merge as long as result fields remain stable.

## 7. Minimal illustrative case

This example is illustrative contract documentation, not the #13 fixture corpus:

```json
{
  "schema_version": 1,
  "id": "placeholder.printf.001",
  "source_locale": "en-US",
  "target_locale": "zh-Hans",
  "domain": "game-ui",
  "source": "Welcome, %s! You have %d coins.",
  "required_tokens": ["%s", "%d"],
  "references": [],
  "assertions": [
    {
      "name": "placeholder_integrity",
      "severity": "hard",
      "params": {"syntax": "printf", "allow_reorder": true}
    }
  ],
  "provenance": {"kind": "synthetic", "license": "GPL-3.0-or-later"},
  "difficulty": "basic",
  "tags": ["printf", "placeholder"]
}
```

## 8. Compatibility responsibilities

### #13

Implement synthetic dev fixtures and provider-independent deterministic checks for
`protocol_complete` and `placeholder_integrity`. Do not implement a separate schema format.

### #15

Translate this same v1 field meaning and assertion vocabulary into actionable offline schema
validation. Do not invent a parallel case model.

### #9

Own the later evaluator/report/baseline runtime and any additive assertion vocabulary. It must
continue to accept valid v1 cases or provide an explicit migration when a future major schema
version is introduced.

## 9. Out of scope for v1 prerequisite

This contract does not define semantic model judging, BLEU/COMET-style metrics, Prompt patching,
baseline acceptance policy, provider adapters, AgentRun integration, release gates, or a hidden
benchmark distribution mechanism.
