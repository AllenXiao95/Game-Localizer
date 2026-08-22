# M8: Agentic Workflows and Tauri Client Design

[中文](../milestone-m8-agent-client.md) | **English**

- Status: design proposal
- Target version: M8
- Implementation status: not started
- Scope: local, single-user game-localization pipelines

## 1. Background and decision

Game Localizer already provides resource scanning, a normalized translation-unit abstraction,
SQLite TM, model-assisted translation, deterministic QA, QualityGate, human revision, artifact
building, and publishing. The Dashboard already exposes local HTTP APIs for task preflight,
execution, review, rebuild, and publishing.

M8 will not rewrite that business kernel. It will add two layers on top:

1. A controlled agent orchestration layer that organizes TM validation, Prompt-material
   construction, evaluation, and builds into planned, pausable, resumable, and auditable tasks.
2. A Tauri desktop shell that packages the Python application as a sidecar, reuses the existing
   WebUI and local API, and does not require end users to install Python or use a command line.

The core decisions are:

- The Python kernel remains the sole authority for configuration, TM, QA, QualityGate, build,
  and publishing semantics.
- Agents may invoke only allowlisted tools; they receive no arbitrary shell, unrestricted path,
  or raw SQLite access.
- Model judgments provide recommendations or evaluation signals only. They do not replace
  deterministic checks for placeholders, terminology, formatting, or release governance.
- Formal TM writes, release, and publish all require independent deterministic validation and
  explicit human authorization.
- Tauri manages windows, native dialogs, single-instance behavior, the sidecar lifecycle,
  installation, and updates.
- By default, the Python sidecar continues serving the WebUI to preserve same-origin APIs and
  maximize reuse of the current implementation.

## 2. Goals and non-goals

### 2.1 Goals

- Provide a TM inspection agent that finds stale, conflicting, duplicated, orphaned, malformed,
  or incorrectly authoritative records and generates reviewable repair proposals.
- Provide a Prompt workbench that assists with creating and maintaining the foundations of
  `prompt.md`, `background.md`, `glossary.yaml`, and `rules.yaml`.
- Build or compliantly reuse an i18n Prompt evaluation set so Prompt and model changes can be
  compared reproducibly.
- Provide a build agent that orchestrates validate, scan, plan, preview, QA, rebuild, release,
  and publish while explaining every block with structured evidence.
- Ship a Windows client based on Tauri + a Python sidecar while preserving explicit build
  boundaries for macOS and Linux.
- Keep the CLI, Dashboard, and desktop client on the same application services so that three
  separate business implementations cannot drift apart.

### 2.2 Non-goals

- M8 will not build an online collaboration platform with accounts, permissions, task
  assignment, and multi-user approval.
- Agents will not autonomously accept legacy debt, promote glossary terms, overwrite
  human-approved translations, or publish artifacts directly.
- LLM judges will not replace the existing QualityGate.
- Provider keys, publishing credentials, and archive passwords will not be written to Prompts,
  project YAML, agent logs, or TM.
- The first client installer will not include a large language model or Hugging Face model
  weights.
- M8 does not require all operating systems to ship at once. The first production target is
  Windows x86-64.

## 3. Overall architecture

```text
┌──────────────────────── Tauri desktop ────────────────────────┐
│ Window / menus / native pickers / single instance / updates  │
│                         │                                     │
│             startup, handshake, health check                  │
└─────────────────────────┼─────────────────────────────────────┘
                          │ stdin/stdout control channel
┌─────────────────────────▼─────────────────────────────────────┐
│              packaged Python sidecar                         │
│  DashboardServer / Agent Orchestrator / application services │
│          │                    │                    │           │
│       Web API          deterministic tools       audit log    │
└──────────┼────────────────────┼────────────────────┼───────────┘
           │                    │                    │
      existing WebUI      TM / QA / build      run artifacts
                               │
                 Provider / local publish / remote publish
```

