import { Loader, Text } from "@mantine/core";
import { useEffect, useState } from "react";

import { type ConfigResponse, displayableError, fetchConfig } from "./client";
import { HakuUiEmbed } from "./haku_ui_embed";
import { OAuthResultPage } from "./oauth_result_page";
import { useOAuthResultAnnouncement } from "./oauth_result_announcement";
import { useConsoleView } from "./routing";

// The trusted outer shell: a full-page frame for Haku's own UI (a sandboxed cross-origin iframe)
// plus the bridge that brokers the iframe's privileged requests (opening links, launching a run).
// Product chrome lives in haku-ui; only the trusted confirm + capability firing are here.
// Console-owned read surfaces have their own routes (routing.ts). See README + docs/containment.md.
export default function App() {
  const [config, setConfig] = useState<ConfigResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const { view, agentEnrollmentId, oauthResultId, toolCallId, conversationId, sessionFramesId, navigate } =
    useConsoleView();
  useOAuthResultAnnouncement(view);

  useEffect(() => {
    let alive = true;
    fetchConfig()
      .then((c) => {
        if (alive) setConfig(c);
      })
      .catch((e: unknown) => {
        if (alive) setError(displayableError(e));
      });
    return () => {
      alive = false;
    };
  }, []);

  if (view === "oauthResult" && oauthResultId !== null) return <OAuthResultPage resultId={oauthResultId} />;

  // The initial config load is the one thing rendered by the shell itself; a failure
  // leaves nothing to frame, so it gets a persistent page-level message — except while the
  // login redirect below is in flight, where the loader carries the handover instead.
  if (error)
    return (
      <Text c="red" className="mx-auto max-w-3xl p-4">
        Failed to load: {error}
      </Text>
    );
  if (!config)
    return (
      <div className="flex justify-center p-8">
        <Loader />
      </div>
    );

  // launch_routine_url is set iff the launch capability is configured (it's the routine's
  // page URL); pass that as whether the shell can honor a requestLaunch from the iframe.
  return (
    <HakuUiEmbed
      uiUrl={config.haku_ui_url}
      launchAvailable={config.launch_routine_url != null}
      view={view}
      agentEnrollmentId={agentEnrollmentId}
      toolCallId={toolCallId}
      conversationId={conversationId}
      sessionFramesId={sessionFramesId}
      onNavigate={navigate}
    />
  );
}
