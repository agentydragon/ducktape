import { createTheme, type MantineThemeOverride } from "@mantine/core";

/** The app's Mantine theme, which the visual harness renders with too. `autoContrast` gives a filled
 * component on a light colour (yellow, orange, cyan) dark text: white on those does not read. */
export const theme: MantineThemeOverride = createTheme({ autoContrast: true });
