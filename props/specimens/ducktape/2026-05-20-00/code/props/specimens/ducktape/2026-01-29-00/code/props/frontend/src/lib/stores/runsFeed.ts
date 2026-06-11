/**
 * WebSocket-based store for live runs and jobs feed.
 */
import { writable, derived } from "svelte/store";
import type { RunInfo, JobInfo } from "$lib/api/client";

// State
export const runs = writable<RunInfo[]>([]);
export const jobs = writable<JobInfo[]>([]);
export const connected = writable(false);

// Derived stores
export const activeJobs = derived(jobs, ($jobs) => $jobs.filter((j) => j.status === "running"));

// WebSocket connection (singleton)
let ws: WebSocket | null = null;
let reconnectTimer: ReturnType<typeof setTimeout> | null = null;
let started = false;

function getWsUrl(): string {
  const protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
  return `${protocol}//${window.location.host}/api/runs/feed`;
}

function doConnect() {
  if (ws) {
    ws.close();
    ws = null;
  }

  try {
    ws = new WebSocket(getWsUrl());
  } catch (e) {
    console.warn("Failed to create WebSocket:", e);
    scheduleReconnect();
    return;
  }

  ws.onopen = () => {
    connected.set(true);
  };

  ws.onmessage = (event) => {
    try {
      const msg = JSON.parse(event.data);
      if (msg.type === "runs") {
        runs.set(msg.runs);
      } else if (msg.type === "jobs") {
        jobs.set(msg.jobs);
      }
    } catch (e) {
      console.warn("Failed to parse feed message:", e);
    }
  };

  ws.onclose = () => {
    connected.set(false);
    ws = null;
    scheduleReconnect();
  };

  ws.onerror = () => {
    // onclose will be called after this
  };
}

function scheduleReconnect() {
  if (reconnectTimer) return;
  reconnectTimer = setTimeout(() => {
    reconnectTimer = null;
    doConnect();
  }, 3000);
}

/** Start the WebSocket connection. Safe to call multiple times. */
export function startFeed() {
  if (started) return;
  started = true;
  doConnect();
}

/** Stop the WebSocket connection and cleanup. */
export function stopFeed() {
  started = false;
  if (reconnectTimer) {
    clearTimeout(reconnectTimer);
    reconnectTimer = null;
  }
  if (ws) {
    ws.close();
    ws = null;
  }
  connected.set(false);
}
