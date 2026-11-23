import {byId, count, duration, yesNo, tally, facts, table, mountStandard, renderStandard, start} from "/health-common.js";

mountStandard(byId("standard-summary"), byId("standard-detail"));

// Average and longest time of a latency aggregate kept in nanoseconds.
function latency(figures) {
  const value = figures || {};
  if (!value.observations) return "No samples yet";
  return duration(value.total_ns / value.observations / 1e9) + " average · " + duration(value.max_ns / 1e9) + " longest · " + count(value.observations) + " samples";
}

function render(snapshot) {
  renderStandard("Engram", snapshot);
  const rollout = snapshot.rollout || {};
  facts("engram-facts", [
    ["State", snapshot.state || "Not observed"],
    ["Healthy", yesNo(snapshot.healthy)],
    ["Memory only", yesNo(snapshot.memory_only)],
    ["Active conversations", count(snapshot.active_conversations)],
    ["Rollout default", rollout.default_mode || "Not observed"],
    ["Namespace overrides", count(rollout.namespace_override_count) + " · " + tally(rollout.namespace_modes)],
  ]);
  const issues = Object.entries(snapshot.components || {}).filter(([, status]) => status.enabled && (!status.ready || status.error));
  byId("dependency-issues").hidden = !issues.length;
  table("dependency-issue-rows", issues, [
    ([name]) => name,
    ([, status]) => status.ready ? "Error reported" : "Not ready",
    ([, status]) => status.error || "—",
  ], "");
  const telemetry = snapshot.telemetry || {};
  const resolution = telemetry.resolution || {};
  const matcher = telemetry.matcher || {};
  const cache = telemetry.regulated_cache || {};
  const graph = telemetry.graph_recall || {};
  facts("recall-facts", [
    ["Pattern matcher", count(matcher.queries) + " queries · " + count(matcher.hits) + " hits · " + count(matcher.evictions) + " evictions"],
    ["Regulated cache", count(cache.proposals) + " proposals · " + count(cache.misses) + " misses · " + count(cache.accepted) + " accepted"],
    ["Learned / replaced / retired", count(cache.learned_created) + " / " + count(cache.learned_replaced) + " / " + count(cache.retired)],
    ["Proposals pending / retained", count(cache.pending_proposals) + " / " + count(cache.retained_proposals)],
    ["Regulator outcomes", tally(telemetry.regulator_outcomes)],
    ["Graph recall", count(graph.consultations) + " consultations · " + count(graph.hits) + " hits · " + count(graph.misses) + " misses · " + count(graph.failures) + " failures"],
    ["Resolutions", count(resolution.requests) + " requests · " + count(resolution.executions) + " run · " + count(resolution.replays) + " replayed"],
    ["Resolution outcomes", tally(resolution.outcomes)],
    ["Resolution time", latency(resolution.latency)],
  ]);
  table("resolver-rows", Object.entries(telemetry.resolvers || {}), [
    ([name]) => name,
    ([, resolver]) => count(resolver.invocations),
    ([, resolver]) => tally(resolver.states),
    ([, resolver]) => count(resolver.selected_contributions),
  ], "No resolvers reported");
}

start("Engram", render);
