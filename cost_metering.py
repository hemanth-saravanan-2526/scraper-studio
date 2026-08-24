"""
Cost metering for the Inspection Agent Service.

Reads the real fields the Claude Agent SDK's ResultMessage carries:
  total_cost_usd, usage (dict), model_usage (dict), num_turns,
  duration_ms, duration_api_ms, session_id, is_error, subtype

One ResultMessage is emitted per query() call / ClaudeSDKClient session -
exactly one per inspection job in this service's design (one client session
per job), so no cross-job double-counting risk here. If you later move to
multi-turn sessions per job (e.g. resuming a session for re-inspection),
sum total_cost_usd across each call rather than trusting a session-level
total - the SDK does not track that for you.

This also uses max_budget_usd on ClaudeAgentOptions as a hard SDK-enforced
ceiling, independent of and in addition to the step-count hook in
inspection_agent_service.py. Two different failure modes, two different
guards: step cap protects against a runaway loop of cheap calls, budget cap
protects against a smaller number of unexpectedly expensive ones.
"""

import json
from abc import ABC, abstractmethod
from dataclasses import dataclass, asdict
from datetime import datetime
from pathlib import Path


@dataclass
class CostRecord:
    job_id: str
    session_id: str
    recorded_at: str
    subtype: str                    # "success" | "error_max_turns" | "error_max_budget_usd" | ...
    is_error: bool
    total_cost_usd: float | None
    input_tokens: int | None
    output_tokens: int | None
    cache_creation_input_tokens: int | None
    cache_read_input_tokens: int | None
    num_turns: int
    duration_ms: int
    duration_api_ms: int
    model_usage: dict | None        # per-model breakdown, as returned by the SDK
    phase: str = ""                 # "recon" | "codegen" | "" (pre-refactor records
                                     # have no phase - default keeps old ledger lines
                                     # loadable). See inspection_agent_service.py's
                                     # two-phase pipeline for what these mean.


def build_cost_record(job_id: str, result_message, phase: str = "") -> CostRecord:
    """
    result_message is a claude_agent_sdk.ResultMessage instance. Pulled out
    as a plain function (not a method) so it's easy to unit test against a
    stub object with the same field names.
    """
    usage = result_message.usage or {}
    return CostRecord(
        job_id=job_id,
        session_id=result_message.session_id,
        recorded_at=datetime.now().isoformat(timespec="seconds"),
        subtype=result_message.subtype,
        is_error=result_message.is_error,
        total_cost_usd=result_message.total_cost_usd,
        input_tokens=usage.get("input_tokens"),
        output_tokens=usage.get("output_tokens"),
        cache_creation_input_tokens=usage.get("cache_creation_input_tokens"),
        cache_read_input_tokens=usage.get("cache_read_input_tokens"),
        num_turns=result_message.num_turns,
        duration_ms=result_message.duration_ms,
        duration_api_ms=result_message.duration_api_ms,
        model_usage=result_message.model_usage,
        phase=phase,
    )


def build_fallback_cost_record(job_id: str, status: str, phase: str = "") -> CostRecord:
    """
    Used when a job never produced a ResultMessage at all - e.g. it errored
    before the SDK could finish a turn, or the connection to the MCP server
    dropped mid-session. Without this, such jobs leave zero trace in the
    cost ledger, which looks indistinguishable from "job never ran at all."
    total_cost_usd is None (genuinely unknown, not zero) - don't let this be
    summed as $0 in a dashboard rollup without checking for None first.
    """
    return CostRecord(
        job_id=job_id,
        session_id="unknown",
        recorded_at=datetime.now().isoformat(timespec="seconds"),
        subtype=f"no_result_message ({status})",
        is_error=True,
        total_cost_usd=None,
        input_tokens=None,
        output_tokens=None,
        cache_creation_input_tokens=None,
        cache_read_input_tokens=None,
        num_turns=0,
        duration_ms=0,
        duration_api_ms=0,
        model_usage=None,
        phase=phase,
    )


def combine_cost_records(records: list[CostRecord]) -> dict:
    """
    Merge per-phase CostRecords (recon + codegen - see
    inspection_agent_service.run_inspection_job) into a single flat dict
    that keeps the same top-level keys the original single-phase design had
    (total_cost_usd, num_turns, ...), so existing consumers - api_service.py's
    JobRecord.cost, scraper_studio.html's detail-cost readout - keep working
    unchanged. The full per-phase breakdown is preserved under "phases" for
    anyone who wants to see where the spend actually went (recon/browser-
    driving vs. codegen), which is exactly the visibility that was missing
    before this refactor.
    """
    if not records:
        return {
            "job_id": None, "session_id": None, "recorded_at": None,
            "subtype": "", "is_error": False, "total_cost_usd": None,
            "input_tokens": None, "output_tokens": None,
            "cache_creation_input_tokens": None, "cache_read_input_tokens": None,
            "num_turns": 0, "duration_ms": 0, "duration_api_ms": 0,
            "model_usage": None, "phases": [],
        }

    def _sum(attr):
        vals = [getattr(r, attr) for r in records if getattr(r, attr) is not None]
        return sum(vals) if vals else None

    return {
        "job_id": records[0].job_id,
        "session_id": [r.session_id for r in records],
        "recorded_at": records[-1].recorded_at,
        "subtype": "; ".join(f"{r.phase or 'unknown'}={r.subtype}" for r in records),
        "is_error": any(r.is_error for r in records),
        "total_cost_usd": _sum("total_cost_usd"),
        "input_tokens": _sum("input_tokens"),
        "output_tokens": _sum("output_tokens"),
        "cache_creation_input_tokens": _sum("cache_creation_input_tokens"),
        "cache_read_input_tokens": _sum("cache_read_input_tokens"),
        "num_turns": sum(r.num_turns for r in records),
        "duration_ms": sum(r.duration_ms for r in records),
        "duration_api_ms": sum(r.duration_api_ms for r in records),
        "model_usage": None,  # see "phases" for per-phase model_usage instead
        "phases": [asdict(r) for r in records],
    }


