import { toJson, type JsonObject } from "@bufbuild/protobuf";

import type { RunnerStatusView } from "../schema";
import { api, ok } from "../client";
import { SessionSpecSchema, type SessionSpec } from "../../../proto/runner_pb";

export type SessionOpening = {
  sessionId: string;
  sandbox: string;
  state: string;
  reason?: string;
};

/**
 * Create the Session `sessionId`, a fresh UUID, names. On an ambiguous response, look for its
 * Thread (`findThread`); never re-run bootstrap implicitly.
 */
export async function openSession(
  sandbox: string,
  sessionId: string,
  spec: SessionSpec,
  setupScript?: string
): Promise<SessionOpening> {
  const data = ok(
    await api.POST("/sandboxes/{name}/sessions", {
      params: { path: { name: sandbox } },
      body: {
        session_id: sessionId,
        spec: toJson(SessionSpecSchema, spec) as JsonObject,
        setup_script: setupScript,
      },
    })
  );
  return data as SessionOpening;
}

export async function getSessionOpening(sandbox: string, sessionId: string): Promise<SessionOpening> {
  return ok(
    await api.GET("/sandboxes/{name}/sessions/{session_id}/opening", {
      params: { path: { name: sandbox, session_id: sessionId } },
    })
  ) as SessionOpening;
}

export async function getRunnerStatus(sandbox: string): Promise<RunnerStatusView> {
  return ok(
    await api.GET("/sandboxes/{name}/runner-status", {
      params: { path: { name: sandbox } },
    })
  );
}

/** Asks for the Sandbox to run; the watch behind the live streams brings back the change. */
export async function resumeSandbox(name: string): Promise<void> {
  ok(await api.POST("/sandboxes/{name}/resume", { params: { path: { name } } }));
}
