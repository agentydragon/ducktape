import { createTheme, MantineProvider } from "@mantine/core";
import type { JSX, ReactNode } from "react";

// `autoContrast` gives a filled component on a light colour (yellow, orange, green) dark text: white on
// those does not read.
const theme = createTheme({ autoContrast: true });

/** Mantine as the app renders with it. The visual harness renders with it too, so screenshots show
 * what users see. The entry point imports Mantine's stylesheet itself, ahead of the app's own CSS. */
export function ThemeProvider({ children }: { children: ReactNode }): JSX.Element {
  return (
    <MantineProvider theme={theme} defaultColorScheme="auto">
      {children}
    </MantineProvider>
  );
}
