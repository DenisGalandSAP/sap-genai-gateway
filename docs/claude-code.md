# Claude Code against the gateway

Primary client — see D15. Continue is secondary, for quick in-editor work.

Verified working 2026-08-06 on the localhost gateway: real session, scoped virtual key,
via `/v1/messages`, with tool calling intact.

## Setup

Locally, use the launcher — it sets the variables for **one shell only**, so a Claude
Code session you already have open is not redirected mid-task:

```powershell
.\scripts\claude-governed.ps1
```

It reads `CLAUDE_CODE_KEY` from `.env`, un-escapes the `$$` form (D9), checks the
gateway is up, and sets:

```
ANTHROPIC_BASE_URL            = http://localhost:4000       # no /v1 suffix
ANTHROPIC_AUTH_TOKEN          = <scoped virtual key>
ANTHROPIC_MODEL               = claude-sonnet-governed
ANTHROPIC_DEFAULT_HAIKU_MODEL = claude-haiku-governed
```

Against a deployed gateway it is the same four variables with
`ANTHROPIC_BASE_URL=https://<route>`.

**`ANTHROPIC_DEFAULT_HAIKU_MODEL` is not optional.** Claude Code runs background work
(conversation titles, file summaries) on a separate small model. Without the alias
those calls request a model the gateway does not expose and fail quietly while the main
model looks fine. Note the variable name — `ANTHROPIC_SMALL_FAST_MODEL` is the older
one and does not apply here.

## Gateway side

Already in `config/litellm.config.yaml`:

- `claude-sonnet-governed` → `sap/anthropic--claude-4.5-sonnet`
- `claude-haiku-governed` → `sap/anthropic--claude-4.5-haiku`
- `additional_drop_params: ["thinking"]` on both — Claude Code sends `thinking` on
  every request and SAP orchestration has no such param (D12)

Aliases are deliberately plain names. Clients should not need to know the backend, and
the SAP model id is not a stable thing to hard-code in every developer's environment.

Anthropic model names in AI Core use a **double dash** (`anthropic--claude-4.5-sonnet`);
OpenAI models use a single slash. Both take the `sap/` prefix. Verify current names
against SAP Note 3437766 before adding one — the version numbers move.

## Adding or changing an alias

Re-verify per alias. `supports_function_calling` is an assertion in the config, not
something LiteLLM checks:

```bash
SMOKE_MODEL=<new-alias> python scripts/smoke_test.py
```

The smoke test drives `/v1/chat/completions`. Claude Code uses `/v1/messages` — a
different translation path — so a green smoke test is necessary and not sufficient.
Run an actual Claude Code session against the alias before writing "works" anywhere;
that distinction is what D12 came from.

## Diagnostic: edits described instead of made

**File editing IS tool calling.** Read, Write, Edit and Bash are tool definitions in
the request — there is no separate mechanism.

So if tools stop reaching the model, Claude Code does not fail loudly. It degrades into
a chatbot that says *"you should change line 42 to…"* instead of changing it. That
reads like a weak model, and it is easy to lose a day tuning prompts before suspecting
the gateway.

**Rule: Claude Code describing edits instead of making them means tools are not
reaching the model. Run the smoke test — do not debug the prompt.**

## Budget

Claude Code gets its own virtual key so its spend is a visible line, not a share of a
total (D5, D15).

```bash
curl -X POST $PROXY_BASE_URL/key/generate \
  -H "Authorization: Bearer $LITELLM_MASTER_KEY" \
  -H "Content-Type: application/json" \
  -d '{"key_alias":"claude-code-dev",
       "models":["claude-sonnet-governed","claude-haiku-governed"],
       "max_budget":200,"budget_duration":"30d","duration":"7d",
       "metadata":{"client_tool":"claude-code"}}'
```

**The numbers are capacity units, not euros** (D11), and **200 is a placeholder, not a
recommendation** — it was sized assuming occasional agentic use. With Claude Code as
the primary tool, real consumption is higher and workload-dependent. Set the first
budget generously enough not to block work, watch `/spend/report` for two to three
weeks, then set team defaults from evidence. The first adopter is the reference case.

Set `duration` too — see D13. IAS deprovisioning does not revoke a live key.

## Exposure

Tool execution is entirely local. Files are read and written on the developer's
machine; only conversation content crosses to AI Core. The gateway never sees the
filesystem — it sees whatever file content Claude Code chose to include as context.
Same exposure surface as Continue chat, despite the agent feeling more invasive.

Worth stating explicitly in client-facing governance material, because "an AI agent
with filesystem access" sounds alarming until the boundary is drawn.
