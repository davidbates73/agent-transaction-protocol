#!/usr/bin/env python3
"""ATP command-line tool (Python).

    python3 atp_cli.py verify <bundle.json> --keys <keys.json> [--mode full|tip|bounded|redacted] [--depth N]
    python3 atp_cli.py nodeid <node.json>
    python3 atp_cli.py interop                 # rebuild Appendix A and compare with the Node.js bundle
"""

import argparse
import hashlib
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import atp  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
EXAMPLE = os.path.join(HERE, "..", "..", "examples", "appendix-a")


def build_appendix_a():
    """Build the draft's Appendix A chain exactly as implementations/node/bin/demo.js does."""
    issuers = {}
    for issuer_id, key_id in (("platform.example", "platform-2026-04"), ("mcp-broker.example", "broker-2026-04"), ("tool-crm.example", "crm-2026-04")):
        seed = hashlib.sha256(f"atp-demo-seed:{issuer_id}:{key_id}".encode()).digest()
        issuers[issuer_id] = {"issuerId": issuer_id, "keyId": key_id, "seed": seed}
    P = atp.load_json(os.path.join(EXAMPLE, "payloads.json"))
    H = {k: atp.payload_hash(v) for k, v in P.items()}
    H["synthesisInput"] = atp.payload_hash({"result": H["result"], "selection": H["selection"]})
    scope = "wf-8f3a1b"
    bob = {"actorId": "psn:9c3a7e4f-bob", "authContext": "saml:corp-idp"}
    orch = {"agentId": "orchestrator-agent", "version": "1.3.0"}

    def sign(iid, fields):
        i = issuers[iid]
        return atp.create_node({"scope": scope, "issuer": {"issuerId": i["issuerId"], "keyId": i["keyId"]}, **fields}, i["seed"])

    n1 = sign("platform.example", {"timestamp": "2026-04-23T12:58:00Z", "agent": orch, "actor": bob, "action": {"type": "atp:request", "subtype": "tool_catalog_query", "inputHash": H["catalogQuery"]}, "parents": []})
    n2 = sign("mcp-broker.example", {"timestamp": "2026-04-23T12:58:00.110Z", "agent": {"agentId": "mcp-catalog-service", "version": "2.1.0"}, "action": {"type": "atp:completion", "subtype": "tool_catalog_response", "inputHash": H["catalogQuery"], "outputHash": H["catalog"]}, "parents": [n1["nodeId"]]})
    n3 = sign("platform.example", {"timestamp": "2026-04-23T12:58:00.240Z", "agent": orch, "actor": bob, "action": {"type": "atp:decision", "subtype": "tool_selection_decision", "inputHash": H["catalog"], "outputHash": H["selection"]}, "parents": [n2["nodeId"]]})
    n4 = sign("platform.example", {"timestamp": "2026-04-23T12:58:00.380Z", "agent": orch, "actor": bob, "action": {"type": "atp:request", "subtype": "tool_invocation_request", "inputHash": H["invocation"]}, "parents": [n3["nodeId"]]})
    n5 = sign("tool-crm.example", {"timestamp": "2026-04-23T12:58:00.610Z", "agent": {"agentId": "crm-lookup-service", "version": "5.0.2"}, "action": {"type": "atp:completion", "subtype": "tool_execution", "inputHash": H["invocation"], "outputHash": H["result"]}, "parents": [n4["nodeId"]]})
    n6 = sign("mcp-broker.example", {"timestamp": "2026-04-23T12:58:00.630Z", "agent": {"agentId": "mcp-relay-service", "version": "2.1.0"}, "action": {"type": "atp:relay", "subtype": "tool_execution_response", "inputHash": H["result"], "outputHash": H["result"]}, "parents": [n5["nodeId"]]})
    n7 = sign("platform.example", {"timestamp": "2026-04-23T12:58:00.820Z", "agent": orch, "actor": bob, "action": {"type": "atp:decision", "subtype": "decision_synthesis", "inputHash": H["synthesisInput"], "outputHash": H["final"]}, "parents": [n6["nodeId"], n3["nodeId"]]})
    return [n1, n2, n3, n4, n5, n6, n7]


def cmd_verify(a):
    bundle = atp.load_json(a.bundle)
    nodes = bundle if isinstance(bundle, list) else bundle["nodes"]
    boundary = {"depth": a.depth} if a.mode == "bounded" else None
    result = atp.validate(atp.tips(nodes) if not a.target else [a.target], atp.index_nodes(nodes),
                          atp.key_resolver(atp.load_json(a.keys)), mode=a.mode, boundary=boundary,
                          withheld=bundle.get("withheldNodeIds", []) if isinstance(bundle, dict) else [])
    print(json.dumps(result, indent=2))
    return 2 if result["invalid"] else 0


def cmd_nodeid(a):
    node = atp.load_json(a.node)
    print(atp.canonicalize(atp.node_content(node)))
    print(atp.compute_node_id(node))
    return 0


def cmd_interop(_a):
    print("Cross-implementation check: Python rebuild vs Node.js bundle (examples/appendix-a)\n")
    mine = build_appendix_a()
    theirs = atp.load_json(os.path.join(EXAMPLE, "bundle.json"))["nodes"]
    keys = atp.key_resolver(atp.load_json(os.path.join(EXAMPLE, "keys.json")))
    ok = len(mine) == len(theirs)
    for i, (m, t) in enumerate(zip(mine, theirs), 1):
        same = m == t
        ok = ok and same
        print(f"  N{i}  {m['nodeId'][:16]}…  nodeId {'==' if m['nodeId'] == t['nodeId'] else '!='}  "
              f"signature {'==' if m['signature'] == t['signature'] else '!='}  {'PASS' if same else 'FAIL'}")
    r = atp.validate([theirs[-1]["nodeId"]], atp.index_nodes(theirs), keys, mode="full")
    full = len(r["verified"]) == 7 and not r["invalid"]
    print(f"\n  Python full-recursive validation of the Node.js bundle: {len(r['verified'])}/7 verified, "
          f"relay N6 = {list(r.get('relayFidelity', {}).values())}  {'PASS' if full else 'FAIL'}")
    ok = ok and full
    print(f"\n{'Interop OK: byte-identical nodes and signatures' if ok else 'Interop FAILED'}")
    return 0 if ok else 1


def main():
    p = argparse.ArgumentParser(description="ATP command-line tool (Python)")
    sub = p.add_subparsers(dest="cmd", required=True)
    v = sub.add_parser("verify"); v.add_argument("bundle"); v.add_argument("--keys", required=True)
    v.add_argument("--mode", default="full", choices=["full", "tip", "bounded", "redacted"])
    v.add_argument("--depth", type=int, default=1); v.add_argument("--target")
    v.set_defaults(fn=cmd_verify)
    n = sub.add_parser("nodeid"); n.add_argument("node"); n.set_defaults(fn=cmd_nodeid)
    i = sub.add_parser("interop"); i.set_defaults(fn=cmd_interop)
    a = p.parse_args()
    sys.exit(a.fn(a))


if __name__ == "__main__":
    main()
