# go-deploy — `internal/deploy` (+ `internal/embeddedengine`, the `go:embed` engine unpacker)

## Identity
The one deployment. Collapses the documented 5-step manual ritual (three inits, three
`.mcp.json` entries, manual executor-agent creation) into `arbiter init`, and migrates legacy
checkouts via `arbiter adopt`. Inherits chess deploy's idempotent structured-merge skeleton;
the rest is rewritten.

## Public surface
`arbiter init [--no-executor] [--remove] [--embedded-engine]`, `arbiter adopt`.

## `arbiter init` — wiring only, seconds, never builds or indexes
Writes/merges, all idempotent and atomic (temp+rename), merge-preserving for foreign content:
- `.arbiter/run/engines.json` (machine-local, gitignored): resolve the Python interpreter +
  `arbiter-engine` package, verify version handshake, record `{python, engine_version,
  verified_at}`. Typed staleness errors surface in `arbiter status` (ADR-0007).
- Seat key: `.arbiter/match/seat.key`, 0600, gitignored.
- Agents: `.claude/agents/arbiter-{curator,executor,implementer,test-author}.md` — generated,
  key-injected, 0600, gitignored (the `arbiter-debugger` companion variant is written below).
  Executor-seat agent creation is automated (was the worst manual step).
- Engine resolution (ADR-0011, automatic ladder): (1) an installed `arbiter-engine` package for
  `python3` — probed with a PYTHONPATH-scrubbed env so a dev shell can't fake "installed" —
  else (2) materialize the go:embed engine into `.arbiter/engine/` (digest-keyed idempotent
  unpack, `*.py` only, Edit/Write deny rules, gitignored) — else (3) no `python3` at all:
  diagnostics skipped with a loud single-prerequisite hint. Guidance always reports the mode.
- Companion diagnostics (ADR-0010): with a resolved engine, add-if-missing the two `.mcp.json`
  entries — launched via the resolved interpreter (`python3 -m arbiter_engine.gdbmcp serve
  --root .` / `… perfmcp serve`; embedded mode adds `env.PYTHONPATH=.arbiter/engine`), NEVER
  via the arbiter binary (deny-self, ADR-0006); an existing entry is foreign content and
  survives untouched — and write `.claude/agents/arbiter-debugger.md`, an executor-seat agent
  variant wired with both companions (key-injected, 0600, gitignored, deny-read).
- Skills: `.claude/skills/{arbiter-play,arbiter-intro,playbook-create}/` (see
  skills-and-playbooks.md).
- `.mcp.json`: ONE arbiter entry (`arbiter` → `serve player --root <abs>`) via the
  merge-preserving atomic writer, plus the add-if-missing companion entries above. Every seat
  entry, agent frontmatter server, and the Stop-hook command carries an explicit absolute
  `--root` (ADR-0014) — spawn cwd is never load-bearing.
- `.claude/settings.json`: deny rules — `Read(.arbiter/playbook/**)`, `Read(.arbiter/match/**)`,
  `Read(.claude/agents/arbiter-*.md)`, plus **unconditional** Edit/Write deny on the same three
  zones (`.arbiter/playbook/**`, `.arbiter/match/**`, `.claude/agents/arbiter-*.md`) — ADR-0015;
  and Edit/Write deny on `.arbiter/engine/**` **in embedded mode (flag or automatic fallback)**.
  Stop hook claimed by **exact command match**, plus reclamation of provably
  dead entries (first-token basename `arbiter` + ` hook stop` suffix + the binary no longer
  exists on disk) — a live foreign hook is never hijacked.
- `.gitignore`: `.arbiter/` derived-state entries.
- `.arbiter/config.yml` + `recipes.yaml` scaffolds with commented schema headers (write-if-missing,
  user state). The `.arbiter/playbook/` directory is **not** empty: init refreshes `FORMAT.md` plus
  the 8 deploy-owned starter openings (ADR-0012) to the latest shipped templates on every run, so an
  upgraded binary re-seeds the newest playbooks; user-authored playbooks (names outside the starter
  set) are never touched.
- Filesystem probe: refuse network mounts with a typed error (flock/WAL need a local FS,
  ADR-0009). `--remove` reverses everything init wrote and nothing else.

## `arbiter adopt` — legacy migration
Moves committed knowledge: `.chess/playbook/*` → `.arbiter/playbook/`, crun recipe book →
`recipes.yaml` (schema-upgraded, content-preserved), cipher `config.yml` extractor settings →
`facts:` section. Deletes derived state by contract (all three projects disclaim derived-state
compatibility). Emits a **whole-token scan checklist** of files containing legacy tool names
(`LoadPlayBook` server names, `crun-mcp` references, …) for manual rewrite — the constitution
forbids automated prose edits. Removes the legacy `.mcp.json` entries it recognizes.

**PreToolUse guard (ADR-0015):** init wires `arbiter hook guard --root <abs>` (matcher Bash|Read|Edit|Write|NotebookEdit|Glob|Grep) denying model access to playbook/match/engine/agent paths with teaching reasons; deny rules remain as defense in depth; `--remove` strips it.

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
