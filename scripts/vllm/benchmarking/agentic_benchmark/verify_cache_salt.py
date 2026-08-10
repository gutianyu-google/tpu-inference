# SPDX-License-Identifier: Apache-2.0
"""Proves that `cache_salt` actually gates prefix-cache reuse.

`benchmark_agentic.py --rl-batch-size N` relies on rotating `cache_salt`
between training batches so a new policy's rollouts cannot reuse KV computed
under the old one. Nothing in the engine enforces that: `cache_salt` is a
per-request field the client supplies, and the engine's `weight_version` is a
label that never reaches the block hasher. So if the field were ignored, the
benchmark would report plausible numbers and silently measure nothing.

This sends the same prompt four times:

  1. salt A -> cold, expect cached_tokens == 0
  2. salt A -> warm, expect cached_tokens > 0   (prefix caching works at all)
  3. salt B -> expect cached_tokens back to 0   (the salt is what gates it)
  4. salt B -> warm again, expect cached_tokens > 0

Step 3 is the point. Step 2 alone only shows prefix caching works; it says
nothing about the salt. Step 4 rules out the alternative reading of step 3 --
that the pool had simply been evicted -- by showing the same prompt still
caches fine under the new salt.

The server must run with `--enable-prefix-caching` and
`--enable-prompt-tokens-details`; without the latter `cached_tokens` is
silently absent (it defaults to False, see vllm/entrypoints/openai/cli_args.py)
and there is nothing to measure.

Example:
    python verify_cache_salt.py --port 8100 --model Qwen/Qwen3-0.6B
"""

import argparse
import json
import sys
import urllib.request
import uuid


def ask(url: str, model: str, body_text: str, salt, timeout: float):
    """Sends one completion and returns (prompt_tokens, cached_tokens)."""
    payload = {
        "model": model,
        "messages": [
            {
                "role": "system",
                "content": "You are a helpful assistant."
            },
            {
                "role": "user",
                "content": body_text
            },
        ],
        "max_tokens": 1,
        "temperature": 0,
    }
    if salt is not None:
        payload["cache_salt"] = salt
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = json.loads(resp.read())
    usage = data["usage"]
    details = usage.get("prompt_tokens_details") or {}
    return usage["prompt_tokens"], details.get("cached_tokens")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Verify that cache_salt gates prefix-cache reuse.")
    parser.add_argument("--host", default="localhost")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--model", default="Qwen/Qwen3-0.6B")
    parser.add_argument(
        "--prompt-words",
        type=int,
        default=1000,
        help="Distinct words in the probe prompt. Must produce enough tokens "
        "to span several blocks, and stay under --max-model-len.",
    )
    parser.add_argument("--timeout", type=float, default=600.0)
    parser.add_argument(
        "--run-id",
        default=None,
        help="Disambiguates this run's salts. Defaults to a fresh uuid, which "
        "is what makes step 1 genuinely cold; reusing a run-id against a "
        "server that already saw it will report a false failure.",
    )
    args = parser.parse_args()

    url = f"http://{args.host}:{args.port}/v1/chat/completions"
    body_text = " ".join(f"token{i}" for i in range(args.prompt_words))

    # Salts must be unique per invocation. With fixed "A"/"B" the second run
    # against a live server finds both already cached, so step 1 is not cold
    # and the whole comparison collapses into a false FAIL.
    run_id = args.run_id or uuid.uuid4().hex[:12]
    salt_a, salt_b = f"A-{run_id}", f"B-{run_id}"
    print(f"run-id {run_id} (salts {salt_a} / {salt_b})\n")

    steps = [
        (f"1. salt=A (cold)", salt_a),
        (f"2. salt=A (warm)", salt_a),
        (f"3. salt=B (rotated)", salt_b),
        (f"4. salt=B (warm under B)", salt_b),
    ]
    cached = []
    for label, salt in steps:
        prompt_tokens, cached_tokens = ask(url, args.model, body_text, salt,
                                           args.timeout)
        cached.append(cached_tokens)
        print(f"{label:30s} prompt={prompt_tokens:6d} cached={cached_tokens}")

    _, warm_a, rotated, warm_b = cached

    print()
    ok = True
    if warm_a is None or rotated is None:
        print("FAIL: cached_tokens not reported -- start the server with "
              "--enable-prompt-tokens-details")
        return 1
    if warm_a <= 0:
        print(f"FAIL: prefix caching is not working at all "
              f"(warm salt=A cached={warm_a})")
        ok = False
    elif rotated >= warm_a:
        print(f"FAIL: rotating the salt did not invalidate "
              f"(salt=B cached={rotated} >= salt=A warm cached={warm_a}); "
              f"cache_salt is being ignored")
        ok = False
    else:
        print(f"PASS: salt rotation dropped reuse from {warm_a} to "
              f"{rotated} cached tokens")

    if not warm_b or warm_b <= 0:
        print(f"FAIL: the prompt does not re-cache under the new salt "
              f"(cached={warm_b}); step 3 may just have been an evicted pool")
        ok = False
    else:
        print(f"PASS: under salt=B the same prompt re-caches ({warm_b} "
              f"tokens), so the step-3 drop was the salt, not eviction")

    print("\nVERDICT:", "SALTING VERIFIED" if ok else "SALTING BROKEN")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
