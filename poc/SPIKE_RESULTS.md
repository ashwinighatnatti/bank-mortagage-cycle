# Foundry spike — results

Run: 2026-08-20 · `claude-opus-4-7` · endpoint `testbot4909484192.services.ai.azure.com/anthropic/`
SDK: `anthropic==0.125.0` (re-verified 2026-08-20; originally passed on 0.112.0)

**7/7 probes passed.** Everything the build plan depends on is available.

| Capability | Result | Notes |
|---|---|---|
| Messages (basic) | PASS | ~1.3s round trip |
| Adaptive thinking + `effort` | PASS | `{"type":"adaptive"}` accepted; `budget_tokens` correctly absent |
| Structured outputs (`messages.parse`) | PASS | Pydantic model validated on return |
| Strict tool use | PASS | `strict: true` + `additionalProperties: false` honoured |
| Prompt caching | PASS | 3,591-token prefix read from cache |
| Streaming | PASS | needed for the live agent log |
| Tool runner loop | PASS | 2 turns, tool executed and result fed back |

## Decisions this settles

- **Use `messages.parse()` with Pydantic for findings.** The strict-tool fallback is
  not needed. Both work, so `raise_exception` can still be a strict tool for the
  gate's benefit — but the schema guarantee is available either way.
- ~~**Use the SDK tool runner**, not a hand-written loop.~~ **Reversed at step 5.**
  The runner works on Foundry — that part held. It does not fit this system: it
  builds tool schemas from decorated function signatures (we have thirteen
  hand-written strict schemas with per-agent surfaces and tests over them), it
  cannot mark a tool result `is_error` (which is the shape of every gate
  refusal), and it owns the loop, so a budget ceiling is discovered a turn late.
  `app/agents/runner.py` is a manual loop. Everything else here still stands.
- **Cache the guideline pack.** Confirmed working and worth ~90% on the cached prefix.

## Two things learned the hard way

**1 — Use an explicit breakpoint on the system block, not top-level `cache_control`.**

```python
system=[{"type":"text","text":guideline,"cache_control":{"type":"ephemeral"}}]
```

This pins the cache boundary at the end of the stable prefix, which is exactly
where `build_context_pack()` wants it. Top-level `cache_control` caches "the last
cacheable block", which is less predictable once messages vary.

**2 — A cache write is not instantly readable.**

The first version of this probe fired two calls back-to-back and reported a miss
on the second, which looked like "caching unavailable on Foundry". It was not —
the write had not propagated yet. Any test asserting a cache hit needs either a
short pause or several attempts, or it will produce a false negative.

## SDK version

Both 0.112.0 and 0.125.0 pass all seven probes against this deployment. The
requirement is pinned to `anthropic==0.125.0` exactly rather than `>=`, because
a range means the Docker image installs whatever is current on build day and the
container ends up running an SDK nobody ran the spike against. Re-run the spike
before changing that number.

## Environment note

This deployment uses a **full endpoint URL**, not a resource name. The SDK accepts
either, but they are **mutually exclusive** — passing both raises. `Settings.
foundry_client_kwargs()` returns exactly one.

## Not tested here (unavailable on Foundry per the platform matrix)

Managed Agents · Message Batches · Models API · server-side `fallbacks`.
This is why the agent loop is self-hosted rather than platform-managed.

## Worth considering before production

`AnthropicFoundry` accepts `azure_ad_token_provider`. That allows **Entra ID /
managed identity auth with no API key at all** — strictly better than a key in
Key Vault, because there is no long-lived secret to rotate or leak. Revisit at
the deployment step.
