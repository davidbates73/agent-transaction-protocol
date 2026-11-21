#!/usr/bin/env python3
"""Generate kibana/hermes-atp-lifecycle.ndjson: the "Hermes ATP request lifecycle" dashboard.

One request = a parent Hermes session plus the subagent sessions it delegated to. Panels:
  * options-list controls (session, turn, event) that filter every panel;
  * rollup tiles (Lens): tokens, elapsed, model time, tool time, errors, retries, verify pass rate;
  * session tree (Vega): parent session -> child subagent sessions, linked by the
    parent_session_id / parent_turn_id / parent_subagent_id the recorder writes on each child
    session's FIRST record (seq 0);
  * step waterfall (Vega-Lite): one bar per completed step (model call, tool call, API error),
    rows ordered by session start then seq, plus a marker for any step that never completed;
  * step table (saved Discover session): one row per record, sorted session_id, seq.

Everything is free Basic-license Kibana: Lens, Vega/Vega-Lite visualizations, controls, Discover.
The Vega specs query Elasticsearch with "%context%": true and "%timefield%", so the dashboard's
time range, query, filters and controls apply to them like to every other panel.

Stable ids; the data view object is the same one hermes-atp-dashboard.ndjson carries, so either
file imports on its own. Run after editing (a test checks the committed ndjson is up to date):

    python3 kibana/build_lifecycle_dashboard.py
"""

from __future__ import annotations

import json
import os
import uuid

import build_saved_objects as base

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "hermes-atp-lifecycle.ndjson")
INDEX = "hermes-atp-records-*"
DASHBOARD_ID = "hermes-atp-lifecycle"
DATA_VIEW_ID = base.DATA_VIEW_ID
LAYER = base.LAYER
# Model versions as written by Kibana 9.5.x (read back from a real export), as in build_saved_objects.
TYPE_MIGRATION_VERSION = dict(base.TYPE_MIGRATION_VERSION, visualization="8.5.0", search="10.13.0")
# uuid5 namespace for the ids Kibana wants as UUIDs (control ids, the Discover tab id).
NS = uuid.uuid5(uuid.NAMESPACE_URL, "https://github.com/davidbates73/agent-transaction-protocol/hermes-atp-lifecycle")


def stable_uuid(name: str) -> str:
    return str(uuid.uuid5(NS, name))


def compact(obj) -> str:
    return json.dumps(obj, separators=(",", ":"), ensure_ascii=False)


# --- shared ES query fragments ---------------------------------------------------------------
# A failed step: an API error record, or a completed tool call whose status is not "ok".
ERROR_FILTER = {"bool": {"should": [
    {"term": {"event": "api_request_error"}},
    {"bool": {"filter": [{"term": {"event": "post_tool_call"}}], "must_not": [{"term": {"status": "ok"}}]}},
], "minimum_should_match": 1}}
ERROR_KQL = 'event : "api_request_error" or (event : "post_tool_call" and not status : "ok")'
# A retry: an API attempt Hermes made after a failed one (retry_count is the attempt's index).
RETRY_KQL = 'event : "pre_api_request" and retry_count > 0'


def es_url(body):
    return {"%context%": True, "%timefield%": "@timestamp", "index": INDEX, "body": body}


# --- session labels (shared by the tree and the waterfall) -------------------------------------
# A label is "<role> · …<last 6 chars of session_id>": role "parent" for a top-level session,
# "sub" for a subagent, "sub²" / "sub³" for nested ones, "sub⁴⁺" deeper. The role comes from the
# parent_session_id links in the time range, NOT from the dashboard filters, so a session keeps
# its label when you filter down to it. The full session id is in every tooltip.
LABEL_TAIL = 6
MAX_LABEL_DEPTH = 4
ROLE_SUFFIX = ["", "²", "³", "⁴⁺"]
# Links for the labels: every subagent session's first record (it carries parent_session_id).
# Time range only (no %context%), so dashboard filters don't change a session's label.
LINKS_BODY = {"size": 10000, "_source": ["session_id", "parent_session_id"],
              "query": {"bool": {"filter": [
                  {"term": {"is_session_start": True}}, {"exists": {"field": "parent_session_id"}},
                  {"range": {"@timestamp": {"%timefilter%": True}}}]}}}
