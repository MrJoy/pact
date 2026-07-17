# Lean Review

Use this after correctness, security, and production-readiness review. It is a
separate pass that hunts unnecessary complexity. It does not get to waive
controls.

## Ladder

Stop at the first rung that holds:

1. Does this need to exist at all?
2. Does stdlib solve it?
3. Does a native platform feature solve it?
4. Does an already-installed dependency solve it?
5. Can the minimum correct behavior be expressed more directly?
6. Only then add the minimum custom code.

## Hunt

- Wrapper-only files and one-implementation interfaces.
- Factories, registries, queues, brokers, services, or config layers with one
  caller and no measured reason to exist.
- New dependencies that duplicate stdlib, native platform behavior, or an
  installed dependency.
- Hand-rolled parsing, validation, caching, formatting, retry, or scheduling
  behavior that a trusted existing capability already covers.
- Speculative extension points, flags, knobs, and abstractions nobody uses.
- Comments and code paths that claim future scale without naming the trigger
  and upgrade path.

## Load-Bearing Review

For each layer, abstraction, or dependency, ask:

1. Does removing it make a trust boundary unenforceable?
2. Does removing it make a failure mode unobservable?
3. Does removing it make rollback non-atomic or evidence gathering impossible?
4. Does removing it turn a hard invariant into a manual discipline
   requirement where forgetting causes silent corruption or unrecoverable
   failure?

If the answer is yes to any question, the structure is load-bearing. If the
answer is no to all four, it is ceremony and can be cut.

This is structured judgment, not an automatic proof. Trace the actual
enforcement path before cutting; if the answer is unclear, keep the structure
and record the ambiguity for an ADR or follow-up review.

Worked example:

- `validate_request_schema`: removing it makes the external-input trust
  boundary unenforceable. Keep it.
- `log_outcome`: removing it makes failure and audit evidence unobservable.
  Keep it.
- `parse_request`: if it only delegates to `json.loads()` after schema
  validation already rejects malformed input, removing it does not weaken a
  trust boundary, observability, or rollback/evidence. Cut it.
- `with_transaction`: keep it if removing it would rely on every caller to
  remember `BEGIN` / `COMMIT` / `ROLLBACK` manually; the wrapper enforces the
  invariant that all writes are transactional.

## Never Cut

Do not remove:

- Trust-boundary validation.
- Error handling that prevents data loss or silent failure.
- Security, authorization, privacy, accessibility, or audit controls.
- Observability, structured logs, metrics, alerts, runbooks, rollback, or
  migration safety.
- Live validation, evidence-backed done gates, or required tests.
- Explicit user requirements.

## Shortcut Rule

If a shortcut has a known ceiling, document:

- Ceiling: the limit or tradeoff.
- Trigger: the signal that says the limit is real.
- Upgrade path: the next implementation when the trigger fires.

Example:

```python
# ceiling: global lock; trigger: p95 latency or contention exceeds target;
# upgrade: per-account locks after measurement
with global_lock:
    update_balance(...)
```

## Output Format

One line per finding:

```text
path:line: tag: what to cut. Replacement: <nothing|stdlib|native|installed dependency|smaller form>.
```

Tags:

- `delete:` dead code, unused flexibility, speculative feature.
- `stdlib:` hand-rolled behavior already in stdlib.
- `native:` code or dependency that the platform already provides.
- `yagni:` abstraction with one implementation, one caller, or no measured need.
- `shrink:` same behavior in fewer lines without weakening controls.
- `ceiling:` shortcut lacks ceiling, trigger, or upgrade path.

End with:

```text
net: -<lines> lines, -<deps> deps possible; controls preserved: <yes/no>.
```
