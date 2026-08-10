export const CHAT_BOTTOM_THRESHOLD_PX = 48;

export function isNearChatBottom(
  metrics: Pick<HTMLElement, "scrollHeight" | "scrollTop" | "clientHeight">,
  threshold = CHAT_BOTTOM_THRESHOLD_PX
): boolean {
  return metrics.scrollHeight - metrics.scrollTop - metrics.clientHeight <= threshold;
}
