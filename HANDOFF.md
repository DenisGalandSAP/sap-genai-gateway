# Handoff — where this stands

Last updated 2026-08-07. Read this, then `CLAUDE.md` for the constraints that must not
be "improved", then `docs/decisions.md` for the reasoning.

## Done — Phase 1, localhost PoC

**The question the PoC existed to answer is answered: tool calling survives the
orchestration hop.** Verified on `gpt-4o` and `anthropic--claude-4.5-sonnet`; all six
smoke checks pass on both. The direct-access fallback (D2) stays commented out.

Also settled, and demonstrated rather than merely configured:

- **Streaming + tool calling together.** Each passed alone while the combination — the
  only shape agentic clients send — 500'd inside the SSE body. `config/litellm_patches.py`
  fixes it and the smoke test now covers the combination (D10).
- **Three clients drive real tasks.** Cline (8-turn tool loop), Continue, and Claude
  Code — the last two on scoped virtual keys, not the master key.
- **Virtual keys enforce.** Scoping, attribution, budgets and revocation all shown
  working, including a 403 on an out-of-scope model (D5).
- **Cost is metered in capacity units**, pulled from SAP's own catalogue (D11). Not
  euros — the UI renders it like currency and it is not.
- **Copilot is out of scope** (decided 2026-08-06, not required, not blocked). Don't
  re-add it to status tables as a gap.

## Decided since, not yet built

| Question | Answer | ADR |
|---|---|---|
| Per-user identity | IAS SSO on the **admin UI** only; virtual keys stay the data-path auth | D13 |
| Principal propagation to AI Core | Impossible — client_credentials carries no user | D14 |
| Which clients | Claude Code primary, Continue secondary, Cline supported | D15 |
| Where to deploy | Cloud Foundry, Python buildpack — **after** local validation | D16 |
| CAP control plane | No. LiteLLM's admin UI covers current needs | D17 |
| Central logging | Dropped. Reporting comes from Postgres directly | — |

## Next up — Phase 2, still on localhost

Deliberately not on CF yet. The identity questions are cheaper to get wrong locally.
Full sequence in `docs/spec.md`.

1. **Wire IAS SSO against `http://localhost:4000`** — `docs/identity.md`. Inspect the
   claims at `/sso/debug/login` first and set `GENERIC_USER_ID_ATTRIBUTE=sub`, or
   identities fork silently on the next profile change. If the tenant refuses the
   non-HTTPS loopback redirect URI, tunnel with ngrok on a static domain and set
   `PROXY_BASE_URL` to the tunnel URL — not localhost, or the flow dies on the last
   hop. Take the tunnel down afterwards: while it is up, a live AI Core key is behind
   a public route with virtual keys as the only gate.
2. **Auto-provision defaults must grant all three aliases** — `gpt-4o-governed`,
   `claude-sonnet-governed`, `claude-haiku-governed`. Granting only the first locks new
   users out of Claude Code on their first login.
3. **Prove it with a second identity**: self-served key, own spend rows, budget caps,
   revocation. Master key stays out of everyone else's hands.
4. **Set key TTLs.** IAS deprovisioning blocks new logins; it does not revoke a key
   already issued.
5. **Onboard someone from the docs alone** — `docs/claude-code.md` and
   `config/continue.config.yaml`. If they can't, the docs failed, not them.

## Then — Phase 3, Cloud Foundry

`deploy/cf/` is written but **never exercised**. Three things to establish, in order:

1. **Streaming through the Gorouter.** Unknown timeout, possible response buffering.
   The one risk that could make CF the wrong target — push a five-minute streaming
   test before migrating properly.
2. **Prisma migrations on a buildpack container** — it fetches engine binaries at first
   boot; untested. Raise the startup timeout regardless (`cf push -t 180`).
3. **`litellm_patches.py` must be in the push directory.** Without it the proxy boots
   clean, the health check goes green, and every agentic request fails inside the SSE
   body. `start.sh` refuses to start without it for exactly this reason.

## Known gaps, accepted

- **No route-level IP allowlist on CF.** Kyma would give an Istio `AuthorizationPolicy`
  scoped to VPN egress; CF has no equivalent, so virtual keys are the only gate on a
  public HTTPS route. Compensate with short TTLs and budget caps (D16).
- **Per-user attribution exists only in our Postgres.** AI Core sees one technical
  user, so a client auditing their own subaccount sees one anonymous consumer. In a
  client landscape, spend export becomes a contractual deliverable (D14).
- **Autocomplete is not governed.** No FIM-capable model in the resource group;
  Continue's autocomplete stays on local Ollama. Stated rather than hidden (D15).

## Unresolved, needs a human decision

**Whose subaccount.** Running dev tooling in a client landscape means their capacity
units, their security review, and an awkward conversation at contract end. A
Flexso-operated gateway with per-client virtual keys and chargeback is the cleaner
commercial shape. Not a technical question — decide before Phase 3 starts.

**Claude Code budget sizing.** The 200 CU/month placeholder assumed occasional agentic
use. With Claude Code as the primary tool, observe two to three weeks of real spend
before setting team defaults — the first adopter is the reference case.

**Which further models to expose.** The tenant also has Gemini and Mistral families.
Each new alias re-opens the tool-calling question: re-run `scripts/smoke_test.py` with
`SMOKE_MODEL` pointed at it before trusting it.
