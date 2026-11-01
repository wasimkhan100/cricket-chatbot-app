# Reports

Generated locally by a review run and not committed (they contain machine paths and point-in-time findings):

| File | Written by |
|---|---|
| `findings.json`, `findings.sarif`, `summary.md` | the security-reviewer agent |
| `raw/` | gitleaks, trivy and promptfoo outputs |
| `decisions.json`, `signoff.json` | you, in the Review Console (triage and release gate) |
| `guard.log` | `hooks/guard.py`: every allowed and blocked tool call of the reviewer |

`findings.schema.json` is the contract every `findings.json` must satisfy.
