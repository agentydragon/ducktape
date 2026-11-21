import type { ReactNode } from "react";

import {
  LaunchRoutinePreview,
  validateLaunchRoutineArguments,
  type LaunchRoutineArguments,
} from "./haku_routine/requests";
import type { PreviewVariant } from "./vocabulary";

export type RegisteredToolPreviewFixture = {
  serverId: "haku_routine";
  toolName: "launch_routine";
  args: LaunchRoutineArguments;
  result?: unknown;
};

export function toolPreview(
  serverId: string,
  toolName: string,
  args: Record<string, unknown>,
  variant: PreviewVariant
): ReactNode | null {
  if (serverId !== "haku_routine" || toolName !== "launch_routine" || !validateLaunchRoutineArguments(args)) {
    return null;
  }
  return <LaunchRoutinePreview args={args} variant={variant} />;
}
