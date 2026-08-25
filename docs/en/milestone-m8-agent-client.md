# M8: Quality Engineering, Controlled Intelligence, and Productization

[中文](../milestone-m8-agent-client.md) | **English**

- Status: design proposal
- Scope: local, single-user game-localization pipelines
- Roadmap umbrella: [#30](https://github.com/AllenXiao95/Game-Localizer/issues/30)
- Core architecture constraints: [Game Localizer Core Architecture and Adapter Contract](core-architecture.md)

## 1. Positioning

M8 is **not** a project to turn Game Localizer into a generic agent platform, nor is it a second localization architecture.

The existing deterministic kernel already owns:

```text
ResourceAdapter
→ TranslationUnit
→ TM resolution / translation / QA / review
→ Build
→ ResourceAdapter.render(original source, resolved units)
→ Release / Publish
```

M8 may only build on top of that kernel. If every Controlled Intelligence feature disappeared, the deterministic CLI / Dashboard localization, review, build, and publish workflow must remain fully usable.

M8 is now split into three related but independently optional tracks:

```text
Track A — Quality Engineering
├── Eval Harness
└── deterministic TM Audit

Track B — Controlled Intelligence
├── Prompt Workbench
├── optional TM Repair Advisor
└── optional Release Readiness Advisor

Track C — Productization
└── Windows Tauri client
```

These tracks are not a waterfall of “AgentRuntime → TM Agent → Prompt Agent → Build Agent → Desktop”.

## 2. Core boundaries M8 must preserve

M8 follows the project-level invariants:

1. The original resource is the structural source of truth.
2. ResourceAdapter owns format-specific projection and reconstruction semantics.
3. TranslationUnit is the canonical work-unit boundary consumed by the translation core, not a universal resource IR.
4. Translation knowledge, provenance, and review/authority state are domain state persisted by the TM repository; TM is not a resource interchange format.
5. ProjectRunner, Planner, QA / QualityGate, Review, Build, Artifact, and Publish application services remain authoritative for deterministic workflow semantics.
6. CLI, Dashboard, Tauri, and Controlled Intelligence are callers of application services and may not own parallel resource, TM, QA, Build, or Publish semantics.

Agent/advisor tools should stay at application-level granularity, for example:

```text
project.inspect
translation.plan
tm.audit
prompt.evaluate
build.preview
release.readiness
```

Do not expose low-level escape hatches such as:

```text
arbitrary shell
raw SQL
raw Python
unscoped filesystem writes
po.write / yaml.patch / adapter.raw_render
```

## 3. Track A — Quality Engineering

Quality Engineering is the least speculative M8 work and remains useful even if no broader agentic runtime is ever built.

### 3.1 Eval Harness — #9

Goal: reproducibly answer whether a Prompt / model / config change improves localization quality without deterministic regressions, and how cost/latency changes.

Principles:

- deterministic assertions are the first authority layer;
- baseline diff is a core artifact;
- model judging is limited to semantic/style dimensions that cannot be expressed deterministically and is report-only initially;
- public / development / locked cases remain clearly separated;
- Eval does not reimplement resource parsers; when production resource semantics are needed, enter through TranslationUnit / production validation capabilities;
- keep independent fixture oracles as well, so production code does not generate its own expected answers and test itself.

The shared EvalCase v1 contract was frozen by #32 / #33. Existing #13 and #15 continue under their current scopes; this roadmap realignment does not modify the #13 contributor contract.

### 3.2 Deterministic TM Audit — #8

TM audit has two domains.

**TM-internal audit** may directly inspect:

- SQLite schema / integrity / WAL / backup readiness;
- formal / shadow / review / provenance state;
- authority combinations, internal conflicts, duplicates, or inconsistent records.

**Project-correlated audit** should reuse existing application evidence for:

- current source-fingerprint drift;
- orphan / unreachable coordinates;
- coverage / match-scope / pending deltas;
- anomalies correlated with current TranslationUnit / TranslationPlan / QA state.

The TM Auditor must not grow its own PO/YAML/JSON parser.

Historical audit implementation and tests already exist. The goal is therefore **legacy behavior parity plus adaptation into the current architecture**, not writing a second auditor from scratch. Legacy code acts as an executable specification / regression oracle while the final implementation reuses current TM, Adapter, Planner, and QA capabilities.

## 4. Track B — Controlled Intelligence

Controlled Intelligence is reserved for work that actually needs open-ended evidence synthesis, option comparison, or dynamic next-step selection.

Before adding an intelligent capability, all must be true:

1. the task contains meaningful open-ended judgment rather than a fixed sequence;
2. model errors can be constrained or detected by deterministic validation or permissions;
3. the capability materially reduces operator cognitive load compared with the existing Dashboard / CLI.

Otherwise, implement normal application logic.

### 4.1 Prompt Workbench — #10

This remains the primary potentially valuable intelligence use case.

Recommended flow:

```text
inventory / lint
→ reviewable patch proposal
→ before / after Eval
→ evidence
→ human accept / reject
→ baseline lock
```

Boundaries:

- Prompt Workbench does not parse PO/YAML/JSON resources directly;
- format-specific translation context enters only through Adapter / TranslationUnit / application capabilities;
- Python Prompt composition remains the protocol authority;
- deterministic hard regressions cannot be overridden by model preference;
- the first version should prove a useful non-agent CLI/service path before proposal orchestration is added.

### 4.2 TM Repair Advisor — #31

This is an optional follow-up to #8, not a mandatory deliverable.

Use a model only if deterministic findings actually require open-ended causal analysis, grouping, prioritization, or multi-option repair reasoning.

Normal flow:

```text
tm-audit.json
→ reviewable repair proposal
→ explicit Apply approval
→ TM repository mutation
→ re-audit / re-plan / QA
→ normal Build
→ Adapter.render
```

TM repair never directly mutates source/resource files. Resource changes occur only through the normal TranslationPlan / Build / Adapter-render path.

### 4.3 Governed orchestration — #7

#7 is not a prerequisite for Quality Engineering and must not become a generic harness merely because multiple advisor ideas exist on the roadmap.

If #10 / #31 or other real workflows show duplicated plan / approval / event / recovery needs, extract the smallest shared protocol:

```text
LLM/planner
→ reviewable proposal/plan
→ allowlisted application tool
→ deterministic service
→ verification
```

Permissions remain Inspect / Propose / Apply / Release / Publish and fail closed in Python application services.

### 4.4 Release Readiness Advisor — #11

Keep this P2 and usage-driven.

Phase 1 only synthesizes evidence: why is the release blocked, which blockers actually matter, and what is the minimum safe recovery path?

It consumes existing ResourceScanner / Planner / TM audit / Eval / QualityGate / artifact / publish-receipt evidence. It does not parse resource formats independently, rebuild the fixed Dashboard sequence as chat, or assume a future Build Agent.

If it merely restates Prepare → Preflight → Run → Validate → Repair → Build → Publish, it should not expand.

## 5. Track C — Productization

### Windows Tauri client — #12

Tauri is an independent productization track with no dependency on whether an Agent runtime exists.

The thinnest technical validation is:

```text
Tauri
→ packaged Python sidecar
→ 127.0.0.1 random port
→ versioned health/session handshake
→ existing Dashboard
```

Tauri owns windows, native file pickers, single-instance behavior, sidecar lifecycle, installation, and updates. Python application services continue to own localization, TM, QA, Build, Release, and Publish semantics.

The first prototype proves packaging/lifecycle feasibility only. Do not expand the desktop product merely to “complete M8” without evidence of user value.

## 6. Adapter Conformance and cross-project validation

M8 discussions exposed a more fundamental engineering need: officially supported Adapters should have a shared behavioral contract and conformance coverage.

The current public format boundary remains unchanged:

- Gettext PO/MO;
- ParaTranz JSON;
- Paradox YAML.

Before adding new formats, prioritize verification of:

- identity stability / collision behavior;
- projection semantics;
- no-op semantic round-trip;
- single-unit partial update;
- unrelated structure preservation;
- destination planning;
- render validation.

External games should first serve as static compatibility corpora and falsification targets, not as automatic requirement generators. A project using `.pot`, Fluent, Qt TS, or private placeholder syntax does not by itself expand the roadmap.

Promote a compatibility observation into implementation work only when at least one is true:

1. the current public support contract contains a real defect;
2. two or more real projects repeatedly need the same generic capability;
3. a selected cross-project target cannot work under the current Adapter contract and a non-game-specific improvement can be stated;
4. the project deliberately expands its public support boundary.

## 7. Dependency model

Think in parallel tracks rather than one chain:

```text
Core localization architecture
        │
        ├── Adapter Contract / Conformance
        │
        ├── Quality Engineering
        │      ├── #13 / #15
        │      ├── #9 Eval Harness
        │      └── #8 TM Audit migration
        │
        ├── Controlled Intelligence (conditional)
        │      ├── #10 Prompt Workbench
        │      ├── #31 TM Repair Advisor
        │      ├── #7 minimal shared orchestration if duplication appears
        │      └── #11 Release Readiness Advisor
        │
        └── Productization
               └── #12 Tauri
```

## 8. Stop conditions

M8 does not define success as “implement every issue”.

Valid outcomes include:

- Eval + TM audit provide enough value and no broader Agent is implemented;
- Prompt Workbench remains deterministic tooling rather than becoming conversational;
- most TM findings have deterministic repairs, so #31 never needs an LLM advisor;
- Release Readiness adds no measurable value beyond Dashboard, so #11 remains unimplemented;
- the Tauri thin POC does not demonstrate enough user benefit, so productization pauses.

Project maturity includes making evidence-based decisions **not to implement** roadmap hypotheses.

## 9. Documentation responsibility

This page owns stable architecture constraints, track relationships, and decision rules only.

Concrete schemas, acceptance criteria, implementation slices, and contributor scopes remain in the corresponding GitHub issues so this design page does not drift by duplicating issue details.
