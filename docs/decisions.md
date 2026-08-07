# Decisions

Short ADRs. The value here is the **rejected** options — a fresh agent will otherwise
propose them again, and several look more correct than what we chose.

---

## D1 — LiteLLM as gateway, not direct client integration

**Status:** accepted

AI Core cannot be consumed directly by coding clients. Three blockers: OAuth2
client_credentials instead of a static API key, a mandatory `AI-Resource-Group`
header with nowhere to put it, and deployment-id in the URL path rather than a model
name in the body. No client solves all three.

LiteLLM has a first-class SAP Gen AI Hub provider (`sap/*`) that handles the OAuth2
service-key exchange, and SAP maintains a contributions repo of examples — so this
isn't a fringe integration.

**Rejected:** writing a bespoke FastAPI shim. Works, but then we own token refresh,
streaming, tool-call translation, retries, and spend accounting. LiteLLM gives all of
that plus virtual keys.

---

## D2 — Orchestration service, not direct deployment access

**Status:** accepted — smoke test passed 2026-08-06, reversal condition not met

Orchestration gives one governed ingress, model swap without client changes, and
output content filtering. Direct `/v2/inference/deployments/{id}/chat/completions` is
simpler and has a better-known request shape.

**Rejected:** direct access — bypasses output filtering, which is most of the
governance story.

**Reversal condition:** if `scripts/smoke_test.py` shows tool calling does not survive
orchestration, direct access becomes the only viable path for agentic clients. Record
it here if that happens.

**Outcome (2026-08-06, gpt-4o via orchestration, litellm 1.95.0):** all five smoke
checks pass. Tool calling survives — the model emitted `read_file({'path':
'src/config.py'})` and accepted the tool result back on the next turn. Streaming does
not truncate. **The reversal condition was not met; the fallback stays commented out.**

Two things checked while here, because "the smoke test passed" would otherwise hide
them:

- **Output filtering costs no streaming granularity.** Same prompt streamed straight
  at the deployment with and without the output filter gave 3 SSE events either way.
  The coarse chunking is orchestration's default `chunk_size`, not the filter — so
  the governance story in this ADR is not being paid for in responsiveness.
- **Chunking is tunable.** `stream_options.chunk_size` maps to `config.stream.chunk_size`.
  `chunk_size: 10` turned 3 events into 20, but tripled wall-clock (1.2s → 3.3s).
  Left at the default; raise it only if a client's typing feel actually matters more
  than latency.

---

## D3 — Masking module OFF

**Status:** accepted

This is the decision most likely to be reversed by someone optimising for a governance
checkbox, so the reasoning matters.

Pseudonymization rewrites prompt content and reverses it on the way out. That's correct
behaviour for a form letter. It is actively harmful when the model is expected to
reproduce code verbatim: any email or person name in a comment, test fixture, or config
becomes an opaque token the model then edits around, and the returned diff no longer
applies cleanly.

**Rejected:** masking on. Gives a checkbox and a broken tool. The underlying concern —
client source code in prompts — is a policy and repo-scoping question, not something
masking solves.

---

## D4 — Content filter: permissive input, meaningful output

**Status:** accepted

Azure Content Safety categories fire on ordinary developer vocabulary. Violence and
SelfHarm in particular trip on: kill, abort, terminate, execute, child process, attack
surface, orphan, deadlock, hang, crash, exploit, sanitize.

In a chat UI a blocked request is a bad moment. In an agent loop it's a hard mid-task
failure with an error that doesn't point at the cause — which is worse, because it
burns an afternoon.

Input thresholds at the most permissive tier (6). Output filtering stays meaningful.

**Status of the evidence (measured 2026-08-06, gpt-4o via orchestration).** The
decision stands, but the stated premise did not reproduce and the next person should
know that rather than rediscover it:

| Prompt | threshold 0 (strictest) | threshold 6 |
|---|---|---|
| kill a hung child process / force terminate thread pool | allowed | allowed |
| abort a transaction, clean up orphan records after a crash | allowed | allowed |
| injection attack surface, unsafe exec calls | allowed | allowed |
| daemon deadlock, parent dies before reaping children | allowed | allowed |
| *control:* unambiguous real-world violence | **blocked** (Filtering Module) | blocked (LLM Module) |

