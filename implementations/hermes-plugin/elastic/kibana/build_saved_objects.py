#!/usr/bin/env python3
"""Generate kibana/hermes-atp-dashboard.ndjson (data view + Lens panels + dashboard).

Run after editing; the test suite checks the committed ndjson is up to date and that every
field a panel uses exists in setup/index-template.json.

    python3 kibana/build_saved_objects.py
"""

from __future__ import annotations

import json
import os

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "hermes-atp-dashboard.ndjson")
DATA_VIEW_ID = "hermes-atp-records"
LAYER = "layer1"
# Saved-object model versions as written by Kibana 9.5.x (read back from a real export). Newer
# Kibana versions migrate these forward on import; without them Kibana would run every legacy
# (7.x-era) Lens migration over this modern format and the import fails.
CORE_MIGRATION_VERSION = "8.8.0"
TYPE_MIGRATION_VERSION = {"index-pattern": "8.0.0", "lens": "10.1.0", "dashboard": "10.3.0"}


# --- Lens column helpers -----------------------------------------------------------------
def date_histogram():
    return {"label": "@timestamp", "dataType": "date", "operationType": "date_histogram",
            "sourceField": "@timestamp", "isBucketed": True, "scale": "interval",
            "params": {"interval": "auto", "includeEmptyRows": True, "dropPartials": False}}


def terms(field, label, order_col, size=10):
    return {"label": label, "customLabel": True, "dataType": "string", "operationType": "terms",
            "sourceField": field, "isBucketed": True, "scale": "ordinal",
            "params": {"size": size, "orderBy": {"type": "column", "columnId": order_col},
                       "orderDirection": "desc", "otherBucket": True, "missingBucket": False,
                       "parentFormat": {"id": "terms"}}}


def count(label="Count"):
    return {"label": label, "customLabel": True, "dataType": "number", "operationType": "count",
            "sourceField": "___records___", "isBucketed": False, "scale": "ratio",
            "params": {"emptyAsNull": True}}


def metric(op, field, label, **params):
    return {"label": label, "customLabel": True, "dataType": "number", "operationType": op,
            "sourceField": field, "isBucketed": False, "scale": "ratio",
            "params": {"emptyAsNull": True, **params}}


def lens(obj_id, title, vis_type, columns, visualization, query="", description=""):
    return {
        "type": "lens", "id": obj_id,
        "attributes": {
            "title": title, "description": description, "visualizationType": vis_type,
            "state": {
                "datasourceStates": {"formBased": {"layers": {LAYER: {
                    "columns": dict(columns), "columnOrder": [c for c, _ in columns], "incompleteColumns": {}}}}},
                "visualization": visualization,
                "query": {"query": query, "language": "kuery"},
                "filters": [],
                "internalReferences": [], "adHocDataViews": {},
            },
        },
        "references": [{"type": "index-pattern", "id": DATA_VIEW_ID,
                        "name": f"indexpattern-datasource-layer-{LAYER}"}],
    }


def xy(series_type, x, accessors, split=None, legend=True):
    layer = {"layerId": LAYER, "layerType": "data", "seriesType": series_type, "xAccessor": x,
             "accessors": accessors, "position": "top", "showGridlines": False}
    if split:
        layer["splitAccessor"] = split
    return {"legend": {"isVisible": legend, "position": "right"}, "valueLabels": "hide",
            "preferredSeriesType": series_type, "layers": [layer]}


def table(cols):
    return {"layerId": LAYER, "layerType": "data", "columns": [{"columnId": c} for c in cols]}


def metric_vis(col):
    return {"layerId": LAYER, "layerType": "data", "metricAccessor": col}


