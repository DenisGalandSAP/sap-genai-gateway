# SAP Gen AI Hub → coding-assistant gateway

LiteLLM proxy putting local AI coding tools on SAP Generative AI Hub (BTP AI Core)
instead of public model endpoints.

**Phase 1, localhost PoC — complete.** Tool calling survives orchestration (verified
2026-08-06 on `gpt-4o` and Claude Sonnet 4.5; all six smoke checks pass on both). Cline,
Continue and Claude Code all drive real tasks through it, with per-request cost metered
in capacity units.

**Phase 2, current — identity and multi-user, still on localhost.** IAS SSO on the
admin UI, self-served keys with TTLs and budgets, proved locally before anything is
deployed. Cloud Foundry is decided (D16) and is Phase 3; `deploy/cf/` is written and
not yet exercised. `HANDOFF.md` is the short version of where this stands.

## Quickstart

```bash
cp .env.example .env      # paste the AI Core service key — read the $-escaping note
docker compose up -d
set -a && source .env && set +a
pip install requests
python scripts/smoke_test.py
```

There is no orchestration deployment ID to configure; the provider discovers it
(`docs/decisions.md` D8). The one setup trap that bites is `$` in the service key —
docker compose eats it and you get a 401 that looks like bad credentials (D9).

`GET /health/liveliness` returns 200 whenever the process is up — it does **not**
check the database or AI Core, and it stays green while the proxy is unusable. The
smoke test is the real health signal.

The smoke test is the point of this phase. It answers whether tool calling survives
the orchestration hop — if it doesn't, agentic clients won't work on this path and
the design changes. It currently answers yes:

```
[PASS] basic completion
[PASS] streaming                  3 chunks
[PASS] tool calling               called read_file({'path': 'src/config.py'})
[PASS] tool result round-trip
[PASS] streaming + tool calling   write_to_file(['path','content']) reassembled
[PASS] filter false positives     all 4 dev prompts passed
```

The `streaming + tool calling` check exists because streaming and tool calling each
passed on their own while the combination — the only shape agentic clients send —
failed with a 500 inside the SSE body. `config/litellm_patches.py` works around that
LiteLLM bug at startup; see `docs/decisions.md` D10, including its removal condition.

## Connect a client

Cline is the least fiddly starting point:

- **Cline** — OpenAI Compatible, base `http://localhost:4000/v1`, key, model `gpt-4o-governed`
- **Claude Code** — run `scripts\claude-governed.ps1` **in a separate terminal**. It sets the `ANTHROPIC_*` vars for that shell only, so a Claude Code session you already have open is not redirected mid-task. Sets `ANTHROPIC_DEFAULT_HAIKU_MODEL` too — without it, background calls fail quietly while the main model looks fine.
- **Continue** — copy `config/continue.config.yaml` to `~/.continue/config.yaml`; it reads the key and URL from `GENAI_GATEWAY_KEY` / `GENAI_GATEWAY_URL`, so the file holds no secrets
Copilot BYOK is out of scope (not required). See `docs/spec.md` if that changes.

Give each client its **own virtual key**, not the master key — scoped to the aliases it
needs, with a budget. Continue is set up this way (D5); its config is plaintext on disk,
so the master key has no business being in it.

```bash
curl -X POST localhost:4000/key/generate -H "Authorization: Bearer $LITELLM_MASTER_KEY" \
  -d '{"key_alias":"continue-dev","models":["claude-sonnet-governed"],"max_budget":5,"budget_duration":"30d"}'
```

## Layout

```
CLAUDE.md                    constraints for Claude Code — read first
HANDOFF.md                   where this stands, what's next, what's undecided
docs/spec.md                 phased plan; Phase 3 traps recorded in advance
docs/decisions.md            ADRs, including what was rejected and why
docs/identity.md             IAS SSO, key issuance and TTLs (D13) — Phase 2
docs/claude-code.md          primary client: setup, budget, the tools diagnostic (D15)
config/litellm.config.yaml   proxy config; verified against the pinned image
config/litellm_patches.py    startup workaround for the stream+tools bug (D10)
config/continue.config.yaml  distributable Continue template; no secrets in the file
scripts/smoke_test.py        the PoC deliverable
scripts/claude-governed.ps1  launch Claude Code at the gateway, this shell only
scripts/refresh_costs.py     re-pull SAP capacity-unit rates (D11)
deploy/cf/                   Cloud Foundry push (D16) — written, never exercised
```

## Cost

Spend is recorded in **capacity units**, not currency — SAP's own billing unit, pulled
from the AI Core model catalogue. LiteLLM labels the field `spend` and renders it like
dollars; it isn't. Re-run `python scripts/refresh_costs.py` after any model change:
rates drift and nothing warns you. See `docs/decisions.md` D11.

## Before changing the config

`docs/decisions.md` records four choices that look like obvious improvements and
each break something: masking off, permissive input filters, `drop_params: false`,
orchestration over direct access. Read the reasoning before reversing any of them.

Also note the provider's own docs page does not describe the orchestration path and
will send you toward parameters that do not exist. The contract is the provider source
inside the pinned image — see the "Verify before trusting" section of `CLAUDE.md`.
