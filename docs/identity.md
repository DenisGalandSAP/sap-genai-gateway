# Identity — IAS SSO and key issuance

IAS authenticates humans at the **admin UI**. The virtual key carries that identity
thereafter. See D13 for the reasoning, D6 for why IAS cannot sit on the data path, and
D14 for why identity stops at the gateway and never reaches AI Core.

Status: designed, not yet wired. Wire it against **localhost first** — Phase 2 in
`docs/spec.md` — not as part of the Cloud Foundry push.

## What SSO does and does not protect

| | Authenticated by |
|---|---|
| Admin UI (`/ui`, key issuance, spend views) | IAS login |
| Data path (`/v1/chat/completions`, `/v1/messages`) | virtual key only |

No IDE client can run an OAuth2 flow; they send a static `Authorization: Bearer`
string and nothing else. So IAS decides *who may obtain a key*, and the key alone
decides *what may call the gateway*. Do not put an OIDC proxy in front of `/v1/*` —
it breaks every client (D6).

## IAS app registration

Create an OIDC application in IAS. Redirect URI:

    <PROXY_BASE_URL>/sso/callback

Access is controlled by IAS group/role assignment on this app. Not assigned, no login,
no key.

### Testing on localhost

Try `http://localhost:4000/sso/callback` first. Loopback redirect URIs are a
recognised exception in the OAuth spec for native apps (RFC 8252) and many IAS tenants
accept them. If yours does, nothing else on this page changes.

**If IAS refuses the non-HTTPS URI, tunnel with ngrok.** Cheaper than deferring the
whole step to first deploy:

```bash
ngrok http --domain=<your-static-domain>.ngrok-free.app 4000
```

```bash
PROXY_BASE_URL=https://<your-static-domain>.ngrok-free.app
```

Three things to get right:

- **`PROXY_BASE_URL` must be the ngrok URL, not localhost.** LiteLLM builds the
  redirect it hands IAS from this value. Point it at localhost and IAS redirects the
  browser back to a host it can't reach — a flow that fails at the last step, which is
  the most confusing place for it to fail.
- **Use a reserved/static domain.** A random ngrok URL changes on every restart, and
  the IAS redirect URI has to be re-registered each time. The free tier includes one
  static domain; take it.
- **Expect the free-tier browser interstitial.** It appears on first visit and you
  click through. If it lands on the `/sso/callback` hop and breaks the flow, that is
  the interstitial, not your config — the `ngrok-skip-browser-warning` header can't be
  set on an IAS-issued redirect, so the fix is a paid tunnel or falling back to
  deferring the step.

### The tunnel is a public route to a live AI Core key

Worth being blunt about, because it is the same exposure D6 and D16 are careful about,
minus every compensating control:

While the tunnel is up, `/v1/chat/completions` and `/v1/messages` are on the public
internet, fronted only by whatever keys exist — and ngrok URLs are scanned. A leaked or
guessed key burns tenant capacity units.

- Run the tunnel **only during the login test**, and kill it after. This is not a
  standing local setup.
- Do not put the master key anywhere near it — issue a short-TTL scoped key for any
  request testing through the tunnel.
- Do not point IDE clients at the ngrok URL. They talk to `localhost:4000` directly;
  the tunnel exists for the browser leg of the OIDC flow and nothing else.
- ngrok's own edge auth (basic auth / OAuth) protects the UI but would also sit in
  front of the IAS callback and the data path, so it is not a clean mitigation here.
  Short-lived tunnel is.

Whichever route works — loopback, ngrok, or deferring — record which one, rather than
leaving the next person to rediscover the tenant's policy.

## Inspect the claims first

Do this before wiring anything. IAS claim naming is not reliably what you would guess,
and the wrong user-id attribute is expensive to unpick once ledger rows exist.

1. Add `<PROXY_BASE_URL>/sso/debug/callback` as a second redirect URI.
2. Visit `<PROXY_BASE_URL>/sso/debug/login` and complete the flow.
3. Note the actual field names returned for user id, email, display name.

## LiteLLM configuration

```bash
PROXY_BASE_URL=http://localhost:4000        # https://<route> once on CF
GENERIC_CLIENT_ID=<IAS app client id>
GENERIC_CLIENT_SECRET=<secret>
GENERIC_AUTHORIZATION_ENDPOINT=https://<tenant>.accounts.ondemand.com/oauth2/authorize
GENERIC_TOKEN_ENDPOINT=https://<tenant>.accounts.ondemand.com/oauth2/token
GENERIC_USERINFO_ENDPOINT=https://<tenant>.accounts.ondemand.com/oauth2/userinfo

# Critical — see D13. The default is preferred_username, which users can change,
# and a changed value is treated as a different person: separate keys, forked spend.
GENERIC_USER_ID_ATTRIBUTE=sub

AUTO_REDIRECT_UI_LOGIN_TO_SSO=true
```

These go in `.env` alongside the AI Core key. Same `$$`-escaping rule applies to the
client secret (D9) — an unescaped `$` truncates it and surfaces as a confusing auth
failure.

Auto-provision on first login:

```yaml
litellm_settings:
  default_internal_user_params:
    user_role: internal_user      # own keys + own spend, cannot add users
    max_budget: 50                # CAPACITY UNITS, not euros — see D11
    budget_duration: 30d
    models:
      - gpt-4o-governed
      - claude-sonnet-governed
      - claude-haiku-governed
```

**All three aliases.** Granting only `gpt-4o-governed` locks new users out of Claude
Code, the primary client (D15), and the first login becomes a support ticket. The
haiku alias is not optional for Claude Code — it runs background work on it.

Roles: `proxy_admin` (everything), `internal_user` (own keys + own spend),
`internal_user_viewer` (read-only on their own).

## Key issuance

Developers self-serve from the UI once SSO is wired. The manual equivalent, and what
Phase 1 used:

```bash
curl -X POST $PROXY_BASE_URL/key/generate \
  -H "Authorization: Bearer $LITELLM_MASTER_KEY" \
  -H "Content-Type: application/json" \
  -d '{"key_alias":"denis-claude-code",
       "models":["claude-sonnet-governed","claude-haiku-governed"],
       "max_budget":50,"budget_duration":"30d","duration":"7d",
       "metadata":{"team":"btp","client_tool":"claude-code"}}'
```

**`duration` is the key TTL — always set it.** Removing someone from the IAS app blocks
new logins but does **not** revoke a key already issued; LiteLLM never re-checks IAS on
a data-path request. Expiry is what makes offboarding automatic. It is also the main
compensating control for the missing IP allowlist on CF (D16).

Give each *client tool* its own key, not just each person — Claude Code and Continue on
separate keys makes agentic spend a visible line rather than a mystery inside a total.

## Reporting

- Admin UI — per user, per key, per model
- `GET /spend/report?start_date=&end_date=`
- Direct SQL against `LiteLLM_SpendLogs` for chargeback export

All figures are capacity units (D11). The UI renders them like dollars; they are not.

## What this cannot tell a client

AI Core sees one technical user, so per-developer attribution exists only in our
Postgres (D14). A client auditing their own subaccount sees a single anonymous
consumer. If this runs in a client landscape, exporting the ledger is a contractual
deliverable, not a convenience.