Tauri does not duplicate Python domain logic. Apart from windows, file selection, credential
bridging, sidecar management, and client updates, all business operations go through a versioned
local API.

### 3.1 Sidecar startup protocol

Add a dedicated entry point:

```text
localizer-sidecar desktop-server --config <project.yaml> --port 0
```

Protocol requirements:

1. Tauri starts the sidecar and passes session material through stdin or controlled IPC. Secrets
   must not appear in command-line arguments, URLs, or persistent logs.
2. The sidecar binds to a random port on `127.0.0.1`.
3. The sidecar writes one machine-readable handshake record to stdout containing at least the
   `protocol_version`, port, PID, and project identity. Subsequent business logs go to stderr or
   structured log files and must not contaminate the handshake channel.
4. Tauri calls `/api/health` and shows the main window only after the API protocol and project
   configuration are ready.
5. The WebUI obtains a short-lived session capability through Tauri IPC and attaches it to write
   requests. The capability must not be placed in a query string or local storage.
6. On exit, Tauri first checks run state. With no active task it shuts down gracefully. With an
   active task, the user chooses whether to keep it in the tray, wait, or confirm cancellation;
   the client must not silently kill a process writing a checkpoint or TM.
7. If Tauri exits unexpectedly, the sidecar detects the missing parent and performs bounded
   cleanup. The next launch reuses existing owner-lock, task-snapshot, and checkpoint recovery
   rules.

### 3.2 Local security boundary

- Bind to loopback only; production builds do not expose `--host 0.0.0.0`.
- Generate a new session capability on every launch and validate it, together with the existing
  action header, on all write APIs.
- Prevent arbitrary external navigation in the WebView. New windows and external links go to the
  system browser through an allowlist.
- Paths returned by Tauri dialogs are still validated by Python using `resolve(strict=True)` and
  scope checks.
- Version the sidecar API and fail closed when the client and sidecar protocols do not match.
- Store credentials in operating-system credential facilities or another reviewed secure store,
  and inject them into the sidecar only for a single task execution.
- The agent tool layer rejects arbitrary SQL, Python expressions, shell commands, and
  unconstrained paths.

## 4. Agent execution model

Here, an agent is a combination of model planning, deterministic tools, and a state machine—not a
chatbot with machine-control privileges. Every AgentRun produces an immutable plan and an
append-only event record.

### 4.1 Shared state machine

```text
draft → planned → awaiting_approval → running → verifying → completed
                   │                  │          │
                   └→ rejected        ├→ paused  └→ failed
                                      └→ failed
```

Every step records at least:

- `agent_run_id`, agent type, and protocol version;
- project, resource variant, game version, and operator;
- configuration revision, source-resource fingerprint, TM revision, Prompt revision, and
  evaluation-set version;
- model, Provider, sampling parameters, tokens/cost, and tool calls;
- input/output summaries, generated files, deterministic validation results, and human decisions;
- recovery point, failure classification, and recommended next action.

Agent output cannot exist only as natural language. Every plan, repair proposal, and execution
result must conform to a versioned JSON schema.

### 4.2 Tool permission levels

| Level | Examples | Default authorization |
| --- | --- | --- |
| Inspect | Config validation, scan, TM query, QA summary, Prompt lint | Automatic and read-only |
| Propose | Generate TM patch, Prompt patch, or build plan | Automatic; writes only to an isolated proposal directory |
| Apply | Apply TM or Prompt repair | Human confirmation; backup and fingerprint recheck first |
| Release | Production build or debt acceptance | Explicit confirmation every time; debt acceptance may require a higher privilege |
| Publish | Upload to a remote target | Separate from release; confirm each target independently |

A model cannot turn a lower-level tool call into a higher-level action. Python application
services enforce permissions; Prompt instructions do not define the security boundary.

## 5. TM validation agent

### 5.1 Read-only inspection scope

The first TM-agent pass is inspection-only and covers at least:

