# Live Inspection & Codegen Agent Service

Backend module for Scraper Studio. Runs the "Claude drives a real browser and
writes the scraper" step as an unattended service, using the Claude Agent SDK
+ Playwright MCP.

## Why this exists

Earlier prototyping (in-chat, via Chrome DevTools MCP) proved the concept but
depended on a human-in-the-loop chat session with a local desktop app. This
service does the same inspect-then-generate work as a callable backend
component - the Builder page hits it directly, and it can also be triggered
autonomously by the Site Change Detection module for re-inspection.

## Components

| File | Role |
|---|---|
| `inspection_agent_service.py` | Core agent logic, now a **two-phase pipeline** per job: a `recon` session (browser tools, `ClaudeSDKClient` + `ClaudeAgentOptions` wired to Playwright MCP over HTTP) that produces a compact JSON of *findings* rather than code, followed by a separate `codegen` session (no browser tools, fresh/small context) that writes the script from those findings. Each phase has its own permission scoping, hooks (audit log, step cap, redundant-call dedup, anti-bot early-exit), and budget share. |
| `api_service.py` | FastAPI layer: `POST /jobs`, `GET /jobs/{id}`, `GET /jobs/{id}/stream` (SSE for the live agent log), `GET /jobs/{id}/cost`, `GET /health`, `GET /jobs`, `POST /jobs/{id}/test-run` + `GET /jobs/{id}/test-run` (actually run the generated script against a sample input, in an isolated per-job venv), `GET /jobs/{id}/sample-output` (download the result) |
| `auth_config.py` | Anthropic credential provisioning - env-based, validates at startup, fails fast with an actionable error. Supports direct API key, Bedrock, and Vertex. |
| `cost_metering.py` | Builds a `CostRecord` from each phase's real `ResultMessage` (cost, tokens, cache stats, turns, duration, tagged with `phase`), writes each to a pluggable `CostSink`, and merges both phases into one summary dict (`combine_cost_records`) for API/UI backward compatibility. Also owns the per-phase budget split (`split_budget_ceiling`) and the SDK budget safety margin. |
| `requirements.txt` | `claude-agent-sdk`, `fastapi`, `uvicorn`, `pydantic` |

### Why two phases (2026-08-20 token/cost refactor)

A review of `cost_ledger.jsonl` from real runs showed 4 of 5 jobs blowing
through the (then single, whole-job) $0.75 default budget - actual spend
$0.77-$1.10 - and dying via `error_max_budget_usd` with no script delivered.
The dominant cost driver was `cache_read_input_tokens` in the 340K-545K
range per job, compounding across 14-21 turns of one long session that both
drove the browser *and* wrote the final code - every earlier snapshot/
network-log tool result stayed in context and got re-paid on every later
turn.

Splitting recon (gather facts) from codegen (write the script from those
facts, in a fresh context) means codegen never pays for the accumulated
browser-tool transcript - its input is just the findings JSON + the
requester's rules. Three additional guards reduce recon's own footprint:

- **Redundant-call dedup** - the `PreToolUse` hook now denies an immediate
  repeat of an expensive read-only call (`browser_snapshot`,
  `browser_network_requests`, `browser_take_screenshot`) with identical
  arguments, since re-fetching unchanged page state only re-pays for
  information already in context.
- **Anti-bot early exit** - if a tool result contains a block/CAPTCHA/WAF
  marker, the session stops immediately (`status: "anti_bot_detected"`)
  instead of continuing to explore past a wall for the rest of the step
  budget.
- **Per-phase budget split + safety margin** - `max_budget_usd` is now split
  across recon/codegen (`DEFAULT_RECON_BUDGET_SHARE`, recon gets the larger
  share by default) and shaved by `SDK_BUDGET_SAFETY_MARGIN` before being
  handed to the SDK, leaving headroom against the fact that the SDK's
  budget check fires *between* turns, not mid-turn.

`InspectionJob` also accepts optional `recon_model` / `codegen_model`
overrides, since the mechanical browser-navigation phase and the code-
writing phase have different quality requirements - plug in whatever model
tiers make sense for your account once you've confirmed pricing/quality
trade-offs; they default to the service/SDK default (unset).

## Credentials

The Claude Agent SDK shells out to the bundled Claude Code CLI, which reads
credentials from environment variables - there's no `api_key=` argument on
`ClaudeAgentOptions`. `auth_config.py` centralizes provisioning across three
backends and validates at service startup, not on first job:

**Direct API key (default):**
```
export ANTHROPIC_API_KEY=sk-ant-...
```

