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

## Driver follow-up: isolated path and temporary-directory diagnostics

Two further native diagnostics on 2026-09-05 used the same verified `0.153.4`
executable, Sol (`gpt-5.6-sol`, low effort), real authentication, real policy
inspection, and the production bounded process transport. Each used a new
disposable linked Git worktree under `C:\git\DialecticDriverQualification`.
No production version eligibility, permission profile, or installed CLI was changed.
The diagnostic harness bypassed candidate version eligibility only, and always
stopped without producing a passing capability attestation.

The controller supplied one explicit diagnostic command per turn, preserving
working directories, operation-specific errors, and Git exit codes. The native
runner's working-directory handling was checked using both PowerShell's location
and the process working directory. Product outputs were independently checked on
disk. Private evidence remains outside Git, under
`C:\Users\user\AppData\Local\dialectic\qualifications`:

| Evidence directory | Result |
| --- | --- |
| `codex-0.153.4-driver-diagnostic-20260905T131117566731Z` | Existing profile on an ordinary Git path: both working-directory observations matched the linked worktree; absolute and relative product writes succeeded. Scratch `tmp` writes raised `System.UnauthorizedAccessException`. Both `git status` and `git cat-file -t HEAD` exited 128 with `fatal: not a git repository: (NULL)`. |
| `codex-0.153.4-driver-diagnostic-20260905T131246993194Z` | Candidate profile removed only `:tmpdir = "deny"`; all exact path, credential, and network rules were retained. The Windows elevated runner rejected the child launch with `UnsupportedOperation`: `windows elevated sandbox cannot reopen writable descendants under read-only carveouts directly; refusing to run unsandboxed`. |

The second error was verified in the actual native tool result, rather than only
the model's final summary: session `01a071b3-516c-7480-978b-8ae851cbcaaf`,
`response_item` ordinal 14 at `2026-09-05T13:13:47.941Z`. Both outer CLI invocations
exited 0 and their controller-owned process units closed; that does not make the
rejected model-generated child launch successful.

This narrows the earlier failure finding: product writes and the worktree CWD can
work on an ordinary path. The original qualification beneath the denied Windows
OS-temp tree did not establish a general product-write failure.