- SQLite schema and integrity, WAL state, and the ability to create a recoverable backup;
- source-fingerprint drift at a stable coordinate, stale formal records, and abnormal
  shadow/formal state;
- multiple translations for the same source, coordinate conflicts, duplicate records, and
  unreachable or orphaned records;
- authority precedence among human-approved content, machine candidates, historical migration,
  and ParaTranz synchronization;
- placeholders, control characters, source-language residue, normalization, and approved-term
  violations;
- traceability of run, model, Prompt hash, reviewer, and provenance fields;
- changes in TM coverage, match scopes, and untranslated counts against the current resource scan.

Inspection produces a unified `tm-audit.json`. Every issue includes a stable identity, severity,
evidence, recommended action, and whether an automatic repair proposal can be generated.

### 5.2 Repair proposal and application

The TM agent does not modify the database directly. It first generates
`tm-repair-plan.json`:

- The plan is bound to the database file identity, pre-transaction revision, resource
  fingerprint, and configuration revision.
- Every action declares its previous value, target value, rationale, provenance, and rollback
  information.
- Formal retirement, authority switching, bulk unification, and deletion actions are grouped
  separately.
- Before applying a plan, create a consistent backup through the SQLite backup API.
- At apply time, revalidate the plan fingerprint and execute inside a single transaction.
- After application, rerun TM audit, translation planning, and relevant QA. Roll back the
  transaction and retain the report if validation fails.
- Do not overwrite a higher-authority human record unless the user explicitly authorizes it
  through a dedicated governance entry point.

The first version may automatically propose deletion, authority switching, and legacy-debt
acceptance, but it cannot apply them automatically.

## 6. Prompt foundation and design agent

### 6.1 Prompt-material boundaries

The Prompt agent manages project-level foundation materials:

- `prompt.md`: target language, tone, form of address, naming, and formatting requirements;
- `background.md`: world building, UI context, character relationships, and usage context;
- `glossary.yaml`: structured, reviewable, and optionally scoped terminology;
- `rules.yaml`: filtering, normalization, and deterministic QA rules.

Batch numbering, output protocol, actual source text, and structured glossary injection remain
assembled by Python code such as `PromptComposer`, so projects do not copy protocol text. Agents
must not write runtime data, credentials, or evaluation answers into project Prompts.

### 6.2 Workflow

```text
project metadata/samples → inventory → scaffold → lint → eval → diff → human acceptance
                         → versioned files
```

The Prompt agent supports:

- generating initial materials from locales, the resource adapter, representative text samples,
  and a user-provided style target;
- finding duplication or conflicts across Prompt, background, glossary, and rules;
- moving deterministically expressible constraints into glossary/rules instead of continuing to
  accumulate natural-language instructions;
- generating file-level patches without silently overwriting existing content;
- running the same evaluation set before and after a change and showing quality, cost, and latency
  differences;
- storing a Prompt bundle manifest containing material digests and the assembler version;
- locking an accepted published Prompt baseline and retaining its revision identity in release
  manifests.

## 7. i18n Prompt evaluation set

### 7.1 Construction and reuse principles

M8 is expected to combine compliantly reusable public data with original synthetic and regression
cases rather than copying game text with unknown licensing. Before importing external data, record:

- original source, version, license, and permitted use and redistribution;
- language pair, domain, cleaning, and transformation methods;
- whether it contains personal information, unpublished text, or restricted material;
- differences from real project distributions and known bias.

Data with uncertain provenance or licensing may be used only for temporary local research and
cannot enter the repository or public benchmark. Real-project regression cases require
authorization and de-identification. The public default set should prefer original synthetic,
public-domain, or explicitly compatible-licensed text.

### 7.2 Evaluation dimensions

- Response protocol: numbering, item count, terminator, and JSON/text structural completeness.
- Placeholders and markup: printf, Python, ICU, XML/BBCode, escapes, newlines, and custom game
  tokens.