**Amazon Bedrock:**
```
export CLAUDE_CODE_USE_BEDROCK=1
export AWS_REGION=us-east-1
# plus standard AWS credentials via env/instance role/~/.aws/credentials
```

**Google Vertex AI:**
```
export CLAUDE_CODE_USE_VERTEX=1
export ANTHROPIC_VERTEX_PROJECT_ID=your-project
export CLOUD_ML_REGION=us-central1
```

**Secrets manager instead of a plaintext env var:** wire your provider (AWS
Secrets Manager, GCP Secret Manager, Vault, etc.) into
`get_secret_from_manager()` in `auth_config.py` - it's called automatically
as a fallback when `ANTHROPIC_API_KEY` isn't already set in the environment.

If credentials are missing or malformed, the service **refuses to start**
(`CredentialError` raised from the FastAPI `startup` event, which exits
uvicorn non-zero) rather than accepting jobs that will fail partway through
after already spending tokens. `GET /health` re-checks resolvability without
ever exposing the key itself (logs and health responses only show a masked
form, e.g. `sk-ant...7890`).

## Cost metering

Every job produces exactly one `ResultMessage` (one `ClaudeSDKClient` session
per job in this design), which carries `total_cost_usd`, token usage
(including cache creation/read breakdown), `num_turns`, and per-model usage.
`cost_metering.py` turns that into a `CostRecord` and hands it to a
`CostSink` - the default `JSONLCostSink` appends to `cost_ledger.jsonl` as a
stand-in for your real Cost Ops ingestion point (swap it for a sink that
writes to your billing datastore, a Kafka topic, or calls the Usage and Cost
API directly - the calling code in `inspection_agent_service.py` doesn't
change).

**Two independent spend guards, not one:**
- **Step cap** (`max_steps` for recon, `CODEGEN_MAX_TURNS` for codegen,
  enforced by the `PreToolUse` hook) - protects against a runaway loop of
  many cheap tool calls.
- **Budget cap** (`max_budget_usd`, enforced by the SDK itself via
  `ClaudeAgentOptions`) - protects against a smaller number of unexpectedly
  expensive ones. Defaults to **$1.50/job total** if the Builder page doesn't
  set one (`cost_metering.DEFAULT_MAX_BUDGET_USD` - raised 2026-08-21 from
  $0.75, which production data showed was too tight: most jobs against real
  sites cost $0.90-$1.10+ to actually finish), split across the two phases by
  `split_budget_ceiling()` and shaved by `SDK_BUDGET_SAFETY_MARGIN` before
  being passed to the SDK. When hit, the SDK returns a `ResultMessage` with
  `subtype="error_max_budget_usd"`, which the service surfaces as job status
  `budget_limit_hit` along with whatever partial output was produced.

Poll `GET /jobs/{id}/cost` for just the spend data without pulling the full
generated-code/action-log payload - useful for a Cost Ops dashboard widget
that polls many jobs at once.

**If you can't find `cost_ledger.jsonl`:** it's a relative path, so it gets
created wherever your working directory was when you ran `uvicorn` - not
necessarily this folder. Check the service's startup log (it prints the
resolved absolute path), or call `GET /cost-ledger-location` to get it
programmatically. Also note: nothing is written until at least one job
reaches a terminal status - a job still `running` or `queued` won't have
a ledger entry yet. Jobs that error out *before* the SDK ever returns a
`ResultMessage` (e.g. a credential or MCP connection failure mid-run) still
get a ledger entry - a fallback record with `total_cost_usd: null` and
`subtype: "no_result_message (<status>)"` - so a crashed job is never
silently invisible in the ledger.

## Running it for real

1. **Start the Playwright MCP server** (shared by the worker fleet, HTTP transport
   so workers don't spawn a subprocess per job, `--isolated` so jobs don't
   share browser/cookie state):
   ```
   npx @playwright/mcp@latest --port 8931 --host 0.0.0.0 --isolated
   ```

2. **Install and run the API service**:
   ```
   pip install -r requirements.txt
   export ANTHROPIC_API_KEY=sk-ant-your-key-here
   uvicorn api_service:app --host 0.0.0.0 --port 8000
   ```
   The service validates credentials immediately on startup and will refuse
   to come up if `ANTHROPIC_API_KEY` is missing or malformed - check the
   startup log for `Authenticated via Direct API key (sk-ant...XXXX)`.

