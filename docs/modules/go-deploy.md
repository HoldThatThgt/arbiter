# go-deploy — `internal/deploy`

## Identity
The one deployment. Collapses the documented 5-step manual ritual (three inits, three
`.mcp.json` entries, manual executor-agent creation) into `arbiter init`, and migrates legacy
checkouts via `arbiter adopt`. Inherits chess deploy's idempotent structured-merge skeleton;
the rest is rewritten.

## Public surface
`arbiter init [--openings] [--no-executor] [--remove] [--embedded-engine]`, `arbiter adopt`.

## `arbiter init` — wiring only, seconds, never builds or indexes
Writes/merges, all idempotent and atomic (temp+rename), merge-preserving for foreign content:
- `.arbiter/run/engines.json` (machine-local, gitignored): resolve the Python interpreter +
  `arbiter-engine` package, verify version handshake, record `{python, engine_version,
  verified_at}`. Typed staleness errors surface in `arbiter status` (ADR-0007).
- Seat key: `.arbiter/match/seat.key`, 0600, gitignored.
- Agents: `.claude/agents/arbiter-curator.md`, `arbiter-executor.md` — generated, key-injected,
  0600, gitignored. Executor agent creation is automated (was the worst manual step).
- Skills: `.claude/skills/{arbiter-play,arbiter-intro,playbook-create}/` (see
  skills-and-playbooks.md).
- `.mcp.json`: ONE entry (`arbiter` → `serve player`) via the merge-preserving atomic writer.
- `.claude/settings.json`: deny rules — `Read(.arbiter/playbook/**)`, `Read(.arbiter/match/**)`,
  `Read(.claude/agents/arbiter-*.md)`; Edit/Write deny on `.arbiter/engine/**` when
  `--embedded-engine`. Stop hook claimed by **exact command match**, plus reclamation of provably
  dead entries (first-token basename `arbiter` + ` hook stop` suffix + the binary no longer
  exists on disk) — a live foreign hook is never hijacked.
- `.gitignore`: `.arbiter/` derived-state entries.
- `.arbiter/config.yml` + empty `playbook/`/`recipes.yaml` scaffolds with commented schema headers.
- Filesystem probe: refuse network mounts with a typed error (flock/WAL need a local FS,
  ADR-0009). `--remove` reverses everything init wrote and nothing else.

## `arbiter adopt` — legacy migration
Moves committed knowledge: `.chess/playbook/*` → `.arbiter/playbook/`, crun recipe book →
`recipes.yaml` (schema-upgraded, content-preserved), cipher `config.yml` extractor settings →
`facts:` section. Deletes derived state by contract (all three projects disclaim derived-state
compatibility). Emits a **whole-token scan checklist** of files containing legacy tool names
(`LoadPlayBook` server names, `crun-mcp` references, …) for manual rewrite — the constitution
forbids automated prose edits. Removes the legacy `.mcp.json` entries it recognizes.

## Invariants
Non-interactive; idempotent (run twice = no diff); never touches files it didn't write except
via the structured mergers; secrets never world-readable; recipe `env` values linted against
secret-shaped names with warnings.

## Tests
Golden-tree tests: init into {empty repo, repo with existing foreign .mcp.json/settings/hooks,
repo with all three legacy tools deployed}; assert exact resulting trees + second-run no-op.
`--remove` round-trip. Adopt fixtures from real chess/crun/cipher deployments. Stop-hook claim
collision fixture (foreign hook with similar trailing words must survive untouched).

## Done
M7. The deny-rule set and Stop-hook claim are `needs-human` on change.