The control is what makes the four negatives trustworthy: at threshold 0 it is
rejected by the **Filtering Module**, so the filter is demonstrably live and wired
correctly. At threshold 6 the same prompt is rejected by the **LLM Module** instead —
the Azure filter genuinely passed it and the model itself refused. So threshold 6 is
really ALLOW_ALL, and the module is really enforced.

**Conclusion:** on this model, the four canonical developer prompts do not trip the
filter even at the strictest setting. Keep the permissive input thresholds anyway —
they cost nothing, the sample is four prompts on one model, and the failure mode being
guarded against (a hard mid-agent-loop block with a misleading error) is expensive
enough to stay insured against. But do not cite "kill/abort/terminate get blocked" as
established fact; it was not observed here.

---

## D5 — Virtual keys and budgets in LiteLLM, not orchestration

**Status:** accepted — **demonstrated 2026-08-06**, not just configured

Orchestration's contribution is the governed ingress, model abstraction, and output
filtering. It does **not** give per-developer identity, spend caps, or revocation.
Those live in LiteLLM virtual keys and are what a reviewer actually asks about: who
used what, how much, and can we cut it off.

Consequence: Postgres is required for any shared deployment. In-memory loses the
ledger on restart.

**Verified with a real client.** Continue runs on a scoped key rather than the master
key, because its config is plaintext YAML in the user's home directory — handing that
file the credential fronting a live AI Core service key would be the D6 blast-radius
problem in miniature.

```
POST /key/generate  alias=continue-dev
  models: [claude-sonnet-governed, gpt-4o-governed]   max_budget: 5 CU / 30d
```

All four properties a reviewer asks about hold:

| Question | Evidence |
|---|---|
| Who used it? | every ledger row stamped `user_api_key_alias = continue-dev` |
| How much? | key spend rises per request; budget caps at 5 CU |
| Only what they should? | `claude-sonnet-governed` → 200, `claude-haiku-governed` → **403** (outside its allow-list), master key → 200 on the same model |
| Can we cut it off? | revoke by alias via `/key/delete` |

Two details worth keeping:

- **Denied requests are logged too** — the 403 appears in the ledger with `in=0 out=0`,
  attributed to the key. Rejections are auditable, not silently dropped.
- **Scoping bounds a leak; it does not prevent one.** The key still sits in plaintext in
  `~/.continue/config.yaml`. That is an accepted trade for a localhost PoC, and it is
  the same argument as D6: bound the blast radius when you cannot remove the exposure.

---

## D6 — Ingress `noAuth` + virtual keys, not JWT

**Status:** accepted for Phase 2, deferred

The secure-looking choice (Kyma APIRule `jwt` handler validating IAS tokens) is
unusable: Copilot, Cline, Claude Code and Continue can only send a static
`Authorization: Bearer <string>`. None can perform an OAuth2 flow first.

So LiteLLM's virtual keys must serve as the authentication layer, and the gateway is
`noAuth`. That means a public endpoint fronting a live AI Core service key — a leaked
developer key burns tenant capacity units until noticed.

**Compensating controls:** Istio `AuthorizationPolicy` limited to corporate VPN egress
ranges; per-developer keys with budget caps so blast radius is bounded; spend
monitoring via the OTEL callback.

**Rejected:** mTLS per developer. Defensible, but per-developer cert provisioning is
real friction and probably the point at which to question whether this belongs in the
client's subaccount at all.

---

## D7 — `drop_params: false`

**Status:** accepted

LiteLLM can silently drop params a backend doesn't support. For tool calling that
converts a hard error into a model that just quietly stops using tools — which reads
as "the model is bad at this" and sends you debugging the wrong thing.

---

## D12 — Drop `thinking` by name, never `drop_params: true`

**Status:** accepted — this is the exception D7 did not anticipate

Claude Code sends `thinking` on every request. SAP orchestration has no such
parameter, so with `drop_params: false` (D7) every call 400s before reaching the model:

```
litellm.UnsupportedParamsError: sap does not support parameters: ['thinking'],
for model=anthropic--claude-4.5-sonnet
```

**LiteLLM's own error message suggests the wrong fix.** It recommends
`drop_params: true`, which would make `tools[]` droppable too — the exact failure D7
exists to prevent: tool calling degrades into a model that mysteriously stops using
tools, and you debug the model instead of the config.

**Accepted:** `additional_drop_params: ["thinking"]` on the Claude aliases. Drops that
one parameter by name and leaves `drop_params: false` in force for everything else.

