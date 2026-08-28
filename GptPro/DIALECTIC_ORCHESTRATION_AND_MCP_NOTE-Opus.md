# Note: Ad-hoc Cross-Model Orchestration, and MCP for Dialectic

**Author:** Opus

**Date:** 2026-08-28

**Type:** Opinion / design note. Not a spec review, not normative. Written against
`DIALECTIC_MVP_IMPLEMENTATION_AND_TEST_SPEC_V0.3.md`.

**Prompt:** a description of a working daily setup — Claude Desktop plus an MCP
shell tool, calling a script that opens a Grok session and returns its answer, with
Claude deciding autonomously when to invoke it — and the question of whether that
pattern, and MCP generally, has a place in Dialectic.

---

## 1. Summary position

The ad-hoc pattern is a good tool badly suited to Dialectic's job. Its headline
property — *the orchestrating model decides when and what to ask the other model* —
is precisely the property Dialectic exists to remove. For research, second opinions,
and pulling live data a model lacks, it is the right amount of machinery. For review
and adjudication it destroys the thing being purchased.

MCP itself is a different question from the pattern, and the two get conflated. My
position:

- **Dialectic consuming MCP** (agents inside a run getting MCP servers) must stay
  closed, permanently, not just for the MVP.
- **Dialectic exposed over MCP** (a run triggerable from Claude Desktop or any MCP
  client) is worth doing after v0.1.0, is cheap because v0.3's contracts already
  provide the substrate, and needs exactly one design rule plus one new security
  constraint to avoid recreating the ad-hoc pattern's failure.
- Nothing about this should change the MVP. One sentence in section 2.2 or 14, and
  one constraint written down before it is forgotten.

---

## 2. On the ad-hoc pattern

### 2.1 What it gets right

It is honest about what it is, the ergonomics are genuinely good, and the escalation
ladder it lays out (script to n8n to MCP to function calling) is accurate. "I just
talk to Claude and it pulls in what it needs" is a real experience improvement, and
Dialectic currently has nothing like it — that is the part worth taking seriously
and returning to in section 3.

For the stated use cases it is also simply correct. Research, a second opinion, and
real-time X data that one model has and another does not are all tasks where the
orchestrating model's judgment about *when* to reach out is the useful part, and
where nobody is going to audit the decision six months later. Dialectic applied to
that would be absurd overkill.

### 2.2 Where it fails for adjudication

**Curation destroys independence.** This is the whole objection, and everything else
is a detail. If Claude decides what to send Grok, Grok is evaluating Claude's
framing of the artifact, not the artifact. The output looks like a second opinion
and is functionally an echo with provenance laundering — worse than no second
opinion, because it carries unearned confidence.

Independence is not a nice property of a review; it *is* the product. Dialectic
spends most of its complexity buying it: CODE-05 makes the packet core byte-
identical across reviewers and records both the common-core hash and each per-
reviewer packet hash; reviewers never receive the driver's transcript or self-
assessment; COUNCIL-02 fans out blind; COUNCIL-03 anonymizes the ledger and tells
each participant only its own alias. Every one of those mechanisms exists to prevent
one model from shaping what another model sees. The ad-hoc pattern makes that
shaping the central feature.

**Non-deterministic invocation.** "Whenever it needs Grok's input" means two
identical sessions can call Grok zero times, once, or four times, with different
prompts each time. Dialectic's CODE-019 and COUNCIL-014 exact-call-count guards
exist because a variable number of variable calls is not a process you can reason
about, reproduce, or defend.

**No schema validation.** The script captures a response as text and Claude reads
it. Dialectic's rule (section 2.1) is that every model-facing output used for a
control decision validates against a controller-owned schema, with section 5.4.6's
deterministic extraction and no format-repair retry. The difference matters exactly
when the response is malformed or evasive, which is when a second opinion is most
load-bearing.

**Isolation inverted.** Desktop Commander is full shell access — the precise inverse
of section 5.4.4's packet-only profile, where reviewers get a neutral CWD, no tools,
no MCP, and never learn the repository path. And a shell script that pipes
model-adjacent content into a shell session runs against section 10's flat rule:
model output is data and must never be executed as a command. In practice the risk
is modest for a solo user on a trusted machine; as a pattern to build a review
system on, it is the wrong default.

**No audit trail.** No `run.json`, no retained prompts and responses, no diff hash,
no alias map. Fine for a conversation. Useless for answering "which model said this,
about which SHA, having seen exactly what."

