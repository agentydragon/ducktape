import { createElement, useEffect, useState } from "react";
import { createRoot } from "react-dom/client";
import { MantineProvider, Text } from "@mantine/core";
import htm from "htm";

export const html = htm.bind(createElement);

export function ManifestStamp({ src }) {
  const [stamp, setStamp] = useState("Loading publication metadata…");
  useEffect(() => {
    const controller = new AbortController();
    fetch(src, { signal: controller.signal })
      .then((response) => {
        if (!response.ok) throw new Error(`Manifest HTTP ${response.status}`);
        return response.json();
      })
      .then((manifest) => {
        setStamp(
          manifest.audited_at_utc
            ? `Reviewed ${manifest.audited_at_utc} · devel ${manifest.source_devel_commit.slice(0, 10)}`
            : `Window ${manifest.window_start_utc} → ${manifest.window_end_utc} · devel ${manifest.source_devel_commit.slice(0, 10)} · published ${manifest.published_at_utc.slice(0, 16)}Z`,
        );
      })
      .catch((error) => {
        if (error.name !== "AbortError")
          setStamp("Publication metadata unavailable");
      });
    return () => controller.abort();
  }, [src]);
  return html`<${Text}
    component="span"
    size="sm"
    c="dimmed"
    data-manifest=${src}
    >${stamp}<//
  >`;
}

export function mount(Report) {
  createRoot(document.getElementById("report-content")).render(
    createElement(
      MantineProvider,
      { defaultColorScheme: "auto" },
      createElement(Report),
    ),
  );
}
