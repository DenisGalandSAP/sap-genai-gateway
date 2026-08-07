#!/usr/bin/env python3
"""
Smoke test for the Gen AI Hub gateway.

Answers the question the PoC exists to answer: does tool calling survive the
orchestration hop? If test 3 fails, agentic clients (Copilot agent mode, Cline,
Claude Code) are unusable on this path and the design changes — see decisions D2.

Usage:
    pip install requests
    python scripts/smoke_test.py
"""

import json
import os
import sys
import time

import requests

BASE = os.environ.get("LITELLM_BASE_URL", "http://localhost:4000")
KEY = os.environ.get("LITELLM_MASTER_KEY")
MODEL = os.environ.get("SMOKE_MODEL", "gpt-4o-governed")
TIMEOUT = 120

if not KEY:
    sys.exit("LITELLM_MASTER_KEY not set. Source your .env first.")

HEADERS = {"Authorization": f"Bearer {KEY}", "Content-Type": "application/json"}
results = []


def record(name, passed, detail=""):
    results.append((name, passed, detail))
    mark = "PASS" if passed else "FAIL"
    print(f"[{mark}] {name}" + (f"\n       {detail}" if detail else ""))


def post(payload, stream=False):
    return requests.post(
        f"{BASE}/v1/chat/completions",
        headers=HEADERS,
        json=payload,
        stream=stream,
        timeout=TIMEOUT,
    )


# --------------------------------------------------------------------------
# 1. Basic completion — is the OAuth exchange and routing working at all
# --------------------------------------------------------------------------
def test_basic():
    try:
        r = post({
            "model": MODEL,
            "messages": [{"role": "user", "content": "Reply with exactly: OK"}],
            "max_tokens": 10,
        })
        if r.status_code != 200:
            return record("basic completion", False, f"HTTP {r.status_code}: {r.text[:300]}")
        content = r.json()["choices"][0]["message"]["content"]
        record("basic completion", True, f"got: {content.strip()!r}")
    except Exception as e:
        record("basic completion", False, repr(e))


# --------------------------------------------------------------------------
# 2. Streaming — long agent turns rely on SSE not truncating
# --------------------------------------------------------------------------
def test_streaming():
    try:
        r = post({
            "model": MODEL,
            "messages": [{"role": "user", "content": "Count slowly from 1 to 30."}],
            "stream": True,
            "max_tokens": 300,
        }, stream=True)
        if r.status_code != 200:
            return record("streaming", False, f"HTTP {r.status_code}: {r.text[:300]}")

        chunks, started = 0, time.time()
        for line in r.iter_lines():
            if line and line.startswith(b"data: ") and b"[DONE]" not in line:
                chunks += 1
        elapsed = time.time() - started

        if chunks < 2:
            return record("streaming", False, f"only {chunks} chunk(s) — not really streaming")
        record("streaming", True, f"{chunks} chunks in {elapsed:.1f}s")
    except Exception as e:
        record("streaming", False, repr(e))


# --------------------------------------------------------------------------
# 3. TOOL CALLING — the one that decides the architecture
# --------------------------------------------------------------------------
def test_tool_calling():
    tools = [{
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read the contents of a file from the repository.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Relative file path"},
                },
                "required": ["path"],
            },
        },
    }]
    try:
        r = post({
            "model": MODEL,
            "messages": [{
                "role": "user",
                "content": "What is in the file src/config.py? Use the tool to look.",
            }],
            "tools": tools,
            "tool_choice": "auto",
        })
        if r.status_code != 200:
            return record("tool calling", False, f"HTTP {r.status_code}: {r.text[:400]}")

        msg = r.json()["choices"][0]["message"]
        calls = msg.get("tool_calls")
        if not calls:
            return record(
                "tool calling", False,
                "no tool_calls in response — orchestration likely strips the tools param.\n"
                "       This is the blocking case. See docs/decisions.md D2.\n"
                f"       content was: {str(msg.get('content'))[:200]!r}",
            )

        fn = calls[0]["function"]
        args = json.loads(fn["arguments"])
        record("tool calling", True, f"called {fn['name']}({args})")

        # Round trip: does a tool result get accepted back?
        r2 = post({
            "model": MODEL,
            "messages": [
                {"role": "user", "content": "What is in src/config.py? Use the tool."},
                msg,
                {
                    "role": "tool",
                    "tool_call_id": calls[0]["id"],
                    "content": "DEBUG = False\nTIMEOUT = 30",
                },
            ],
            "tools": tools,
        })
        if r2.status_code != 200:
            return record("tool result round-trip", False, f"HTTP {r2.status_code}: {r2.text[:300]}")
        record("tool result round-trip", True,
               r2.json()["choices"][0]["message"]["content"][:120].replace("\n", " "))
    except Exception as e:
        record("tool calling", False, repr(e))