- Terminology: required terms, prohibited renderings, casing, inflection, and path scope.
- Semantics: omission, addition, negation, numbers, units, entities, and reference resolution.
- Locale conventions: punctuation, spacing, numbers, dates, plural forms, honorifics, gender, and
  locale-specific formatting.
- UI constraints: short button text, width budgets, menu consistency, and shortcut markers.
- Style and register: character voice, system messages, narrative text, and age rating.
- Consistency: identical source text, cross-file entities, contextual variants, and historical
  baselines.
- Robustness: prompt-injection-like source text, mixed languages, unusual Unicode, long text, and
  incomplete context.
- Cost and performance: input/output tokens, latency, failure rate, repair retries, and batch
  throughput.

### 7.3 Proposed data format

```json
{
  "schema_version": 1,
  "id": "placeholder.printf.001",
  "source_locale": "en-US",
  "target_locale": "zh-Hans",
  "domain": "game-ui",
  "source": "Welcome, %s! You have %d coins.",
  "context": {"screen": "login-reward", "max_chars": 40},
  "glossary": [],
  "required_tokens": ["%s", "%d"],
  "references": ["欢迎你，%s！你有 %d 枚金币。"],
  "assertions": ["protocol", "placeholder_set", "number_fidelity"],
  "provenance": {"kind": "synthetic", "license": "CC0-1.0"},
  "difficulty": "basic",
  "tags": ["ui", "printf"]
}
```

Proposed directory:

```text
evals/i18n-prompt/
├── manifest.yaml
├── cases/
│   ├── protocol.jsonl
│   ├── placeholders.jsonl
│   ├── terminology.jsonl
│   ├── semantics.jsonl
│   └── locale-style.jsonl
├── rubrics/
│   └── semantic-fidelity.yaml
└── baselines/
    └── <prompt-bundle>-<provider-model>.json
```

### 7.4 Scoring and gates

Scoring has three layers:

1. Deterministic assertions for protocol, placeholders, numbers, length, terminology, and format.
2. Reference- and rule-based scoring through normalized matching, multiple accepted references,
   and character/word-level differences.
3. Model judging and human sampling only for semantics, register, and style that cannot be judged
   deterministically.

Every model-judge result records the judge model, Prompt, parameters, and raw rationale, and the
judge is calibrated against a fixed sample. In the first version, model judging produces reports
only and does not directly block release. Hard gates should favor deterministic metrics, for
example:

- Protocol completeness and placeholder preservation must be 100%.
- No new hard number or entity errors are allowed.
- Deterministic failure counts must not regress relative to an accepted baseline.
- Semantic/style scores below baseline request human review instead of automatically rewriting
  formal TM.

The development set may be used to iterate on Prompts. A locked test set must not be injected into
Prompts, background, or few-shot examples, preventing answer-specific overfitting. Reports compare
quality, cost, and latency rather than optimizing only one aggregate score.

## 8. Build agent

The build agent reuses the existing ProjectRunner and web task service; it does not reimplement the
pipeline. Proposed tool split:

| Tool | Permission | Artifact |
| --- | --- | --- |
| `project.validate` | Inspect | Configuration diagnostics |
| `resource.scan` | Inspect | Scan manifest and resource fingerprint |
| `tm.audit` | Inspect | TM audit report |
| `translation.plan` | Inspect | Translation plan and cost estimate |
| `prompt.evaluate` | Inspect | Evaluation report and baseline diff |
| `build.preview` | Apply | Preview, QA, and checkpoint |
| `review.propose` | Propose | Revision proposal without formal TM writes |
| `build.release` | Release | Release manifest and artifact |
| `publish.prepare` | Inspect | Targets, credential status, and read-back plan |
| `publish.execute` | Publish | Per-target publishing result |

Recommended execution order:

```text
validate → scan → TM audit → translation plan → prompt baseline check
         → preview → QA → human revision/incremental rebuild
         → release preflight → human confirmation → release
         → publish preflight → per-target confirmation → publish + read-back
```

Constraints:

