import type { CreateWidgetRequest, Widget, paths } from "./openapi_types";

export const request: CreateWidgetRequest = { name: "desk lamp" };
export const response: Widget = { id: 12, name: "desk lamp" };
export type CreateWidgetOperation = paths["/widgets"]["post"];