# --------------------------------------------------------------------------
# 3b. STREAMING + TOOL CALLING TOGETHER
#
# Tests 2 and 3 pass individually while this combination fails, and this is the
# combination every agentic client actually sends. It regressed once already:
# litellm 1.95.0 emitted `logprobs: {}` on tool-call chunks, whose pydantic
# class had a mock serializer, and the stream died with a 500 in the SSE body
# instead of [DONE]. See docs/decisions.md D10.
# --------------------------------------------------------------------------
def test_streaming_tool_calling():
    tools = [{
        "type": "function",
        "function": {
            "name": "write_to_file",
            "description": "Write content to a file.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "content": {"type": "string"},
                },
                "required": ["path", "content"],
            },
        },
    }]
    try:
        r = post({
            "model": MODEL,
            "messages": [
                {"role": "system", "content": "You are a coding agent. Use the tools."},
                {"role": "user", "content": "Create hello.py that prints Hello, world!"},
            ],
            "tools": tools,
            "tool_choice": "auto",
            "stream": True,
            "max_tokens": 400,
        }, stream=True)
        if r.status_code != 200:
            return record("streaming + tool calling", False,
                          f"HTTP {r.status_code}: {r.text[:300]}")

        acc, saw_done, err = {}, False, None
        for line in r.iter_lines():
            if not line or not line.startswith(b"data: "):
                continue
            raw = line[6:]
            if raw.strip() == b"[DONE]":
                saw_done = True
                break
            try:
                chunk = json.loads(raw)
            except ValueError:
                continue
            # An error mid-stream arrives as a data: frame, not an HTTP status.
            if isinstance(chunk, dict) and "error" in chunk:
                err = str(chunk["error"].get("message"))[:200]
                break
            delta = (chunk["choices"][0].get("delta") or {})
            for tc in delta.get("tool_calls") or []:
                slot = acc.setdefault(tc.get("index", 0), {"name": "", "args": ""})
                fn = tc.get("function") or {}
                if fn.get("name"):
                    slot["name"] = fn["name"]
                slot["args"] += fn.get("arguments") or ""

        if err:
            return record("streaming + tool calling", False,
                          f"stream aborted mid-response: {err}")
        if not saw_done:
            return record("streaming + tool calling", False,
                          "stream ended without [DONE] — truncated or errored")
        if not acc:
            return record("streaming + tool calling", False,
                          "no tool_calls arrived in the stream")

        slot = acc[sorted(acc)[0]]
        try:
            args = json.loads(slot["args"])
        except ValueError as e:
            return record("streaming + tool calling", False,
                          f"streamed arguments are not valid JSON ({e}): "
                          f"{slot['args'][:120]!r}")
        record("streaming + tool calling", True,
               f"{slot['name']}({list(args)}) reassembled from stream")
    except Exception as e:
        record("streaming + tool calling", False, repr(e))


# --------------------------------------------------------------------------
# 4. Content-filter false positives on ordinary developer vocabulary
# --------------------------------------------------------------------------
DEV_PROMPTS = [
    "How do I kill a hung child process and force terminate the thread pool?",
    "Explain how to abort a transaction and clean up orphan records after a crash.",
    "Review this auth code for injection attack surface and unsafe exec calls.",
    "Why does my daemon deadlock when the parent dies before reaping children?",
]


def test_filter_false_positives():
    blocked = []
    for p in DEV_PROMPTS:
        try:
            r = post({"model": MODEL, "messages": [{"role": "user", "content": p}],
                      "max_tokens": 40})
            if r.status_code != 200:
                blocked.append((p[:55], f"HTTP {r.status_code}"))
        except Exception as e:
            blocked.append((p[:55], repr(e)[:60]))

    if blocked:
        detail = "\n".join(f"       - {p}... → {why}" for p, why in blocked)
        return record("filter false positives", False,
                      f"{len(blocked)}/{len(DEV_PROMPTS)} dev prompts blocked. "
                      f"Raise INPUT thresholds (see D4).\n{detail}")
    record("filter false positives", True, f"all {len(DEV_PROMPTS)} dev prompts passed")


if __name__ == "__main__":
    print(f"Gateway : {BASE}\nModel   : {MODEL}\n" + "-" * 68)
    test_basic()
    test_streaming()
    test_tool_calling()
    test_streaming_tool_calling()
    test_filter_false_positives()

    print("-" * 68)
    failed = [n for n, ok, _ in results if not ok]
    if failed:
        print(f"{len(failed)} failed: {', '.join(failed)}")

        # Distinguish the two very different tool-calling failures. Sending
        # someone to the direct-access fallback for a proxy-side serialization
        # bug would be the wrong fix applied to the wrong layer.
        if "streaming + tool calling" in failed and "tool calling" not in failed:
            print("\nTool calling works, but not while streaming. That is a")
            print("LiteLLM-side bug, NOT an orchestration limitation — do not")
            print("switch to direct deployment access for it. Check that")
            print("config/litellm_patches.py is still loaded (litellm_settings")
            print("-> callbacks) and see docs/decisions.md D10.")
        elif any("tool" in f for f in failed):
            print("\nTool calling failed — this is the blocking result.")
            print("Enable the gpt-4o-direct fallback in config and re-run, then")
            print("record the outcome in docs/decisions.md D2.")
        sys.exit(1)
    print("All checks passed. Orchestration path is viable for agentic clients.")
