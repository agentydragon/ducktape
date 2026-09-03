// The "session_raw" scenario of harness.tsx: the session view with the Raw frames switch on
// (`?raw=1`), so the frames render beside the item, input and turn they were translated into, and
// what belongs to none of them under "outside the transcript".
import { main } from "../../../../../util/testing/frontend_visual/visual-test-lib.mjs";

// Taller than the plain session scenario: the transcript scrolls to the newest event and the
// stack is three times as tall with the frames in it, so 900 lands above the second turn's header
// and the review misses the frames a turn carries.
await main("session_raw", { element: "#app", viewport: { width: 1200, height: 1200 } });