LINKS_URL = {"index": INDEX, "body": LINKS_BODY}
# The same links as an aggregation (for the Vega-Lite waterfall, which can take only one ES
# request): global (escapes the dashboard query/filters), then the time range, then child -> parent.
LINKS_AGG = {"global": {}, "aggs": {"f": {"filter": LINKS_BODY["query"], "aggs": {"s": {
    "terms": {"field": "session_id", "size": 10000},
    "aggs": {"p": {"terms": {"field": "parent_session_id", "size": 1}}}}}}}}


def up_expr(sid: str) -> str:
    """Vega expression: the parent of session `sid` from the flattened links (lk keys, lp parents)."""
    i = f"indexof(datum.lk, {sid})"
    return f"({sid} && {i} >= 0 && datum.lp[{i}].buckets.length ? datum.lp[{i}].buckets[0].key : null)"


def role_expr(level: str) -> str:
    """Vega expression: role for a session with `level` known ancestors."""
    return (f"({level} === 0 ? 'parent' : 'sub' + "
            f"{json.dumps(ROLE_SUFFIX, ensure_ascii=False).replace(chr(34), chr(39))}[min({level}, {MAX_LABEL_DEPTH}) - 1])")


def short_expr(sid: str) -> str:
    return f"(length({sid}) > {LABEL_TAIL} ? '…' + slice({sid}, -{LABEL_TAIL}) : {sid})"


def label_expr(sid: str, level: str) -> str:
    return f"{role_expr(level)} + ' · ' + {short_expr(sid)}"


def session_label(session_id: str, parents: dict) -> str:
    """Python twin of label_expr (used by the tests): parents maps child -> parent session."""
    level, cur = 0, session_id
    while level < MAX_LABEL_DEPTH and parents.get(cur):
        cur = parents[cur]
        level += 1
    role = "parent" if level == 0 else "sub" + ROLE_SUFFIX[level - 1]
    tail = "…" + session_id[-LABEL_TAIL:] if len(session_id) > LABEL_TAIL else session_id
    return f"{role} · {tail}"


def level_expr(*ps: str) -> str:
    return " + ".join(f"({p} ? 1 : 0)" for p in ps)


# --- session tree (Vega) -----------------------------------------------------------------------


