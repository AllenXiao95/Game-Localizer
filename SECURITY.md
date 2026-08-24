# Security Policy

Game Localizer handles local files, model-provider credentials, publishing credentials, translation memory, and release artifacts. Security reports are therefore treated as part of release quality, not as ordinary feature requests.

## Supported versions

Security fixes are applied to the latest development branch and, after tagged releases begin, to supported release lines when the issue materially affects users of those versions.

## Reporting a vulnerability

Please do not open a public issue for a vulnerability that could expose credentials, allow unintended file access, bypass release authorization, enable unsafe command execution, or compromise published artifacts.

Instead, use GitHub's private vulnerability reporting / security advisory flow for this repository when available. If that channel is unavailable, contact the maintainer privately through the contact method on the maintainer's GitHub profile.

Please include, when possible:

- affected version or commit;
- impact and attack prerequisites;
- minimal reproduction steps;
- affected operating system and workflow;
- whether credentials, local paths, translation memory, build outputs, or remote publishing are involved;
- any suggested mitigation.

Do not include real credentials, private localization data, or copyrighted game assets in a report.

## Security boundaries

The project follows these principles:

- provider and publishing secrets must not be committed to the repository or written into project prompts, translation memory, or routine logs;
- local services should bind to loopback by default unless an operator explicitly configures otherwise;
- paths supplied through UI, configuration, or future desktop integrations must be validated before file operations;
- model-assisted decisions do not replace deterministic checks for placeholders, terminology, formatting, QualityGate, or release governance;
- agentic workflows must use allowlisted application tools rather than arbitrary shell, unrestricted path access, raw SQLite access, or arbitrary code execution;
- applying translation-memory changes, creating release artifacts, and publishing to remote targets must preserve explicit validation and authorization boundaries;
- security-sensitive failures should fail closed rather than silently downgrade protections.

## Secrets in Git history

CI performs full-history secret scanning. If a credential is committed, removing it from the current tree is not sufficient: revoke or rotate the credential first, then treat repository-history cleanup as a separate remediation step.

## Disclosure

The maintainer will aim to acknowledge valid reports, assess severity and affected versions, prepare a fix or mitigation, and coordinate disclosure proportionally to the issue. Exact response times are not guaranteed for this volunteer-maintained project.