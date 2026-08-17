"use strict";

const state = { review: null, selected: null };

function node(tag, className, text) {
  const value = document.createElement(tag);
  if (className) value.className = className;
  if (text !== undefined && text !== null) value.textContent = String(text);
  return value;
}

function badge(text, kind) {
  return node("span", `badge ${kind || ""}`, text);
}

function metricCard(citation) {
  const card = node("article", "metric-card");
  const path = node("div", "metric-path");
  path.append(
    badge(citation.axis_name, "axis"), node("span", "chevron", "›"),
    badge(citation.subaxis_name, "subaxis"), node("span", "chevron", "›"),
    badge(citation.variant_name, "variant")
  );
  card.append(path);
  const verdicts = node("div", "verdicts");
  verdicts.append(
    badge(`source ${citation.source_verdict ?? "N/A"}`, citation.source_verdict === "FAIL" ? "fail" : "pass"),
    badge(`golden ${citation.golden_verdict ?? "N/A"}`, citation.golden_verdict === "PASS" ? "pass" : "muted"),
    badge(`${Math.round((citation.confidence || 0) * 100)}% confidence`, "muted")
  );
  card.append(verdicts);
  if (citation.expected_behavior) card.append(node("p", "metric-copy", `Expected: ${citation.expected_behavior}`));
  if (citation.observed_behavior) card.append(node("p", "metric-copy", `Observed: ${citation.observed_behavior}`));
  return card;
}

function turnCard(turn) {
  const card = node("article", `turn ${turn.role}`);
  const head = node("div", "turn-head");
  head.append(badge(turn.role, turn.role), node("code", "turn-id", turn.turn_id));
  if (turn.action) head.append(badge(turn.action, turn.action === "REPLACE" ? "replace" : "keep"));
  card.append(head);
  if (turn.role === "user") {
    card.append(node("p", "utterance", turn.text));
    card.append(node("p", "preservation", "Source user turn preserved byte-for-byte"));
    return card;
  }
  if (turn.role !== "assistant") {
    card.append(node("p", "utterance", turn.text));
    card.append(node("p", "preservation", "Workflow context preserved byte-for-byte"));
    return card;
  }
  if (turn.action === "REPLACE") {
    const compare = node("div", "compare");
    const before = node("div", "pane before");
    before.append(node("h4", "", "Source assistant"), node("p", "utterance", turn.source_text));
    const after = node("div", "pane after");
    after.append(node("h4", "", "Golden candidate"), node("p", "utterance", turn.golden_text));
    compare.append(before, after);
    card.append(compare);
  } else {
    card.append(node("p", "utterance", turn.golden_text));
  }
  if (turn.correction_reason) card.append(node("p", "reason", `Why: ${turn.correction_reason}`));
  const metrics = node("details", "metrics");
  metrics.append(node("summary", "", `${turn.metric_citations.length} applicable metric citation${turn.metric_citations.length === 1 ? "" : "s"}`));
  turn.metric_citations.forEach(item => metrics.append(metricCard(item)));
  card.append(metrics);
  return card;
}

function renderDetail(conversation) {
  state.selected = conversation.packet_id;
  const detail = document.getElementById("detail");
  detail.replaceChildren();
  const title = node("div", "detail-title");
  const info = node("div");
  info.append(node("p", "eyebrow", `PACKET ${conversation.packet_index + 1}`));
  info.append(node("h2", "", `${conversation.classification.domain} · ${conversation.classification.primary_language}`));
  info.append(node("p", "mono", conversation.packet_id));
  title.append(info, badge(conversation.status.replaceAll("_", " "), conversation.status === "READY_FOR_HUMAN_REVIEW" ? "pass" : "fail"));
  detail.append(title);
  const verifier = node("section", "verifier-panel");
  verifier.append(node("h3", "", "Independent verification"));
  verifier.append(badge(conversation.verifier.decision, conversation.verifier.decision === "PASS" ? "pass" : "fail"));
  verifier.append(node("span", "verifier-copy", `${conversation.verifier.findings.length} findings · replay ${conversation.verifier.replay_required ? "required" : "not required"}`));
  detail.append(verifier);
  if (conversation.quarantine_reasons.length) {
    const alert = node("section", "alert");
    alert.append(node("h3", "", "Why this candidate is quarantined"));
    const list = node("ul");
    conversation.quarantine_reasons.forEach(reason => list.append(node("li", "", reason)));
    alert.append(list);
    detail.append(alert);
  }
  const turns = node("section", "turns");
  conversation.turns.forEach(turn => turns.append(turnCard(turn)));
  detail.append(turns);
  renderList();
}

function renderList() {
  const list = document.getElementById("conversationList");
  const status = document.getElementById("statusFilter").value;
  const query = document.getElementById("search").value.trim().toLowerCase();
  list.replaceChildren();
  state.review.conversations
    .filter(item => status === "ALL" || item.status === status)
    .filter(item => JSON.stringify({source: item.source, classification: item.classification}).toLowerCase().includes(query))
    .forEach(item => {
      const button = node("button", `conversation-item ${state.selected === item.packet_id ? "selected" : ""}`);
      button.type = "button";
      button.append(node("strong", "", `${item.classification.domain} · ${item.classification.primary_language}`));
      button.append(node("span", "", `Conversation ${item.packet_index + 1}`));
      button.append(badge(item.verifier.decision, item.verifier.decision === "PASS" ? "pass" : "fail"));
      button.addEventListener("click", () => renderDetail(item));
      list.append(button);
    });
}

async function start() {
  const response = await fetch("/review.json", {credentials: "same-origin"});
  if (!response.ok) throw new Error(`Could not load review data (${response.status})`);
  state.review = await response.json();
  const counts = state.review.counts;
  document.getElementById("summary").append(
    badge(`${counts.total} total`, "muted"),
    badge(`${counts.READY_FOR_HUMAN_REVIEW} ready`, "pass"),
    badge(`${counts.QUARANTINED} quarantined`, "fail")
  );
  document.getElementById("statusFilter").addEventListener("change", renderList);
  document.getElementById("search").addEventListener("input", renderList);
  renderList();
  if (state.review.conversations.length) renderDetail(state.review.conversations[0]);
}

start().catch(error => {
  document.getElementById("detail").replaceChildren(node("div", "alert", error.message));
});