def session_tree_spec():
    return {
        "$schema": "https://vega.github.io/schema/vega/v5.json",
        "description": "Hermes ATP session tree: each request's parent session with the subagent sessions it "
                       "delegated to. Node size = tokens, colour = time in model + tool calls, red ring = "
                       "the session has a failed step. Sessions whose parent is outside the current "
                       "filters/time range (or whose first record is filtered out) hang off the left edge.",
        "autosize": {"type": "fit", "contains": "padding"},
        "padding": 8,
        "signals": [
            {"name": "labelSpace", "update": "min(360, width * 0.45)"},
            {"name": "maxMs", "update": "length(data('shown')) ? extent(pluck(data('shown'), 'ms'))[1] : 0"},
            {"name": "maxTokens", "update": "length(data('shown')) ? extent(pluck(data('shown'), 'n_tokens'))[1] : 0"},
            {"name": "maxDepth", "update": "length(data('shown')) ? extent(pluck(data('shown'), 'depth'))[1] : 1"},
        ],
        "data": [
            {
                "name": "linkdocs", "url": LINKS_URL, "format": {"property": "hits.hits"},
                "transform": [{"type": "formula", "as": "id", "expr": "datum._source.session_id"},
                              {"type": "formula", "as": "up", "expr": "datum._source.parent_session_id || null"}],
            },
            {
                "name": "sessions",
                "url": es_url({"size": 0, "aggs": {"sessions": {
                    "terms": {"field": "session_id", "size": 500, "order": {"first": "asc"}},
                    "aggs": {
                        "first": {"min": {"field": "@timestamp"}},
                        "last": {"max": {"field": "@timestamp"}},
                        "tokens": {"sum": {"field": "tokens.total"}},
                        "model_ms": {"sum": {"field": "api_duration_ms"}},
                        "tool_ms": {"sum": {"field": "duration_ms"}},
                        "model_calls": {"filter": {"term": {"event": "post_api_request"}}},
                        "tool_calls": {"filter": {"term": {"event": "post_tool_call"}}},
                        "errors": {"filter": ERROR_FILTER},
                        "model": {"terms": {"field": "model", "size": 1}},
                        "platform": {"terms": {"field": "platform", "size": 1}},
                        "start": {"filter": {"term": {"is_session_start": True}}, "aggs": {"doc": {"top_hits": {
                            "size": 1, "_source": {"includes": [
                                "parent_session_id", "parent_turn_id", "parent_subagent_id", "task_id"]}}}}},
                    }}}}),
                "format": {"property": "aggregations.sessions.buckets"},
                "transform": [
                    {"type": "formula", "as": "id", "expr": "datum.key"},
                    {"type": "formula", "as": "link",
                     "expr": "datum.start.doc.hits.hits.length ? datum.start.doc.hits.hits[0]._source : {}"},
                    {"type": "formula", "as": "declared_parent", "expr": "datum.link.parent_session_id || null"},
                    {"type": "formula", "as": "t_first", "expr": "datum.first.value"},
                    {"type": "formula", "as": "t_last", "expr": "datum.last.value"},
                    {"type": "formula", "as": "n_tokens", "expr": "datum.tokens.value || 0"},
                    {"type": "formula", "as": "ms", "expr": "(datum.model_ms.value || 0) + (datum.tool_ms.value || 0)"},
                    {"type": "formula", "as": "n_errors", "expr": "datum.errors.doc_count"},
                    {"type": "formula", "as": "model_name",
                     "expr": "datum.model.buckets.length ? datum.model.buckets[0].key : ''"},
                    {"type": "formula", "as": "platform_name",
                     "expr": "datum.platform.buckets.length ? datum.platform.buckets[0].key : ''"},
                    # ancestors for the role label: up to MAX_LABEL_DEPTH hops through the links
                    {"type": "lookup", "from": "linkdocs", "key": "id", "fields": ["id"], "as": ["a0"]},
                    {"type": "formula", "as": "p1", "expr": "datum.declared_parent || (datum.a0 ? datum.a0.up : null)"},
                    {"type": "lookup", "from": "linkdocs", "key": "id", "fields": ["p1"], "as": ["a1"]},
                    {"type": "formula", "as": "p2", "expr": "datum.a1 ? datum.a1.up : null"},
                    {"type": "lookup", "from": "linkdocs", "key": "id", "fields": ["p2"], "as": ["a2"]},
                    {"type": "formula", "as": "p3", "expr": "datum.a2 ? datum.a2.up : null"},
                    {"type": "lookup", "from": "linkdocs", "key": "id", "fields": ["p3"], "as": ["a3"]},
                    {"type": "formula", "as": "p4", "expr": "datum.a3 ? datum.a3.up : null"},
                    {"type": "formula", "as": "level", "expr": level_expr("datum.p1", "datum.p2", "datum.p3", "datum.p4")},
                    {"type": "formula", "as": "label", "expr": label_expr("datum.id", "datum.level")},
                ],
            },
            {
                "name": "nodes", "source": "sessions",
                "transform": [
                    {"type": "lookup", "from": "sessions", "key": "id", "fields": ["declared_parent"], "as": ["prow"]},
                    # Parent not in the result set (filtered out / outside the time range / no link) -> top level.
                    {"type": "formula", "as": "parent",
                     "expr": "datum.prow && datum.declared_parent !== datum.id ? datum.declared_parent : '__root__'"},
                    {"type": "formula", "as": "orphan", "expr": "!!datum.declared_parent && !datum.prow"},
                ],
            },
            {"name": "top", "values": [{"id": "__root__", "parent": None, "t_first": 0}]},
            {
                "name": "tree", "source": ["top", "nodes"],
                "transform": [
                    {"type": "stratify", "key": "id", "parentKey": "parent"},
                    {"type": "tree", "method": "tidy", "separation": True,
                     "sort": {"field": "t_first", "order": "ascending"},
                     "size": [{"signal": "height"}, {"signal": "width - labelSpace"}],
                     "as": ["y", "x", "depth", "children"]},
                ],
            },
            {"name": "links", "source": "tree",
             "transform": [{"type": "treelinks"},
                           {"type": "filter", "expr": "datum.source.id !== '__root__'"},
                           {"type": "formula", "as": "sx", "expr": "scale('xdepth', datum.source.depth)"},
                           {"type": "formula", "as": "tx", "expr": "scale('xdepth', datum.target.depth)"},
                           {"type": "linkpath", "orient": "horizontal", "shape": "diagonal",
                            "sourceX": "sx", "sourceY": "source.y", "targetX": "tx", "targetY": "target.y"}]},
            {"name": "shown", "source": "tree", "transform": [{"type": "filter", "expr": "datum.id !== '__root__'"}]},
        ],
        "scales": [
            {"name": "xdepth", "type": "linear", "domain": {"signal": "[1, max(2, maxDepth)]"},
             "range": [{"signal": "16"}, {"signal": "max(32, width - labelSpace)"}], "zero": False, "nice": False},
            {"name": "size", "type": "sqrt", "domain": {"signal": "[0, max(1, maxTokens)]"},
             "range": [60, 900]},
            {"name": "color", "type": "linear", "domain": {"signal": "[0, max(1, maxMs)]"},
             "range": {"scheme": "blues"}},
        ],
        "legends": [
            {"fill": "color", "type": "gradient", "title": "model + tool ms", "orient": "top-left",
             "direction": "horizontal", "gradientLength": 120},
        ],
        "marks": [
            {"type": "path", "from": {"data": "links"},
             "encode": {"update": {"path": {"field": "path"}, "stroke": {"value": "#98a2b3"}, "strokeWidth": {"value": 1.5}}}},
            {"type": "symbol", "from": {"data": "shown"},
             "encode": {"update": {
                 "x": {"scale": "xdepth", "field": "depth"}, "y": {"field": "y"},
                 "size": {"scale": "size", "field": "n_tokens"},
                 "fill": {"scale": "color", "field": "ms"},
                 "stroke": [{"test": "datum.n_errors > 0", "value": "#d92d20"}, {"value": "#344054"}],
                 "strokeWidth": [{"test": "datum.n_errors > 0", "value": 3}, {"value": 1}],
                 "strokeDash": [{"test": "datum.orphan", "value": [3, 2]}, {"value": []}],
                 "tooltip": {"signal": "{"
                     "'session': datum.id, "
                     "'parent session': datum.declared_parent || '(none: top-level request)', "
                     "'parent turn': datum.link.parent_turn_id || '', "
                     "'parent subagent': datum.link.parent_subagent_id || '', "
                     "'task': datum.link.task_id || '', "
                     "'platform': datum.platform_name, 'model': datum.model_name, "
                     "'model calls': datum.model_calls.doc_count, 'tool calls': datum.tool_calls.doc_count, "
                     "'failed steps': datum.n_errors, 'tokens': format(datum.n_tokens, ','), "
                     "'model + tool ms': format(datum.ms, ','), "
                     "'first record': timeFormat(datum.t_first, '%Y-%m-%d %H:%M:%S'), "
                     "'elapsed s': format((datum.t_last - datum.t_first) / 1000, ',.0f'), "
                     "'records': datum.doc_count}"},
             }}},
            {"type": "text", "from": {"data": "shown"},
             "encode": {"update": {
                 "x": {"scale": "xdepth", "field": "depth", "offset": 18}, "y": {"field": "y"},
                 # a node with children: label above its links; a leaf: label beside it
                 "dy": [{"test": "datum.children > 0", "value": -14}, {"value": 0}],
                 "baseline": {"value": "middle"},
                 "fontSize": {"value": 11}, "fill": {"value": "#101828"}, "limit": {"signal": "labelSpace - 24"},
                 "text": {"signal": "datum.label + '  ' + (datum.model_name || 'no model') "
                                    "+ ' · ' + format(datum.n_tokens, ',') + ' tok · ' + format(datum.ms, ',') + ' ms'"},
             }}},
            {"type": "text", "encode": {"update": {
                "x": {"signal": "width / 2"}, "y": {"signal": "height / 2"}, "align": {"value": "center"},
                "fill": {"value": "#667085"}, "fontSize": {"value": 13},
                "text": {"signal": "length(data('shown')) ? '' : 'No sessions in the selected time range / filters'"}}}},
        ],
    }


