import { useEffect, useState } from "react";

// Matches sidebar.css's own phone breakpoint.
const PHONE_QUERY = "(max-width: 560px)";

/** Whether the viewport is phone-width, where the sidebar is a full-screen overlay and the docked
 * workspace shows one pane at a time. */
export function usePhoneWidth(): boolean {
  const [phone, setPhone] = useState(() => window.matchMedia(PHONE_QUERY).matches);
  useEffect(() => {
    const query = window.matchMedia(PHONE_QUERY);
    const onChange = (event: MediaQueryListEvent): void => setPhone(event.matches);
    query.addEventListener("change", onChange);
    return () => query.removeEventListener("change", onChange);
  }, []);
  return phone;
}
