#!/usr/bin/env python3

from __future__ import annotations

import ipaddress
import json
import re
import shlex
import shutil
import subprocess
import sys
import webbrowser
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from process_identity import (
    ProcessIdentity,
    ProcessIdentityError,
    probe_local_identity,
    validate_remote_host,
)


TASK_ID_RE = re.compile(r"^[A-Za-z0-9_.:-]+$")
ACTIVE_PHASES = {
    "reserving",
    "registration_recovery_required",
    "scheduled",
    "watching",
    "event_staged",
    "native_message_ready",
    "native_message_queued",
    "native_message_submitting",
    "native_message_submitted",
    "native_message_deferred",
    "marker_claiming",
}
ERROR_PHASES = {
    "marker_blocked",
    "native_message_blocked",
    "registration_blocked",
    "resume_failed",
    "schedule_failed",
}
WARNING_PHASES = {
    "marker_pending",
    "marker_unknown",
    "cancelled",
    "native_message_unknown",
}
SUCCESS_PHASES = {
    "native_message_accepted",
    "marker_claimed",
    "resume_dry_run_complete",
}
TEXT_FILE_FIELDS = {
    "watch_log": "log_file",
    "prompt": "prompt_file",
}
OPEN_FILE_KINDS = set(TEXT_FILE_FIELDS) | {"observed_log", "raw", "task"}


INDEX_HTML = r"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Codex Wait Handoff</title>
  <link rel="stylesheet" href="/app.css">
</head>
<body>
  <div class="app-shell">
    <aside class="sidebar">
      <header class="brand">
        <div>
          <h1>Wait Handoff</h1>
          <p>Live status, files, and single-handoff controls.</p>
        </div>
        <span id="liveDot" class="live-dot" title="Auto refresh is enabled"></span>
      </header>

      <section class="summary-grid" aria-label="Summary">
        <div class="summary-tile">
          <span class="summary-label">Active</span>
          <strong id="countActive">0</strong>
        </div>
        <div class="summary-tile">
          <span class="summary-label">Done</span>
          <strong id="countDone">0</strong>
        </div>
        <div class="summary-tile">
          <span class="summary-label">Needs Look</span>
          <strong id="countNeedsLook">0</strong>
        </div>
      </section>

      <div class="toolbar">
        <label class="search-box">
          <span>Search</span>
          <input id="searchInput" type="search" placeholder="task, host, note, phase">
        </label>
        <button id="refreshButton" type="button">Refresh</button>
      </div>

      <div class="meta-line">
        <span id="stateDirLabel">state dir loading...</span>
        <span id="refreshLabel">never</span>
      </div>

      <nav id="taskList" class="task-list" aria-label="Recent handoffs"></nav>
    </aside>

    <main class="detail">
      <section id="emptyState" class="empty-state">
        <div class="empty-mark"></div>
        <h2>No handoff selected</h2>
        <p>Select a recent task to inspect status, logs, continuation content, and the resumed answer.</p>
      </section>

      <section id="detailContent" class="detail-content hidden">
        <header class="detail-header">
          <div>
            <div class="eyeline">
              <span id="phaseBadge" class="phase-badge">phase</span>
              <span id="liveBadge" class="live-badge hidden">live</span>
            </div>
            <h2 id="taskTitle">task</h2>
            <p id="targetLine">target</p>
          </div>
          <div class="header-actions">
            <button id="copyTaskButton" type="button">Copy ID</button>
            <button id="openTaskButton" type="button">Open Task</button>
            <button id="killTaskButton" class="danger-button" type="button">Kill Handoff</button>
          </div>
        </header>

        <section class="status-panel">
          <div class="progress-row">
            <div>
              <span class="small-label">Wait progress</span>
              <strong id="progressLabel">0%</strong>
            </div>
            <span id="elapsedLabel" class="muted">elapsed unknown</span>
          </div>
          <div class="progress-track" aria-hidden="true"><div id="progressBar"></div></div>
          <div id="factsGrid" class="facts-grid"></div>
        </section>

        <section class="tabs-panel">
          <div class="tabs" role="tablist" aria-label="Task files">
            <button class="tab active" data-tab="watch_log" type="button">Watch Log</button>
            <button class="tab" data-tab="observed_log" type="button">Observed Log</button>
            <button class="tab" data-tab="prompt" type="button">Prompt</button>
            <button class="tab" data-tab="raw" type="button">JSON</button>
          </div>
          <div class="file-header">
            <span id="filePathLabel"></span>
            <div class="file-actions">
              <span id="fileSizeLabel"></span>
              <button id="openFileButton" type="button">Open File</button>
            </div>
          </div>
          <pre id="fileViewer" class="file-viewer"></pre>
        </section>

        <section class="note-panel">
          <h3>Continuation Content</h3>
          <div class="note-grid">
            <article>
              <h4>Scheduler Note</h4>
              <p id="noteText" class="copy-text"></p>
            </article>
            <article>
              <h4>Resume Prompt</h4>
              <p id="continuationText" class="copy-text"></p>
            </article>
          </div>
        </section>
      </section>
    </main>
  </div>
  <script src="/app.js"></script>
</body>
</html>
"""


APP_CSS = r""":root {
  color-scheme: light;
  --bg: #f6f7f4;
  --panel: #ffffff;
  --panel-soft: #fbfcfa;
  --ink: #17201b;
  --muted: #6a746d;
  --faint: #8d968f;
  --border: #dde4dc;
  --border-strong: #c7d2ca;
  --accent: #2f6fed;
  --accent-soft: #edf3ff;
  --ok: #0f8a5f;
  --ok-soft: #e7f6ef;
  --warn: #a86913;
  --warn-soft: #fff4dc;
  --bad: #bb3e24;
  --bad-soft: #ffede8;
  --shadow: 0 18px 48px rgba(28, 36, 32, 0.08);
}

* {
  box-sizing: border-box;
}

body {
  margin: 0;
  min-height: 100vh;
  background:
    linear-gradient(180deg, rgba(255,255,255,0.74), rgba(246,247,244,0.92)),
    var(--bg);
  color: var(--ink);
  font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
  letter-spacing: 0;
}

button,
input {
  font: inherit;
}

button {
  border: 1px solid var(--border);
  background: var(--panel);
  color: var(--ink);
  border-radius: 7px;
  min-height: 34px;
  padding: 0 12px;
  cursor: pointer;
}

button:hover {
  border-color: var(--border-strong);
  background: var(--panel-soft);
}