# --------------------------------------------------------------------------
# Cost Ops sink - pluggable so this drops straight into your existing
# Operations & Cost tier without this module needing to know what your
# billing pipeline looks like.
# --------------------------------------------------------------------------

class CostSink(ABC):
    @abstractmethod
    def record(self, cost_record: CostRecord) -> None: ...


class JSONLCostSink(CostSink):
    """
    Default sink: appends one JSON line per job to a local ledger file.
    This is a stand-in for your real Cost Ops ingestion point - swap this
    class for one that writes to your cost/billing datastore (a Kafka
    topic, a Postgres table, the Usage and Cost API, etc.) without touching
    the calling code in inspection_agent_service.py.
    """

    def __init__(self, path: str = "cost_ledger.jsonl"):
        self.path = Path(path)

    def record(self, cost_record: CostRecord) -> None:
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(asdict(cost_record)) + "\n")


class StdoutCostSink(CostSink):
    """Useful for local dev / demos - prints instead of writing a file."""

    def record(self, cost_record: CostRecord) -> None:
        print(f"[cost] job={cost_record.job_id} "
              f"cost=${cost_record.total_cost_usd or 0:.4f} "
              f"turns={cost_record.num_turns} "
              f"tokens(in/out)={cost_record.input_tokens}/{cost_record.output_tokens}")


# --------------------------------------------------------------------------
# Per-job budget ceiling helper
# --------------------------------------------------------------------------

DEFAULT_MAX_BUDGET_USD = 1.50  # raised from $0.75 (2026-08-21) - production data
                                # (cost_ledger.jsonl, single-phase era) showed real jobs
                                # against real sites commonly needed $0.90-$1.10+ to
                                # finish; $0.75 was causing most jobs to hit the budget
                                # cap before producing usable output. $1.50 gives real
                                # headroom above that observed range. Split across both
                                # phases per job - see split_budget_ceiling() below.


def resolve_budget_ceiling(job_max_budget_usd: float | None) -> float:
    """
    Centralizes the default so both the Builder UI's "advanced settings"
    and any backend-triggered (autonomous re-inspection) jobs share one
    source of truth for what "unset" means.
    """
    if job_max_budget_usd is None or job_max_budget_usd <= 0:
        return DEFAULT_MAX_BUDGET_USD
    return job_max_budget_usd


# --------------------------------------------------------------------------
# Two-phase budget split (2026-08-20 refactor)
# --------------------------------------------------------------------------
# Production data (cost_ledger.jsonl) showed jobs consistently costing
# $0.77-$1.10 against a $0.75 default, dying via error_max_budget_usd with
# nothing delivered - the SDK-side check only fires *between* turns, so a
# single expensive turn can blow past the ceiling before it's caught.
#
# Two mitigations, both applied where the ceiling is actually handed to the
# SDK (see inspection_agent_service.run_inspection_job):
#   1. Split the total job budget across the two phases instead of handing
#      the whole thing to one long session - recon (the more variable,
#      multi-turn, browser-driving phase) gets the larger share by default.
#   2. Shave a safety margin off whatever ceiling is actually passed to the
#      SDK, so the last, cap-triggering turn is smaller relative to what's
#      left - reducing (not eliminating - a single huge turn can still
#      overshoot) how far actual spend runs past the job's intended budget.

DEFAULT_RECON_BUDGET_SHARE = 0.7   # recon is the more expensive, more variable
                                    # phase (multi-turn browser driving) - give
                                    # it most of the budget; codegen is closer
                                    # to a single-shot completion.
SDK_BUDGET_SAFETY_MARGIN = 0.85    # ceiling actually passed to ClaudeAgentOptions
                                    # is this fraction of the resolved per-phase
                                    # budget, to leave headroom against the
                                    # check-between-turns overshoot above.


def split_budget_ceiling(
    total_budget_usd: float, recon_share: float = DEFAULT_RECON_BUDGET_SHARE
) -> tuple[float, float]:
    """Returns (recon_budget, codegen_budget) summing to total_budget_usd."""
    recon_budget = total_budget_usd * recon_share
    codegen_budget = total_budget_usd - recon_budget
    return recon_budget, codegen_budget
