# Phase 1 report — localhost PoC

**Status: complete.** 6 August 2026.

The PoC existed to answer one question before any infrastructure was bought:
**does tool calling survive the SAP AI Core orchestration hop?** It does — verified on
two models, through three coding clients, across both client API dialects.

This document is the handoff record: what was built, what broke, what was decided, and
what is still open. `docs/decisions.md` holds the reasoning; this is the summary.

---

## 1. What runs

```
Cline · Continue · Claude Code
        │  OpenAI /chat/completions  or  Anthropic /v1/messages
        │  static bearer (master key or scoped virtual key)
        ▼
LiteLLM 1.95.0  (localhost:4000, pinned by digest)  + Postgres
        │  OAuth2 client_credentials + AI-Resource-Group header
        ▼
AI Core orchestration  da15f1f9fac8a4c1  (eu-central-1, resource group `default`)
        ▼
gpt-4o · Claude Sonnet 4.5 · Claude Haiku 4.5
```

**Model aliases** (clients never see `sap/*`):

| Alias | Backing model | Used by |
|---|---|---|
| `gpt-4o-governed` | `gpt-4o` | Cline |
| `claude-sonnet-governed` | `anthropic--claude-4.5-sonnet` | Cline, Continue, Claude Code |
| `claude-haiku-governed` | `anthropic--claude-4.5-haiku` | Claude Code background tasks |

**Clients driven end to end:**

| Client | Endpoint | Key | Evidence |
|---|---|---|---|
| Cline | `/chat/completions` | master | 8-turn tool loop; wrote `example.csv` and `scripts/count_rows.py` |
| Continue | `/chat/completions` | `continue-dev` (scoped) | chat + edit |
| Claude Code | `/v1/messages` | `claude-code-dev` (scoped) | ~5.6k-token turns, background tasks on haiku |

Copilot BYOK is **out of scope** — dropped as not required, not blocked.

---

## 2. Numbers

| | |
|---|---|
| Requests through the gateway | 182 |
| By endpoint | 130 `acompletion`, 13 `anthropic_messages`, 39 rejected |
| Tokens | 238,013 in / 7,078 out |
| Spend recorded | 0.065822 CU across 15 priced rows |
| Decisions on file | 12 (D1–D12) |

Rejected requests are logged with zero tokens and full key attribution — refusals stay
in the audit trail rather than disappearing. Rows predating the cost map (§4, D11) show
`spend = 0`; cost is computed at request time and is not backfilled.

---

## 3. The question, answered

**Tool calling survives orchestration.** Six smoke checks pass on both `gpt-4o` and
`anthropic--claude-4.5-sonnet`: basic completion, streaming, tool calling, tool-result
round-trip, **streaming + tool calling**, and developer-vocabulary filter false
positives. The direct-deployment fallback in D2 stays commented out.

Two governance claims moved from *configured* to *demonstrated*:

- **Cost** (D11) — priced in capacity units from SAP's own model catalogue.
- **Virtual keys** (D5) — scoping, attribution, budgets and revocation, with an
  out-of-scope model returning 403 while the master key returns 200 on the same call.

---

## 4. What broke — the useful part

Five failures that a status page would not have shown. Each is written up in
`docs/decisions.md`.

**The config was confidently wrong (D8).** `sap/orchestration`,
`sap_orchestration_deployment_id`, `sap_resource_group`, `llm_model_name` — none of
those parameters exist. The provider docs page does not document the orchestration path
at all, which is why it misled rather than merely omitted. `sap/<model>` *is* the
orchestration path, and the deployment is discovered at runtime (newest wins,
unpinnable — standing up a second one silently redirects all traffic).

**Docker Compose ate the client secret (D9).** Compose expands `$VAR` inside `env_file`
values; the secret contained a literal `$`, so the container received 72 of 81
characters. No error — just a 401 that looks like bad credentials. Escaped as `$$`,
verified by hashing inside the container. Note `docker run --env-file` does *not*
unescape, so one-off containers need the raw value.

**The gateway was broken for its actual purpose while reporting healthy (D10).**
Streaming passed. Tool calling passed. **Streaming *with* tools** — the only shape an
agentic client ever sends — returned a 500 buried inside the SSE body. Root cause was a
LiteLLM/pydantic serializer bug on `logprobs` in tool-call chunks; three plausible fixes
failed first, including one that reported `repaired=69 failed=0` and changed nothing.
`config/litellm_patches.py` fixes it at startup; the smoke test now covers the
combination and was confirmed to fail without the patch.