- The agent first presents the plan, estimated model-call volume, write locations, and irreversible
  actions.
- Preview may execute automatically after the user approves one workflow. Release and publish
  cannot reuse a vague, long-lived authorization.
- Revalidate source/config/TM/Prompt fingerprints before and after execution; replan on drift.
- A QualityGate failure can lead only to explanation, revision proposals, or rebuild. The agent
  cannot bypass the gate by lowering severity.
- Publish and release are separate authorization domains. Each publishing target returns its own
  success, failure, and retryable state.
- Task recovery continues to reuse checkpoints and does not repay already successful model calls.

## 9. Client user experience

The first client version provides at least:

- Welcome page: open `project.yaml`, recent projects, and remove invalid recent entries.
- Project page: reuse the current Dashboard and add native source-directory, single-file, and
  `.env` picker buttons.
- Agent center: show plan, permission level, estimated cost, current step, evidence, and actions
  awaiting confirmation.
- Prompt workbench: material diff, lint, evaluation-set selection, baseline comparison, and patch
  acceptance.
- TM inspection: issue clusters, repair proposals, backup location, and post-apply verification.
- Task tray: long-running background work, reopen window, and exit confirmation.
- Diagnostic bundle: export redacted logs, versions, and environment state after explicit user
  selection; exclude credentials and original private text.
- About/update page: client, sidecar, API protocol, and data-schema versions.

Client-owned data follows platform application-data conventions instead of the installation
directory:

```text
<app-data>/game-localizer/
├── client.json
├── logs/
├── diagnostics/
└── updates/
```

Project TM, workspace, and output remain defined by `project.yaml`; client upgrades must not move
or delete them.

## 10. Proposed code and artifact layout

```text
src/localizer/agent/
├── models.py          # AgentRun, plan, permission, and event schemas
├── orchestrator.py    # State machine, recovery, and approval
├── tools/
│   ├── tm.py
│   ├── prompt.py
│   └── build.py
└── audit.py            # Append-only audit events

src/localizer/web/
├── server.py           # /api/agent/*, /api/health, session validation
└── static/index.html   # Agent/Prompt/TM UI; split static assets later if needed

src-tauri/
├── src/                # Sidecar lifecycle, windows, tray, and native capabilities
├── capabilities/       # Minimum-permission allowlist
└── tauri.conf.json

evals/i18n-prompt/      # Compliant evaluation data, rubrics, and baselines
```

All data crossing the Tauri/Python boundary has a schema and `protocol_version`. Tauri understands
only the client control protocol and generic task states, not TM classifications or QualityGate
details.

## 11. Delivery stages

### M8.1: Protocol and agent foundations

- Define AgentRun, plan, event, approval, and tool-result schemas.
- Add an append-only agent audit log and resumable state machine.
- Build allowlisted tool adapters over existing application services.
- Add API versioning, health checks, and session capabilities.
- Cover privilege escalation, fingerprint drift, and recovery with a fake planner/provider.

### M8.2: Prompt workbench and evaluation set

- Complete the Prompt bundle manifest and linting.
- Complete evaluation-set manifest, JSONL schema, license checks, and data versioning.
- Start with deterministic protocol, placeholder, number, and terminology cases.
- Add semantic/style rubrics and human sampling without making them release hard gates.
- Produce at least one reproducible baseline for an existing example Prompt.

### M8.3: TM and build agents

- Implement read-only TM audit and structured repair proposals.
- Implement backup, fingerprint recheck, transactional application, and post-apply verification.
- Implement preview orchestration, QA explanation, and incremental rebuild.
- Keep release and publish approvals independent and add privilege-boundary regression tests.

### M8.4: Tauri Windows client

- Produce the Windows Python sidecar with either PyInstaller or Nuitka through a fixed,
  reproducible build.
- Implement sidecar handshake, random port, window, file dialogs, single-instance behavior, and
  tray in Tauri.
