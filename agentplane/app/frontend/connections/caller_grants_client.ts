import { api, ok } from "../client";
import type { CallerGrantReader } from "./models";

export const readCallerGrants: CallerGrantReader = async (account, signal) =>
  ok(await api.GET("/caller-grants/{namespace}/{name}", { params: { path: account }, signal }));
