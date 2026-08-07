# SAP Gen AI Hub → local coding-assistant gateway

## What this is

A LiteLLM proxy that lets local AI coding tools (Cline, Claude Code, Continue) talk to
**SAP Generative AI Hub** on BTP AI Core, so developer AI traffic runs on the governed
model path instead of public endpoints.

Copilot BYOK is **out of scope** — dropped 2026-08-06 as not required, not blocked.
Don't re-add it to status tables as an open gap; see `docs/spec.md`.

**Current phase: Phase 2 — identity and multi-user, still on localhost.** Phase 1 (the
PoC) is complete. IAS SSO, per-user keys and TTLs get wired and proved against
`localhost:4000` *before* anything is deployed — see `docs/spec.md` and `docs/identity.md`.

**Cloud Foundry is decided (D16) but is Phase 3.** `deploy/cf/` exists and has never
been exercised; don't treat it as working, and don't add Kyma or Azure artifacts —
both were considered and rejected in D16.

## Architecture

```
VS Code / Cline / Claude Code
        │  OpenAI-compatible, static bearer token
        ▼
LiteLLM proxy  (localhost:4000)
        │  OAuth2 client_credentials + AI-Resource-Group header
        ▼
AI Core Orchestration deployment  →  foundation model
```

LiteLLM exists because AI Core is **not** directly consumable by these clients:
OAuth2 client_credentials rather than a static key, a mandatory `AI-Resource-Group`
header, and deployment-id in the URL path. No client can do all three.

## Hard constraints — do not "improve" these

These are decided. Each one looks like an obvious improvement and each one breaks
something. Full reasoning in `docs/decisions.md`.

1. **Masking module stays OFF.** Pseudonymization rewrites prompt content and
   reverses on output. Any name or email in a comment, fixture, or config becomes a
   token the model edits around, and code no longer round-trips verbatim. Do not
   enable it to "improve governance" — it produces corrupted diffs.

2. **Content-filter INPUT thresholds stay permissive (6 / most lenient).** The concern
   is that developer vocabulary — kill, abort, terminate, child process, attack
   surface, orphan, deadlock — trips Violence and SelfHarm, and in an agent loop a
   blocked request is a hard mid-task failure with a misleading error. Meaningful
   filtering goes on **output** only.
   *Measured caveat:* those prompts did **not** actually trip the filter on `gpt-4o`,
   even at the strictest threshold (D4 has the table and the positive control). Keep
   the setting — it costs nothing — but don't repeat the premise as established fact.

3. **`drop_params: false`.** Silently dropping unsupported params turns a tool-calling
   failure into a model that just stops using tools. Fail loudly.
   *One named exception:* `additional_drop_params: ["thinking"]` on the Claude aliases,
   because Claude Code sends `thinking` and SAP orchestration has no such param. Drop
   unsupported params **by name**; never flip `drop_params: true`, which is what
   LiteLLM's own error message will tell you to do. See D12.

4. **Never inline the AI Core service key.** Local: `.env`, gitignored. Deployed:
   BTP Operator ServiceBinding or CF service binding. `.env.example` holds
   placeholders only.

5. **Orchestration, not direct deployment access.** Direct `/v2/inference/deployments/
   {id}/chat/completions` is simpler but bypasses output filtering and the single
   governed ingress. Only fall back to direct if the smoke test proves tool calling
   does not survive orchestration — and say so explicitly if you do.

## The question the PoC existed to answer — answered

**Does tool calling survive the orchestration hop?** **Yes.** Verified 2026-08-06 on
`gpt-4o` and `anthropic--claude-4.5-sonnet` — all six smoke checks pass on both; the
model emits tool calls and accepts tool results back. The direct-access fallback stays
commented out.

Three aliases are exposed, all on the same governed path:

| Alias | Backing model | Used by |
|---|---|---|
| `gpt-4o-governed` | `gpt-4o` | Cline |
| `claude-sonnet-governed` | `anthropic--claude-4.5-sonnet` | Cline, Continue, Claude Code |
| `claude-haiku-governed` | `anthropic--claude-4.5-haiku` | Claude Code background tasks |

