# Spec — Gen AI Hub coding-assistant gateway

## Goal

Route local AI coding tools through SAP Generative AI Hub so that developer AI usage
runs on the governed path (SAP enterprise model contracts, no training on customer
data, resource-group scoping, auditable spend) rather than on public model endpoints
with individual developer accounts.

Secondary goal: per-developer attribution, budget caps, and instant revocation —
which is what makes this defensible in a client governance review.

## Non-goals

- **Inline code completion.** Copilot completions run on GitHub infrastructure and
  cannot be redirected by BYOK. Continue.dev is the only client with a separate
  autocomplete slot, and it needs a FIM-capable model — check what's actually
  deployed in the resource group before promising this to anyone.
- Replacing Joule / SAP Build Code. Those are SAP's sanctioned ABAP-side assistants;
  this is for general development work.
- Multi-tenant SaaS. Single team, single resource group.

---

## Phase 1 — localhost PoC  ← current

**Purpose: answer the tool-calling question before spending money on infrastructure.**

### Deliverables

1. LiteLLM running locally against the client's AI Core orchestration deployment.
   — **done**, litellm 1.95.0 pinned by digest.
2. `scripts/smoke_test.py` passing — specifically the tool-calling and streaming cases.
   — **done, all five checks pass (2026-08-06).**
3. One coding client (recommend **Cline** — fewest moving parts) driving a real task
   end to end. — **done.** Cline ran an 8-turn tool loop and wrote a file, then ran
   again on `claude-sonnet-governed`; Continue drives chat and edit on a scoped key.

### Steps

1. Obtain an AI Core service key (Extended plan) for the target resource group and put
   it in `.env`. There is **no** orchestration deployment ID to collect — the provider
   discovers it. See D8. Mind the `$`-escaping trap in `.env.example`; it fails as a
   confusing 401. See D9.
2. `docker compose up`. Postgres is included even though the PoC could run without it
   — running the real configuration from the start avoids a rewrite, and virtual keys
   don't persist without it.
3. `python scripts/smoke_test.py`. Four checks:
   - basic completion
   - streaming (SSE)
   - **tool calling** — the one that matters
   - developer-vocabulary content-filter false positives
4. Point a client at `http://localhost:4000/v1` with the master key.

### Exit criteria

Tool calling works through orchestration, streaming does not truncate, and no
false-positive filter blocks on ordinary code. If tool calling fails, stop and
reconsider direct deployment access — record the decision in `docs/decisions.md`.

**Met on 2026-08-06** for `gpt-4o` through orchestration deployment `da15f1f9fac8a4c1`
in resource group `default`. Tool calling works both directions (call emitted, tool
result accepted back); streaming returns complete output; no dev-vocabulary blocks
even at the strictest threshold. The direct-access fallback stays commented out.

Verified on two models: `gpt-4o` and `anthropic--claude-4.5-sonnet`.
`supports_function_calling` is asserted per alias in the config, so each new alias
re-opens the question — the tenant also exposes Gemini and Mistral families, untested
here.

### Phase 1 is complete

All three deliverables are met. **Three** clients drive the gateway end to end — Cline
(on both `gpt-4o-governed` and `claude-sonnet-governed`), Continue, and Claude Code
(the latter two on scoped virtual keys) — and per-request cost is recorded in capacity
units (D11), which makes the Phase 2 chargeback model function rather than compare
against zero.

The two governance claims that were previously only configured are now demonstrated:
cost metering (D11) and virtual-key scoping, attribution, budgets and revocation (D5).

**Attribute client traffic with `call_type`, not prompt size.** `anthropic_messages`
is the Claude Code path; `acompletion` is the OpenAI path Cline and Continue use. A
long Cline turn is indistinguishable from a Claude Code turn by token count alone —
that confusion produced a wrong "Claude Code verified" claim in these docs once.

**Running the real client found what probes could not.** Synthetic probes reproduced
Claude Code's request *shape* — system prompt, tools, streaming — and all passed, but
missed its parameter *dialect*: it sends `thinking`, which SAP orchestration rejects
(D12). Endpoint verified is not the same as client verified.

### Client setup