### 2.3 Two small factual corrections

- A Custom GPT cannot call Claude's API directly. GPT Actions require a hosted
  HTTPS endpoint with an OpenAPI schema, so that route means standing up a proxy
  service. The Assistants-API path mentioned second is the one that works without
  hosting anything.
- "Opens a Grok session, sends a prompt, captures the response" is only durable if
  it drives the documented headless interface. If it automates a chat UI it will
  break on the next interface change. Section 15 of the spec already points at
  `docs.x.ai/build/cli/headless-scripting` for this reason, and section 11.6's
  version-labeled fixture requirement exists so that exactly this kind of drift
  produces an explicit failure rather than silent misparsing.

### 2.4 The honest summary

These are not competing designs. The ad-hoc setup is an *interface*; Dialectic is an
*engine*. Using the ad-hoc setup as an engine is the thing Dialectic exists to
replace. Using Dialectic without any interface like it is the thing Dialectic is
currently missing. That leads directly to the next section.

---

## 3. MCP and Dialectic

### 3.1 Separate the two directions

They are routinely conflated and have opposite answers.

| Direction | Meaning | Position |
|---|---|---|
| **Inbound** | Dialectic runs an MCP server; Claude Desktop or another client triggers `code`/`council` runs | Worth doing post-MVP, with constraints |
| **Outbound** | Agents inside a Dialectic run are given MCP servers | Must stay closed permanently |

### 3.2 Outbound must stay closed

Section 5.4.4 already forbids it: packet-only Claude roles use `--safe-mode` with an
empty tool set and no MCP configuration; Grok ACP roles advertise no filesystem,
terminal, or MCP client capabilities; the Codex driver disables MCP, apps, web
search, and subagents.

This should be stated as a durable design commitment rather than an MVP simplifying
choice, because the failure is subtle and someone will eventually propose relaxing
it as a convenience. A single globally configured filesystem MCP server turns a
diff-only reviewer into a repository-reading one, which silently voids the isolation
that CODE-05 and CODE-006 assert. The reviewer would still return a valid report;
nothing would fail; the packet-equality tests would still pass. The property lost is
invisible to the test suite, which is exactly why it needs to be a stated commitment
rather than an implementation detail.