**Attribute traffic with `call_type`, not prompt size.** `acompletion` is
`/v1/chat/completions` (Cline, Continue); `anthropic_messages` is `/v1/messages`
(Claude Code). A long Cline turn and a Claude Code turn look identical by token count —
this exact confusion produced a wrong "Claude Code verified" claim in these docs once.

**Give each client its own virtual key, not the master key.** Continue is set up that
way — scoped to two aliases, 5 CU / 30d budget — because its config is plaintext YAML
in the user's home directory. Scoping was verified to enforce, not just to be accepted
(403 on an out-of-scope model). See D5.

Still open for models beyond those — the tenant exposes Gemini and Mistral families
too, and each new alias re-opens the question. Re-run `scripts/smoke_test.py` with
`SMOKE_MODEL` pointed at a new alias before trusting it. Note `.env` sets
`SMOKE_MODEL`, so the runner must not overwrite an already-set value.

## Verify before trusting

The docs page — https://docs.litellm.ai/docs/providers/sap — **does not document the
orchestration path at all**. It was the source of several confidently-wrong parameter
names in the original config (`sap/orchestration`, `sap_orchestration_deployment_id`,
`llm_model_name`); none of them exist. Read the provider source in the pinned image
instead, which is the actual contract:

```bash
docker compose exec litellm python -c \
  "import litellm,os;print(os.path.dirname(litellm.__file__)+'/llms/sap/chat/transformation.py')"
```

The image is pinned **by digest** in `docker-compose.yml`, not by a floating tag. Treat
upgrades as breaking and re-read that source before moving the digest.

Two traps that fail silently rather than loudly, both written up in `docs/decisions.md`:

- **`drop_params: false` does not validate module params.** `filtering` is not an
  OpenAI param and is accepted without error whether or not it is honoured. Absence of
  an error is not evidence a module is applied — inspect the transformed payload for
  `config.modules.filtering`.
- **`$` in `.env` is eaten by docker compose.** Escape it as `$$`. An unescaped `$` in
  the client secret truncates it and surfaces as a 401 that looks like bad credentials.
  Verify by hashing the secret inside the container, not by checking the proxy starts.
  Note `docker run --env-file` does **not** unescape `$$`, so a throwaway container
  started from `.env` will fail auth — un-escape first for one-off containers.

## Test the combination, not the features

`streaming` and `tool calling` both passed while `streaming + tool calling` — the only
shape agentic clients actually send — failed with a 500 inside the SSE body. The
gateway looked healthy and was useless. `config/litellm_patches.py` fixes it and the
smoke test now covers the combination (D10).

Keep that generalisation: when adding a model alias or bumping the image, verify the
**combination** an agent sends (system prompt + multi-turn + tools + stream), not the
features one at a time.

The same applies per *endpoint*. Claude Code talks `/v1/messages`, not
`/v1/chat/completions` — a different code path the smoke test does not touch.

**And per client, because a probe can reproduce the shape but not the dialect.** Every
synthetic `/v1/messages` probe passed; the first real Claude Code session still 400'd,
because it sends a `thinking` param no probe thought to include (D12). Endpoint
verified ≠ client verified. Run the actual client before writing "works" anywhere.

## Cost is in capacity units

Spend is real now, but denominated in **capacity units** — SAP's billing unit, pulled
from the AI Core model catalogue, not currency. LiteLLM's UI labels it `spend` and
renders it like dollars; it is not. Do not "fix" this by converting to euros: the rate
is contract-specific, exposed by no API, and would drift silently. Re-run
`scripts/refresh_costs.py` after any model change. See D11.

## Conventions

- Python 3.11+, `requests` for the smoke test, no framework.
- Secrets never leave `.env`. Never echo a service key into logs or terminal output.
- Model aliases exposed to clients are plain names (`gpt-4o-governed`), not `sap/*`
  — clients should not need to know the backend.
