# Native executable selection extension v0.1

This post-baseline extension adds optional controller-only native executable
selection to the frozen MVP v0.5.4 contract. It does not qualify new CLI versions,
change model identities or authentication contexts, or alter workflow bounds.

## Configuration

Version-one configuration MAY contain `native_executables`, a mapping from
`codex`, `claude-code`, or `grok-build` to role/path mappings. Roles are `driver`,
`reviewer`, `participant`, and `moderator`; only Codex may specify `driver`.
Every configured value MUST be a nonempty absolute path for the controller's
operating system, at most 4096 Unicode scalars, without surrounding whitespace,
control characters, arguments, shell expansion, or a command prefix. A complete
`${NAME}` scalar supports the existing configuration environment-expansion rules.
No relative-path resolution against a task repository is permitted.

```yaml
native_executables:
  codex:
    driver: ${CODEX_DRIVER_EXECUTABLE}
    reviewer: ${CODEX_REVIEWER_EXECUTABLE}
```

An omitted runtime/role uses the existing PATH lookup. An explicit invalid,
missing, or unsupported executable MUST fail closed without trying PATH or
another role's executable. Paths must name native CLI programs; this extension
does not introduce interpreter arguments, WSL launchers, remote execution,
wrappers that change permissions, or new authentication mechanisms. Existing
Windows shim restrictions still apply.

All targets of a given runtime and execution role use that role's selected
executable. `@driver` continues to inherit the driver's model, runtime, effort,
and authentication context; its fresh review session uses the **reviewer**
executable selection and packet-only permissions. Council participants and
moderators have separate selections. No target inherits another role's path.

## Controller and evidence

Selection belongs to the trusted user's workflow configuration, not an
`AgentTarget` or model-authored request. This extends MVP section 5.2's exclusion
of executable selectors at ingress solely for these validated configuration
values; service method arguments remain unchanged. Paths MUST NOT be added to
model-facing packets, prompts, targets, lenses, or provider environment variables.
They are retained in private normalized configuration for auditability. Absent
selections MUST preserve existing normalized configuration serialization.

`dial doctor` and workflow execution MUST use the same adapter construction and
selection. Resolution happens before the normal native preflight, and the exact
resolved executable remains bound to its existing identity, content hash,
version, role/access-mode fixture, capability attestation, and per-turn drift
checks. Selecting another binary cannot reuse an incompatible attestation or
bypass an unqualified-version rejection. The controller MUST NOT modify PATH,
the CLI installation, saved credentials, or user configuration to select a role.

Both desktop and browser frontends expose optional CLI paths by runtime and role;
empty UI fields omit the override. The desktop retains these local settings across
launches. Each frontend submits the same validated configuration as the CLI.

## Verification

Supplemental tests cover accepted paths and environment expansion; malformed and
unknown selectors; omitted-field compatibility; role separation including
`@driver` and Council; identical doctor/run selection; version rejection; distinct
identity-bound attestations; no PATH fallback; exact argv path handling; and UI
submission/persistence. Existing MVP test IDs and their count remain unchanged.
Full offline and integration suites are required. Fake native transports prove
routing and gate preservation, not live qualification of a selected CLI build.
