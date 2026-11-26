import { Fragment, type JSX } from "react";
import type { EnvironmentDescriptor } from "../../../environments/v1/environment_service_pb";
import { sandboxStatusRows } from "./sandbox_status";

/** The status glyph's hover card; condition ages share a column across both resources. */
export function SandboxStatusTooltip({ sandbox }: { sandbox: EnvironmentDescriptor }): JSX.Element {
  return (
    <div
      style={{
        display: "grid",
        gridTemplateColumns: "minmax(0, 1fr) auto",
        gap: "0.2em 1em",
        maxWidth: "min(30rem, 84vw)",
        whiteSpace: "normal",
      }}
    >
      {sandboxStatusRows(sandbox).map(({ text, age, heading }, index) =>
        heading ? (
          <strong key={index} style={{ gridColumn: "1 / -1", marginTop: index ? "0.4em" : 0 }}>
            {text}
          </strong>
        ) : (
          <Fragment key={index}>
            <span style={{ overflowWrap: "anywhere" }}>{text}</span>
            <span style={{ textAlign: "right", fontVariantNumeric: "tabular-nums" }}>{age}</span>
          </Fragment>
        )
      )}
    </div>
  );
}
