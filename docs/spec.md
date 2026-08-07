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

## Phase 1 — localhost PoC  ✅ complete (2026-08-06)

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

## Phase 2 — identity and multi-user, still on localhost  ← current

**Purpose: make the gateway survive more than one user before it survives a network.**

Phase 1 issued keys by hand with the master key. That does not scale past two people,
and the deploy questions (IAS redirect URIs, key TTLs, per-user budgets) are cheaper to
get wrong locally than on a route. Everything here runs against
`http://localhost:4000` — deliberately, so a failure costs a `docker compose restart`
and nothing else.

### Steps

1. **Wire IAS SSO on the admin UI** — `docs/identity.md`, D13.
   - Register the OIDC app with `http://localhost:4000/sso/callback`. If tenant policy
     refuses a non-HTTPS redirect URI, tunnel with ngrok on a static domain and set
     `PROXY_BASE_URL` to the tunnel — details and the exposure caveat in
     `docs/identity.md`. The tunnel comes down when the test is done; it is not a
     standing setup, because it puts a live AI Core key on a public route.
   - Inspect the claims at `/sso/debug/login` **before** wiring, then set
     `GENERIC_USER_ID_ATTRIBUTE=sub`.
   - Auto-provision defaults must grant all three aliases, or Claude Code is broken on
     first login.
2. **Prove the multi-user story end to end.** Log in as a second identity, self-serve a
   key, confirm the spend rows carry that user, confirm budgets cap and revocation
   bites. This is D5's evidence table repeated per *user* rather than per *key*.
3. **Set key TTLs.** `duration` on every key. IAS deprovisioning blocks new logins but
   does not revoke a live key (D13) — expiry is what makes offboarding automatic.
4. **Distribute the client configs.** `config/continue.config.yaml` (key and URL from
   env vars, no secrets in the file) and `docs/claude-code.md`. Second developer
   onboards from the docs alone — if they can't, the docs are the deliverable that
   failed, not them.
5. **Re-run the smoke test per alias** and drive one real Claude Code session on a
   self-served key, not the master key.

### Exit criteria

Two distinct IAS identities, each self-serving a scoped and budgeted key, each showing
up separately in the spend ledger, with a real client driving a real task on each.
No master key outside the operator's shell.

---

## Phase 3 — Cloud Foundry

**Decided 2026-08-07: Cloud Foundry, Python buildpack.** Rationale, trade-offs and the
rejected alternatives (Kyma, Azure, `--docker-image`) are in D16. Artifacts and the
push procedure are in `deploy/cf/`.

### Sequence

1. `cf create-service` for `aicore` and `postgresql-db`.
2. Assemble the push directory — including `litellm_patches.py`, the easiest thing to
   forget and the one whose absence fails silently (D10, D16).
3. `cf set-env` for `LITELLM_MASTER_KEY` and the IAS `GENERIC_*` vars; update the IAS
   redirect URI to the route.
4. `cf push -t 180` — first boot runs Prisma migrations.
5. Verify in the order below, then re-run Phase 2's exit criteria against the route.

### Verify empirically, in priority order

- **Streaming through the Gorouter.** Unknown timeout, and possible response buffering
  that would deliver a stream as one late lump. The one open risk that could make CF
  unsuitable — test a five-minute stream before migrating anything else.
- **Prisma on a buildpack container.** It fetches its own engine binaries at first
  boot; untested here. First place to look if the initial push hangs.
- **Postgres URI rewrite** — `postgres://` → `postgresql://` plus `sslmode=require`.
  Handled in `start.sh`, silent if bypassed.
- **A real Claude Code session**, not just the smoke test. Different endpoint,
  different translation path (D12).

### Accepted gaps

- **No route-level IP allowlist.** D6's compensating control was an Istio
  `AuthorizationPolicy` scoped to VPN egress; CF has no equivalent, so virtual keys are
  the only gate on a public HTTPS route. Compensate with short TTLs and budget caps so
  a leaked key has bounded blast radius. Revisit if a security review pushes back.
- **Attribution lives only in our Postgres.** AI Core sees one technical user (D14), so
  a client auditing their subaccount sees one anonymous consumer. If this runs in a
  client landscape, spend export becomes a contractual deliverable.

### Still open — needs a human decision

**Whose subaccount.** CF answers *how* to deploy, not *whose* landscape it runs in.
Client landscape means their capacity units, their security review, and an awkward
conversation at contract end. A Flexso-operated gateway with per-client virtual keys
and chargeback via the spend ledger is the cleaner commercial shape. Decide before
Phase 3 starts, not during.

### If CF turns out to be wrong

The gateway is a **stateless container plus Postgres** holding one secret and talking
outbound HTTPS to one endpoint. Nothing in it depends on BTP, so the target is
reversible:

| Stays true wherever it lands | Changes with the host |
|---|---|
| The governed path — AI Core is still the model backend | Service-key delivery: CF binding vs Key Vault, Secrets Manager, … |
| The whole config: aliases, filtering, cost map, `drop_params` | Ingress and network policy |
| Virtual keys, budgets, spend ledger (D5, D11, D13) | Whether the AI Core service key leaves the SAP landscape — a governance question, not a technical one |
| The `$$`-escaping, pinning and patch traps (D9, D10, D16) | Who pays, and who runs it |

Hosting outside BTP would **not** mean traffic leaves the governed path — AI Core is
still the backend and the enterprise contract still applies. It would mean the service
key is stored outside SAP's landscape, which is the part a reviewer asks about.

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