# --- waterfall (Vega-Lite) ---------------------------------------------------------------------
WATERFALL_SOURCE = ["@timestamp", "session_id", "seq", "event", "action_type", "tool_name", "tool_call_id",
                    "api_request_id", "model", "status", "status_code", "error_type", "duration_ms",
                    "api_duration_ms", "tokens.total", "retry_count", "verify.status", "platform", "source.line"]


def waterfall_spec():
    s = "datum.hit._source"
    return {
        "$schema": "https://vega.github.io/schema/vega-lite/v5.json",
        "description": "Hermes ATP step waterfall: one bar per completed step (model call, tool call, API "
                       "error), ending at the completion record's @timestamp and as long as its duration_ms / "
                       "api_duration_ms. Rows: sessions in start order, then seq. A hollow diamond is a step "
                       "that started (pre_*) but has no completion record in range (still running or crashed). "
                       "Recorder timestamps are whole seconds, so bars are placed to the second; seq gives the "
                       "exact order.",
        "autosize": {"type": "fit", "contains": "padding"},
        # One request: the steps (hits) plus, in a global aggregation that ignores the dashboard
        # filters but keeps the time range, the subagent links the role labels need.
        "data": {"url": es_url({"size": 2000, "sort": [{"@timestamp": "asc"}, {"source.line": "asc"}, {"seq": "asc"}],
                                "_source": WATERFALL_SOURCE, "aggs": {"links": LINKS_AGG}})},
        "transform": [
            {"calculate": "datum.aggregations.links.f.s.buckets", "as": "lb"},
            {"calculate": "pluck(datum.lb, 'key')", "as": "lk"},
            {"calculate": "pluck(datum.lb, 'p')", "as": "lp"},
            {"flatten": ["hits.hits"], "as": ["hit"]},
            {"calculate": f"{s}.session_id", "as": "session"},
            {"filter": "isValid(datum.session)"},  # unparseable anomaly lines have no session
            {"calculate": f"{s}.seq", "as": "seq"},
            {"calculate": f"{s}.event", "as": "event"},
            {"calculate": f"time(toDate({s}['@timestamp']))", "as": "end"},
            {"calculate": f"{s}.duration_ms != null ? {s}.duration_ms : ({s}.api_duration_ms != null ? {s}.api_duration_ms : 0)",
             "as": "dur"},
            {"calculate": "datum.end - datum.dur", "as": "start"},
            {"calculate": f"indexof(datum.event, 'tool') >= 0 ? 'tool' : 'model'", "as": "family"},
            {"calculate": f"datum.family === 'tool' ? {s}.tool_call_id : {s}.api_request_id", "as": "step_id"},
            {"calculate": "datum.session + '|' + datum.family + '|' + datum.step_id", "as": "pair"},
            # Session order = first record's time, then its line in the log (timestamps are whole
            # seconds, so a parent and the subagent it launched usually share a start second).
            {"calculate": f"{s}.source ? {s}.source.line : 0", "as": "line"},
            {"joinaggregate": [{"op": "min", "field": "end", "as": "session_start"},
                               {"op": "min", "field": "line", "as": "session_line"}], "groupby": ["session"]},
            {"joinaggregate": [{"op": "count", "as": "pair_n"}], "groupby": ["pair"]},
            {"calculate": "indexof(datum.event, 'pre_') === 0", "as": "is_pre"},
            # Completed steps become bars; a pre_* record is kept only when nothing completed it.
            {"filter": "!datum.is_pre || datum.pair_n === 1 || !datum.step_id"},
            {"calculate": "datum.is_pre ? 'started, no completion' : datum.event", "as": "kind"},
            {"calculate": f"{s}.tool_name || {s}.model || ''", "as": "what"},
            # Row key = zero-padded rank (session start, session, seq) + TAB + label: sorts as a plain
            # string in every layer, and the axis shows only the label.
            {"window": [{"op": "row_number", "as": "rank"}],
             "sort": [{"field": "session_start"}, {"field": "session_line"}, {"field": "session"}, {"field": "seq"}]},
            # role label: up to MAX_LABEL_DEPTH hops through the subagent links (same as the tree)
            # role label: up to MAX_LABEL_DEPTH hops through the subagent links (same as the tree)
            *[{"calculate": up_expr(f"datum.{src}"), "as": f"p{i + 1}"}
              for i, src in enumerate(("session", "p1", "p2", "p3"))],
            {"calculate": level_expr("datum.p1", "datum.p2", "datum.p3", "datum.p4"), "as": "level"},
            {"calculate": "slice('00000' + datum.rank, -5) + '\\t' + " + label_expr("datum.session", "datum.level")
                          + " + ' #' + datum.seq + '  ' + datum.what", "as": "row"},
            {"calculate": f"{s}.status || ({s}.status_code ? 'HTTP ' + {s}.status_code : '')", "as": "status"},
            {"calculate": f"{s}.tokens ? {s}.tokens.total : null", "as": "tokens"},
            {"calculate": f"{s}.verify ? {s}.verify.status : ''", "as": "verify"},
        ],
        "encoding": {
            "y": {"field": "row", "type": "ordinal", "sort": "ascending",
                  "axis": {"title": None, "labelLimit": 260, "labelFontSize": 10,
                           "labelExpr": "split(datum.label, '\\t')[1]"}},
            "color": {"field": "kind", "type": "nominal", "title": "step",
                      "scale": {"domain": ["post_api_request", "post_tool_call", "api_request_error",
                                           "started, no completion"],
                                "range": ["#2e90fa", "#12b76a", "#d92d20", "#f79009"]},
                      "legend": {"orient": "top"}},
            "tooltip": [
                {"field": "session", "title": "session"}, {"field": "seq", "title": "seq"},
                {"field": "event", "title": "event"}, {"field": "what", "title": "tool / model"},
                {"field": "status", "title": "status"}, {"field": "dur", "title": "duration ms"},
                {"field": "tokens", "title": "tokens"}, {"field": "verify", "title": "verify"},
                {"field": "end", "type": "temporal", "title": "recorded", "format": "%H:%M:%S"},
            ],
        },
        "layer": [
            {"transform": [{"filter": "!datum.is_pre"}],
             "mark": {"type": "bar", "cornerRadius": 2, "minBandSize": 2},
             "encoding": {"x": {"field": "start", "type": "temporal", "title": None,
                                "axis": {"format": "%H:%M:%S", "labelFontSize": 10}},
                          "x2": {"field": "end"}}},
            {"transform": [{"filter": "datum.is_pre"}],
             "mark": {"type": "point", "shape": "diamond", "filled": False, "size": 80, "strokeWidth": 2},
             "encoding": {"x": {"field": "end", "type": "temporal"}}},
        ],
    }


