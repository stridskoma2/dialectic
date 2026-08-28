# Dialectic, Conversational Orchestration, and MCP

**Author:** Sol  
**Date:** 2026-08-28  
**Type:** Opinion / architecture note; non-normative  
**Written against:** [`DIALECTIC_MVP_IMPLEMENTATION_AND_TEST_SPEC_V0.3.md`](./DIALECTIC_MVP_IMPLEMENTATION_AND_TEST_SPEC_V0.3.md)

## Executive opinion

The quoted Claude-to-Grok setup has the right user experience and the wrong
authority model for Dialectic.

It is an excellent personal delegation pattern: talk to one assistant, let it call
another specialist when useful, and eliminate manual copy-paste. It is not, by
itself, an independent review or deliberation protocol. The host model decides
whether to ask for another opinion, how to frame the request, which parts of the
answer to retain, whether to retry, and how to interpret the result. That makes the
host both an advocate and the adjudicator.

My recommendation is:

> Use MCP as a narrow front door into Dialectic. Do not make an MCP host, a general
> shell tool, or any participant model the Dialectic controller.

The conversational host may request a run and present its result. Dialectic must
still own participant selection, packet construction, concurrency, session
continuation, schemas, timeouts, consensus, Git operations, and durable evidence.

This should not change the v0.1.0 MVP. MCP belongs after the CLI/controller flows are
proven, and asynchronous MCP launch should wait until execution ownership is
specified rather than being treated as a trivial wrapper.

## 1. What the quoted workflow gets right

The pattern solves a real problem:

- The user has one conversational front end.
- The host assistant can recognize when another model has a comparative advantage.
- The second model's response returns to the working context automatically.
- A small script can prototype the experience without building a full product.
- Live or provider-specific capabilities can be reached without the user manually
  moving prompts and answers between applications.

For informal research, brainstorming, and discretionary second opinions, this is
often the correct amount of engineering. The autonomy is a feature because the user
is buying convenience and breadth, not a reproducible adjudication record.

The most valuable idea for Dialectic is therefore the interface: the user should be
able to say “run a council on this” or “have Dialectic review this change” without
manually operating every model.

## 2. Why that workflow is not the Dialectic engine

Dialectic exists to make properties mandatory that ad-hoc delegation leaves to the
judgment of the orchestrating model.

### 2.1 Invocation is policy, not model preference

In the quoted pattern, Claude asks Grok “whenever it needs Grok's input.” For a
review gate, the driver must not decide whether its own work deserves review. A
configured reviewer either runs or the workflow records a failure. The same input
should not produce zero peer calls today and three selectively framed calls
tomorrow merely because the host model took a different path.

### 2.2 Framing can destroy independence

If the first model summarizes the material for the second model, the second model is
reviewing that summary. Dialectic v0.3 instead creates controller-owned, bounded
packets; sends the same immutable core to parallel reviewers; hides the driver's
transcript and self-assessment; and records packet hashes. In council mode it fans
out blind openings and controls what enters cross-examination.

The host can supply the user's original task or council question. Once the run
starts, it must not compose per-reviewer prompts, suppress a configured participant,
edit a ballot, or choose which findings reach the repair turn.

### 2.3 Prose is not a control contract

A shell script that captures an answer as text is sufficient for conversation. It
is not sufficient for a state machine. Dialectic needs schema-validated outputs,
explicit failure kinds, exact session identities, deterministic extraction, bounded
streams, and no silent format-repair loop.

### 2.4 General shell access is the wrong production boundary

Desktop shell access is powerful because it can do nearly anything. That is also
why it should not be the supported Dialectic boundary. Repository content and model
output are untrusted data; exposing a universal `run_shell` capability gives those
inputs a path toward arbitrary local actions.

For a trusted individual's prototype, a narrow script invoked through a desktop
shell tool is reasonable. For the product, the host should receive a few typed
Dialectic operations and no generic command execution through Dialectic.

### 2.5 The audit trail is part of the product

The useful question is not merely “what did Grok say?” It is:

- Which configured target answered?
- Which exact packet and repository SHA did it see?
- Did all required participants start concurrently?
- Which schema version validated the answer?
- What failed, timed out, or was excluded?
- Which findings reached the original driver session?
- How did the controller derive the final outcome?

The v0.3 run artifacts answer those questions. A chat transcript generally does
not.

