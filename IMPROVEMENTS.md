# Pact — improvement notes

Field notes from using `pact` in the MEA build (2026-06-24). Logged by the MEA
engineering agent.

## 1. Spec format: `.md` rejected, error is late

Status: fixed for v1.2.0 by validating `--spec` before project initialization
and tightening CLI/README wording to `.json`, `.yaml`, or `.yml`.

`pact init <dir> --spec SPEC.md` runs `init` successfully, then prints
`Unsupported build spec format .md; expected .json, .yaml, or .yml.` — so the project
is half-initialized and the spec is ignored.

**Fixes:**
- Validate the spec extension **before** `init` does any work, and fail fast.
- Consider accepting Markdown specs (a lot of AI-authored specs are `.md`), or
  document the required YAML/JSON schema prominently in `pact init --help` and the
  README's "Initialize from AI-authored build spec" line (it doesn't say the format).
- `pact checklist <dir>` then says "No decomposition tree found. Run decomposition
  first" — chain the hint (tell the user the exact next command).

## 2. Increment fit: from-scratch decomposition vs. an existing service

Status: still a larger product/workflow follow-up; `pact assess` and `pact
adopt` are adjacent but not yet a first-class documented increment mode.

Pact's model (decompose → contracts → black-box implementations verified by contract
tests) is excellent for **greenfield** components. It does **not** fit an *increment to
an existing service* (e.g. "add one immutable table + one endpoint to a 2200-line
FastAPI service that already has its own patterns, migrations, and test harness").
Driving `pact run` there would generate a separate codebase, not a diff.

**Suggestion:** a first-class **increment / adopt mode**: point pact at an existing
repo + a change spec; have it emit *just* the interface contract + contract tests +
anti-gaming tests for the new surface (plan-only), to be implemented in-place. `pact
assess <dir>` and `pact adopt` are close — a documented "plan-only contracts for a
change to an existing codebase" path would make the contract-first discipline usable
mid-project. (For this MEA increment I authored the contract spec by hand and used
pact at the genuinely-applicable gates: `pact assess` for the architectural baseline,
and `advocate`/review for the adversarial gate.)

## 3. Scope clarity: production / certify gates

Status: clarified for v1.2.0 in CLI help and README command descriptions. The
new constrained agent commands also default to dry-run reports and require
explicit `--apply` before writing files.

`pact production validate` and `pact certify` operate on a pact *project* (with the
production-readiness pack). They don't apply to a service that lives in another repo
with its own CI gate cascade. Worth a one-line note in `--help`: these gates assume a
pact-managed project, not an arbitrary external codebase. (`pact assess` correctly
works on any directory — that's the right model for the external-repo case.)

## What worked well

- `pact assess <dir>` on a real 81-file service: fast, dependency-free, useful
  fan-in/fan-out + test-gap signal.
- The toolchain *discipline* (constrain → plan-only contracts → implement → adversarial
  review → skeptic → operational gates) is the right shape; the friction is only in the
  CLI's assumption of a greenfield, pact-owned project.