# --- panels --------------------------------------------------------------------------------
PANELS = [
    lens("hermes-atp-sessions-over-time", "Sessions over time", "lnsXY",
         [("x", date_histogram()), ("y", metric("unique_count", "session_id", "Sessions"))],
         xy("bar_stacked", "x", ["y"], legend=False),
         description="Distinct Hermes sessions (incl. subagent sessions) with records in each interval."),
    lens("hermes-atp-model-calls", "Model calls by model", "lnsXY",
         [("x", date_histogram()), ("model", terms("model", "Model", "n")), ("n", count("Model calls"))],
         xy("bar_stacked", "x", ["n"], split="model"),
         query='event : "post_api_request"',
         description="Completed model API calls (post_api_request), split by model."),
    lens("hermes-atp-model-latency", "Model call latency (ms)", "lnsXY",
         [("x", date_histogram()), ("p50", metric("median", "api_duration_ms", "Median")),
          ("p95", metric("percentile", "api_duration_ms", "p95", percentile=95))],
         xy("line", "x", ["p50", "p95"]),
         query='event : "post_api_request"',
         description="api_duration_ms of completed model API calls: median and 95th percentile."),
    lens("hermes-atp-tool-calls", "Tool calls by name", "lnsXY",
         [("tool", terms("tool_name", "Tool", "n", size=20)), ("n", count("Tool calls"))],
         xy("bar_horizontal", "tool", ["n"], legend=False),
         query='event : "post_tool_call"',
         description="Completed tool calls (post_tool_call) by tool name."),
    lens("hermes-atp-subagent-links", "Subagent links (parent → child session)", "lnsDatatable",
         [("parent", terms("parent_session_id", "Parent session", "n", size=100)),
          ("child", terms("session_id", "Child session", "n", size=100)),
          ("n", count("Records"))],
         table(["parent", "child", "n"]),
         query="is_session_start : true and parent_session_id : *",
         description="First record of each subagent session carries hermes.parentSessionId."),
    lens("hermes-atp-verify-failures-count", "Verify failures", "lnsMetric",
         [("n", count("Records failing verification"))],
         metric_vis("n"),
         query="verify.valid : false",
         description="Records whose ingest-time verification failed. Authoritative check: atp_cli.py verify."),
    lens("hermes-atp-verify-failures", "Verify failures by kind and session", "lnsDatatable",
         [("status", terms("verify.status", "Verify status", "n")),
          ("session", terms("session_id", "Session", "n", size=50)),
          ("n", count("Records"))],
         table(["status", "session", "n"]),
         query="verify.valid : false",
         description="tampered / missing / out-of-order / body-mismatch, per session."),
]

LAYOUT = [  # (x, y, w, h) on Kibana's 48-column grid
    (0, 0, 32, 12), (32, 0, 16, 12),
    (0, 12, 24, 14), (24, 12, 24, 14),
    (0, 26, 24, 15), (24, 26, 24, 15),
    (0, 41, 48, 14),
]
ORDER = ["hermes-atp-sessions-over-time", "hermes-atp-verify-failures-count", "hermes-atp-model-calls",
         "hermes-atp-model-latency", "hermes-atp-tool-calls", "hermes-atp-subagent-links",
         "hermes-atp-verify-failures"]


def build():
    data_view = {"type": "index-pattern", "id": DATA_VIEW_ID,
                 "attributes": {"title": "hermes-atp-records-*", "name": "Hermes ATP records",
                                "timeFieldName": "@timestamp"},
                 "references": []}
    by_id = {p["id"]: p for p in PANELS}
    panels, refs = [], []
    for i, (oid, (x, y, w, h)) in enumerate(zip(ORDER, LAYOUT)):
        pid = f"p{i + 1}"
        panels.append({"type": "lens", "panelIndex": pid, "gridData": {"x": x, "y": y, "w": w, "h": h, "i": pid},
                       "embeddableConfig": {"enhancements": {}}, "panelRefName": f"panel_{pid}"})
        refs.append({"name": f"{pid}:panel_{pid}", "type": "lens", "id": by_id[oid]["id"]})
    dashboard = {
        "type": "dashboard", "id": "hermes-atp-overview",
        "attributes": {
            "title": "Hermes ATP overview",
            "description": "Hermes ATP recorder: sessions, model calls and latency, tool calls, subagent links, "
                           "verify failures. Ingest-time verify is provisional; run atp_cli.py verify for the verdict.",
            "panelsJSON": json.dumps(panels, separators=(",", ":")),
            "optionsJSON": json.dumps({"useMargins": True, "syncColors": False, "syncCursor": True,
                                       "syncTooltips": False, "hidePanelTitles": False}, separators=(",", ":")),
            "timeRestore": True, "timeFrom": "now-7d", "timeTo": "now", "refreshInterval": {"pause": True, "value": 60000},
            "kibanaSavedObjectMeta": {"searchSourceJSON": json.dumps(
                {"query": {"query": "", "language": "kuery"}, "filter": []}, separators=(",", ":"))},
        },
        "references": refs,
    }
    objs = [data_view] + [by_id[o] for o in ORDER] + [dashboard]
    for o in objs:
        o["coreMigrationVersion"] = CORE_MIGRATION_VERSION
        o["typeMigrationVersion"] = TYPE_MIGRATION_VERSION[o["type"]]
        o["managed"] = False
    return "".join(json.dumps(o, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n" for o in objs)


if __name__ == "__main__":
    with open(OUT, "w", encoding="utf-8") as f:
        f.write(build())
    print(f"wrote {OUT}")
