# Cloud Foundry deployment

**Not yet exercised.** Phase 2 is identity and multi-user validation on localhost; this
is Phase 3. See `docs/spec.md` for the sequence and `docs/decisions.md` D16 for why
buildpack rather than Docker, and CF rather than Kyma.

## Prerequisites

```bash
cf create-service aicore extended aicore
cf create-service postgresql-db <plan> ai-gateway-db
```

## The push directory

`cf push` uploads one directory. Assemble it — do not push the repo root:

```
manifest.yml            from deploy/cf/
start.sh                from deploy/cf/     (chmod +x)
requirements.txt        from deploy/cf/
.cfignore               from deploy/cf/
config.yaml             <- config/litellm.config.yaml, renamed
litellm_patches.py      <- config/litellm_patches.py, NOT renamed
```

**`litellm_patches.py` is not optional and is the easiest thing to forget.**
`config.yaml` loads it as `callbacks: litellm_patches.patch_handler`. Without it the
proxy boots clean, the health check goes green, and every `streaming + tool calling`
request — the only shape agentic clients send — 500s inside the SSE body. That is D10's
failure mode, silently reintroduced. `start.sh` refuses to start if the file is absent,
which is the whole reason that check is there.

Check first whether the patch is still needed at the pinned version — D10 records the
removal condition.

## Deploy

```bash
cf set-env ai-gateway LITELLM_MASTER_KEY sk-...     # plus the GENERIC_* IAS vars, see docs/identity.md
cf push -t 180                                      # raised timeout for first-boot Prisma migrations
```

## Verify before trusting it

In this order — the first one can invalidate the whole target.

1. **Streaming through the Gorouter.** Unknown timeout, and possible response
   buffering that would deliver a stream as one late lump. Run a request that streams
   for five minutes and confirm it neither times out nor arrives buffered. If it fails
   and cannot be tuned, revisit Kyma (D16). Note `request_timeout: 600` in
   `config.yaml` — an agent turn can legitimately run that long.
2. **`scripts/smoke_test.py` against the CF route**, not localhost:
   `LITELLM_BASE_URL=https://<route> python scripts/smoke_test.py`. Then repeat with
   `SMOKE_MODEL` set to each alias.
3. **A real Claude Code session against the route.** The smoke test drives
   `/v1/chat/completions`; Claude Code uses `/v1/messages`. Endpoint verified is not
   client verified (D12).
4. **Virtual keys are enforced and the route is HTTPS-only.** CF has no route-level IP
   allowlist, so keys are the only gate (D16). Confirm an out-of-scope model returns
   403 on a scoped key, as D5 did locally.

## Known unknowns

- **Prisma on the buildpack.** LiteLLM runs Prisma migrations at first boot and Prisma
  fetches its own engine binaries. Whether that works on a CF Python buildpack
  container with no outbound proxy configured is untested — if the first push hangs or
  dies during migration, this is the first place to look.
- **Python version.** The buildpack's default may drift from the 3.11+ this expects.
  Pin with a `runtime.txt` if it does.

## Do not add an AppRouter

The instinctive BTP move, and it breaks every IDE client — they can only send a static
bearer token, not run an OAuth2 flow. Same reason IAS stays off the data path. See D6
and `docs/identity.md`.