| Client | Config | Status |
|---|---|---|
| Cline | OpenAI Compatible → base `http://localhost:4000/v1`, key, model `gpt-4o-governed`. UI-only (settings live in VS Code's `state.vscdb`). | **driven** — 8-turn tool loop |
| Claude Code | `scripts\claude-governed.ps1` — sets `ANTHROPIC_BASE_URL`, `ANTHROPIC_AUTH_TOKEN`, `ANTHROPIC_MODEL`, `ANTHROPIC_DEFAULT_HAIKU_MODEL` for one shell only | **driven** — on a scoped key, via `/v1/messages` |
| Continue | `~/.continue/config.yaml`: `provider: openai`, `apiBase`, `apiKey`, roles `[chat, edit, apply]`. File-based, so scriptable. | **driven** — on a scoped virtual key |
| Copilot | VS Code 1.122+, `Chat: Manage Language Models` → Custom Endpoint. Chat/agent only — not completions. Business/Enterprise needs the org BYOK policy enabled. | **out of scope** — see below |

**Set `ANTHROPIC_DEFAULT_HAIKU_MODEL` for Claude Code.** It runs background work
(titles, summaries) on a separate small model; without the alias those calls request a
model the gateway does not expose and fail quietly while the main model looks fine.

**Continue's autocomplete stays on local Ollama.** No FIM-capable model is deployed in
the resource group, so the gateway serves chat/edit/apply only — inline completion is
the honest exception to "all developer AI traffic is governed".

**Copilot is out of scope (decided 2026-08-06).** Not required, so it was dropped
rather than left as an open item. It is not a technical gap: Copilot BYOK sends the
same OpenAI-compatible `/chat/completions` with tools and streaming that Cline and
Continue already drive, and `GET /v1/models` returns the fields its picker needs. If it
is ever wanted, the work is client-side only — install, licence, and the org BYOK
policy, which on a Business or Enterprise account is an admin toggle no local config
can work around. Copilot still appears in D1 and D6 as one of the clients whose
constraints shaped the design; that reasoning stands.

---

## Phase 2 — shared deployment (not yet)

Only once Phase 1 proves the pattern and more than one person wants it.

### Decisions still open

**Where to host is undecided, and BTP is not the only candidate** (noted 2026-08-06 —
Azure is in the running). Worth being explicit about what that does and does not change:

The gateway is a **stateless container plus Postgres** that holds an AI Core service
key and talks outbound HTTPS to one endpoint. Nothing in Phase 1 depends on BTP. It
runs anywhere that can host a container, hold a secret, and reach
`api.ai.prod.eu-central-1.aws.ml.hana.ondemand.com`.

| Stays true wherever it lands | Changes with the host |
|---|---|
| The governed path — AI Core is still the model backend | Service-key delivery: BTP Operator / CF binding vs Key Vault, Secrets Manager, … |
| The whole config: aliases, filtering, cost map, `drop_params` | Ingress and network policy (D6's `noAuth` + VPN-range argument) |
| Virtual keys, budgets, spend ledger (D5, D11) | Whether the AI Core service key leaves the SAP landscape — a governance question, not a technical one |
| The `$$`-escaping, digest pinning and patch traps (D9, D10) | Who pays, and who runs it |

Hosting outside BTP does **not** mean traffic leaves the governed path — AI Core is
still the backend and the enterprise contract still applies. It does mean the service
key is stored outside SAP's landscape, which is the part a reviewer will ask about.

- **Whose subaccount / tenancy.** Running dev tooling in the client's landscape means
  their capacity units, their security review, and an awkward conversation at contract
  end. A Flexso-operated gateway with per-client virtual keys and chargeback via the
  spend ledger is the cleaner commercial shape. Decide before building.
- **Azure (or any container host).** Container Apps or AKS, secret in Key Vault, private
  networking. Attractive if the team already operates Azure or the gateway should not
  live in the client's subaccount. Loses BTP-native service binding — the service key
  becomes something you rotate yourself. Note the Kyma/CF trade-off below applies only
  if you stay on BTP.
- **Kyma vs Cloud Foundry.** Kyma gives BTP Operator service bindings, HPA, and
  Istio-level network policy. But a Kyma runtime is a minimum three-node cluster
  consuming CUs continuously — heavy for one I/O-bound container. If Kyma isn't
  already running for other reasons, `cf push --docker-image` is cheaper and simpler
  and still gets native service binding.

### Known traps for Phase 2

Recording these now so they aren't rediscovered:

- **Ingress auth is a genuine conflict.** The "correct" answer (Kyma APIRule `jwt`
  handler / IAS) breaks every client, because none can run an OAuth2 flow — they can
  only send a static bearer. So the gateway must be `noAuth` with LiteLLM virtual keys
  *as* the authentication, which means a public endpoint fronting a live AI Core
  service key. Mitigate with an Istio `AuthorizationPolicy` restricted to corporate
  VPN egress ranges, or mTLS if the client demands it.
- **Streaming timeouts.** Istio's default route timeout severs long agent streams
  mid-response and surfaces as a generic client error. Set APIRule `timeout: 3600`
  (max 3900).
- **Istio sidecar required.** APIRule v2 validates that the workload is in the mesh;
  label the namespace `istio-injection=enabled`.
- **Postgres is not optional** once shared. The spend ledger *is* the audit story;
  it cannot live in memory or on an unbacked PVC.
- **APIRule v1beta1 is gone.** Use `gateway.kyma-project.io/v2`, handlers `noAuth` /
  `jwt` / `extAuth`.

### Observability

Route LiteLLM's OTEL callback to the SAP Cloud Logging instance rather than standing
up separate tooling. Per-request spend, model, latency, and virtual-key identity land
in OpenSearch; the existing DLS-by-tenant pattern scopes it per client or team.

---

## Governance note

The governed path answers *which model and under what contract*. It does not answer
*whether this source code belongs in a prompt at all*. That's a separate policy call
per engagement and should be written down alongside the technical control — a reviewer
will ask, and "it goes through AI Core" is not an answer to it.
