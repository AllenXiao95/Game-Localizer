# Build TM from existing translations

[中文](../tm-bootstrap.md) | **English**

## Why an unknown structure cannot be guessed safely

For arbitrary files, the framework cannot reliably infer the stable key, source field, translation field, or whether a coordinate should remain identical after files move. A wrong guess is worse than a missed import because a translation can be attached to another key and reused in later releases.

There are two ingestion paths:

1. A configured adapter already supports the structure: inspect and import the translated resources directly.
2. The structure is unknown or unsupported: explicitly map it to neutral TM Seed JSON first.

Both commands are dry-run by default. Only `--apply --accepted-by <operator>` writes validated translations as `reviewed + formal + human` rows.

## Path 1: bootstrap supported resources directly

### Supported cases

| Structure | Existing translation can be read when |
| --- | --- |
| Gettext PO/MO | `layout: standard`; `msgid` is source and `msgstr` is translation |
| ParaTranz JSON | Each item contains the configured key/source/translation fields |
| Paradox YAML | Source- and target-locale files coexist according to adapter directory rules |

Gettext `keyed_source` treats `msgstr` as source text and contains no target translation by itself, so it cannot bootstrap from that file alone.

### 1. Configure the resource

Example for standard PO:

```yaml
paths:
  source: ./existing-localization

resources:
  adapters:
    - type: gettext
      include: ["**/*.po"]
      options:
        layout: standard
        empty_source: skip
        source_filter: all
```

### 2. Scan and dry-run

```powershell
localizer validate-config projects/my-project/project.yaml
localizer scan projects/my-project/project.yaml
localizer tm-bootstrap-resources projects/my-project/project.yaml
```

The last command does not write TM. Its report is:

```text
<workspace>/reports/tm-bootstrap-resources.json
```

Key fields:

- `total`: every unit extracted by adapters;
- `accepted`: non-empty translations that passed placeholder, language-rule, and glossary checks;
- `rejected`: input coordinates with at least one blocking issue;
- `issue_counts` / `issues`: grouped and coordinate-level details.

If `rejected` is nonzero, apply refuses the whole batch instead of leaving a partial, hard-to-explain baseline.

### 3. Attest and write

```powershell
localizer tm-bootstrap-resources projects/my-project/project.yaml `
  --apply `
  --accepted-by localization-owner
```

An existing database is first copied with the SQLite backup API into a sibling `backups` directory. A new project gets a new database and schema. The report records operator, backup, and final counts.

For source variants:

```powershell
localizer tm-bootstrap-resources projects/my-project/project.yaml `
  --variant stable `
  --apply `
  --accepted-by localization-owner
```

## Path 2: convert an unknown structure to TM Seed

### Coordinate semantics

Every record must map these fields:

| Field | Meaning | Requirement |
| --- | --- | --- |
| `adapter_id` | Adapter that will read the real resource | Must equal a configured `resources.adapters[].type` |
| `relative_path` | Resource path relative to `paths.source` | Must match adapter scan output; use `/` separators |
| `logical_key` | Stable key within the file | Must match what the adapter extracts |
| `source_text` | Original source-language text | Used to compute the source fingerprint |
| `translation` | Existing target-language translation | Must be non-empty and passes QA |

`project_id` and locales come only from `project.yaml`; a Seed cannot override them.

If the old structure has no stable key, generate a deterministic key from a business ID. Do not use an order-dependent array index unless the production adapter will use exactly the same index forever.

### Single-file example

Use top-level defaults when all entries belong to one adapter/path:

```json
{
  "schema_version": 1,
  "defaults": {
    "adapter_id": "paratranz_json",
    "relative_path": "ui/menu.json"
  },
  "entries": [
    {
      "logical_key": "menu.start",
      "source_text": "Start Game",
      "translation": "开始游戏",
      "context": "main menu button"
    },
    {
      "logical_key": "menu.exit",
      "source_text": "Exit",
      "translation": "退出"
    }
  ]
}
```

Copy [`../examples/tm-seed-single.json`](../examples/tm-seed-single.json).

### One Seed containing several resource files

Omit defaults and put full coordinates on each entry:

```json
{
  "schema_version": 1,
  "entries": [
    {
      "adapter_id": "gettext",
      "relative_path": "ui/menu.po",
      "logical_key": "Start Game",
      "source_text": "Start Game",
      "translation": "开始游戏"
    },
    {
      "adapter_id": "gettext",
      "relative_path": "items/names.po",
      "logical_key": "Mana Potion",
      "source_text": "Mana Potion",
      "translation": "法力药水"
    }
  ]
}
```

### Multiple Seed files in one import

Large projects should generate one Seed per original resource:

```text
tm-seed/
├── ui-menu.json
└── item-names.json
```

Pass all files to one command:

```powershell
localizer tm-import-seed projects/my-project/project.yaml `
  docs/examples/tm-seed-multi/ui-menu.json `
  docs/examples/tm-seed-multi/item-names.json
```

Copy the shared [`examples/tm-seed-multi`](https://github.com/AllenXiao95/Game-Localizer/tree/master/docs/examples/tm-seed-multi) examples. Inputs are merged before validation. A repeated `adapter_id + relative_path + logical_key`, including across files, produces `duplicate_coordinate`.

### Dry-run and apply

```powershell
localizer tm-import-seed projects/my-project/project.yaml path/to/seed.json
```

Default report:

```text
<workspace>/reports/tm-seed-import.json
```

After review:

```powershell
localizer tm-import-seed projects/my-project/project.yaml path/to/seed.json `
  --apply `
  --accepted-by localization-owner
```

`--report` and `--backup` select explicit locations. When a database exists, the backup destination must not already exist.

## Mapping an unknown structure

Answer five questions before writing a read-only converter:

1. What defines one translation record?
2. Which field or combination identifies it permanently?
3. Where are source and translation stored?
4. Which adapter will read the production resource?
5. What `relative_path` and `logical_key` will that adapter produce?

Map those answers to Seed. The converter must not edit input files and should report input count, output count, and skip reasons. Validate a small sample before converting everything.

If questions 4 and 5 cannot be answered, the missing component is a production resource adapter, not merely an import format. Implement the adapter first; otherwise imported coordinates will never match future builds.

## Verify after import

```powershell
localizer build projects/my-project/project.yaml --mode preview --run-id tm-check
```

Check that `tm_hits` is close to the number of valid imported coordinates, `machine_successes` covers only new/changed source, QA shows no coordinate mismatch, and sampled path/key/source triples match the original resources. Row count alone does not prove a useful TM; production adapter coordinates and source fingerprints must match.