Verified surgical, not blanket:

| Request | Result |
|---|---|
| `thinking` | 200 — dropped |
| `thinking` + `tools` + `stream` | clean `message_stop`, tool call intact |
| `audio` (unsupported, *not* listed) | **400** — still fails loudly |

That third row is the test that matters. Re-run it after touching this setting: if an
unlisted unsupported param starts returning 200, the drop has gone blanket and D7 is
silently broken again.

**What is actually lost.** Nothing that was available. Extended thinking is not exposed
by the orchestration API at all, so the choice is dropping the parameter or failing the
request. Claude Code works without it; it simply does not get extended thinking on this
path. If SAP later exposes thinking through orchestration, remove the drop and re-test.

**Generalisation:** each client sends its own parameter dialect. The next unsupported
param from a new client should be added here **by name** after checking the provider's
`get_supported_openai_params()`, not fixed by widening the drop.

---

## D8 — The orchestration deployment is discovered, not configured

**Status:** forced by the provider, recorded because it surprises people

`config/litellm.config.yaml` originally carried `model: sap/orchestration` plus
`sap_orchestration_deployment_id`, `sap_resource_group` and `llm_model_name`, all
marked `# VERIFY`. **None of those parameters exist.** Verified against
`litellm/llms/sap/chat/transformation.py` inside the pinned image, not the docs page —
https://docs.litellm.ai/docs/providers/sap does not mention orchestration at all.

What is actually true in litellm 1.95.0:

- `sap/<model>` **is** the orchestration path. `get_complete_url()` ignores `api_base`
  entirely and always posts to `<deploymentUrl>/v2/completion`. There is no
  non-orchestration mode to opt out of, and no `sap/orchestration` model string.
- The deployment is found at runtime: list `/lm/deployments`, keep those with
  `scenarioId == "orchestration"` whose configuration has
  `executableId == "orchestration"`, sort by `createdAt`, **take the newest**.
- Resource group comes from `AICORE_RESOURCE_GROUP` / the service key, not a param.
- Orchestration modules are top-level `litellm_params` named for the module:
  `grounding`, `masking`, `filtering`, `translation`. Filter thresholds are
  **snake_case** (`self_harm`, not `SelfHarm`) and take `0|2|4|6`, where `6` is
  ALLOW_ALL and `0` is strictest — the inverse of what "6 = permissive" suggests if
  you assume higher means stricter.

**Consequence worth watching:** because the newest orchestration deployment wins and
cannot be pinned, standing up a second orchestration deployment in the resource group
silently redirects all gateway traffic to it. Nothing in this repo can prevent that.
If that becomes a real risk, the mitigation is a dedicated resource group for the
gateway, not a config change.

Current tenant: orchestration deployment `da15f1f9fac8a4c1`, resource group `default`.
The other resource group, `bas-dev-agent`, has **no** orchestration deployment — only
direct foundation-model deployments — so pointing the gateway at it would fail.

---

## D9 — `$` in the service key must be escaped in `.env`

**Status:** accepted, learned the hard way

docker compose expands `$VAR` inside `env_file` values. The AI Core client secret
contains literal `$`. The result is not an error: compose substitutes an empty string,
the container receives a **silently truncated** secret (81 chars → 72 here), and the
only symptom is a 401 from the token endpoint that looks like bad credentials.

`.env` therefore stores `$` as `$$`, which compose converts back to a single `$`.
`.env.example` documents it. Anything reading `.env` **host-side** must undo the
escape; the smoke test does not need to, because only `AICORE_SERVICE_KEY` contains a
`$` and the host-side variables (`LITELLM_MASTER_KEY`, `LITELLM_BASE_URL`,
`SMOKE_MODEL`) do not.

**Verify after any change to `.env`** by comparing a hash of the secret inside the
container against the source, not by checking that the proxy starts — it starts fine
with a broken key and fails only on the first real request.

Worth knowing alongside D7: `filtering` is not an OpenAI param, yet `drop_params: false`
does not reject it. Absence of an error is therefore **not** evidence a module is
applied. Confirmed it reaches the wire by transforming a request and inspecting the
payload for `config.modules.filtering`.

---

## D10 — Startup patch for the stream+tools serialization bug

**Status:** accepted as a workaround, with a removal condition

**Symptom.** Any request that streams *and* passes `tools` terminated with an HTTP 500
embedded in the SSE body instead of a clean `[DONE]`:

