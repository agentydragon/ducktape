import { useState, type JSX } from "react";

import { finishPairing, startPairing, type SyncStatus } from "./api";
import { sentence } from "./status";

type Props = {
  paired: boolean;
  /** Called with the fresh status once a pairing completes. */
  onPaired: (status: SyncStatus) => void;
};

/**
 * The paste-the-redirect flow: Claude's authorize page ends by sending the browser to a loopback
 * address nothing listens on, so the page fails to load and the URL in the address bar is the
 * result. The human copies it back here.
 */
export function Pairing({ paired, onPaired }: Props): JSX.Element {
  const [authorizationUrl, setAuthorizationUrl] = useState<string | null>(null);
  const [redirectUrl, setRedirectUrl] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const run = async (action: () => Promise<void>): Promise<void> => {
    setBusy(true);
    setError(null);
    try {
      await action();
    } catch (reason) {
      setError(reason instanceof Error ? sentence(reason.message) : "Something went wrong.");
    } finally {
      setBusy(false);
    }
  };

  const start = (): Promise<void> =>
    run(async () => {
      setAuthorizationUrl((await startPairing()).authorization_url);
      setRedirectUrl("");
    });

  const finish = (): Promise<void> =>
    run(async () => {
      const status = await finishPairing(redirectUrl);
      setAuthorizationUrl(null);
      setRedirectUrl("");
      onPaired(status);
    });

  return (
    <section aria-labelledby="pairing-heading">
      <h2 id="pairing-heading">{paired ? "Pair again" : "Pair with Claude"}</h2>
      {authorizationUrl === null ? (
        <>
          <p>
            {paired
              ? "Replaces the grant this page holds. The sync switches to the new one straight away."
              : "The sync has no credential yet. Pairing gives it its own grant, separate from any Claude Code login."}
          </p>
          <button type="button" onClick={() => void start()} disabled={busy}>
            {paired ? "Pair again" : "Start pairing"}
          </button>
        </>
      ) : (
        <ol className="steps">
          <li>
            <a href={authorizationUrl} target="_blank" rel="noreferrer">
              Open Claude&rsquo;s authorize page
            </a>{" "}
            in a browser signed in to the account, and approve.
          </li>
          <li>
            The browser is then sent to an address starting <code>http://localhost:54545/callback</code>. Nothing
            listens there, so the page fails to load; that is expected. Copy the whole address from the address bar.
          </li>
          <li>
            <label htmlFor="redirect-url">Paste it here</label>
            <textarea
              id="redirect-url"
              rows={3}
              spellCheck={false}
              autoComplete="off"
              placeholder="http://localhost:54545/callback?code=…&state=…"
              value={redirectUrl}
              onChange={(event) => setRedirectUrl(event.target.value)}
            />
            <div className="actions">
              <button type="button" onClick={() => void finish()} disabled={busy || redirectUrl.trim() === ""}>
                Finish pairing
              </button>
              <button type="button" className="secondary" onClick={() => void start()} disabled={busy}>
                Start over
              </button>
            </div>
          </li>
        </ol>
      )}
      {error !== null && (
        <p role="alert" className="error">
          {error}
        </p>
      )}
    </section>
  );
}