button:disabled {
  cursor: not-allowed;
  opacity: 0.5;
}

.danger-button {
  border-color: rgba(187, 62, 36, 0.32);
  color: var(--bad);
  background: var(--bad-soft);
}

.danger-button:hover {
  border-color: rgba(187, 62, 36, 0.58);
  background: #ffe3dc;
}

.app-shell {
  display: grid;
  grid-template-columns: minmax(320px, 390px) minmax(0, 1fr);
  min-height: 100vh;
}

.sidebar {
  border-right: 1px solid var(--border);
  background: rgba(255,255,255,0.62);
  backdrop-filter: blur(18px);
  padding: 26px 20px;
  display: flex;
  flex-direction: column;
  min-height: 100vh;
}

.brand {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 16px;
  margin-bottom: 22px;
}

.brand h1,
.detail-header h2,
.empty-state h2 {
  margin: 0;
  line-height: 1.05;
  font-weight: 720;
  letter-spacing: 0;
}

.brand h1 {
  font-size: 28px;
}

.brand p,
.empty-state p,
.target-line,
#targetLine {
  margin: 8px 0 0;
  color: var(--muted);
  line-height: 1.45;
}

.live-dot {
  width: 10px;
  height: 10px;
  border-radius: 999px;
  background: var(--ok);
  box-shadow: 0 0 0 5px rgba(15, 138, 95, 0.14);
  margin-top: 9px;
  flex: 0 0 auto;
}

.live-dot.paused {
  background: var(--warn);
  box-shadow: 0 0 0 5px rgba(168, 105, 19, 0.14);
}

.summary-grid {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: 8px;
  margin-bottom: 18px;
}

.summary-tile {
  background: var(--panel);
  border: 1px solid var(--border);
  border-radius: 8px;
  padding: 12px 10px;
}

.summary-label {
  display: block;
  color: var(--muted);
  font-size: 12px;
  line-height: 1.2;
  margin-bottom: 6px;
}

.summary-tile strong {
  font-size: 24px;
  line-height: 1;
}

.toolbar {
  display: grid;
  grid-template-columns: minmax(0, 1fr) auto;
  gap: 10px;
  align-items: end;
}

.search-box span {
  display: block;
  color: var(--muted);
  font-size: 12px;
  margin-bottom: 6px;
}

.search-box input {
  width: 100%;
  height: 36px;
  border: 1px solid var(--border);
  border-radius: 7px;
  background: var(--panel);
  color: var(--ink);
  padding: 0 11px;
  outline: none;
}

.search-box input:focus {
  border-color: var(--accent);
  box-shadow: 0 0 0 3px rgba(47, 111, 237, 0.12);
}

.meta-line {
  display: flex;
  justify-content: space-between;
  gap: 12px;
  color: var(--faint);
  font-size: 12px;
  margin: 12px 1px;
}

.meta-line span {
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.task-list {
  display: flex;
  flex-direction: column;
  gap: 8px;
  overflow: auto;
  padding-right: 2px;
}

.task-card {
  text-align: left;
  border: 1px solid var(--border);
  background: rgba(255,255,255,0.82);
  border-radius: 8px;
  padding: 12px;
  min-height: 92px;
}

.task-card.active {
  border-color: rgba(47, 111, 237, 0.58);
  box-shadow: 0 0 0 3px rgba(47, 111, 237, 0.10);
}

.task-card-top,
.task-card-bottom,
.progress-row,
.file-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
}

.task-id {
  font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, "Liberation Mono", monospace;
  font-size: 12px;
  color: var(--ink);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.task-target {
  margin: 10px 0;
  color: var(--muted);
  font-size: 13px;
  line-height: 1.35;
  overflow: hidden;
  display: -webkit-box;
  -webkit-line-clamp: 2;
  -webkit-box-orient: vertical;
}

.task-time {
  color: var(--faint);
  font-size: 12px;
}

.phase-badge,
.mini-badge,
.live-badge {
  display: inline-flex;
  align-items: center;
  border-radius: 999px;
  min-height: 24px;
  padding: 0 9px;
  font-size: 12px;
  font-weight: 650;
  line-height: 1;
}

.mini-badge {
  min-height: 21px;
  font-size: 11px;
}

.phase-default {
  color: #425048;
  background: #eef1ed;
}

.phase-ok {
  color: var(--ok);
  background: var(--ok-soft);
}

.phase-active {
  color: var(--accent);
  background: var(--accent-soft);
}

.phase-warn {
  color: var(--warn);
  background: var(--warn-soft);
}

.phase-bad {
  color: var(--bad);
  background: var(--bad-soft);
}

.live-badge {
  color: var(--ok);
  background: var(--ok-soft);
}

.detail {
  min-width: 0;
  padding: 34px;
}

.empty-state {
  min-height: calc(100vh - 68px);
  display: grid;
  place-items: center;
  text-align: center;
  align-content: center;
}

.empty-state h2 {
  font-size: 32px;
}

.empty-state p {
  max-width: 460px;
}

.empty-mark {
  width: 78px;
  height: 78px;
  border-radius: 22px;
  border: 1px solid var(--border);
  background:
    linear-gradient(135deg, rgba(47,111,237,0.16), rgba(15,138,95,0.12)),
    var(--panel);
  box-shadow: var(--shadow);
  margin-bottom: 18px;
}

.hidden {
  display: none !important;
}

.detail-content {
  display: flex;
  flex-direction: column;
  gap: 16px;
  max-width: 1180px;
  margin: 0 auto;
}

.detail-header {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 22px;
}

.detail-header h2 {
  font-size: 34px;
  margin-top: 10px;
  overflow-wrap: anywhere;
}

.eyeline {
  display: flex;
  gap: 8px;
  flex-wrap: wrap;
}

.status-panel,
.note-panel,
.tabs-panel {
  background: rgba(255,255,255,0.88);
  border: 1px solid var(--border);
  border-radius: 8px;
  box-shadow: var(--shadow);
}

.status-panel {
  padding: 18px;
}

.small-label {
  display: block;
  color: var(--muted);
  font-size: 12px;
  margin-bottom: 5px;
}

#progressLabel {
  font-size: 18px;
}

.muted {
  color: var(--muted);
}

.progress-track {
  height: 9px;
  border-radius: 999px;
  background: #e7ece6;
  overflow: hidden;
  margin: 15px 0 16px;
}

