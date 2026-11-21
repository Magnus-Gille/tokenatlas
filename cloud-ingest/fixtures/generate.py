#!/usr/bin/env python3
"""Regenerate fixtures/digest-valid.ndjson from the production hook's own digest_row().

The transcript rows below follow the shape of a real cloud transcript (main session, a subagent, a workflow subagent, an
advisor iteration, 1h cache split, a user prompt). Each is reduced by scripts/cloud_digest.digest_row and then given the wire
fields the slice-3 hook will send: the enclosing `session` and `_path`. Nothing here is a real prompt or id.
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
import cloud_digest  # noqa: E402

SESSION = "01J9psAFeuAEj8JwHLu6UTQe"
USAGE = {"input_tokens": 6, "output_tokens": 412, "cache_read_input_tokens": 51234, "cache_creation_input_tokens": 3100,
         "cache_creation": {"ephemeral_5m_input_tokens": 100, "ephemeral_1h_input_tokens": 3000},
         "service_tier": "standard", "speed": "fast", "server_tool_use": {"web_search_requests": 0},
         "iterations": [{"input_tokens": 6, "output_tokens": 300, "type": "message"},
                        {"input_tokens": 2, "output_tokens": 112, "type": "advisor_message", "model": "claude-fable-5-2"}]}


def asst(uuid, ts, req, sid, model="claude-sonnet-5-5", **extra):
    return {"type": "assistant", "uuid": uuid, "timestamp": ts, "sessionId": sid, "cwd": "/home/user/tokenatlas",
            "gitBranch": "claude/project-thread-4rqpn7", "requestId": req, "entrypoint": "remote_projects",
            "version": "2.1.329", "effort": "high",
            "message": {"id": "msg_" + req, "role": "assistant", "model": model, "usage": USAGE, "stop_reason": "tool_use",
                        "content": [{"type": "text", "text": "SECRET ANSWER"}]}, **extra}


prompt = {"type": "user", "uuid": "u0", "timestamp": "2026-11-21T21:46:00.000Z", "sessionId": SESSION,
          "cwd": "/home/user/tokenatlas", "message": {"role": "user", "content": "SECRET PROMPT"}}
wire = [
    ("", prompt),
    ("", asst("a1", "2026-11-21T21:46:05.123Z", "req_main1", SESSION)),
    ("", asst("a2", "2026-11-21T21:46:09.000Z", "req_main1", SESSION)),  # second content block of the same request
    # subagent rows have no usable sessionId of their own: they carry the agent id and parent session
    ("subagents/agent-a1b2c3d4.jsonl", asst("s1", "2026-11-21T21:47:00Z", "req_sub1", "agent-session", agentId="a1b2c3d4",
                                            parentSessionId=SESSION, isSidechain=True, attributionAgent="Explore")),
    ("subagents/workflows/wf_r9x/agent-e5f6.jsonl", asst("w1", "2026-11-21T21:48:00Z", "req_wf1", "agent-session",
                                                         agentId="e5f6", parentSessionId=SESSION, isSidechain=True)),
    ("", asst("a3", "2026-11-21T21:49:00Z", "req_main2", SESSION, model="claude-opus-5-5[1m]")),
]
lines = []
for rel, row in wire:
    digest = cloud_digest.digest_row(row)
    assert digest, row
    out = {**digest, "session": SESSION}
    if rel:
        out["_path"] = rel
    lines.append(json.dumps(out, separators=(",", ":")))
text = "\n".join(lines) + "\n"
assert "SECRET" not in text
(Path(__file__).parent / "digest-valid.ndjson").write_text(text)
print(f"wrote {len(lines)} rows")
