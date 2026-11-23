import type { CSSProperties, JSX } from "react";

import { GMAIL_ICON_DATA_URI, GOOGLE_CALENDAR_ICON_DATA_URI } from "./brand_icon_data";

// Gmail's and Google Calendar's own multicolor app icons. They identify links and previews for the
// app, where a generic mail or calendar glyph would be ambiguous. Their
// data URIs are generated at build time (BUILD.bazel's `brand_icon_data`), so the bundle makes no
// runtime fetch to google.com. Decorative: the link text carries the accessible name.
const inline: CSSProperties = { width: "1.15em", height: "1.15em", marginRight: "0.3em", verticalAlign: "text-bottom" };

export function GmailIcon(): JSX.Element {
  return <img src={GMAIL_ICON_DATA_URI} alt="" style={inline} />;
}

export function GoogleCalendarIcon(): JSX.Element {
  return <img src={GOOGLE_CALENDAR_ICON_DATA_URI} alt="" style={inline} />;
}