#progressBar {
  height: 100%;
  width: 0%;
  background: linear-gradient(90deg, var(--accent), var(--ok));
  border-radius: inherit;
  transition: width 240ms ease;
}

.facts-grid {
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  gap: 10px;
}

.fact {
  border: 1px solid var(--border);
  background: var(--panel-soft);
  border-radius: 7px;
  padding: 10px;
  min-height: 68px;
}

.fact span {
  display: block;
  color: var(--muted);
  font-size: 12px;
  margin-bottom: 6px;
}

.fact strong {
  display: block;
  font-size: 13px;
  line-height: 1.35;
  overflow-wrap: anywhere;
}

.note-panel {
  padding: 18px;
}

.note-panel h3 {
  margin: 0 0 14px;
  font-size: 16px;
}

.note-grid {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 14px;
}

.note-grid article {
  border: 1px solid var(--border);
  background: var(--panel-soft);
  border-radius: 7px;
  padding: 14px;
  min-height: 126px;
}

.note-grid h4 {
  margin: 0 0 8px;
  font-size: 13px;
}

.copy-text {
  margin: 0;
  color: var(--muted);
  font-size: 13px;
  line-height: 1.5;
  white-space: pre-wrap;
  overflow-wrap: anywhere;
}

.tabs-panel {
  overflow: hidden;
}

.tabs {
  display: flex;
  gap: 4px;
  padding: 10px 10px 0;
  overflow-x: auto;
}

.tab {
  border-color: transparent;
  background: transparent;
  color: var(--muted);
  flex: 0 0 auto;
}

.tab.active {
  background: var(--ink);
  border-color: var(--ink);
  color: #ffffff;
}

.file-header {
  padding: 10px 14px;
  color: var(--faint);
  font-size: 12px;
  border-bottom: 1px solid var(--border);
}

.file-header span {
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.file-actions {
  display: flex;
  align-items: center;
  gap: 10px;
  flex: 0 0 auto;
}

.file-actions button {
  min-height: 30px;
  padding: 0 10px;
  font-size: 12px;
}

.file-viewer {
  margin: 0;
  min-height: 360px;
  max-height: 56vh;
  overflow: auto;
  background: #101512;
  color: #dce8df;
  padding: 16px;
  font: 12.5px/1.55 ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, "Liberation Mono", monospace;
  white-space: pre-wrap;
  overflow-wrap: anywhere;
}

@media (max-width: 920px) {
  .app-shell {
    grid-template-columns: 1fr;
  }

  .sidebar {
    min-height: auto;
    border-right: none;
    border-bottom: 1px solid var(--border);
  }

  .task-list {
    max-height: 44vh;
  }

  .detail {
    padding: 22px 16px;
  }

  .detail-header,
  .progress-row {
    align-items: flex-start;
    flex-direction: column;
  }

  .facts-grid,
  .note-grid {
    grid-template-columns: 1fr;
  }
}

@media (max-width: 560px) {
  .summary-grid {
    grid-template-columns: 1fr;
  }

  .toolbar {
    grid-template-columns: 1fr;
  }

  .detail-header h2 {
    font-size: 27px;
  }
}
"""


APP_JS = r"""const state = {
  tasks: [],
  selectedTaskId: null,
  selectedTask: null,
  selectedTab: "watch_log",
  refreshSeconds: 2,
  search: "",
};

const $ = (id) => document.getElementById(id);

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

function phaseClass(phase) {
  if (["native_message_accepted", "marker_claimed", "resume_dry_run_complete"].includes(phase)) return "phase-ok";
  if (["reserving", "registration_recovery_required", "scheduled", "watching", "event_staged", "native_message_ready", "native_message_queued", "native_message_submitting", "native_message_submitted", "native_message_deferred", "marker_claiming"].includes(phase)) return "phase-active";
  if (["marker_pending", "marker_unknown", "cancelled", "native_message_unknown"].includes(phase)) return "phase-warn";
  if (["marker_blocked", "native_message_blocked", "registration_blocked", "resume_failed", "schedule_failed"].includes(phase)) return "phase-bad";
  return "phase-default";
}

function formatTime(value) {
  if (!value) return "unknown";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return date.toLocaleString(undefined, {
    month: "short",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  });
}

function formatDuration(seconds) {
  if (seconds === null || seconds === undefined || Number.isNaN(Number(seconds))) return "unknown";
  const total = Math.max(0, Math.floor(Number(seconds)));
  const h = Math.floor(total / 3600);
  const m = Math.floor((total % 3600) / 60);
  const s = total % 60;
  if (h > 0) return `${h}h ${m}m`;
  if (m > 0) return `${m}m ${s}s`;
  return `${s}s`;
}

function formatTarget(task) {
  return task?.derived?.target_summary || "unknown target";
}

function concisePath(value) {
  const path = String(value || "");
  if (path.length <= 70) return path;
  return `${path.slice(0, 28)}...${path.slice(-36)}`;
}

async function fetchJson(path) {
  const response = await fetch(path, { cache: "no-store" });
  if (!response.ok) {
    throw new Error(`${response.status} ${response.statusText}`);
  }
  return response.json();
}

async function postJson(path, payload) {
  const response = await fetch(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload || {}),
  });
  let data = {};
  try {
    data = await response.json();
  } catch (_) {
    data = {};
  }
  if (!response.ok) {
    throw new Error(data.error || `${response.status} ${response.statusText}`);
  }
  return data;
}

function filteredTasks() {
  const query = state.search.trim().toLowerCase();
  if (!query) return state.tasks;
  return state.tasks.filter((task) => {
    const haystack = [
      task.task_id,
      task.phase,
      task.target_summary,
      task.session_id,
      task.note,
    ].join(" ").toLowerCase();
    return haystack.includes(query);
  });
}

function renderSummary(payload) {
  $("countActive").textContent = payload.counts.active;
  $("countDone").textContent = payload.counts.done;
  $("countNeedsLook").textContent = payload.counts.needs_look;
  $("stateDirLabel").textContent = concisePath(payload.state_dir);
  $("stateDirLabel").title = payload.state_dir;
  $("refreshLabel").textContent = `updated ${new Date().toLocaleTimeString()}`;
  $("liveDot").classList.toggle("paused", Boolean(payload.process_error));
}

