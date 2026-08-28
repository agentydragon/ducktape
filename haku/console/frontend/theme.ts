import { createTheme, type MantineColorsTuple, type MantineThemeOverride } from "@mantine/core";

const haku: MantineColorsTuple = [
  "#edf9f5",
  "#d9f1eb",
  "#bae3d8",
  "#8fd0bf",
  "#7ac5b2",
  "#55aa99",
  "#2f8f80",
  "#237d73",
  "#1d645e",
  "#154a46",
];

const hakuSpirit: MantineColorsTuple = [
  "#f4f1f7",
  "#e8e2ef",
  "#d2c8dc",
  "#c2bacd",
  "#b8aec7",
  "#9a8dab",
  "#756a8d",
  "#5f5477",
  "#4a415e",
  "#332d42",
];

export const ACTION_COLOR = "hakuSpirit";
export const SUCCESS_COLOR = "haku";

export const hakuTheme: MantineThemeOverride = createTheme({
  primaryColor: ACTION_COLOR,
  colors: { haku, hakuSpirit },
});