- Require no system Python and do not include tokenizer weights in the installer.
- Add Windows installation, update, uninstall, and abnormal-exit smoke tests.
- Complete code signing, third-party notices, and the GPL corresponding-source delivery process.

### M8.5: Cross-platform and release hardening

- Add macOS and Linux sidecar/client build matrices.
- Verify WebUI behavior under WKWebView and WebKitGTK.
- Complete macOS signing/notarization and per-platform update artifacts.
- Establish a client/sidecar protocol compatibility matrix and rollback drills.

## 12. Acceptance criteria

### Agent and governance

- An agent cannot execute arbitrary shell, SQL, or out-of-scope path access through Prompts or tool
  parameters.
- Inspect and Propose do not modify formal project data.
- TM Apply requires a consistent backup, human approval, and an unchanged plan fingerprint.
- An agent cannot overwrite higher-authority human TM, accept legacy debt, or bypass QualityGate.
- Release and every remote publish leave independent human decisions and append-only audit records.
- An interrupted agent resumes from the last committed step without repeating successful model
  requests.

### Prompt evaluation

- Every evaluation case has a stable ID, version, provenance, and license field.
- Locked test and development sets are separated, and reports demonstrate that test answers did
  not enter the evaluated Prompt.
- The same Prompt bundle, model snapshot/identity, and parameters produce structurally consistent
  reports.
- Deterministic code can independently recompute protocol, placeholder, terminology, and numeric
  metrics.
- Baseline reports include quality, failure categories, tokens, cost, and latency.
- A model judge alone cannot promote a failing result to release-ready status.

### Client

- On a clean Windows environment, a user can open a project and run preview without installing
  Python.
- The sidecar uses a random loopback port and rejects write requests without the current session
  capability.
- For identical input, client and CLI produce identical plan, QA, manifest, and TM results.
- Closing the window during a run does not corrupt checkpoint/TM, and the user can resume or keep
  the task running in the background.
- Install, update, and uninstall do not delete project TM, workspace, output, or user-selected game
  resources.
- The installer provides signatures, versions, third-party notices, and a corresponding-source
  access path.
- The existing full test suite continues to pass, with added sidecar-protocol and client smoke
  tests.

## 13. Risks and mitigations

| Risk | Mitigation |
| --- | --- |
| Agent hallucination or privilege escalation | Allowlisted tools, permission levels, schema validation, deterministic gates, and human approval |
| Prompt overfitting to the evaluation set | Separate development/locked test sets, hidden tests, category-level regression, and real human sampling |
| Unclear external-data licensing | Require provenance/license; default to original synthetic or compatible-licensed data |
| Bulk TM repair damages authoritative data | Consistent SQLite backup, transaction, fingerprint, authority precedence, and post-apply validation |
| Tauri/sidecar lifecycle mismatch | Versioned handshake, parent detection, health checks, tray policy, and crash-recovery tests |
| Oversized Python package | Package only target dependencies, omit model weights, and start with a directory-mode sidecar |
| Cross-platform WebView differences | Ship Windows first; later add real macOS/Linux builds and UI smoke tests |
| Automatic update damages data | Update application files only; keep project data external and make protocol migrations reversible |

## 14. Decisions to freeze before implementation

Before M8.1 begins, design review must decide:

1. The first Provider interface for the agent planner and its offline fake implementation.
2. Whether agent audit uses JSONL files, a dedicated SQLite table, or both.
3. How Tauri passes session capabilities to the sidecar and how protocol versions are managed.
4. Whether the Windows sidecar uses PyInstaller or Nuitka, and directory or one-file delivery.
5. Whether credentials are managed by Tauri secure storage or Python keyring.
6. License-review results and redistribution terms for the first public i18n data sources.
7. Which deterministic evaluation metrics become hard gates for Prompt baselines.
8. The default behavior when closing a window during a run and the exact meaning of “cancel task.”

Schema prototypes and read-only evaluation may proceed before these decisions are frozen, but
formal TM Apply, automatic updates, and remote publish agents should not be implemented early.
