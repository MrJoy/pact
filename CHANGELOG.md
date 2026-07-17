# Changelog

## v1.2.0 - 2026-07-17

### Added

- Added constrained `pact agent spec-author` and `pact agent repair` commands.
  These commands use the OpenAI Responses API directly, require `PACT_AGENT_*`
  budget/context environment variables, collect a bounded file snapshot, and
  write only inside validated allowed roots when `--apply` is supplied. Without
  `--apply`, they produce a dry-run report with proposed paths.
- Added `IMPROVEMENTS.md` with field notes from using Pact against an existing
  service and marked which v1.2.0 items were addressed.
- Added the Pact engineer lean-review checklist and documented
  production-ready minimalism as a separate review lens.

### Changed

- `pact certify` now fails closed when a contracted component is missing its
  emission compliance test. Existing projects that use certification should
  regenerate or add emission tests before expecting a passing certification.
- `pact init --spec` now validates the build-spec file before project
  initialization, so unsupported formats such as `.md` do not leave a
  half-initialized project behind.
- CLI help and README wording now state that build specs are `.json`, `.yaml`,
  or `.yml`, and that `pact certify` / `pact production validate` operate on
  Pact-managed projects.
- `pact agent repair` now requires each `--source-root` to be scoped to the
  component name or `PACT_AGENT_COMPONENT_ID`; broad roots such as `services`
  are rejected.

### Failure Modes

- Constrained agent commands exit before model calls when required
  `PACT_AGENT_*` caps, `OPENAI_API_KEY`, component-scoped Pact project paths,
  allowed context, or source-root policies are invalid. JSON reports include
  `policy.violations` and no files are written on policy failure. `AGENT_SAFE_*`
  names are intentionally not aliases; wrappers must set the exact
  `PACT_AGENT_*` names.
- Agent reports include `agent.proposed_paths` for dry runs and
  `agent.applied_paths` only for validated `--apply` writes. Apply mode accepts
  one file change per run in v1.2.0.
- Constrained agent writes are rejected when the requested path is outside the
  allowed roots, resolves outside the workspace, crosses an existing symlink, is
  a directory, contains NUL bytes, or exceeds file-size limits.
- Certification artifacts now include emission-test hashes/results. A missing
  emission test records `missing_test: true`, produces a failing verdict, and
  is reported as missing during `--verify-only` artifact checks.

### Validation

- Full local suite: `python3 -m pytest tests/ -v` -> `2268 passed`.

## v1.1.0 - 2026-06-12

- Added typed readiness profiles, AI-authored build specs, optional production
  readiness artifact packs, and production validation/fingerprint commands.

## v1.0.0 - 2026-06-04

- Made Pact plan-first by default: `pact run` stops after contracts/tests unless
  `--implement` is supplied.
- Added the packaged Simulacrum runtime and the `pact review` workflow.