3. **Submit a job** (this is what the Builder page's "Generate Scraper" does):
   ```
   curl -X POST http://localhost:8000/jobs \
     -H "Content-Type: application/json" \
     -d '{
       "target_url": "https://example.com/products/",
       "sample_keyword": "example item",
       "search_rules": "Match by product title.",
       "filter_rules": "None.",
       "navigation_rules": "Listing to detail pages.",
       "extraction_rules": "All attribute/value pairs."
     }'
   ```

4. **Watch it work live**:
   ```
   curl http://localhost:8000/jobs/<job_id>/stream
   ```
   Streams each browser action as the agent takes it - this feed is what
   the Detail page's live agent log panel subscribes to.

5. **Test-run the generated script** once the job completes (this is what
   the Detail page's "Test run script" button does):
   ```
   curl -X POST http://localhost:8000/jobs/<job_id>/test-run
   curl http://localhost:8000/jobs/<job_id>/test-run   # poll for status
   curl http://localhost:8000/jobs/<job_id>/sample-output -o sample_output.xlsx
   ```
   This builds a one-row `sample_input.xlsx` from the job's own
   `target_url`/`sample_keyword`, creates an isolated venv under
   `generated_scripts/<job>/.venv`, installs that job's `requirements.txt`
   into it, and actually runs `generated_scraper.py` against the **live
   target site**, capped at `TEST_RUN_MAX_PRODUCTS` (3) rows so it stays
   quick and cheap. It is a real run, not a mock - the same anti-bot/robots
   considerations apply as running the script yourself. See the trust-
   boundary note in `api_service.py` above `_run_test_run()`: this executes
   agent-generated code with dependency isolation only, no process/network
   sandboxing - do not expose these endpoints to untrusted job submitters
   without adding real sandboxing first.

## Guardrails already built in

- **Tool scope**: `allowed_tools` only includes browser actions (navigate,
  snapshot, click, type, network inspection). `disallowed_tools` explicitly
  blocks `Bash`, `Write`, `Edit`, `Read` - the agent can look at a webpage,
  nothing else.
- **Step cap**: a `PreToolUse` hook counts every tool call and denies further
  action once `max_steps` is hit - enforced by the application, not by asking
  the agent nicely.
- **Time cap**: `max_seconds` per job, checked in the message loop.
- **Audit log**: every tool call is logged with timestamp and input before
  it's allowed to run, regardless of outcome.
- **No anti-bot bypass**: the system prompt explicitly instructs the agent to
  flag CAPTCHA/block encounters and stop that inspection thread rather than
  attempt to work around them - consistent with the rest of the platform.
  When recon's anti-bot early-exit fires, the marker it matched (e.g.
  `"captcha"`, `"cloudflare"`) is stored on the job as
  `anti_bot_marker` - a first-class field, not just text inside
  `confidence_notes` - so `scraper_studio.html`'s Dashboard can render a
  `⚠ <marker>` warning in its own column across every job at a glance,
  and the Detail page shows it as a red status tag.

## Where the generated script actually lives on disk

Two places, for two different reasons:

- **`jobs_store.json`** — the code as a string field inside the job history
  blob. This is what survives a restart and backs the Dashboard/API, but
  it's not a file you'd open directly.
- **`generated_scripts/<name>_<short-id>/`** — the *real*, standalone files:
  `generated_scraper.py` and `requirements.txt`, written the moment a job
  completes. This is what you'd actually open in File Explorer, hand to a
  colleague, or run directly - no extraction from JSON required. The folder
  name is built from the job's name (sanitized) plus a short ID fragment for
  uniqueness, e.g. `weekly_price_check_5e1188c4/`.

Both paths are relative to wherever you launched `uvicorn` from - same
caveat as `cost_ledger.jsonl`. The Detail page shows the resolved absolute
path once a job completes ("📁 Also saved on the server at: ...").

## Not yet wired (by design, for this deliverable)

- Real job queue / durable orchestration (swap `asyncio.create_task` in
  `api_service.py` for your Temporal/DAG orchestration)
- `jobs_store.json` is a flat JSON file, not a real database - it's genuinely
  durable across restarts (verified: job history, including names and
  results, survives the process stopping and starting again), but a single
  file isn't a substitute for a proper datastore at scale - swap `JobStore`
  for one backed by your database when you're ready
- `JSONLCostSink` is a local-file stand-in - swap for your actual Cost Ops
  ingestion (see "Cost metering" above)
- `get_secret_from_manager()` in `auth_config.py` is a stub - wire it to
  your actual secrets provider
- Multi-tenant isolation policy beyond `--isolated` browser contexts (network
  egress rules, per-customer worker pools, etc.)
- `/jobs/{id}/test-run` executes agent-generated code with dependency
  isolation only (a per-job venv) - no process, filesystem, or network
  sandboxing. Fine for local/internal use; add a real sandbox (container,
  seccomp, network egress limits) before exposing it to untrusted submitters.
