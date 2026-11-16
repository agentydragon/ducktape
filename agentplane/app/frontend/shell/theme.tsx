import { createTheme, MantineProvider, rem } from "@mantine/core";
import type { JSX, ReactNode } from "react";

// `autoContrast` gives a filled component on a light colour (yellow, orange, green) dark text: white on
// those does not read.
//
// `fontSizes` is 80% of Mantine's default scale (12/14/16/18/20px), so more of a long, text-heavy
// agent session fits on screen without scrolling. `lineHeights` is left at the Mantine default: it's
// unitless ratios that already scale down proportionally with the smaller font size.
const theme = createTheme({
  autoContrast: true,
  fontSizes: {
    xs: rem(9.6),
    sm: rem(11.2),
    md: rem(12.8),
    lg: rem(14.4),
    xl: rem(16),
  },
});

/** Mantine as the app renders with it. The visual harness renders with it too, so screenshots show
 * what users see. The entry point imports Mantine's stylesheet itself, ahead of the app's own CSS. */
export function ThemeProvider({ children }: { children: ReactNode }): JSX.Element {
  return (
    <MantineProvider theme={theme} defaultColorScheme="auto">
      {children}
    </MantineProvider>
  );
}
