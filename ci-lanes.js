import {
  Accordion,
  Anchor,
  Badge,
  Group,
  Paper,
  Stack,
  Table,
  Text,
  Title,
} from "@mantine/core";
import { html } from "./report.js";

export function CiLanes({ data }) {
  return html`<${Stack}
    component="section"
    id="ten-lane-followups"
    gap="md"
    my="xl"
  >
    <${Title} order=${2}
      >Ten dispatched lanes: experiments and next measurements<//
    >
    <${Text}>${data.summary}<//>
    <${Text} size="sm" c="dimmed">
      Reviewed ${data.observed_at_utc}; inspected devel${" "}
      <${Anchor}
        href=${`https://github.com/agentydragon/ducktape/commit/${data.source_devel_commit}`}
        >${data.source_devel_commit}<//
      >. Each issue keeps the next measurement open until its exposure and
      outcome are recorded.
    <//>
    <${Text} size="sm" c="dimmed">
      Scroll horizontally on narrow screens, or expand a lane below for its
      evidence and next measurement.
    <//>
    <${Table.ScrollContainer} minWidth=${760}>
      <${Table} striped highlightOnHover withTableBorder>
        <${Table.Thead}
          ><${Table.Tr}>
            <${Table.Th}>Lane / tracking<//><${Table.Th}>Current evidence<//
            ><${Table.Th}>Next measurement or decision<//>
          <//><//
        >
        <${Table.Tbody}>
          ${data.lanes.map(
            (lane) =>
              html`<${Table.Tr} key=${lane.id}>
                <${Table.Td} miw=${180}>
                  <${Anchor} href=${lane.issue_url}
                    >${`${lane.id}. ${lane.title}`}<//
                  >
                  <${Text} size="xs" c="dimmed">${lane.status}<//>
                <//>
                <${Table.Td}>${lane.observed}<//>
                <${Table.Td}>${lane.measurement}<//>
              <//>`,
          )}
        <//>
      <//>
    <//>
    <${Accordion} variant="contained" multiple=${true}>
      ${data.lanes.map(
        (lane) =>
          html`<${Accordion.Item} key=${lane.id} value=${`lane-${lane.id}`}>
            <${Accordion.Control}
              >${`${lane.id}. ${lane.title}: evidence and acceptance`}<//
            >
            <${Accordion.Panel}>
              <${Stack} gap="sm">
                <${Group} gap="xs"
                  ><${Badge} variant="light">${lane.status}<//>
                  <${Anchor} href=${lane.issue_url}
                    >${`Tracking #${lane.issue_number}`}<//
                  >
                  ${(lane.prs || []).map((pr) => html`<${Anchor} key=${pr.url} href=${pr.url}>${pr.label}<//>`)}
                <//>
                <${Text}>${lane.observed}<//>
                <${Text} size="sm">${lane.acceptance}<//>
                <${Text} size="sm" fw=${600}>${lane.measurement}<//>
                ${(lane.evidence_links || []).map((link) => html`<${Anchor} key=${link.url} size="sm" href=${link.url}>${link.label}<//>`)}
              <//>
            <//>
          <//>`,
      )}
    <//>
    <${Paper} withBorder p="md">
      <${Text} size="sm">${data.guardrails}<//>
    <//>
  <//>`;
}