function renderTaskList() {
  const list = $("taskList");
  const tasks = filteredTasks();
  if (!tasks.length) {
    list.innerHTML = `<div class="task-card"><div class="task-target">No matching handoffs.</div></div>`;
    return;
  }
  list.innerHTML = tasks.map((task) => {
    const active = task.task_id === state.selectedTaskId ? " active" : "";
    const cls = phaseClass(task.phase);
    const live = task.process_live ? `<span class="mini-badge phase-ok">live</span>` : "";
    return `
      <button class="task-card${active}" data-task-id="${escapeHtml(task.task_id)}" type="button">
        <span class="task-card-top">
          <span class="task-id">${escapeHtml(task.task_id)}</span>
          <span class="mini-badge ${cls}">${escapeHtml(task.phase || "unknown")}</span>
        </span>
        <span class="task-target">${escapeHtml(task.target_summary || "unknown target")}</span>
        <span class="task-card-bottom">
          <span class="task-time">${escapeHtml(formatTime(task.created_at))}</span>
          ${live}
        </span>
      </button>
    `;
  }).join("");
  list.querySelectorAll("[data-task-id]").forEach((button) => {
    button.addEventListener("click", () => selectTask(button.dataset.taskId));
  });
}

function renderFacts(summary, task) {
  const facts = [
    ["Session", task.session_id || "unknown"],
    ["Target", summary.target_summary || "unknown"],
    ["Watcher", summary.watcher_alive ? `pid ${summary.watcher_pid}` : (summary.watcher_pid ? `pid ${summary.watcher_pid} not live` : "none")],
    ["Resume", task.resume_returncode === undefined ? "not started" : `returncode ${task.resume_returncode}`],
    ["Created", formatTime(task.created_at)],
    ["Completed", formatTime(task.completed_at || task.resume_completed_at)],
    ["Reason", task.completion_reason || task.resume_retry_reason || "not complete"],
    ["State dir", concisePath(summary.state_dir || "")],
  ];
  $("factsGrid").innerHTML = facts.map(([label, value]) => `
    <div class="fact">
      <span>${escapeHtml(label)}</span>
      <strong>${escapeHtml(value)}</strong>
    </div>
  `).join("");
}

function renderFiles(payload) {
  const viewer = $("fileViewer");
  let fileInfo;
  let text;
  if (state.selectedTab === "raw") {
    fileInfo = { path: payload.task_file || "", size: JSON.stringify(payload.task, null, 2).length, truncated: false };
    text = JSON.stringify(payload.task, null, 2);
  } else {
    fileInfo = payload.files[state.selectedTab] || {};
    text = fileInfo.text || "";
    if (!fileInfo.exists) {
      text = text || `(file not found yet)\n${fileInfo.path || ""}`;
    } else if (!text.trim()) {
      text = "(file exists but is currently empty)";
    }
  }
  $("filePathLabel").textContent = concisePath(fileInfo.path || "");
  $("filePathLabel").title = fileInfo.path || "";
  $("fileSizeLabel").textContent = [
    fileInfo.size !== undefined ? `${fileInfo.size} bytes` : "",
    fileInfo.truncated ? "tail view" : "",
  ].filter(Boolean).join(" / ");
  const openButton = $("openFileButton");
  const canOpen = state.selectedTab === "raw" || Boolean(fileInfo.openable);
  openButton.disabled = !canOpen;
  openButton.textContent = "Open File";
  viewer.textContent = text;
}

function renderDetail(payload) {
  state.selectedTask = payload;
  const task = payload.task;
  const summary = payload.derived;
  $("emptyState").classList.add("hidden");
  $("detailContent").classList.remove("hidden");
  $("taskTitle").textContent = task.task_id;
  $("targetLine").textContent = summary.target_summary || "unknown target";
  $("phaseBadge").textContent = task.phase || "unknown";
  $("phaseBadge").className = `phase-badge ${phaseClass(task.phase)}`;
  $("liveBadge").classList.toggle("hidden", !summary.process_live);
  $("openTaskButton").textContent = "Open Task";
  $("openTaskButton").disabled = false;
  $("killTaskButton").textContent = "Kill Handoff";
  $("killTaskButton").disabled = !summary.stoppable;

  const ratio = Math.max(0, Math.min(1, Number(summary.progress_ratio || 0)));
  $("progressBar").style.width = `${Math.round(ratio * 100)}%`;
  $("progressLabel").textContent = `${Math.round(ratio * 100)}%`;
  $("elapsedLabel").textContent = `${formatDuration(summary.elapsed_seconds)} elapsed / ${formatDuration(task.max_wait_seconds)} max`;

  $("noteText").textContent = task.note || "No scheduler note recorded.";
  $("continuationText").textContent = task.continuation_prompt_text || "No extra continuation prompt recorded.";
  renderFacts(summary, task);
  renderFiles(payload);
}

async function loadSelectedTask() {
  if (!state.selectedTaskId) return;
  const payload = await fetchJson(`/api/task/${encodeURIComponent(state.selectedTaskId)}?file=${encodeURIComponent(state.selectedTab)}`);
  renderDetail(payload);
}

async function refresh() {
  try {
    const payload = await fetchJson("/api/summary");
    state.tasks = payload.tasks;
    state.refreshSeconds = payload.refresh_seconds || state.refreshSeconds;
    renderSummary(payload);
    if (!state.selectedTaskId && state.tasks.length) {
      state.selectedTaskId = state.tasks[0].task_id;
    }
    renderTaskList();
    await loadSelectedTask();
  } catch (error) {
    $("liveDot").classList.add("paused");
    $("refreshLabel").textContent = `error: ${error.message}`;
  }
}

async function selectTask(taskId) {
  state.selectedTaskId = taskId;
  renderTaskList();
  await loadSelectedTask();
}

function currentOpenKind() {
  return state.selectedTab === "raw" ? "task" : state.selectedTab;
}

async function openKind(kind, button) {
  if (!state.selectedTaskId) return;
  const original = button.textContent;
  button.disabled = true;
  button.textContent = "Opening";
  try {
    await postJson(`/api/task/${encodeURIComponent(state.selectedTaskId)}/open`, { kind });
    button.textContent = "Opened";
    window.setTimeout(() => {
      button.textContent = original;
      if (state.selectedTask) renderFiles(state.selectedTask);
    }, 900);
  } catch (error) {
    button.textContent = "Open failed";
    window.setTimeout(() => {
      button.textContent = original;
      if (state.selectedTask) renderFiles(state.selectedTask);
      $("refreshLabel").textContent = `open error: ${error.message}`;
    }, 1200);
  }
}

async function openCurrentFile() {
  await openKind(currentOpenKind(), $("openFileButton"));
}

