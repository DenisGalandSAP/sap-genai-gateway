#!/usr/bin/env bash
set -euo pipefail

# Translate CF service bindings into the env vars LiteLLM expects, then start the
# proxy. See docs/decisions.md D16.

# Whole service-key JSON in one variable, exactly as .env holds it locally.
# NOTE: no '$$' escaping here. That escape is a docker-compose artefact (D9);
# VCAP_SERVICES delivers the secret literally. Anything set by hand with
# `cf set-env` must likewise use a single '$'.
export AICORE_SERVICE_KEY=$(python3 -c "
import os, json
print(json.dumps(json.loads(os.environ['VCAP_SERVICES'])['aicore'][0]['credentials']))")

export DATABASE_URL=$(python3 -c "
import os, json
u = json.loads(os.environ['VCAP_SERVICES'])['postgresql-db'][0]['credentials']['uri']
u = u.replace('postgres://', 'postgresql://', 1)      # Prisma rejects postgres://
sep = '&' if '?' in u else '?'
print(u + sep + 'sslmode=require')")                  # BTP Postgres requires SSL

# Absolute config path on purpose. LiteLLM resolves the `callbacks:` handler by
# inserting the config file's directory on sys.path, which is how
# litellm_patches.py (D10) is found. If that file is missing from the push the
# proxy still boots clean and every stream+tools request 500s inside the SSE
# body — fail here instead.
CONFIG="${PWD}/config.yaml"
if [ ! -f "${PWD}/litellm_patches.py" ]; then
  echo "FATAL: litellm_patches.py missing from the push directory — see D10/D16." >&2
  exit 1
fi

exec litellm --config "${CONFIG}" --host 0.0.0.0 --port "${PORT}"
