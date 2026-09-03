// The "session_raw" scenario of harness.tsx: the session view with the Raw frames switch on
// (`?raw=1`), so the frames render beside the item, input and turn they were translated into, and
// what belongs to none of them under "outside the transcript".
import { main } from "../../../../../util/testing/frontend_visual/visual-test-lib.mjs";

// Taller than the plain session scenario: the transcript scrolls to the newest event and the stack
// is several times as tall with every frame in it, so a 900-tall window ends inside the last item
// and the review never sees a turn or an input take its place in the order.
await main("session_raw", { element: "#app", viewport: { width: 1200, height: 1800 } });