async function openTaskFile() {
  await openKind("task", $("openTaskButton"));
}

async function killSelectedTask() {
  if (!state.selectedTaskId || !state.selectedTask) return;
  const taskId = state.selectedTaskId;
  const ok = window.confirm(`Stop the exact watcher for ${taskId}? This does not stop the watched target process.`);
  if (!ok) return;
  const button = $("killTaskButton");
  button.disabled = true;
  button.textContent = "Killing";
  try {
    await postJson(`/api/task/${encodeURIComponent(taskId)}/stop`, { confirmTaskId: taskId });
    button.textContent = "Killed";
    await refresh();
  } catch (error) {
    button.textContent = "Kill failed";
    $("refreshLabel").textContent = `kill error: ${error.message}`;
    window.setTimeout(() => {
      button.textContent = "Kill Handoff";
      if (state.selectedTask) $("killTaskButton").disabled = !state.selectedTask.derived.stoppable;
    }, 1300);
  }
}

function installEvents() {
  $("refreshButton").addEventListener("click", refresh);
  $("searchInput").addEventListener("input", (event) => {
    state.search = event.target.value;
    renderTaskList();
  });
  $("copyTaskButton").addEventListener("click", async () => {
    if (!state.selectedTaskId) return;
    await navigator.clipboard.writeText(state.selectedTaskId);
    $("copyTaskButton").textContent = "Copied";
    window.setTimeout(() => $("copyTaskButton").textContent = "Copy ID", 900);
  });
  $("openFileButton").addEventListener("click", openCurrentFile);
  $("openTaskButton").addEventListener("click", openTaskFile);
  $("killTaskButton").addEventListener("click", killSelectedTask);
  document.querySelectorAll(".tab").forEach((button) => {
    button.addEventListener("click", async () => {
      document.querySelectorAll(".tab").forEach((item) => item.classList.remove("active"));
      button.classList.add("active");
      state.selectedTab = button.dataset.tab;
      if (state.selectedTask) {
        renderFiles(state.selectedTask);
        await loadSelectedTask();
      }
    });
  });
}

