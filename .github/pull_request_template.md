## Summary

Describe the problem and the change in a few sentences.

## Scope

- Area(s) affected:
- User-visible behavior changed: yes / no
- Compatibility or migration impact:

## Validation

List the tests, commands, or manual checks you ran.

```text
python -X utf8 -m unittest discover -s tests
```

For documentation changes, also consider:

```text
mkdocs build --strict
```

## Safety and release checks

- [ ] No secrets, credentials, private localization data, or non-redistributable game assets are included.
- [ ] New behavior has regression coverage where practical.
- [ ] Configuration or user-facing workflow changes include documentation updates.
- [ ] Changes touching TM writes, QualityGate, release, publishing, filesystem scope, or agent tools preserve deterministic validation and explicit authorization boundaries.
- [ ] The pull request is focused and does not mix unrelated refactors.