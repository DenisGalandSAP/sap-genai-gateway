#!/usr/bin/env python3
"""
Print current SAP capacity-unit rates for the models this gateway exposes.

SAP publishes per-model cost in the AI Core model catalogue as **capacity units
per 1,000 tokens** — the same unit BTP bills in. LiteLLM wants a per-token
number, so the values printed here are already divided by 1000 and can be
pasted straight into `model_info` in config/litellm.config.yaml.

Rates change when SAP re-prices a model or you move to a new model version, and
nothing warns you — re-run this after any model change and whenever a spend
report looks wrong. See docs/decisions.md D11.

Usage:
    python scripts/refresh_costs.py
"""

import json
import os
import pathlib
import sys

import requests

# Aliases exposed by the gateway -> the SAP model name behind each one.
# Keep in sync with config/litellm.config.yaml.
ALIASES = {
    "gpt-4o-governed": "gpt-4o",
    "claude-sonnet-governed": "anthropic--claude-4.5-sonnet",
    "claude-haiku-governed": "anthropic--claude-4.5-haiku",
}


def load_service_key() -> dict:
    raw = os.environ.get("AICORE_SERVICE_KEY")
    if not raw:
        # Fall back to .env so this works without sourcing anything first.
        env_path = pathlib.Path(__file__).resolve().parent.parent / ".env"
        if not env_path.exists():
            sys.exit("AICORE_SERVICE_KEY not set and no .env found.")
        for line in env_path.read_text(encoding="utf-8").splitlines():
            if line.startswith("AICORE_SERVICE_KEY="):
                raw = line.split("=", 1)[1].strip()
                break
        else:
            sys.exit("AICORE_SERVICE_KEY not found in .env")
    # '$$' is the docker-compose escape for a literal '$' (see D9).
    return json.loads(raw.replace("$$", "$"))


def main() -> None:
    cred = load_service_key()
    base = cred["serviceurls"]["AI_API_URL"].rstrip("/")

    token = requests.post(
        cred["url"].rstrip("/") + "/oauth/token",
        data={"grant_type": "client_credentials"},
        auth=(cred["clientid"], cred["clientsecret"]),
        timeout=30,
    ).json()["access_token"]

    resp = requests.get(
        f"{base}/v2/lm/scenarios/foundation-models/models",
        headers={
            "Authorization": f"Bearer {token}",
            "AI-Resource-Group": os.environ.get("AICORE_RESOURCE_GROUP", "default"),
        },
        timeout=60,
    )
    resp.raise_for_status()
    catalogue = {m["model"]: m for m in resp.json()["resources"]}

    print("# Capacity units per token, from the SAP model catalogue.")
    print("# Paste into model_info in config/litellm.config.yaml.\n")

    missing = []
    for alias, model_name in ALIASES.items():
        entry = catalogue.get(model_name)
        if entry is None:
            missing.append(f"{alias} -> {model_name} (not in catalogue)")
            continue

        version = next((v for v in entry["versions"] if v.get("isLatest")),
                       entry["versions"][-1])
        costs: dict[str, str] = {}
        for item in version.get("cost") or []:
            costs.update(item)

        if "inputCost" not in costs or "outputCost" not in costs:
            missing.append(f"{alias} -> {model_name} (version has no cost field)")
            continue

        in_cu = float(costs["inputCost"]) / 1000
        out_cu = float(costs["outputCost"]) / 1000
        print(f"  # {alias}  ({model_name}, version {version.get('name')})")
        print(f"  input_cost_per_token:  {in_cu:.11f}".rstrip("0"))
        print(f"  output_cost_per_token: {out_cu:.11f}".rstrip("0"))
        if version.get("deprecated"):
            print(f"  # WARNING: version deprecated, retires {version.get('retirementDate')}")
        print()

    if missing:
        print("# Could not resolve:")
        for m in missing:
            print(f"#   {m}")
        sys.exit(1)


if __name__ == "__main__":
    main()