```
TypeError: 'MockValSer' object is not an instance of 'SchemaSerializer'
  streaming_handler.py:1950 -> processed_chunk.model_dump()
```

This is the single most important failure mode for this project and the smoke test
did not catch it, because it tested streaming and tool calling **separately**. Every
agentic client — Cline, Copilot agent mode, Claude Code — sends them together. The
gateway looked healthy and was unusable for its actual purpose.

**Root cause.** litellm 1.95.0 emits `logprobs: {}` on tool-call chunks. That value is
an `openai.types.chat.chat_completion_chunk.ChoiceLogprobs`, whose pydantic class is
left with `__pydantic_complete__ = False` and a `MockValSer` placeholder instead of a
real `SchemaSerializer`. Serializing the chunk recurses into it and raises. Plain
streaming carries no `logprobs`, which is why only the combination failed.

**Fix.** `config/litellm_patches.py`, imported at startup via `litellm_settings.callbacks`,
force-rebuilds every mocked pydantic model under the `litellm.*` **and `openai.*`**
namespaces, and wraps `ModelResponseBase.model_dump` to repair and retry if a mock
reappears. 596 classes are rebuilt at boot.

**Three dead ends, recorded so nobody re-walks them:**

- *Rebuilding only the top-level class.* The mock is on a nested value; pydantic
  raises from inside the outer serializer, so the traceback names the outer model and
  points at the wrong class.
- *Sweeping only `litellm.*`.* The offending class lives in the `openai` package. The
  sweep reported "repaired=69 failed=0" and the bug persisted — a fix that looks like
  it worked is worse than one that visibly fails.
- *Downgrading pydantic.* litellm allows `>=2.10,<3`, but 2.11.9 fails identically
  (only the wording of the TypeError changes). This is a litellm bug, not a pydantic
  regression, so there is no version pairing that avoids it. The image also ships
  without pip or uv, so a downgrade needs a derived image for no benefit.

**Regression cover.** `scripts/smoke_test.py` now has a `streaming + tool calling`
check that reassembles the streamed `arguments` and asserts the stream reaches
`[DONE]`. Verified it actually fails when the patch is disabled — a regression test
never observed failing is not evidence of anything.

**Removal condition.** When bumping the pinned digest in `docker-compose.yml`: comment
out `callbacks`, restart, run the smoke test. If `streaming + tool calling` passes,
delete `config/litellm_patches.py` and its volume mount. If not, keep it.

---

## D11 — Spend is denominated in capacity units, sourced from SAP

**Status:** accepted

Every ledger row recorded `spend = 0.000000`: LiteLLM has no cost map for SAP model
names, so it counted tokens and priced them at nothing. Token attribution worked;
budget caps compared against a spend that never rose, and chargeback (D5, and the
Phase 2 commercial model in spec.md) had nothing to bill from.

**Where the rates come from.** SAP publishes per-model cost in the AI Core model
catalogue — `GET /v2/lm/scenarios/foundation-models/models`, under
`versions[].cost[]` as `inputCost` / `outputCost`. The unit is **capacity units per
1,000 tokens**, the same unit BTP bills in. LiteLLM wants per-token, so the values in
`model_info` are those figures divided by 1000.

This beats hand-entering contract rates: it is SAP's own number, it covers every model
in the tenant, and it can be re-pulled. `scripts/refresh_costs.py` prints a
paste-ready block and flags deprecated model versions.

**Rejected: converting to euros or dollars.** A CU→currency rate depends on the
customer's BTP contract, is not exposed by any API, and would silently drift. Capacity
units are the honest unit — they are what SAP actually charges, so a CU figure is
directly reconcilable against the BTP bill. A currency figure would be a guess wearing
a decimal point.

**Consequence — the number is labelled wrong.** LiteLLM's UI and API call this field
`spend` and render it as dollars. It is CU. Anyone reading the dashboard without
knowing that will misread the magnitude, so say so wherever the figure is surfaced.

**Two caveats:**

- **Rates drift and nothing warns you.** They change when SAP re-prices a model or
  when a model version rolls over. Re-run `refresh_costs.py` after any model change,
  and whenever a spend report looks off.
- **Historical rows stay at zero.** Cost is computed at request time and written to
  the row; adding rates does not backfill. The ledger therefore has a discontinuity at
  the point rates were added — 163 zero rows before, priced rows after.
