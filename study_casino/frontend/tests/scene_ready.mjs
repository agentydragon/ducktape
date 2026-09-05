/**
 * What a full-app Study Casino scene still has to wait for once it has mounted.
 *
 * The harness seeds `casinoSync.state` synchronously, but importing it also constructs the
 * module-level singleton, which runs its own startup fetches against the harness's 503 stub. Their
 * result lands after mount and is the last change the page makes: the seeded `{kind: "ok"}` sync
 * status is replaced by `offline`, flipping the header's sync icon from a green check to a red
 * bolt. Capturing before that photographs the icon mid-flight.
 *
 * So the settled state a full-app scene renders is the offline one, and this waits for exactly it.
 * A harness that answered those fetches with its fixtures instead of 503 would settle on "ok" —
 * a deliberate change to what these scenes show, which this selector would then fail on by name.
 */
export const SYNC_SETTLED = '[data-testid="sync-banner-offline"]';