def vega_vis(obj_id, title, spec, description):
    vis_state = {"title": title, "type": "vega", "aggs": [],
                 "params": {"spec": json.dumps(spec, indent=2, ensure_ascii=False)}}
    return {"type": "visualization", "id": obj_id,
            "attributes": {"title": title, "description": description, "visState": compact(vis_state),
                           "uiStateJSON": "{}", "version": 1,
                           "kibanaSavedObjectMeta": {"searchSourceJSON": compact(
                               {"query": {"query": "", "language": "kuery"}, "filter": []})}},
            "references": []}


# --- step table (saved Discover session) -------------------------------------------------------
STEP_COLUMNS = ["session_id", "seq", "event", "tool_name", "model", "status", "duration_ms", "api_duration_ms", "tokens.total",
                "verify.status"]
STEP_SORT = [["session_id", "asc"], ["seq", "asc"]]


def step_table():
    return {"type": "search", "id": "hermes-atp-lifecycle-steps",
            "attributes": {"title": "Hermes ATP steps (session, seq)",
                           "description": "One row per ATP record, sorted by session_id then seq, so each "
                                          "session reads top to bottom like a transcript.",
                           "tabs": [{"id": stable_uuid("steps-tab"), "label": "Steps", "attributes": {
                               "columns": STEP_COLUMNS, "sort": STEP_SORT, "grid": {}, "hideChart": True,
                               "isTextBasedQuery": False,
                               "kibanaSavedObjectMeta": {"searchSourceJSON": compact({
                                   "query": {"query": "", "language": "kuery"}, "filter": [],
                                   "indexRefName": "kibanaSavedObjectMeta.searchSourceJSON.index"})}}}]},
            "references": [{"type": "index-pattern", "id": DATA_VIEW_ID,
                            "name": "kibanaSavedObjectMeta.searchSourceJSON.index"}]}