installEvents();
refresh();
window.setInterval(refresh, Math.max(1000, state.refreshSeconds * 1000));
"""


def now_utc() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def parse_timestamp(value: Any) -> float:
    if not value:
        return 0.0
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


def load_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        payload = json.load(handle)
    if isinstance(payload, dict):
        return payload
    return {}


def read_text_tail(path_value: Any, max_chars: int) -> dict[str, Any]:
    if not path_value:
        return {"path": "", "exists": False, "openable": False, "size": 0, "truncated": False, "text": ""}
    path = Path(str(path_value)).expanduser()
    try:
        size = path.stat().st_size
    except OSError:
        return {"path": str(path), "exists": False, "openable": False, "size": 0, "truncated": False, "text": ""}
    read_size = min(max(int(max_chars), 0), size)
    try:
        with path.open("rb") as handle:
            if size > read_size:
                handle.seek(size - read_size)
            data = handle.read(read_size)
    except OSError as exc:
        return {
            "path": str(path),
            "exists": True,
            "openable": True,
            "size": size,
            "truncated": False,
            "text": f"Could not read file: {exc}",
        }
    return {
        "path": str(path),
        "exists": True,
        "openable": True,
        "size": size,
        "truncated": size > read_size,
        "text": data.decode("utf-8", errors="replace"),
    }


def observed_log_metadata(task: dict[str, Any]) -> dict[str, Any]:
    observed_log = task.get("observed_log")
    if not isinstance(observed_log, dict) or not observed_log.get("path"):
        return {
            "path": "",
            "exists": False,
            "openable": False,
            "configured": False,
            "size": 0,
            "truncated": False,
            "text": "No observed log was recorded for this handoff.",
        }
    scope = str(observed_log.get("scope") or "local")
    host = str(observed_log.get("host") or "")
    path = str(observed_log.get("path") or "")
    label = str(observed_log.get("label") or "Observed Log")
    display_path = f"{host}:{path}" if scope == "remote" and host else path
    return {
        "path": display_path,
        "exists": False,
        "openable": False,
        "configured": True,
        "scope": scope,
        "host": host,
        "label": label,
        "size": 0,
        "truncated": False,
        "text": f"{label} is configured but has not been loaded yet.",
    }


def read_observed_log(task: dict[str, Any], max_chars: int) -> dict[str, Any]:
    observed_log = task.get("observed_log")
    metadata = observed_log_metadata(task)
    if not metadata.get("configured"):
        return metadata
    assert isinstance(observed_log, dict)
    scope = str(observed_log.get("scope") or "local")
    path = str(observed_log.get("path") or "")
    if scope == "local":
        payload = read_text_tail(path, max_chars)
        payload.update({
            "configured": True,
            "scope": "local",
            "label": metadata.get("label") or "Observed Log",
        })
        return payload
    if scope != "remote":
        metadata["text"] = f"Unsupported observed log scope: {scope}"
        return metadata
    host = str(observed_log.get("host") or "")
    if not host:
        metadata["text"] = "Remote observed log is missing a host."
        return metadata
    try:
        host = validate_remote_host(host)
    except ProcessIdentityError as exc:
        metadata["text"] = f"Unsafe remote observed-log host: {exc}"
        return metadata
    command = (
        f"path={shlex.quote(path)}; "
        f"if [ ! -e \"$path\" ]; then echo __CODEX_LOG_MISSING__; exit 4; fi; "
        f"printf '__CODEX_SIZE__'; wc -c < \"$path\" 2>/dev/null || echo 0; "
        f"tail -c {max(int(max_chars), 1000)} -- \"$path\""
    )
    try:
        result = subprocess.run(
            [
                "ssh",
                "-o",
                "BatchMode=yes",
                "-o",
                "ConnectTimeout=5",
                "--",
                host,
                command,
            ],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except subprocess.TimeoutExpired:
        metadata["text"] = f"SSH timed out while reading {host}:{path}"
        return metadata
    except OSError as exc:
        metadata["text"] = f"Could not start ssh: {exc}"
        return metadata
    if result.returncode == 4 or "__CODEX_LOG_MISSING__" in result.stdout:
        metadata["text"] = f"Remote log does not exist yet: {host}:{path}"
        return metadata
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or f"ssh exited with {result.returncode}"
        metadata["text"] = detail
        return metadata
    size = 0
    text = result.stdout
    if text.startswith("__CODEX_SIZE__"):
        first_line, _, remainder = text.partition("\n")
        try:
            size = int(first_line.replace("__CODEX_SIZE__", "").strip() or "0")
        except ValueError:
            size = 0
        text = remainder
    return {
        "path": f"{host}:{path}",
        "exists": True,
        "openable": False,
        "configured": True,
        "scope": "remote",
        "host": host,
        "label": metadata.get("label") or "Observed Log",
        "size": size,
        "truncated": size > max_chars,
        "text": text,
    }


def process_rows() -> tuple[list[dict[str, Any]], str | None]:
    try:
        result = subprocess.run(
            ["ps", "-Ao", "pid=,ppid=,command="],
            capture_output=True,
            text=True,
            timeout=8,
            check=False,
        )
    except Exception as exc:
        return [], str(exc)
    if result.returncode != 0:
        return [], result.stderr.strip() or result.stdout.strip() or f"ps exited with {result.returncode}"
    rows: list[dict[str, Any]] = []
    for line in result.stdout.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        parts = stripped.split(None, 2)
        if len(parts) < 3:
            continue
        try:
            rows.append({"pid": int(parts[0]), "ppid": int(parts[1]), "command": parts[2]})
        except ValueError:
            continue
    return rows, None


def task_sort_key(task: dict[str, Any], task_file: Path) -> float:
    for key in ("created_at", "watch_started_at", "watch_loop_started_at", "completed_at", "resume_started_at"):
        value = parse_timestamp(task.get(key))
        if value:
            return value
    try:
        return task_file.stat().st_mtime
    except OSError:
        return 0.0


def load_tasks(state_dir: Path) -> list[dict[str, Any]]:
    tasks_dir = state_dir / "tasks"
    if not tasks_dir.exists():
        return []
    tasks: list[dict[str, Any]] = []
    for task_file in sorted(tasks_dir.glob("*.json")):
        try:
            task = load_json(task_file)
        except (OSError, json.JSONDecodeError):
            continue
        task.setdefault("task_id", task_file.stem)
        task["_task_file"] = str(task_file)
        task["_sort_key"] = task_sort_key(task, task_file)
        tasks.append(task)
    return sorted(tasks, key=lambda item: float(item.get("_sort_key") or 0.0), reverse=True)


def target_summary(target: dict[str, Any] | None) -> str:
    if not isinstance(target, dict):
        return "unknown target"
    scope = str(target.get("scope") or "local")
    mode = str(target.get("mode") or "")
    parts = [scope]
    if scope == "remote" and target.get("host"):
        parts.append(str(target["host"]))
    if mode == "pid":
        parts.append(f"pid {target.get('pid')}")
    elif mode == "pattern":
        parts.append(f"pattern {target.get('pattern')!r}")
    else:
        parts.append("unknown mode")
    return " ".join(parts)


def elapsed_seconds(task: dict[str, Any], generated_at: datetime) -> int | None:
    if task.get("wait_elapsed_seconds") is not None:
        try:
            return max(int(task["wait_elapsed_seconds"]), 0)
        except (TypeError, ValueError):
            return None
    start_value = task.get("watch_loop_started_at") or task.get("watch_started_at") or task.get("created_at")
    start_ts = parse_timestamp(start_value)
    if not start_ts:
        return None
    return max(int(generated_at.timestamp() - start_ts), 0)


def derive_task(task: dict[str, Any], rows: list[dict[str, Any]], state_dir: Path, generated_at: datetime) -> dict[str, Any]:
    task_id_value = str(task.get("task_id") or "")
    watcher_pid = int(task.get("watcher_pid") or 0)
    watcher_alive = False
    watcher_identity_status = "missing"
    raw_watcher_identity = task.get("watcher_identity")
    if isinstance(raw_watcher_identity, dict):
        try:
            watcher_identity = ProcessIdentity.from_dict(raw_watcher_identity)
            if watcher_identity.pid != watcher_pid:
                raise ProcessIdentityError(
                    "watcher_identity does not match watcher_pid"
                )
            probe = probe_local_identity(watcher_identity)
            watcher_identity_status = probe.status
            watcher_alive = probe.status == "alive"
        except (KeyError, TypeError, ValueError, ProcessIdentityError) as error:
            watcher_identity_status = f"invalid: {error}"
    related_pids = [watcher_pid] if watcher_alive else []
    phase = str(task.get("phase") or "unknown")
    elapsed = elapsed_seconds(task, generated_at)
    max_wait = int(task.get("max_wait_seconds") or 0)
    ratio = 0.0
    if elapsed is not None and max_wait > 0:
        ratio = min(max(elapsed / max_wait, 0.0), 1.0)
    elif phase in SUCCESS_PHASES:
        ratio = 1.0
    process_live = watcher_alive
    return {
        "task_id": task_id_value,
        "phase": phase,
        "created_at": task.get("created_at"),
        "session_id": task.get("session_id"),
        "target_summary": target_summary(task.get("target")),
        "note": task.get("note") or "",
        "state_dir": str(state_dir),
        "watcher_pid": watcher_pid or None,
        "watcher_alive": watcher_alive,
        "watcher_identity_status": watcher_identity_status,
        "related_pids": related_pids,
        "process_live": process_live,
        "stoppable": bool(process_live or phase in ACTIVE_PHASES),
        "elapsed_seconds": elapsed,
        "progress_ratio": ratio,
    }


def task_summary(task: dict[str, Any], derived: dict[str, Any]) -> dict[str, Any]:
    return {
        "task_id": derived["task_id"],
        "phase": derived["phase"],
        "created_at": derived["created_at"],
        "session_id": derived["session_id"],
        "target_summary": derived["target_summary"],
        "note": derived["note"],
        "process_live": derived["process_live"],
        "watcher_alive": derived["watcher_alive"],
        "completion_reason": task.get("completion_reason"),
        "resume_returncode": task.get("resume_returncode"),
    }


def counts_for(tasks: list[dict[str, Any]], derived_by_id: dict[str, dict[str, Any]]) -> dict[str, int]:
    active = 0
    done = 0
    needs_look = 0
    for task in tasks:
        phase = str(task.get("phase") or "unknown")
        derived = derived_by_id.get(str(task.get("task_id") or ""), {})
        if phase in ACTIVE_PHASES or derived.get("process_live"):
            active += 1
        if phase in SUCCESS_PHASES:
            done += 1
        if phase in ERROR_PHASES or phase in WARNING_PHASES:
            needs_look += 1
    return {"active": active, "done": done, "needs_look": needs_look}


class DashboardServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, server_address: tuple[str, int], handler: type[BaseHTTPRequestHandler], config: dict[str, Any]):
        super().__init__(server_address, handler)
        self.config = config


class DashboardHandler(BaseHTTPRequestHandler):
    server: DashboardServer

    def log_message(self, format: str, *args: Any) -> None:
        if self.server.config.get("quiet"):
            return
        super().log_message(format, *args)

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/":
            self.send_text(INDEX_HTML, "text/html; charset=utf-8")
            return
        if parsed.path == "/app.css":
            self.send_text(APP_CSS, "text/css; charset=utf-8")
            return
        if parsed.path == "/app.js":
            self.send_text(APP_JS, "application/javascript; charset=utf-8")
            return
        if parsed.path == "/api/summary":
            self.send_json(self.summary_payload(parse_qs(parsed.query)))
            return
        if parsed.path.startswith("/api/task/"):
            task_id_value = parsed.path.rsplit("/", 1)[-1]
            payload = self.task_payload(task_id_value, parse_qs(parsed.query))
            if payload is not None:
                self.send_json(payload)
            return
        if parsed.path == "/api/health":
            self.send_json({"status": "ok", "generated_at": now_utc()})
            return
        self.send_error(HTTPStatus.NOT_FOUND, "Not found")

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        parts = [part for part in parsed.path.split("/") if part]
        if len(parts) != 4 or parts[0] != "api" or parts[1] != "task":
            self.send_json_error("Not found", HTTPStatus.NOT_FOUND)
            return
        task_id_value = parts[2]
        action = parts[3]
        payload = self.read_json_body()
        if payload is None:
            return
        if action == "open":
            result = self.open_task_file(task_id_value, payload)
            if result is not None:
                self.send_json(result)
            return
        if action == "stop":
            result = self.stop_task(task_id_value, payload)
            if result is not None:
                self.send_json(result)
            return
        self.send_json_error("Not found", HTTPStatus.NOT_FOUND)

    def read_json_body(self) -> dict[str, Any] | None:
        try:
            content_length = int(self.headers.get("Content-Length") or "0")
        except ValueError:
            self.send_json_error("Invalid Content-Length", HTTPStatus.BAD_REQUEST)
            return None
        if content_length > 65536:
            self.send_json_error("Request body too large", HTTPStatus.REQUEST_ENTITY_TOO_LARGE)
            return None
        raw = self.rfile.read(content_length) if content_length else b"{}"
        try:
            payload = json.loads(raw.decode("utf-8") or "{}")
        except json.JSONDecodeError:
            self.send_json_error("Invalid JSON body", HTTPStatus.BAD_REQUEST)
            return None
        if not isinstance(payload, dict):
            self.send_json_error("JSON body must be an object", HTTPStatus.BAD_REQUEST)
            return None
        return payload

    def send_text(self, text: str, content_type: str) -> None:
        data = text.encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def send_json_error(self, error: str, status: HTTPStatus) -> None:
        self.send_json({"error": error, "status": int(status)}, status)

    def send_json(self, payload: dict[str, Any], status: HTTPStatus = HTTPStatus.OK) -> None:
        data = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def state_dir(self) -> Path:
        return Path(str(self.server.config["state_dir"])).expanduser().resolve()

    def task_file_path(self, task_id_value: str) -> Path | None:
        if not TASK_ID_RE.match(task_id_value):
            self.send_json_error("Invalid task id", HTTPStatus.BAD_REQUEST)
            return None
        task_file = self.state_dir() / "tasks" / f"{task_id_value}.json"
        if not task_file.exists():
            self.send_json_error("Task not found", HTTPStatus.NOT_FOUND)
            return None
        return task_file

    def load_task_for_action(self, task_id_value: str) -> tuple[Path, dict[str, Any]] | None:
        task_file = self.task_file_path(task_id_value)
        if task_file is None:
            return None
        try:
            return task_file, load_json(task_file)
        except (OSError, json.JSONDecodeError) as exc:
            self.send_json_error(str(exc), HTTPStatus.INTERNAL_SERVER_ERROR)
            return None

    def allowed_file_path(self, task_file: Path, task: dict[str, Any], kind_value: Any) -> Path | None:
        kind = str(kind_value or "")
        if kind not in OPEN_FILE_KINDS:
            self.send_json_error("Unsupported file kind", HTTPStatus.BAD_REQUEST)
            return None
        if kind in {"raw", "task"}:
            candidate = task_file
        elif kind == "observed_log":
            observed_log = task.get("observed_log")
            if not isinstance(observed_log, dict) or not observed_log.get("path"):
                self.send_json_error("No observed log recorded for this task", HTTPStatus.NOT_FOUND)
                return None
            if str(observed_log.get("scope") or "local") == "remote":
                self.send_json_error("Remote observed logs can be viewed in the dashboard but not opened as local files", HTTPStatus.CONFLICT)
                return None
            candidate = Path(str(observed_log["path"])).expanduser()
            try:
                resolved = candidate.resolve()
            except OSError:
                self.send_json_error("Observed log file does not exist yet", HTTPStatus.NOT_FOUND)
                return None
            if not resolved.exists():
                self.send_json_error("Observed log file does not exist yet", HTTPStatus.NOT_FOUND)
                return None
            return resolved
        else:
            field = TEXT_FILE_FIELDS[kind]
            raw_path = task.get(field)
            if not raw_path:
                self.send_json_error(f"No path recorded for {kind}", HTTPStatus.NOT_FOUND)
                return None
            candidate = Path(str(raw_path)).expanduser()
        try:
            resolved = candidate.resolve()
            resolved.relative_to(self.state_dir())
        except (OSError, ValueError):
            self.send_json_error("Refusing to open a file outside the dashboard state directory", HTTPStatus.FORBIDDEN)
            return None
        if not resolved.exists():
            self.send_json_error("File does not exist yet", HTTPStatus.NOT_FOUND)
            return None
        return resolved

    def open_task_file(self, task_id_value: str, body: dict[str, Any]) -> dict[str, Any] | None:
        loaded = self.load_task_for_action(task_id_value)
        if loaded is None:
            return None
        task_file, task = loaded
        path = self.allowed_file_path(task_file, task, body.get("kind"))
        if path is None:
            return None
        opener = ["open"] if sys.platform == "darwin" else ([shutil.which("xdg-open")] if shutil.which("xdg-open") else [])
        if not opener or not opener[0]:
            self.send_json_error("No supported local file opener is available", HTTPStatus.INTERNAL_SERVER_ERROR)
            return None
        try:
            subprocess.Popen(
                [str(opener[0]), str(path)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
        except OSError as exc:
            self.send_json_error(str(exc), HTTPStatus.INTERNAL_SERVER_ERROR)
            return None
        return {"status": "opened", "task_id": task_id_value, "path": str(path), "kind": str(body.get("kind") or "")}

    def stop_task(self, task_id_value: str, body: dict[str, Any]) -> dict[str, Any] | None:
        if body.get("confirmTaskId") != task_id_value:
            self.send_json_error("Task id confirmation mismatch", HTTPStatus.CONFLICT)
            return None
        loaded = self.load_task_for_action(task_id_value)
        if loaded is None:
            return None
        task_file, task = loaded
        rows, process_error = process_rows()
        derived = derive_task(task, rows, self.state_dir(), datetime.now(timezone.utc).replace(microsecond=0))
        if not derived.get("stoppable"):
            self.send_json_error("Handoff is not active/live, so the dashboard will not mutate it", HTTPStatus.CONFLICT)
            return None
        script_path = Path(__file__).with_name("codex_wait_handoff.py")
        command = [
            sys.executable,
            str(script_path),
            "stop",
            "--task-id",
            task_id_value,
            "--state-dir",
            str(self.state_dir()),
            "--json",
        ]
        try:
            result = subprocess.run(command, capture_output=True, text=True, timeout=30, check=False)
        except subprocess.TimeoutExpired:
            self.send_json_error("Timed out while stopping handoff", HTTPStatus.INTERNAL_SERVER_ERROR)
            return None
        if result.returncode != 0:
            detail = result.stderr.strip() or result.stdout.strip() or f"stop exited with {result.returncode}"
            self.send_json_error(detail, HTTPStatus.INTERNAL_SERVER_ERROR)
            return None
        try:
            stop_result = json.loads(result.stdout)
        except json.JSONDecodeError:
            stop_result = {"raw_stdout": result.stdout.strip()}
        return {
            "status": "stopped",
            "task_id": task_id_value,
            "process_error": process_error,
            "task_file": str(task_file),
            "stop_result": stop_result,
        }

    def summary_payload(self, query: dict[str, list[str]]) -> dict[str, Any]:
        state_dir = self.state_dir()
        limit = int(self.server.config["limit"])
        if "limit" in query and query["limit"]:
            try:
                limit = max(1, min(int(query["limit"][0]), 500))
            except ValueError:
                pass
        generated_at = datetime.now(timezone.utc).replace(microsecond=0)
        rows, process_error = process_rows()
        tasks = load_tasks(state_dir)
        derived_by_id = {
            str(task.get("task_id") or ""): derive_task(task, rows, state_dir, generated_at)
            for task in tasks
        }
        return {
            "state_dir": str(state_dir),
            "generated_at": generated_at.isoformat().replace("+00:00", "Z"),
            "refresh_seconds": float(self.server.config["refresh_seconds"]),
            "process_error": process_error,
            "total_count": len(tasks),
            "counts": counts_for(tasks, derived_by_id),
            "tasks": [task_summary(task, derived_by_id[str(task.get("task_id") or "")]) for task in tasks[:limit]],
        }

    def task_payload(self, task_id_value: str, query: dict[str, list[str]] | None = None) -> dict[str, Any] | None:
        state_dir = self.state_dir()
        task_file = self.task_file_path(task_id_value)
        if task_file is None:
            return None
        try:
            task = load_json(task_file)
        except (OSError, json.JSONDecodeError) as exc:
            self.send_json_error(str(exc), HTTPStatus.INTERNAL_SERVER_ERROR)
            return None
        rows, process_error = process_rows()
        generated_at = datetime.now(timezone.utc).replace(microsecond=0)
        derived = derive_task(task, rows, state_dir, generated_at)
        max_log_chars = int(self.server.config["max_log_chars"])
        files = {
            label: read_text_tail(task.get(field), max_log_chars)
            for label, field in TEXT_FILE_FIELDS.items()
        }
        selected_file = ((query or {}).get("file") or [""])[0]
        files["observed_log"] = (
            read_observed_log(task, max_log_chars)
            if selected_file == "observed_log"
            else observed_log_metadata(task)
        )
        return {
            "state_dir": str(state_dir),
            "generated_at": generated_at.isoformat().replace("+00:00", "Z"),
            "process_error": process_error,
            "task_file": str(task_file),
            "task": {key: value for key, value in task.items() if not key.startswith("_")},
            "derived": derived,
            "files": files,
        }


def bind_server(host: str, port: int, config: dict[str, Any]) -> DashboardServer:
    candidates = [0] if port == 0 else list(range(port, port + 20))
    last_error: OSError | None = None
    for candidate in candidates:
        try:
            return DashboardServer((host, candidate), DashboardHandler, config)
        except OSError as exc:
            last_error = exc
            if port == 0:
                break
    raise SystemExit(f"Could not bind dashboard server: {last_error}")


def command_serve(args: Any) -> int:
    state_dir = Path(args.state_dir).expanduser().resolve()
    dashboard_host = str(args.host)
    if dashboard_host.lower() != "localhost":
        try:
            is_loopback = ipaddress.ip_address(dashboard_host).is_loopback
        except ValueError:
            is_loopback = False
        if not is_loopback:
            raise SystemExit(
                "Dashboard contains task control and log data; --host must be a literal "
                "loopback address or localhost."
            )
    config = {
        "state_dir": str(state_dir),
        "limit": max(int(args.limit), 1),
        "refresh_seconds": max(float(args.refresh_seconds), 0.5),
        "max_log_chars": max(int(args.max_log_chars), 1000),
        "quiet": bool(args.quiet),
    }
    server = bind_server(dashboard_host, int(args.port), config)
    actual_host, actual_port = server.server_address[:2]
    display_host = "127.0.0.1" if actual_host in {"", "0.0.0.0"} else actual_host
    url = f"http://{display_host}:{actual_port}/"
    print(f"Serving Codex wait handoff dashboard at {url}")
    print(f"state_dir: {state_dir}")
    print("Press Ctrl-C to stop.")
    if args.open:
        webbrowser.open(url)
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        print("\nDashboard stopped.")
    finally:
        server.server_close()
    return 0