## 3. The correct place for MCP

There are two different directions, and they should not be conflated.

| Direction | Meaning | Recommendation |
|---|---|---|
| **Northbound / inbound** | ChatGPT, Claude Desktop, Codex, an IDE, or another MCP host calls Dialectic | Yes, post-MVP, through a narrow server |
| **Southbound / outbound** | Dialectic gives its driver, reviewers, or council participants MCP tools | No for Code Once and Council Once; consider only a future controller-issued capability profile |

The target architecture is:

```text
Chat / coding assistant / IDE
             |
             | narrow typed MCP request
             v
      Dialectic MCP adapter
             |
             v
 deterministic Dialectic controller
        /          |          \
     Codex       Claude       Grok
       |             controller-owned evidence
       +------ Git, state, artifacts ------+
```

MCP is the transport and discovery layer at the top. It is not the orchestrator in
the middle and does not replace the provider adapters at the bottom.

The current OpenAI Responses API, for example, treats MCP servers and custom
function calls as tools available to a model. That is useful for making Dialectic
callable, but it does not supply Dialectic's quorum, packet-isolation, Git, lifecycle,
or evidence rules. The [Responses API reference](https://developers.openai.com/api/reference/cli/resources/responses/methods/create)
explicitly separates MCP tools from custom function tools, while the
[MCP guide](https://developers.openai.com/api/docs/guides/tools-connectors-mcp)
describes tool discovery, filtering, calls, and approval flows.

## 4. Authority boundary

| Actor | May do | Must not own |
|---|---|---|
| User | Authorize a run; choose a registered repository and profile; inspect evidence | Nothing is taken away from the user |
| Conversational host | Offer or request a run within its granted permissions; pass the bounded task; show status and result | Participant set, packet contents after ingress, adjudication, retries, Git, artifact deletion |
| Dialectic MCP adapter | Authenticate the caller; validate input; map registered IDs; enforce rate/size/approval policy; invoke the application service | Workflow semantics or provider-specific prompting |
| Dialectic controller | Own state transitions, packets, calls, barriers, timeouts, Git, schemas, outcomes, and artifacts | Provider credentials must still remain in adapters/native environments |
| Provider adapter | Translate a neutral `AgentRequest` into a supported CLI, ACP, or API call | Cross-participant policy or consensus |
| Participant model | Produce one role-bound, schema-conforming answer | Calling other participants, changing its permissions, or deciding the workflow |

The central invariant should be:

> A host may initiate the protocol; only the controller may execute and interpret
> the protocol.

## 5. Recommended MCP surface

Start coarse-grained and small.

| Operation | Inputs | Result | Initial authority |
|---|---|---|---|
| `dialectic_list_profiles` | none | Approved profile IDs and safe descriptions | Read-only |
| `dialectic_start_council_once` | `profile_id`, bounded `prompt`, `client_request_id` | Accepted run or synchronous result, depending on the lifecycle design | Model calls only; explicit user approval configurable |
| `dialectic_start_code_once` | `profile_id`, registered `repo_id`, bounded `task`, `client_request_id` | Accepted run or synchronous result | Sensitive; require explicit user/client authorization |
| `dialectic_get_run` | `run_id` | Status, phase, outcome/failure, timestamps | Read-only |
| `dialectic_get_result` | `run_id` | Bounded typed projection of `SummaryRecord` and approved artifact references | Read-only |

The first release should omit cancellation, cleanup, deletion, arbitrary artifact
paths, and arbitrary shell execution. Those can be added only when their approval
and evidence semantics are explicit.

### 5.1 Use registered IDs, not caller-controlled paths

`repo_id` should resolve through a controller-owned registry to a pre-approved
repository identity. A model-supplied `C:\...` path is a writable-driver escalation:
prompt injection or a mistaken inference could point Code Once at a repository the
user never intended to modify.

Likewise, `profile_id` should select a server-owned, user-approved configuration.
The MCP call should not accept:

- executable paths or arbitrary commands;
- arbitrary configuration-file paths;
- runtime/model overrides outside the selected profile;
- reviewer lists or per-reviewer prompts;
- lenses, packet fragments, ballots, or consensus overrides;
- credentials, authentication files, or environment variables;
- arbitrary filesystem paths for task, prompt, or artifact retrieval.

This is stricter than simply mirroring CLI arguments, and deliberately so. A human
typing a path in a terminal and a model supplying a path through a tool are different
trust boundaries.

### 5.2 Make duplicate starts idempotent

`client_request_id` should be unique within an authenticated client scope. Repeating
the same accepted request returns the same `run_id`; reusing the key with different
arguments fails. This prevents a host retry, network retry, or repeated tool call
from launching multiple expensive councils or multiple writable code runs.

### 5.3 Return bounded projections, not raw transcripts

Tool results should expose closed enums, concise summaries, and approved artifact
handles. They should not dump a large diff, native event stream, or complete model
transcript into the host's context.

All model-authored fields must be labeled as untrusted data. A reviewer may have
read adversarial text in a diff; its prose then enters the host model's context. The
host must not be invited to treat that prose as instructions or to execute commands
from it.

## 6. The asynchronous-run problem

The appealing MCP sketch is “start the run, return a `run_id` immediately, then
poll.” That is the right eventual user experience, but it is not free under v0.3.

The MVP explicitly excludes a background daemon and crash resumption. During a CLI
run, the foreground controller owns the subprocess trees and cancellation. If an MCP
`start` call returns while work continues, something else must durably own:

- the controller process and all descendants;
- client disconnect and MCP-server shutdown behavior;
- cancellation and overall wall-clock enforcement;
- the transition from accepted to running;
- duplicate launch prevention;
- crash detection and terminal-state repair;
- cleanup confirmation and final artifact persistence.

A long-lived local stdio MCP server could own the run, but then the desktop client's
lifecycle becomes part of Dialectic's correctness boundary. A remote HTTP service or
local job runner could own it more cleanly, but that is a daemon/service design. A
synchronous MCP call avoids the new owner but may exceed client or transport timeout
limits during a 20-minute Code Once run.

Therefore:

1. Do not promise asynchronous start in v0.1.0.
2. Before choosing synchronous or asynchronous MCP execution, test the intended
   clients' real call-duration, cancellation, and reconnect behavior.
3. If starts return immediately, specify a job owner and recovery semantics first;
   align that work with the post-MVP recovery increment rather than hiding a daemon
   inside the adapter.
4. A read-only MCP surface over existing run artifacts can be delivered earlier,
   but its value is mostly architectural validation.

This is the main reason I would not describe inbound MCP as merely “a thin facade”
yet. The data contracts are ready; execution ownership is not.

## 7. Security and approval policy

MCP makes prompt-injection and confused-deputy risks more important because a model
can cause actions rather than only produce prose. OpenAI's current MCP guidance
defaults remote MCP calls to approval before data is shared, recommends filtering
the exposed tool set, requires particular care for sensitive actions, and warns that
MCP outputs may themselves contain malicious instructions. See
[MCP risks and safety](https://developers.openai.com/api/docs/guides/tools-connectors-mcp#risks-and-safety).

Dialectic should enforce its own policy even when a host also has an approval UI:

- Separate read-only status/result permissions from council-start permission and
  writable Code Once permission.
- Require explicit user/client authorization for `start_code_once`; never trust an
  `authorized: true` boolean supplied by the calling model.
- Authenticate clients and log caller identity, request hash, selected profile,
  registered repository identity, approval decision, and resulting `run_id`.
- Keep provider secrets inside the controller/adapters. Never accept or return them
  through MCP.
- Apply prompt, result, artifact, call-rate, concurrent-run, and cost ceilings on
  the server, regardless of the host's schema validation.
- Treat tool descriptions as hints to a model, not as enforcement; validate every
  invariant server-side.
- Never pass the host's MCP connections or general tool context into participant
  sessions.
- Do not let a participant recursively invoke Dialectic.

For a remote server, transport authentication, TLS, tenant separation, retention,
and data-residency policy become additional product requirements. Local stdio avoids
some network exposure but still inherits the desktop client's process environment
and lifecycle, which must not widen v0.3's credential boundary.

## 8. Should participants ever consume MCP?

For the current Code Once and Council Once profiles: no. The v0.3 restrictions are
correct. Reviewers are packet-only, and the driver is a bounded repository worker;
giving them inherited personal MCP servers could silently add filesystem, network,
account, or recursive orchestration authority.

I would not make “participants never use MCP” a permanent protocol-level law,
however. MCP is only a transport. A future controller-issued capability could be
safe if it is:

- created for one run and one role;
- narrow, read-only where possible, and explicitly allowlisted;
- backed by controller-owned evidence capture;
- absent from all global/user MCP configuration;
- inaccessible to other runs and participants;
- bounded by schema, calls, bytes, time, domains, and cost;
- represented in the packet and artifact contracts;
- tested as part of that named workflow profile.

For example, the post-MVP read-only repository-exploration increment might use a
purpose-built capability server internally. The important property is that the
controller grants a declared capability; the reviewer does not inherit the user's
personal tool universe.

This is a modest disagreement with the strongest possible prohibition: outbound MCP
is wrong for the current modes, but the security property comes from authority and
evidence boundaries, not from banning one wire protocol forever.

## 9. Direct APIs, native CLIs, and ACP

These are transport choices below the controller, not replacements for Dialectic.

- **Native CLI** remains appropriate for the MVP, especially for the writable Codex
  driver, because it preserves the coding-agent tool loop and local repository
  workflow already specified in v0.3.
- **Direct provider APIs** may later simplify structured output, remote execution,
  and process-envelope parsing for packet-only reviewers or council participants.
  They also introduce separate billing, credential, retention, quota, and session
  contracts that must be explicit.
- **ACP or another persistent agent protocol** can improve structured session
  control where a provider supports it, but it remains a provider-adapter concern.
- **MCP northbound** makes Dialectic callable by interactive hosts.

For new OpenAI API work, use the Responses API rather than the deprecated Assistants
API. The current API reference marks Assistants as deprecated in favor of Responses:
[Assistants API reference](https://developers.openai.com/api/reference/typescript/resources/beta/subresources/assistants).

A small Python chain is still a good prototype. In fact, Dialectic is the disciplined
version of that idea: the script grows a neutral state machine, typed packets,
bounded adapters, deterministic outcomes, and evidence. n8n or Make can trigger the
system, but the workflow graph should not duplicate or replace the controller's
semantics.

## 10. Grok and live X/web research

The quoted workflow specifically values Grok for real-time X data. Dialectic v0.3
does not provide that use case: Grok runs with web search, tools, planning, memory,
and subagents disabled. That is a deliberate and correct property of the current
review and council profiles.

If live research becomes a product goal, add a separate, explicit workflow/profile
rather than a hidden `enable_web` switch on Council Once. A credible `research_live`
profile would need:

- a declared network-enabled adapter and source policy;
- retrieval timestamps and source provenance in the evidence packet;
- a distinction between retrieved claims and model interpretation;
- domain/tool allowlists and prompt-injection defenses;
- independent byte, call, cost, timeout, and retention limits;
- an explicit user-visible indication that external data is being sent and fetched;
- no silent fallback from offline council to live research.

MCP could expose `dialectic_start_research` after that workflow exists. It should not
smuggle live tools into the existing council participants.

## 11. Recommended sequence

1. Finish and verify the v0.1.0 CLI/controller exactly as scoped.
2. Preserve a clean application-service boundary so CLI and future MCP entry points
   invoke the same state machines.
3. Define registered repository/profile identities, caller permissions,
   idempotency, and bounded result projections.
4. Add read-only `list_profiles`, `get_run`, and `get_result` operations as a small
   protocol/security proof.
5. Decide and test synchronous versus asynchronously owned execution with the real
   target clients.
6. Add Council Once launch before Code Once launch; it has no writable repository
   target and is the safer action surface.
7. Add guarded Code Once launch only with registered repositories and explicit
   authorization.
8. Add remote HTTP exposure only after authentication, tenant, logging, retention,
   and deployment requirements are specified.
9. Treat live research and participant capabilities as separate named workflows,
   not conveniences attached to the existing modes.

## Final position

The quoted setup is a strong prototype of how Dialectic should feel, not how it
should decide.

The user should be able to remain in one conversation while the host invokes
Dialectic. From that moment onward, however, the host becomes a caller and renderer,
not the supervisor. The neutral Python controller remains the authority; every
required participant sees the controller-defined evidence; every transition and
failure is recorded; and no model can quietly skip, reframe, repeat, or suppress the
independent process.

So my answer is a qualified yes:

> **Yes to MCP as a constrained, model-neutral front door. No to general shell
> access or chat-led delegation as the review engine. No MVP scope expansion. Build
> the execution-lifecycle contract before asynchronous MCP launch.**