# --- rollup tiles (Lens) -----------------------------------------------------------------------
def count_where(kql, label):
    c = base.count(label)
    c["params"]["emptyAsNull"] = False  # show 0, not N/A, when nothing matches
    c["filter"] = {"query": kql, "language": "kuery"}
    return c


def fmt(col, fid, **params):
    col["params"]["format"] = {"id": fid, "params": params}
    return col


def formula(prefix, label, text, parts, op, fmt_id=None, fmt_params=None):
    """A Lens formula column `prefix` = op(parts...), with the hidden part columns Lens stores."""
    cols = []
    for i, part in enumerate(parts):
        part = dict(part, label=f"Part of {label}", customLabel=True)
        cols.append((f"{prefix}X{i}", part))
    ast = {"type": "function", "name": op, "args": [c for c, _ in cols],
           "location": {"min": 0, "max": len(text)}, "text": text}
    cols.append((f"{prefix}X{len(parts)}", {
        "label": f"Part of {label}", "customLabel": True, "dataType": "number", "operationType": "math",
        "isBucketed": False, "scale": "ratio", "params": {"tinymathAst": ast}, "references": [c for c, _ in cols]}))
    main = {"label": label, "customLabel": True, "dataType": "number", "operationType": "formula",
            "isBucketed": False, "scale": "ratio",
            "params": {"formula": text, "isFormulaBroken": False},
            "references": [f"{prefix}X{len(parts)}"]}
    if fmt_id:
        main["params"]["format"] = {"id": fmt_id, "params": fmt_params or {}}
    return [(prefix, main)] + cols


