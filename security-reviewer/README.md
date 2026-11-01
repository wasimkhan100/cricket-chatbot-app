# security-reviewer

A **read-only release security reviewer** for this repo: a Claude Code sub-agent, its skills, two local MCP scanners, a promptfoo
runtime corpus and a findings schema. It reads code, runs the allowlisted scanners, optionally tests the local agent at
`http://localhost:8080`, and writes `reports/findings.json`, `reports/findings.sarif` and `reports/summary.md` with a
**PASS / WARN / BLOCK recommendation**. **CI policy, not the model, makes the final decision.**

## Layout

| Path | What |
|---|---|
| `agents/security-reviewer.md` | The sub-agent: frontmatter (tools, MCP, skills, hook) and the contract |
| `hooks/guard.py` | PreToolUse guard. Writes only to `corpus/` and `reports/`; Bash allowlist; no credential reads; curl only to localhost:8080. Logs to `reports/guard.log` |
| `skills/agent-release-review/` | Our skill: the OWASP ASI/LLM/Web/API taxonomy, the AG→ASI remap, 7 mandatory checks, the disposition rules |
| `skills/{secure-code-review,api-security,llm-top-10,agentic-top-10,prompt-injection}` | UnitOneAI (MIT), with an import-notes block |
| `skills/differential-review` | Trail of Bits (**CC BY-SA 4.0**, modified: see `vendor/SOURCES.md`) |
| `mcp.json` | semgrep and trivy as local stdio MCP servers |
| `corpus/promptfooconfig.yaml`, `corpus/provider.mjs` | 12 runtime cases (golden, edge, integration, adversarial) against localhost:8080 only |
| `reports/findings.schema.json` | The findings contract |
| `vendor/` | Read-only reference clones + `SOURCES.md` (SHAs, licences, what changed) |

## Installed into Claude Code (symlinks, so this folder stays the source of truth)

| Claude Code path (per code.claude.com/docs) | → |
|---|---|
| `.claude/agents/security-reviewer.md` | `security-reviewer/agents/security-reviewer.md` |
| `.claude/skills/<name>` (7 skills) | `security-reviewer/skills/<name>` |
| `.mcp.json` (project scope) | `security-reviewer/mcp.json` |

Claude Code asks you to approve the two project MCP servers, and to trust the folder before the agent's hook runs.
**Accept the folder trust, or the guard hook will not run.** Without it, only the agent's tool list limits it.

## Run it step by step from a browser (human in the loop)

```
python3 security-reviewer/console/server.py     # prints http://127.0.0.1:8090/#token=...; open that URL
```

Six stages run as a chain of agents. Each shows exactly what it will execute and waits for **Approve & run**. You then check
its result and click **Result looks right, continue** (or run it again, or skip it) before the next one unlocks:
1. Secrets scanner (gitleaks)
2. Dependency scanner (trivy)
3. Runtime tester (promptfoo)
4. Security reviewer: a Claude agent with an editable prompt. You watch every tool call and every guard block live.
5. Your triage of each finding (confirm / dispute / accept risk with owner and expiry) → `reports/decisions.json`
6. Release gate: the disposition rules applied to your decisions, and your sign-off → `reports/signoff.json`

The console binds to 127.0.0.1 only, and every API call needs the token from the printed URL.

## Trigger it

In Claude Code, from the repo root:

```
Use the security-reviewer agent to review this repo for release.
Use the security-reviewer agent to review the diff main...HEAD.
@security-reviewer review backend/src/cricket_chatbot/agents/
```

## Rerun

1. Delete or keep the old `reports/findings.*` and `reports/summary.md` (the agent overwrites them).
2. Optional runtime tests: start the app on **port 8080**. Crickbot defaults to 8000:
   `CRICKET_PORT=8080 scripts/start.sh`
3. Optional fault tests (integration suite). In **your own** shell, never through the agent:
   `export SR_ALLOW_FAULTS=1 CRICKBOT_ADMIN_TOKEN=...`, then run promptfoo yourself:
   `PROMPTFOO_DISABLE_TELEMETRY=1 PROMPTFOO_DISABLE_UPDATE=1 promptfoo eval -c security-reviewer/corpus/promptfooconfig.yaml -o security-reviewer/reports/promptfoo.json --no-cache`
   Without these variables the integration cases report **NOT RUN**.
4. Ask the agent again (see "Trigger it").

## Known limits (be honest about these in reviews)

- **This folder is not a git repo**, so `differential-review`, `git diff` and `gitleaks git` (history) are not-assessed until you run `git init`/clone.
- **semgrep** with registry rulesets (`p/python`, `auto`) needs network. The guard only allows local `--config` paths. Either approve
  a rules download yourself or use the semgrep MCP server.
- **trivy fs** needs its vulnerability DB. Download it once yourself (`trivy fs --download-db-only`); the guard then requires `--skip-db-update --skip-check-update --skip-version-check`.
- **MCP tool inputs** (semgrep/trivy MCP) are logged but not path-checked by the guard. They still go through the normal permission prompt.
- **Grep over a folder must set `glob`/`type`**, because ripgrep would otherwise search `.env`.
- Crickbot cannot simulate a model **429**, or a slow **tool timeout**. I1 uses a model failure and I2 an immediate tool failure as stand-ins.
- Integration cases switch Crickbot chaos flags on and back off. Only do this on a local dev instance.
