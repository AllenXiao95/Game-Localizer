# General usage guide

[中文](../usage.md) | **English**

## Workflow overview

```text
Prepare resources → validate → scan → preview → QA/human review → release → publish
                                      ↘ SQLite TM reuse ↗
```

Source resources remain read-only. Intermediate state goes to `paths.workspace`, rendered output and artifacts to `paths.output`, and reusable translations to `tm.database`.

## 1. Prepare resources and adapters

| Format | `type` | Notes |
| --- | --- | --- |
| PO/MO | `gettext` | Standard gettext and keyed-source layouts |
| ParaTranz JSON | `paratranz_json` | Field names are configurable in `options` |
| Paradox YAML | `paradox_yml` | Locale-directory mapping and format checks |

Several adapters may be declared. Give each distinct include/exclude globs so one file is not unintentionally matched by multiple adapters.

## 2. Validate and scan

```powershell
localizer validate-config projects/my-project/project.yaml
localizer scan projects/my-project/project.yaml
```

The schema rejects unknown fields instead of ignoring typos. Scanning does not call the model and should be used to verify paths and globs first.

## 3. Preview

```powershell
localizer build projects/my-project/project.yaml `
  --mode preview `
  --run-id preview-001
```

Use a readable, unique run ID. Preview produces translated resources, checkpoints, `run-state.json`, QA reports, and rebuild evidence. It may retain unresolved issues and does not create a publishable release artifact.

## 4. Review in the Dashboard

```powershell
localizer dashboard projects/my-project/project.yaml --host 127.0.0.1 --port 8765
```

Use it to inspect project/TM/tokenizer preflight, start tasks, track progress, locate QA issues, submit attributed revisions, and launch incremental rebuilds. It is a local single-user tool without account authentication; never expose its write API directly to the public internet.

## 5. Incremental rebuild

```powershell
localizer rebuild-from-run projects/my-project/project.yaml `
  --parent-run-id preview-001 `
  --run-id preview-002 `
  --mode preview
```

Add `--version` when the game/resource version changed. Source fingerprints are checked; changed source text is never reused from the parent.

To converge a reviewed parent run directly into a formal build, use the same rebuild path in release mode:

```powershell
localizer rebuild-from-run projects/my-project/project.yaml `
  --parent-run-id preview-002 `
  --run-id release-001 `
  --mode release
```

A release rebuild safely reuses successful parent-checkpoint results whose source is still identical and sends only unresolved work to the Provider. A zero-Provider release is valid when every required translation is reusable. The full QualityGate still runs. After it passes, eligible machine translations actually used by the release—including safely reused parent-checkpoint machine results—must enter the formal TM baseline for the next incremental plan. Preview rebuilds do not perform this authority transition.

## 6. Release build

```powershell
localizer build projects/my-project/project.yaml `
  --mode release `
  --run-id release-001
```

A release enforces the QualityGate: new errors must be zero, and a legacy-debt baseline only permits explicitly registered existing issues. A successful build creates an archive and manifest and makes eligible machine translations that gain authority in this release part of the formal TM baseline for the next plan. Treat the manifest—not the ZIP filename—as the source for hashes, version, mode, QA status, and build metadata.

## 7. Publish

```powershell
localizer publish projects/my-project/project.yaml path/to/artifact-manifest.json
```

Local copy only:

```powershell
localizer publish-local path/to/artifact-manifest.json path/to/destination
```

Remote publishing additionally requires the relevant optional dependency and environment credentials. A missing rotation record is not treated as evidence of a leak. Set `security.credential_rotation_required: true` only for a known leak, accidental credential commit, or mandatory rotation; remote targets then remain fail-closed until both `credential_rotation_completed_at` and `rotation_record` are present. The artifact view shows this state before publishing and reports the reason for every failed target afterward.

## Resource variants

```yaml
paths:
  sources:
    stable: ./source/stable
    beta: ./source/beta
  default_variant: stable
  workspace: ../../var/my-project/workspace
  output: ../../var/my-project/output
```

```powershell
localizer build projects/my-project/project.yaml `
  --variant beta `
  --mode preview `
  --run-id beta-001
```

Variants share TM, glossary, and rules while workspace/output gain variant subdirectories. TM still checks source fingerprints, so the same coordinate with changed text cannot produce a false hit.

## Retention policy

Commit `project.yaml`, prompts/background, glossary, and rules. Keep `.env`, SQLite/WAL/SHM, workspace/output, and tokenizer caches out of Git. SQLite TM, human-decision logs, and published manifests are not ordinary disposable files; back them up before migration, cleanup, or upgrades.
