import { createTheme } from "@mantine/core";

// Haku UI theme. The brand accent maps to Mantine's teal palette (was --teal #0d9488);
// semantic status colors come from Mantine's built-in palettes via component `color` props
// (danger→red, warn→orange, success→green, info→blue, neutral→gray) — one system, no ad-hoc hex.
export const theme = createTheme({
  primaryColor: "teal",
  fontFamily: "system-ui, -apple-system, sans-serif",
});