**Cost recorded as zero (D11).** LiteLLM has no cost map for SAP model names, so budget
caps compared against a spend that never rose. SAP publishes per-model rates in the AI
Core catalogue as capacity units per 1,000 tokens; `scripts/refresh_costs.py` re-pulls
them. Deliberately **not** converted to currency — the CU→€ rate is contract-specific,
exposed by no API, and would drift silently.

**LiteLLM's own error message recommended the wrong fix (D12).** Claude Code sends a
`thinking` parameter SAP orchestration rejects. The error suggests `drop_params: true`,
which would also silently swallow `tools[]` — precisely what D7 exists to prevent.
Fixed with `additional_drop_params: ["thinking"]`, verified surgical by confirming an
unlisted unsupported param still returns 400.

### Two lessons worth carrying into Phase 2

**Test the combination, not the features.** Streaming and tool calling each passed while
their combination failed.

**A probe reproduces a client's shape, not its dialect.** Every synthetic `/v1/messages`
check passed — system prompt, tools, streaming. The first real Claude Code session still
failed, on a parameter no probe thought to send. Endpoint verified is not client
verified.

A third, procedural: **attribute traffic by `call_type`, not prompt size.** A long Cline
turn is indistinguishable from a Claude Code turn by token count. That confusion put a
wrong "Claude Code verified" claim into these docs once before the ledger corrected it.

---

## 5. Decisions

| | Decision | Status |
|---|---|---|
| D1 | LiteLLM as gateway, not bespoke shim | accepted |
| D2 | Orchestration, not direct deployment access | accepted — fallback unused |
| D3 | Masking module OFF | accepted |
| D4 | Permissive input filters, meaningful output filters | accepted — premise did not reproduce; table in D4 |
| D5 | Virtual keys and budgets in LiteLLM | **demonstrated** |
| D6 | Ingress `noAuth` + virtual keys, not JWT | accepted, deferred to Phase 2 |
| D7 | `drop_params: false` | accepted — one named exception (D12) |
| D8 | Orchestration deployment is discovered, not configured | forced by provider |
| D9 | `$` in the service key escaped as `$$` | accepted |
| D10 | Startup patch for the stream+tools bug | workaround, with removal condition |
| D11 | Spend in capacity units, sourced from SAP | accepted |
| D12 | Drop `thinking` by name, never `drop_params: true` | accepted |

D4 is worth re-reading: the stated premise — that developer vocabulary trips the content
filter — **did not reproduce**, even at the strictest threshold. The setting was kept
(it costs nothing) but the reasoning is now marked as unverified rather than fact.

---

## 6. Open

**Deployment target undecided — and it may not be BTP.** Azure is a candidate. The
gateway is a stateless container plus Postgres; nothing in Phase 1 depends on BTP.
Hosting it elsewhere does not take traffic off the governed path — AI Core is still the
backend — but it does move the service key outside SAP's landscape, which is the part a
reviewer will ask about. The Kyma-vs-Cloud-Foundry trade-off only applies if you stay on
BTP. See `docs/spec.md` § Phase 2.

Also open:

- **Whose tenancy**, and who pays — the commercial shape, not a technical question.
- **Cline still uses the master key.** Continue and Claude Code have scoped keys; Cline
  should get one before this is shared with anyone.
- **Gemini and Mistral aliases untested.** Each new alias reopens the tool-calling
  question; re-run `scripts/smoke_test.py` with `SMOKE_MODEL` pointed at it.
- **Cost rates drift silently.** Re-run `scripts/refresh_costs.py` after any model
  change.
- **Policy, not technology:** the governed path answers *which model, under what
  contract*. It does not answer *whether this source code belongs in a prompt at all*.
  That is a per-engagement call and a reviewer will ask.

---

## 7. Picking it up again

```powershell
cd D:\projects\sap-genai-gateway
docker compose up -d                      # both containers; ~50s for migrations
py scripts\smoke_test.py                  # source .env first; 6 checks
.\scripts\claude-governed.ps1             # Claude Code, in a SEPARATE terminal
py scripts\refresh_costs.py               # re-pull SAP capacity-unit rates
```

`GET /health/liveliness` returns 200 whenever the process is up — it does **not** check
the database or AI Core and stays green while the proxy is unusable. The smoke test is
the real health signal.

Before changing anything in `config/`, read `docs/decisions.md`. Several of those
settings look like obvious improvements and each one breaks something specific.
