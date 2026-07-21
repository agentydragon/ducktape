import { useEffect } from "react";

import { consumeOAuthConnectionResult } from "./client.ts";
import { SETTINGS_PATH, type ConsoleView } from "./routing.ts";
import { toastError, toastSuccess } from "./toast.ts";

const RESULT_ID = /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;

export function useOAuthResultAnnouncement(view: ConsoleView): void {
  useEffect(() => {
    if (view !== "settings" || window.location.pathname !== SETTINGS_PATH) return;
    const url = new URL(window.location.href);
    const resultId = url.searchParams.get("oauth_result");
    if (resultId === null) return;

    // The result is single-use. Remove its opaque locator before fetching so refresh/back cannot
    // accidentally attempt to replay it, and so the settled settings URL is bookmarkable.
    url.searchParams.delete("oauth_result");
    history.replaceState(null, "", `${url.pathname}${url.search}${url.hash}`);

    if (!RESULT_ID.test(resultId)) {
      toastError("Connection result unavailable", "The connection result link is invalid.");
      return;
    }
    void consumeOAuthConnectionResult(resultId).then(
      (result) => {
        if (result.status === "success") toastSuccess(result.title, result.message);
        else toastError(result.title, result.message);
      },
      (error: unknown) => toastError("Connection result unavailable", error)
    );
  }, [view]);
}