The temporary-directory alias was a concrete conflicting-rule candidate:
Dialectic redirects `TMPDIR` to the authorized scratch `tmp` directory, while
the profile denies `:tmpdir`. OpenAI's documented precedence resolves equally
specific deny/write rules to deny. Removing that alias exposes a separate native
limitation: reopening a writable child under the read-only scratch root is not
supported by this elevated runner. This is not resolved by allowing the version.
The Git failure remains separately unresolved. See
[OpenAI's permission semantics](https://learn.chatgpt.com/docs/permissions).

Production driver qualification diagnostics now distinguish `allowed`, `denied`,
and `unavailable` operations; skipped operations and command-launch failures do
not become successful denial evidence. They preserve bounded, redacted errors
and the observed CWD, probe saved-auth and state access independently, use a
separate pre-redirect OS-temp sentinel, and verify sentinel identities as well as
contents. Driver probe fixture-test version
`slice-2-native-driver-diagnostics-v2` invalidates cached evidence from the older
probe. Packet-role fixtures and permission profiles remain unchanged.

The production Windows driver block remains. Making the scratch root broadly
writable would violate the required control/tmp split. LIVE-CODE-001/002 and a
full Code Once workflow were not run after this prerequisite failed. The invoking
user environment also has no `OPENAI_API_KEY`; the current LIVE-CODE-001 test
requires that independent environment-authentication evidence. Existing ChatGPT
authentication successfully authenticated these two diagnostics.

Follow-up verification on the shared checkout:

- Focused driver diagnostic and version rejection tests: **6 passed**.
- Full offline suite: **248 passed, 14 skipped** in 266.80 seconds.
- Integration suite: **8 passed, 254 deselected**.
- Desktop `--check` and `git diff --check` passed.

The suite includes concurrent, separately authored desktop UI changes preserved
in this checkout. No commit, push, or production driver eligibility change was
made by this investigation.

## WSL2 Astra driver follow-up, 2026-09-05

**Result: still unqualified.** With candidate Linux permission corrections, a
real Astra turn wrote product and scratch files and inspected the linked Git
worktree. It failed the required automatic repository `AGENTS.md` discovery.
No production version eligibility or permission profile was changed, and the
TaskPad Code Once demonstration was not started.

The host was Ubuntu 26.04 LTS on WSL2, kernel
`6.18.33.2-microsoft-standard-WSL2`, with Python 3.14.4, Git 2.53.0, and
distribution Bubblewrap 0.11.1. The existing Linux CLI was `0.151.0`, and the
existing Linux virtual environment imported Dialectic from `/mnt/c/git/dialectic`.
This attempt used a fresh source snapshot and disposable linked worktrees on the
Linux filesystem, with the controller importing the snapshot explicitly. Its
source base was `5d8c205`, including the preceding uncommitted driver diagnostics.

The official `0.153.4` Linux package was installed alongside the existing CLI:

- Package: `codex-package-x86_64-unknown-linux-musl.tar.gz`.
- Package SHA-256: `a822187e1a2420c61c5926721bfbd878701ed95547c9bb0d4de4498a16ba1821`.
- Binary SHA-256: `56ef98ab4032d317ab26e9b5e5a175650717351edb16ed9cde0cb6d1734d62da`.
- Requested model: `gpt-6-astra`, low effort.

The package digest matched the
[official release metadata](https://releases.openai.com/codex/releases/0.153.4/release.json).
The existing default CLI was not retargeted. The user completed a fresh device
login after the old saved login failed with expired access and refresh tokens.
Saved login status alone had not proved usable authentication.

Private evidence and reproducible diagnostic scripts are retained outside Git at:

`/home/user/.local/share/dialectic/qualification-workspaces/20260905T140238Z`

The qualification harness admitted this exact candidate binary only for
diagnostics and always stopped without a passing capability attestation. Linux
`doctor` also omits `denied-read restrictions` and `sandbox backend`, which the
current adapter requires. The harness recorded those omissions as unverified
evidence; it did not synthesize enforcement values or change production checks.

| Evidence under the private root | Observation |
| --- | --- |
| `probe-20260905T141148098773Z` | Authenticated Astra with the original profile could not start a child: Bubblewrap could not re-execute the pinned helper hidden by the root-deny policy. |
| `offline-20260905T141415Z` | Native offline sandbox replay with the exact helper readable: product writes worked; removing the redirected `:tmpdir` deny restored scratch writes; inheriting root-deny instead of explicitly masking the original directory restored the narrower Git read mount. The controller's original-repository sentinel remained hidden. |
| `instructions-20260905T141624Z` | `codex debug prompt-input` omitted a unique `AGENTS.md` canary and correctly omitted a conflicting project `.codex/config.toml` instruction. |
| `probe-20260905T141749283228Z` | Authenticated Astra with the candidate corrections returned product-write, scratch-write, and Git-read success; both Git commands exited 0. `agents_md_marker` and `project_codex_marker` were both `NOT_SEEN`. The controller independently verified the output files and the committed AGENTS canary's presence. |

The final candidate retained default root-deny, minimal runtime reads, saved-auth
denial, pre-redirect OS-temp denial, network restriction, no approvals, and the
protected scratch root/control with writable `tmp` child. It removed the
redirected temporary alias and redundant original/state ancestor-deny entries,
relying on root-deny for those contents, and allowed read access to the exact
pinned sandbox executable. These are experimental profile changes, not a fully
validated production fixture: the complete permission, hardlink, credential,
tool-surface, cancellation, and workflow gates were not run after the instruction
gate failed. In particular, the original-sentinel observation does not establish
all other isolation boundaries.

The final native session was `01a071ee-d418-7e80-bb69-ca59edc5c69a`. Both
authenticated diagnostics exited 0 with controller-confirmed process cleanup;
no qualification executable remained running at final inspection. The two turns
reported 57,611 input tokens (27,520 cached) and 1,872 output tokens; no monetary
cost was inferred. The earlier authentication failure is recorded separately and
has no completed-turn usage. Files later created in its disposable worktree by
offline replays are not attributed to that failed model invocation.

The previous WSL failure was also recovered from native session
`01a0584d-3012-7cf3-aab5-322229d7d358` on 2026-08-31: its `0.151.0` probe ran
under `/tmp` and Bubblewrap could not create the original-repository mount beneath
a read-only ancestor. The new attempt got beyond those path problems, but
independently reproduced the instruction-discovery blocker on `0.153.4`.

The WSL follow-up changes repository documentation only. Its native diagnostics,
offline sandbox replays, retained-output checks, and `git diff --check` were run;
the Python suites were not rerun for these documentation edits. The earlier
Windows diagnostic suite results above remain separately scoped to that work.

## Instruction-discovery root cause, 2026-09-05

**Result: the WSL2 instruction blocker is Dialectic's own driver override, and the
same defect is present, previously unmeasured, on native Windows.** No production
version eligibility, permission profile, or installed CLI was changed, and no
model turn, network call, or authentication was used.

Codex `0.153.4` loads repository `AGENTS.md` *through its filesystem sandbox*. The
native Windows failure text names the coupling directly:

`Failed to initialize session: failed to load AGENTS.md instructions for
environment 'local': failed to prepare fs sandbox: ...`

So the driver permission profile decides both whether instruction discovery is
preserved and whether a session starts at all.

### Method

Each probe built a disposable original repository, a linked worktree holding a
unique `AGENTS.md` canary and a project `.codex/config.toml`, and a scratch tree,
then ran `codex debug prompt-input` with override sets derived from the real
`_fixture` driver template through `_codex_overrides`. The bisect runs both
removal variants (candidate minus one override group) and additive variants (one
override group alone), so a single suppressing group is identifiable even when
another group fails independently.

### The suppressing override

`projects.<isolated_worktree>.trust_level = "untrusted"` is the only override
group that suppresses `AGENTS.md`. On WSL2 the complete candidate omits the
canary while every single-group variant except `only-projects` includes it, and
removing only `projects` restores it. Native Windows reproduces the same
`only-projects` suppression.

This is the blocker the earlier WSL2 attempt recorded as a Codex or Bubblewrap
limitation. It is neither: it is a configuration choice in Dialectic's own driver
template, and it behaves identically on both platforms.

### Native Windows additionally cannot build the sandbox at all

Without the trust override, the Windows session fails to initialize, because the
`AGENTS.md` read needs a sandbox that neither backend can express:

| Backend | Refusal |
| --- | --- |
| Elevated (`windows.sandbox = "elevated"`) | `windows elevated sandbox cannot reopen writable descendants under read-only carveouts directly; refusing to run unsandboxed` |
| Unelevated restricted token (no `windows` key) | `windows unelevated restricted-token sandbox cannot enforce split filesystem read restrictions directly; refusing to run unsandboxed` |

WSL2 does not share this: with the corrected candidate profile alone, Bubblewrap
builds the sandbox and the canary is discovered.

### The carveout refusal is the required split, not the temporary alias

Varying only the scratch entries on native Windows separates the two:

| Scratch layout | Wrapper outcome |
| --- | --- |
| Root `read`, `tmp` `write` | Carveout refusal |
| Root `deny`, `tmp` `write` | Carveout refusal |
| Root `deny`, control `deny`, `tmp` `write` | Carveout refusal |
| `tmp` `read` | Policy accepted; failed later launching the setup helper |
| Scratch entries absent | Policy accepted; failed later launching the setup helper |
| Root `write` | Policy accepted; failed later launching the setup helper |

The refusal tracks exactly one property: a writable descendant beneath a
non-writable scratch carveout — that is, the required `control/` versus `tmp/`
split itself. This narrows the earlier reading that removing `:tmpdir = "deny"`
*caused* the refusal. These probes did not redirect `TMPDIR`, so `:tmpdir`
resolved to the OS temporary directory and the scratch `tmp` write rule stood on
its own. In the earlier driver diagnostic `TMPDIR` was redirected into the
scratch `tmp` directory, so the equally specific deny won and `tmp` was not
actually writable; the wrapper built only because the required split had already
been lost, which is what that run's `System.UnauthorizedAccessException` on
scratch `tmp` writes recorded.

### What the trust override actually gates

`projects.<path>` carries exactly one field. `trust_level` accepts only
`trusted` or `untrusted` — an invalid value reports `expected 'trusted' or
'untrusted'` — and the only other project-document keys in this release,
`project_doc_max_bytes` and `project_doc_fallback_filenames`, control size and
fallback names rather than trust. There is no narrower knob.

Planting a distinct canary in every project-scoped surface the binary names, then
comparing both trust settings, shows what the override is worth:

| Worktree surface | Reaches the model with the override | Reaches the model without it |
| --- | --- | --- |
| `AGENTS.md` | no | **yes** |
| `.codex/skills/<name>/SKILL.md` | **yes** | **yes** |
| `.agents/skills/<name>/SKILL.md` | **yes** | **yes** |
| `.codex-plugin/plugin.json` | **yes** | **yes** |
| `.codex/config.toml` | no | no |
| `.codex/agents/<name>.md` | no | no |
| `.codex/hooks.json`, `.codex/hooks/hooks.json` | not observable | not observable |

A worktree `.codex/config.toml` setting `model` also never reaches effective
configuration in `codex doctor --json` under either setting, while the same value
passed as `-c model=` does. The override therefore excludes nothing that is not
already excluded, and costs `AGENTS.md`.

### Worktree-authored skills reach the driver and cannot be blocked

The skills entry is the serious one. The rendered skill-roots table resolves the
injected root to the worktree itself — `r0 = <worktree>/.codex/skills` — and the
canary appears in the developer message as
`<plugin-name>:<skill-name>: <description>`. A repository being driven can
therefore place instructions in front of the only writable role, which is exactly
what spec 5.4.4's untrusted marking exists to prevent.

Nothing available stops it. Seven candidate configuration keys
(`skills.enabled`, `skills_config.enabled`, `skip_host_skill_discovery`,
`skills.skip_host_skill_discovery`, `features.skills`, `plugins.enabled`, and the
`--disable skills` feature flag, which reports `Unknown feature flag: skills`)
left the canary in place or were rejected outright. Denying
`<worktree>/.codex`, `<worktree>/.agents`, and `<worktree>/.codex-plugin` in the
permission profile also left it in place, so skill discovery — unlike the
`AGENTS.md` read — does not pass through the filesystem sandbox.

Two limits on this result. Hooks were not observable through `prompt-input` or
`doctor`, so the hook surface is unmeasured. And `prompt-input` accepts neither
`--ignore-user-config` nor `--ignore-rules`; those flags are documented as
covering `$CODEX_HOME/config.toml` and execpolicy `.rules` respectively, and the
probes already ran with an empty `CODEX_HOME`, so neither plausibly suppresses
project skill discovery — but that was not measured on the `exec` path, which
needs a model turn.

### The specification clause stands; the CLI cannot meet it

Spec 5.4.4 requires the driver adapter to "mark the worktree untrusted for
project `.codex/` configuration/hooks/rules" *and* to "preserve ordinary
`AGENTS.md` repository-instruction discovery". In Codex `0.153.4` the first
clause is not achievable at all for the skills and plugin part of that surface,
by any mechanism found here, and the second is achievable only without the trust
override. The clause is not what is wrong; this release cannot satisfy it.

No spec amendment and no profile change follow from this. Dropping the trust
override would restore `AGENTS.md` without making the driver qualifiable, so it
belongs with a profile that can pass the whole matrix, not as a standalone edit.
Production already fails closed on both platforms: `0.153.4` is added to the
supported set only when `os.name == "nt"`, so Linux and WSL2 reject it for every
role, and native Windows rejects driver-write before any model invocation. Packet
roles are unaffected — they run in a private neutral directory rather than a
repository worktree, so no project skill surface exists for them.

### Unresolved

The three layouts whose policy the elevated wrapper accepted could not be carried
further: launching `codex-resources\codex-windows-sandbox-setup.exe` failed with
`ShellExecuteExW failed to launch setup helper: 1223` and a modal
"specified module could not be found" dialog when the standalone release binary
is invoked directly with an isolated `CODEX_HOME`. The installer-managed copy
under `AppData\Local\OpenAI\Codex\bin` stages and runs that helper normally, so
this is probably a probe-environment limit rather than a policy result. Whether a
non-nested scratch layout would satisfy the elevated wrapper end to end is
therefore still unknown, and no such layout is proposed: the nested split is a
deliberate security boundary.

The production Windows driver block stands, and its preflight message remains
accurate. LIVE-CODE-001/002 and Code Once were not run.

Private evidence, outside Git:

| Evidence directory | Contents |
| --- | --- |
| `C:\Users\user\AppData\Local\dialectic\qualifications\codex-0.153.4-instruction-discovery-20260905T224400Z` | Eight probe scripts with their observations: the Windows and WSL2 override bisects, the Windows sandbox-shape and project-configuration-scope runs, and the WSL2 project-trust-surface, skill-roots, skill-disable-knob, and permission-denial runs. |

Disposable Windows probe workspaces remain under
`C:\git\DialecticDriverQualification`. The global Codex sandbox ACL state was not
modified: every Windows wrapper attempt stopped before ACL application. This
follow-up changes repository documentation only; no Python source changed, so the
suites were not rerun.
