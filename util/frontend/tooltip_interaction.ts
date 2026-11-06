import { useEffect, useRef, useState, type HTMLAttributes, type RefAttributes } from "react";

type TooltipInteraction<T extends HTMLElement> = {
  opened: boolean;
  triggerProps: HTMLAttributes<T> & RefAttributes<T>;
};

export function useTooltipInteraction<T extends HTMLElement>(): TooltipInteraction<T> {
  const triggerRef = useRef<T>(null);
  const [hovered, setHovered] = useState(false);
  const [focused, setFocused] = useState(false);
  const [pinned, setPinned] = useState(false);
  const opened = hovered || focused || pinned;

  useEffect(() => {
    if (!opened) return;

    const dismiss = () => {
      setHovered(false);
      setFocused(false);
      setPinned(false);
    };
    const dismissOnOutsidePointer = (event: globalThis.PointerEvent) => {
      const target = event.target;
      if (!(target instanceof Node) || triggerRef.current?.contains(target)) return;
      if (target instanceof Element && target.closest('[role="tooltip"]')) return;
      dismiss();
    };
    const dismissOnEscape = (event: KeyboardEvent) => {
      if (event.key === "Escape") dismiss();
    };

    document.addEventListener("pointerdown", dismissOnOutsidePointer);
    document.addEventListener("keydown", dismissOnEscape);
    return () => {
      document.removeEventListener("pointerdown", dismissOnOutsidePointer);
      document.removeEventListener("keydown", dismissOnEscape);
    };
  }, [opened]);

  const triggerProps: TooltipInteraction<T>["triggerProps"] = {
    ref: triggerRef,
    onPointerEnter: (event) => {
      if (event.pointerType !== "touch") setHovered(true);
    },
    onPointerLeave: () => setHovered(false),
    onFocus: () => setFocused(true),
    onBlur: () => {
      setFocused(false);
      setPinned(false);
    },
    onClick: () => {
      if (pinned) setFocused(false);
      setPinned(!pinned);
    },
  };

  return { opened, triggerProps };
}