def tile(obj_id, title, columns, description, query=""):
    return base.lens(obj_id, title, "lnsMetric", columns, base.metric_vis(columns[0][0]),
                     query=query, description=description)


DURATION = {"fromUnit": "milliseconds", "toUnit": "humanizePrecise", "decimals": 1}


def tiles():
    return [
        tile("hermes-atp-lc-tokens", "Tokens",
             [("v", fmt(base.metric("sum", "tokens.total", "Total tokens"), "number", decimals=0))],
             "Sum of tokens.total over every model call in the selection (parent and subagent sessions)."),
        tile("hermes-atp-lc-elapsed", "Elapsed (first → last record)",
             formula("v", "Elapsed", "max(@timestamp) - min(@timestamp)",
                     [base.metric("max", "@timestamp", "max"), base.metric("min", "@timestamp", "min")],
                     "subtract", "duration", DURATION),
             "Wall-clock span of the selected records (whole-second recorder timestamps)."),
        tile("hermes-atp-lc-model-ms", "Model time",
             [("v", fmt(base.metric("sum", "api_duration_ms", "Model time"), "duration", **DURATION))],
             "Sum of api_duration_ms (model call latency, including failed calls)."),
        tile("hermes-atp-lc-tool-ms", "Tool time",
             [("v", fmt(base.metric("sum", "duration_ms", "Tool time"), "duration", **DURATION))],
             "Sum of duration_ms of completed tool calls. delegate_task returns when the subagent is "
             "launched, so subagent work shows in the child session's own model/tool time."),
        tile("hermes-atp-lc-errors", "Failed steps", [("v", count_where(ERROR_KQL, "Failed steps"))],
             "API errors (api_request_error) plus tool calls whose status is not ok."),
        tile("hermes-atp-lc-retries", "Retries", [("v", count_where(RETRY_KQL, "Retries"))],
             "Model API attempts made after a failed attempt (pre_api_request with retry_count > 0)."),
        tile("hermes-atp-lc-verify", "Verify pass rate",
             formula("v", "Verify pass rate", "count(kql='verify.valid : true') / count()",
                     [count_where("verify.valid : true", "passed"), base.count("all")],
                     "divide", "percent", {"decimals": 1}),
             "Share of records that passed the exporter's (provisional) ingest verify. Anomaly records "
             "count as failures. Authoritative check: atp_cli.py verify."),
    ]


# --- controls ----------------------------------------------------------------------------------
CONTROLS = [("session_id", "Session", "large"), ("turn_id", "Turn", "medium"), ("event", "Event", "small")]


