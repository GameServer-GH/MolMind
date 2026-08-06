#!/usr/bin/env python3
"""Validate live SCP installation, cache, staging and optional long jobs.

The API key is read exclusively from SCP_HUB_API_KEY and is never printed.
"""
from __future__ import annotations
import argparse, json, os, tempfile, time
from pathlib import Path
from agent.memory import FileRunStore
from agent.runtime.loop import AgentRuntime
from plugins.scp_hub.cache import SCPQueryCache
from plugins.scp_hub.credentials import get_api_key

def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--long-job", action="store_true")
    parser.add_argument("--long-timeout", type=int, default=600)
    args = parser.parse_args()
    if not get_api_key()[0]:
        raise SystemExit("SCP_HUB_API_KEY is not configured")
    with tempfile.TemporaryDirectory(prefix="molmind-scp-e2e-") as raw:
        root = Path(raw); runtime = AgentRuntime(store=FileRunStore(root=root / "runs")); runtime.scp.cache = SCPQueryCache(root / "scp")
        session = runtime.create_session(client_id="scp-e2e-validation")
        installed = {}
        for skill_id in ("mechanism_research", "literature_research", "validation_protocol"):
            session = runtime.install_scp_skill(session.session_id, skill_id)
            installed[skill_id] = len(session.installed_scp_skills[skill_id]["tools"])
        tool_id = "scp:Scholar-KG:health_check"
        first = runtime.scp.call(session, tool_id, {}, allow_live=True, stage=True, molecule_id="E2E-TEST")
        second = runtime.scp.call(session, tool_id, {}, allow_live=True)
        calls = list(reversed(runtime.scp.cache.list_calls(session_id=session.session_id)))
        staging = runtime.scp.cache.list_staging(session_id=session.session_id)
        report = {"installed_tools": installed, "live_call_status": first.status, "cache_replay_same_response": first.response_hash == second.response_hash, "audit_statuses": [item["status"] for item in calls], "staging_count": len(staging), "staging_isolated": not runtime.scp.cache.list_staging(session_id="foreign")}
        if args.long_job:
            long_tool = "scp:Thoth-Plan:protocol_generation"
            job = runtime.scp_jobs.submit(lambda: runtime.scp.call(session, long_tool, {"user_prompt":"Draft a concise, non-executed PCR validation protocol outline for research review."}, allow_live=True), session_id=session.session_id, skill_id="validation_protocol", tool_id=long_tool)
            deadline = time.monotonic() + max(1, args.long_timeout)
            current = job
            while current["status"] in {"queued", "running"} and time.monotonic() < deadline:
                time.sleep(2); current = runtime.scp_jobs.get(job["job_id"], session_id=session.session_id) or current
            report["long_job"] = {"status": current["status"], "has_result": bool(current.get("result")), "error_code": current.get("error_code", "")}
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0 if report["live_call_status"] == "hit" and report["cache_replay_same_response"] and report["staging_count"] == 1 and (not args.long_job or report["long_job"]["status"] == "completed") else 1

if __name__ == "__main__": raise SystemExit(main())
