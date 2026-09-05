# Codex CLI 0.153.4 — native Windows qualification

Date: 2026-09-05. Repository baseline: `325f5a2`.

## Scope and installation

The previous Council run failed when the provider rejected `gpt-6-astra` from
Codex `0.151.0-alpha.7.1` with HTTP 400 and a newer-client requirement.

The official Windows x64 standalone package for `0.153.4` was installed alongside
the retained older releases. No authentication files were copied or replaced.

- Package: `codex-package-x86_64-pc-windows-msvc.tar.gz`.
- Package SHA-256: `a6ef3442cb12766a88b39311d79244289e4f9763e2c53ff4fbebc2cb653cc5f3`.
- Executable SHA-256: `444a3f0008050605cae73cd9b7a2dcac61294062dfaab56dd20430fd6498518b`.
- Version output: `codex-cli 0.153.4`.
- Installation: `C:\Users\user\.codex\packages\standalone\releases\0.153.4-x86_64-pc-windows-msvc`.
- Official release notes: <https://learn.chatgpt.com/docs/changelog>.

The package digest was checked against OpenAI's release metadata before
extraction. The package manifest declares Windows x64, layout version 1, and
`bin/codex.exe` as its entry point.

After the Council smoke succeeded, the installer-owned `current` junction was
atomically retargeted with OpenAI's installer helper under the installer lock.
Both prior releases were retained. The normal PATH-selected `codex --version`
now reports `codex-cli 0.153.4`. The desktop import check passed. An already-open
Dialectic window must be restarted to load the updated Python adapter; its
unsaved in-memory drafts were not discarded by forcibly closing it.

## Native permission evidence

Windows reviewer, participant, and moderator roles retain the existing v3
packet-only profiles. No permission, authentication, configuration-isolation,
web-research, or cleanup boundary was weakened. The executable/version change
produces separate capability-cache identities. Other platforms remain
ineligible for this version pending their own qualification.

Private evidence is retained under
`C:\Users\user\AppData\Local\dialectic\qualifications\`:

| Evidence directory | Result |
| --- | --- |
| `codex-0.153.4-packet-web-20260905T092926Z` | Astra authenticated, completed a structured response, and used native web retrieval. The model declined some file probes, so this alone was insufficient enforcement evidence. |
| `codex-0.153.4-packet-offline-20260905T093049Z` | Astra completed the offline structured probe; file-denial coverage required supplemental evidence. |
| `codex-0.153.4-packet-web-20260905T093420Z` | Sol's native `exec` commands actually attempted file reads and writes and received Windows access-denied errors. Shell HTTPS failed; provider-native web retrieval succeeded. All six fixture checks passed and the process unit closed. |
| `codex-0.153.4-packet-offline-20260905T093741Z` | Sol's native `exec` read and corrected file-creation commands received access-denied errors; shell HTTPS failed. All five fixture checks passed and the process unit closed. |
| `codex-0.153.4-driver-20260905T093141Z` | Driver qualification failed: required product writes, temporary writes, and read-only Git inspection were unavailable. This version remains rejected for the writable driver before model invocation. |

The offline Sol probe first encountered a structured sandbox-provisioning failure
at `20260905T093636Z` and correctly stopped during policy inspection. After the
shared Windows sandbox recorded successful provisioning refreshes, a separate
qualification attempt completed without changing or bypassing the policy check.

A supplemental `codex sandbox` debug-command check at `20260905T093300Z` allowed
the sentinel read and denied writes/network. Its help identifies a restricted-token
execution path; it is not the production elevated `codex exec` path and is not
counted as qualification evidence for that path. The subsequent actual `exec`
commands above establish the required read denial.

The initial candidate harness overrode only version eligibility for the exact
candidate binary. It retained real CLI/authentication checks, managed-policy
inspection, native capability probes, attestation validation, and bounded process
cleanup. The full Council verification uses the production adapter with no
candidate-eligibility override or recorded probe provider.

## Verification

- Focused version/role/fixture tests: 5 passed.
- Integration suite: 8 passed, 223 deselected.
- Full offline suite after initial adapter changes: 218 passed, 13 skipped.
- End-to-end Council: `20260905T094057Z-m6co6koo3u`, finalized `UNANIMOUS` at
  `2026-09-05T09:45:32Z` after approximately 275 seconds.

The Council used Sol, Opus, and Astra as participants and Terra as fresh moderator,
with live web enabled. All ten native turns returned valid typed artifacts: three
openings, three cross-examinations, one fresh moderation, and three ballots. Each
Codex participant retained its exact session across turns; all ten process units
were closed before finalization. Every Codex target preflight recorded `0.153.4`.

The smoke used a short architecture question, rather than replaying the user's
original substantive prompt. Codex participant effort was low; moderator effort
was high. No recorded probe provider, provider retry, extra review round, or
candidate eligibility override was used in this workflow.

Retained Council artifacts:
`C:\Users\user\AppData\Local\Packages\OpenAI.Codex_2p2nqsd0c76g0\LocalCache\Local\dialectic\runs\20260905T094057Z-m6co6koo3u`.

Final offline verification after the corrected driver diagnostic and concurrent
diagnostic changes in the shared checkout: **228 passed, 14 skipped**. Those
unrelated changes are preserved. `compileall`, desktop `--check`, and
`git diff --check` also passed.

After activation, the production `dial doctor --mode council` command resolved
the normal PATH-selected executable to this exact `0.153.4` release and returned
`healthy: true`, with Sol, Opus, Astra, and Terra all ready and no diagnostics.

## Exact failed-prompt replay in the native app

A fresh Dialectic desktop window replayed the original failed run's prompt and
settings through the normal Run Council Once button. The retained prompt was
byte-for-byte identical, and the full normalized configuration matched the failed
run, including default participant effort, high moderator effort, live web,
fresh moderation, zero allowed dissenters, and the original time limits.

Run `20260905T102755Z-izoi74wclf` finalized `UNANIMOUS` at
`2026-09-05T10:33:33.569092Z`, after approximately 338 seconds. The native UI
displayed `UNANIMOUS · FINALIZED` and ten responses. All ten attempts ended as
`response-returned`, with no failure kind and closed process units. Each
participant retained its session across all three turns. All Codex target
preflights recorded the installed `0.153.4` executable and its verified digest.
The prior newer-client rejection did not recur.

The shared checkout's read-only retained-run auditor checked 75 files, seven
events, and ten attempts, reporting `valid: true`, `complete: true`, and no issues.
The four unresolved items in the Council answer are planning questions, not
execution failures. A fresh focused desktop/adapter test run passed all 11 tests.

Private replay artifacts:
`C:\Users\user\AppData\Local\dialectic\runs\20260905T102755Z-izoi74wclf`.
The original app window and its in-memory drafts were preserved; the successful
replay was left visible in the fresh window.

Native Code Once acceptance and Linux/WSL qualification are not claimed.