def control_group():
    panels, refs = {}, []
    for order, (field, title, width) in enumerate(CONTROLS):
        cid = stable_uuid("control:" + field)
        panels[cid] = {"order": order, "width": width, "grow": True, "type": "optionsListControl",
                       "explicitInput": {"id": cid, "fieldName": field, "title": title,
                                         "searchTechnique": "prefix", "selectedOptions": [],
                                         "sort": {"by": "_count", "direction": "desc"}, "enhancements": {}}}
        refs.append({"name": f"controlGroup_{cid}:optionsListDataView", "type": "index-pattern", "id": DATA_VIEW_ID})
    group = {"controlStyle": "oneLine", "chainingSystem": "HIERARCHICAL", "showApplySelections": False,
             "panelsJSON": compact(panels),
             "ignoreParentSettingsJSON": compact({"ignoreFilters": False, "ignoreQuery": False,
                                                  "ignoreTimerange": False, "ignoreValidations": False})}
    return group, refs


# --- dashboard ---------------------------------------------------------------------------------
TREE = vega_vis("hermes-atp-lc-session-tree", "Session tree (parent → subagent sessions)", session_tree_spec(),
                "Vega. Parent session → child subagent sessions via parent_session_id (plus parent_turn_id / "
                "parent_subagent_id in the tooltip). Size = tokens, colour = model + tool ms, red ring = a "
                "failed step, dashed = parent outside the current selection.")
WATERFALL = vega_vis("hermes-atp-lc-waterfall", "Step waterfall", waterfall_spec(),
                     "Vega-Lite. One bar per completed model/tool step, rows by session start then seq.")
STEPS = step_table()
TILES = tiles()

# (object, x, y, w, h) on Kibana's 48-column grid
LAYOUT = [(t, x, 0, w, 7) for t, x, w in zip(TILES, (0, 7, 14, 21, 28, 35, 42), (7, 7, 7, 7, 7, 7, 6))] + [
    (TREE, 0, 7, 48, 16),
    (WATERFALL, 0, 23, 48, 24),
    (STEPS, 0, 47, 48, 22),
]


def build_objects():
    panels, refs = [], []
    for i, (obj, x, y, w, h) in enumerate(LAYOUT):
        pid = f"p{i + 1}"
        panels.append({"type": obj["type"], "panelIndex": pid, "gridData": {"x": x, "y": y, "w": w, "h": h, "i": pid},
                       "embeddableConfig": {"enhancements": {}}, "panelRefName": f"panel_{pid}"})
        refs.append({"name": f"{pid}:panel_{pid}", "type": obj["type"], "id": obj["id"]})
    group, control_refs = control_group()
    dashboard = {
        "type": "dashboard", "id": DASHBOARD_ID,
        "attributes": {
            "title": "Hermes ATP request lifecycle",
            "description": "One request end to end: the parent Hermes session and the subagent sessions it "
                           "delegated to (session tree), every model and tool step on a timeline (waterfall), "
                           "the records in order (step table) and rollups. Pick a session or turn at the top. "
                           "Ingest-time verify is provisional; run atp_cli.py verify for the verdict.",
            "panelsJSON": compact(panels),
            "controlGroupInput": group,
            "optionsJSON": compact({"useMargins": True, "syncColors": False, "syncCursor": True,
                                    "syncTooltips": False, "hidePanelTitles": False}),
            "timeRestore": True, "timeFrom": "now-24h", "timeTo": "now",
            "refreshInterval": {"pause": True, "value": 60000},
            "kibanaSavedObjectMeta": {"searchSourceJSON": compact(
                {"query": {"query": "", "language": "kuery"}, "filter": []})},
        },
        "references": refs + control_refs,
    }
    data_view = {"type": "index-pattern", "id": DATA_VIEW_ID,
                 "attributes": {"title": INDEX, "name": "Hermes ATP records", "timeFieldName": "@timestamp"},
                 "references": []}
    objs = [data_view] + [o for o, *_ in LAYOUT] + [dashboard]
    for o in objs:
        o["coreMigrationVersion"] = base.CORE_MIGRATION_VERSION
        o["typeMigrationVersion"] = TYPE_MIGRATION_VERSION[o["type"]]
        o["managed"] = False
    return objs


def build():
    return "".join(json.dumps(o, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
                   for o in build_objects())


if __name__ == "__main__":
    with open(OUT, "w", encoding="utf-8") as f:
        f.write(build())
    print(f"wrote {OUT}")