Post-MVP item 6 ("give reviewers read-only repository exploration in isolated
sandboxes") is the sanctioned path to more reviewer context, and it is the right
one: the *controller* grants a bounded capability, rather than the reviewer
inheriting whatever the user's global config happens to contain.

### 3.3 Inbound is worth doing, and v0.3 already built the substrate

The case for it is section 2.4: Dialectic's ergonomics are a CLI invocation, and the
ad-hoc pattern's ergonomics are conversation. Exposing runs over MCP gets the second
without giving up the first — the user talks to Claude Desktop, Claude triggers a
run, and what actually executes is a deterministic, schema-validated, audited
workflow rather than an improvised one.

What makes this cheap is that v0.3's contracts are already the right shape:

- Stable run IDs with a validated grammar (section 6), so a run is addressable.
- `dial status <run-id>` returning 0 for non-terminal runs (section 6.3), so
  progress is pollable without inventing anything.
- `RunStatus` / `CodeOutcome` / `ConsensusOutcome` / `FailureKind` as closed enums,
  so a client can branch on results without parsing prose.
- `SummaryRecord` with `artifact_paths`, so a caller can report a result compactly
  and point a human at the evidence.
- A per-repository lock producing `REPOSITORY_BUSY` (section 2.1), so concurrent
  tool calls — which MCP clients absolutely will make — already fail cleanly rather
  than corrupting each other.

That last one is worth noting: concurrency is usually the hard part of putting a
long-running workflow behind a tool interface, and it is already solved.

### 3.4 The one design rule

**The MCP tool surface exposes the CLI's arguments, not the orchestrator's
internals.**

Any parameter that lets the calling model shape what a reviewer or participant sees
reintroduces section 2.2's curation problem, wearing a schema. Concretely, the tool
may accept a configuration file path, a repository path, and a task or prompt file
path. It must **not** accept a reviewer list, model selectors, lenses, per-reviewer
prompts, a diff, or any override of the packet.

The configuration is a user-authored file on disk. The calling model *names* it; it
never *composes* it. That single rule is what separates "Claude triggered an
independent review" from "Claude arranged a review it designed," and the whole value
of the exercise sits on the difference.

### 3.5 The new security constraint

There is one escalation that does not exist today and appears the moment there is an
inbound surface: **the caller chooses `--repo`.**

Today a human types the repository path. Over MCP, a model supplies it — and that
path becomes the target of a Codex driver running with `workspace-write`. A model
persuaded by content it read somewhere (a task file, a web page, a repository's own
README) could point a writable driver at a repository the user never intended.
Dialectic's existing protections do not cover this, because they all assume the
repository selection is a trusted human decision.

The mitigation is small and should be written down now, while it is obvious:
Dialectic's own configuration carries an allowlist of permitted repository roots,
and an inbound run whose `--repo` does not resolve — by the same stable filesystem
identity that CODE-01 step 4 already computes — under an allowlisted root fails as
`INVALID_INPUT` before preflight. The identity machinery for this already exists;
only the allowlist and the check are new.

Two smaller ones in the same family:

- **Environment inheritance.** An MCP server spawned by a desktop client inherits
  that client's environment. Section 5.4.3 builds native CLI environments from an
  OS-minimal baseline plus fixture-declared names, so the boundary holds — but the
  baseline is computed from Dialectic's own environment, which is now a client's
  environment rather than a user's shell. Worth an explicit statement that the
  inbound server does not widen the credential surface.
- **Returned content is untrusted.** This is the one I would most want written down.
  A reviewer's findings and a council's minority reports are *model-authored text*,
  produced by a model that read a diff which may itself contain adversarial content
  from the repository. CODE-08 already acknowledges that authored prose passes
  through unchanged ("Model-authored finding text is preserved"). Over MCP, that
  text lands in an orchestrating agent's context, where prose is instructions. The
  inbound server should return summaries framed explicitly as untrusted data, and
  should return bounded content rather than raw transcripts.

### 3.6 A concrete sketch

Not a proposal to build now — a shape to avoid foreclosing:

| Tool | Input | Returns |
|---|---|---|
| `dialectic_code_start` | `config_path`, `repo_path`, `task_file_path` | `run_id`, initial `RunStatus`, run-directory path |
| `dialectic_council_start` | `config_path`, `prompt_file_path` | `run_id`, initial `RunStatus` |
| `dialectic_status` | `run_id` | `RunStatus`, `RunPhase`, outcome or failure kind |
| `dialectic_result` | `run_id` | Bounded `SummaryRecord` projection plus `artifact_paths` |

Notes on the shape:

- Start returns immediately. A 20-minute `code_run_seconds` cannot live inside a
  synchronous tool call, and making it asynchronous also keeps the calling model
  from holding a run open while it does something else.
- `dialectic_result` returns the summary, never `initial.diff` or a transcript. A
  256 KiB diff does not belong in a chat context, and the human should be reading
  artifacts rather than a model's retelling of them.
- No tool cancels, deletes, or cleans up. Section 10's no-automatic-cleanup rule
  exists so that evidence survives, and a model-triggered delete is the wrong
  capability to hand out.
- Exit codes map to tool errors via section 6.3's existing table, so failures stay
  machine-distinguishable.

### 3.7 What MCP is not

MCP is a tool protocol, not a model-invocation protocol; it is not an alternative to
the native CLI adapters. The nearest analogue in the current design is Grok's ACP
stdio transport (section 5.4.2), which is already the "speak a protocol instead of
parsing an envelope" adapter. If Codex or Claude expose a comparable structured
protocol later, that is an adapter change, not an MCP question.

---

## 4. Recommendation

Nothing to build for v0.1.0. Two small documentation actions, both cheap now and
annoying later:

1. **Section 2.2 or 14.** State that an inbound MCP server is a post-MVP interface —
   alongside the already-deferred TUI and ACP server — and that the run-ID, status,
   and summary contracts are deliberately sufficient to add one without changing the
   engine. This is already true; saying it protects it from being accidentally
   broken by a later refactor.
2. **Section 10.** Record the two durable commitments while the reasoning is fresh:
   agents inside a run never receive MCP servers or other user-configured tool
   surfaces, and any future inbound interface constrains repository selection to a
   configured allowlist checked by stable filesystem identity, because a caller-
   chosen `--repo` is a writable-driver escalation.

Then leave it. The design is right; it just should not be re-derived from scratch in
six months, and the repository-allowlist point in particular is the kind of thing
that is obvious while writing an MCP server and invisible while reviewing one.
