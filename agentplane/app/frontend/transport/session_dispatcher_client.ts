import { fromJson } from "@bufbuild/protobuf";

import {
  WatchSessionsResponseSchema,
  type WatchSessionsResponse,
} from "../../../session_dispatcher/dispatcher/api/session/session_dispatcher_pb";
import { fetchWithLogin, HttpError } from "./client";
import { followWatch, WatchRpcError, type WatchHandlers } from "./watch_stream";

/** One browser client for the generated SessionDispatcher facade, through the App proxy. */
export const sessionDispatcherClient = {
  post(path: string, body: string): Promise<Response> {
    return fetchWithLogin(new URL(`/upstream/dispatcher/v1${path}`, window.location.href), {
      method: "POST",
      headers: { "Content-Type": "application/json", Accept: "application/json" },
      body,
    });
  },
  watchDispatches(query: URLSearchParams, handlers: WatchHandlers): () => void {
    return followWatch(`/upstream/dispatcher/v1/dispatches:watch?${query}`, handlers);
  },
  watchSessions(
    handlers: Omit<WatchHandlers, "onOpen" | "onFrame" | "onError"> & {
      onOpen: (snapshot: boolean) => void;
      onFrame: (frame: WatchSessionsResponse) => void;
    }
  ): () => void {
    // Scoped to this browser watch's lifetime; never persisted across a login or page reload.
    let token = "";
    return followWatch(() => `/upstream/dispatcher/v1/sessions:watch?${new URLSearchParams({ resume_token: token })}`, {
      ...handlers,
      onOpen: () => handlers.onOpen(token === ""),
      onFrame: (value) => {
        const frame = fromJson(WatchSessionsResponseSchema, value);
        if (frame.frame.case === undefined) throw new Error("Dispatcher returned an empty Session watch frame");
        handlers.onFrame(frame);
        token = frame.resumeToken;
      },
      onError: (reason) => {
        if ((reason instanceof WatchRpcError || reason instanceof HttpError) && reason.code === 3) token = "";
      },
    });
  },
};
