import { useEffect, useMemo, useState } from "react";
import {
  Accordion,
  Alert,
  Anchor,
  Badge,
  Box,
  Button,
  Checkbox,
  Container,
  Group,
  Loader,
  Paper,
  Select,
  SimpleGrid,
  Stack,
  Text,
  TextInput,
  Textarea,
  Title,
} from "@mantine/core";
import { useClipboard } from "@mantine/hooks";
import { html, mount } from "../report.js";

const colors = {
  Released: "teal",
  "Merged, no release": "blue",
  "In progress": "blue",
  "Open upstream": "yellow",
  "Present upstream": "orange",
  Investigate: "gray",
};
const priorityOrder = { High: 0, Medium: 1, Low: 2 };

function Summary({ label, value, detail, color }) {
  return html`<${Paper} withBorder radius="md" p="md">
    <${Text} size="sm" c="dimmed">${label}<//>
    <${Text} size="xl" fw=${700} c=${color} mt="xs">${value}<//>
    <${Text} size="sm" c="dimmed" mt="xs">${detail}<//>
  <//>`;
}

function reportText(draft) {
  if (draft.body) return draft.body;
  return Object.entries(draft.form_fields ?? {})
    .map(([name, value]) => `## ${name}\n\n${value}`)
    .join("\n\n");
}

function filingLink(draft) {
  if (draft.manual_url) return draft.url;
  const url = new URL(draft.url);
  if (draft.template) url.searchParams.set("template", draft.template);
  url.searchParams.set("title", draft.title);
  if (draft.form_fields) {
    for (const [name, value] of Object.entries(draft.form_fields)) {
      url.searchParams.set(name, value);
    }
  } else {
    url.searchParams.set("body", draft.body);
  }
  return url.toString();
}

function Draft({ draft, index }) {
  const text = reportText(draft);
  const clipboard = useClipboard({ timeout: 2000 });
  return html`<${Stack} gap="sm" py="xs">
    <${Text} fw=${700} size="sm">${draft.title}<//>
    <${Text} size="sm" c="dimmed">${draft.review}<//>
    <${Textarea}
      label=${`Proposed issue wording ${index + 1}`}
      value=${text}
      readOnly
      autosize
      minRows=${8}
      maxRows=${16}
      styles=${{ input: { fontFamily: "monospace", fontSize: "0.8rem" } }}
    />
    <${Group} gap="sm">
      <${Button}
        component="a"
        href=${filingLink(draft)}
        target="_blank"
        rel="noreferrer"
        size="xs"
        variant="light"
        >${draft.manual_url ? "Open reporting instructions" : "Open prefilled issue"}<//
      >
      <${Button}
        size="xs"
        variant="default"
        onClick=${() => clipboard.copy(text)}
        >${clipboard.copied ? "Copied" : "Copy wording"}<//
      >
    <//>
    ${clipboard.error ? html`<${Text} size="sm" c="red">Clipboard access was denied. Select the wording above and copy it manually.<//>` : null}
  <//>`;
}

function Finding({ item }) {
  return html`<${Paper}
    component="article"
    withBorder
    radius="md"
    p="lg"
    h="100%"
  >
    <${Stack} gap="sm">
      <${Group} justify="space-between" align="flex-start" gap="xs">
        <${Text} size="sm" fw=${600} c="dimmed">${item.project}<//>
        <${Group} gap="xs">
          <${Badge} color=${colors[item.status]} variant="light"
            >${item.status}<//
          >
          <${Badge}
            color=${item.priority === "High" ? "red" : "gray"}
            variant="outline"
            >${item.priority} priority<//
          >
        <//>
      <//>
      <${Title} order=${3} size="h4">${item.title}<//>
      <${Text} size="sm">${item.impact}<//>
      <${Box}>
        <${Text} size="xs" fw=${700} c="dimmed" tt="uppercase"
          >Upstream status<//
        >
        <${Text} size="sm" mt=${4}>${item.upstream}<//>
      <//>
      <${Box}>
        <${Text} size="xs" fw=${700} c="dimmed" tt="uppercase">Next action<//>
        <${Text} size="sm" mt=${4}>${item.next_step}<//>
      <//>
      ${
        item.filing_policy
          ? html`<${Alert}
              color="yellow"
              variant="light"
              title="Upstream filing preference"
            >
              ${item.filing_policy.text}
              <${Anchor} href=${item.filing_policy.template_url} size="sm"
                >Read the template<//
              >
              ·
              <${Anchor} href=${item.filing_policy.url} size="sm"
                >Open issue chooser<//
              >
            <//>`
          : null
      }
      ${
        item.drafts?.length
          ? html`<${Accordion} variant="contained">
              <${Accordion.Item} value="draft">
                <${Accordion.Control}
                  >${item.drafts.length === 1 ? "Proposed upstream report" : `${item.drafts.length} proposed upstream reports`}<//
                >
                <${Accordion.Panel}>
                  <${Text} size="sm" c="dimmed"
                    >Drafts need human review, duplicate checks and any missing
                    reproduction details before filing. No issue has been
                    submitted by this dashboard.<//
                  >
                  ${item.drafts.map((draft, index) => html`<${Draft} key=${draft.title} draft=${draft} index=${index} />`)}
                <//>
              <//>
            <//>`
          : null
      }
      <${Group} gap="sm" mt="xs" aria-label=${`Evidence for ${item.title}`}>
        ${item.links.map((link) => html`<${Anchor} key=${link.url} href=${link.url} size="sm">${link.label}<//>`)}
      <//>
    <//>
  <//>`;
}

function Report() {
  const [data, setData] = useState(null);
  const [error, setError] = useState(null);
  const [query, setQuery] = useState("");
  const [status, setStatus] = useState("All statuses");
  const [project, setProject] = useState("All projects");
  const [kind, setKind] = useState("All types");
  const [filingOnly, setFilingOnly] = useState(false);
  useEffect(() => {
    const controller = new AbortController();
    fetch("./evidence.json", { signal: controller.signal })
      .then((response) => {
        if (!response.ok) throw new Error(`Evidence HTTP ${response.status}`);
        return response.json();
      })
      .then(setData)
      .catch((e) => {
        if (e.name !== "AbortError") setError(e.message);
      });
    return () => controller.abort();
  }, []);
  const entries = data?.entries ?? [];
  const options = (key, all) =>
    [all, ...new Set(entries.map((item) => item[key]))].sort((a, b) =>
      a === all ? -1 : b === all ? 1 : a.localeCompare(b),
    );
  const visible = useMemo(() => {
    const needle = query.trim().toLocaleLowerCase();
    return entries
      .filter(
        (item) =>
          (status === "All statuses" || item.status === status) &&
          (project === "All projects" || item.project === project) &&
          (kind === "All types" || item.kind === kind) &&
          (!filingOnly || item.drafts?.length || item.filing_policy) &&
          (!needle ||
            [
              item.title,
              item.project,
              item.impact,
              item.upstream,
              item.next_step,
              item.id,
            ]
              .join(" ")
              .toLocaleLowerCase()
              .includes(needle)),
      )
      .toSorted(
        (a, b) =>
          priorityOrder[a.priority] - priorityOrder[b.priority] ||
          a.title.localeCompare(b.title),
      );
  }, [entries, query, status, project, kind, filingOnly]);
  const count = (predicate) => entries.filter(predicate).length;
  const reset = () => {
    setQuery("");
    setStatus("All statuses");
    setProject("All projects");
    setKind("All types");
    setFilingOnly(false);
  };
  return html`<${Container}
    component="main"
    size="74rem"
    py="xl"
    style=${{ overflowWrap: "anywhere" }}
  >
    <${Group} justify="space-between" align="center" mb="lg">
      <${Anchor} href="../" size="sm">← Ducktape reports<//>
      <${Group} gap="md"
        ><${Anchor} href="./evidence.json" size="sm">Evidence JSON<//
        ><${Anchor} href="./manifest.json" size="sm">Manifest<//><//
      >
    <//>
    <${Text} size="sm" fw=${700} c="blue" tt="uppercase"
      >Upstream work · Reviewed 9–10 November 2026 UTC<//
    >
    <${Title} order=${1} mt="xs">Upstream patches & issues<//>
    <${Text} c="dimmed" size="lg" mt="sm" maw="55rem">
      Where Ducktape carries a patch, workaround, fork, or incident caused by
      upstream behavior—and what has moved forward.
    <//>
    <${Alert}
      color="blue"
      variant="light"
      mt="lg"
      title="A reviewed snapshot, not a live feed"
    >
      Statuses reflect the November 9–10 investigation. Released fixes are
      candidates for our validation, not proof of deployment or recovery. Some
      causes and duplicate searches remain uncertain. Source review:${" "}
      <${Anchor}
        href=${`https://github.com/agentydragon/ducktape/tree/${data?.source_devel_commit ?? "5720cbd391b4d02dd84d98c75c9e73801d6d3059"}`}
        >devel 5720cbd391<//
      >.
    <//>
    ${
      error
        ? html`<${Alert} color="red" mt="lg" title="Unable to load inventory"
            >${error}<//
          >`
        : !data
          ? html`<${Group} mt="xl"
              ><${Loader} size="sm" /><${Text}>Loading inventory…<//><//
            >`
          : html`
              <${SimpleGrid}
                cols=${{ base: 2, md: 4 }}
                spacing="md"
                mt="xl"
                aria-label="Inventory summary"
              >
                <${Summary}
                  label="Reviewed items"
                  value=${entries.length}
                  detail="Issues, patches and forks"
                  color="blue"
                />
                <${Summary}
                  label="High priority"
                  value=${count((x) => x.priority === "High")}
                  detail="Based on impact and urgency"
                  color="red"
                />
                <${Summary}
                  label="Released or merged"
                  value=${count((x) => x.status === "Released" || x.status === "Merged, no release")}
                  detail="Still needs our validation or release"
                  color="teal"
                />
                <${Summary}
                  label="Needs investigation"
                  value=${count((x) => x.status === "Investigate")}
                  detail="Current cause or resolution uncertain"
                  color="gray"
                />
              <//>
              <${Paper}
                component="section"
                withBorder
                radius="md"
                p="lg"
                mt="xl"
                aria-label="Filter inventory"
              >
                <${Group} justify="space-between" align="end" mb="md">
                  <${Box}
                    ><${Title} order=${2} size="h3">Inventory<//
                    ><${Text} size="sm" c="dimmed" mt=${4}
                      >Filter the snapshot to find a contribution or
                      follow-up.<//
                    ><//
                  >
                  <${Button} variant="subtle" onClick=${reset}>Clear filters<//>
                <//>
                <${SimpleGrid} cols=${{ base: 1, sm: 2, lg: 4 }} spacing="md">
                  <${TextInput}
                    label="Search"
                    placeholder="Project, symptom, or action"
                    value=${query}
                    onChange=${(e) => setQuery(e.currentTarget.value)}
                  />
                  <${Select}
                    label="Upstream status"
                    data=${options("status", "All statuses")}
                    value=${status}
                    onChange=${(v) => setStatus(v ?? "All statuses")}
                    allowDeselect=${false}
                  />
                  <${Select}
                    label="Project"
                    data=${options("project", "All projects")}
                    value=${project}
                    onChange=${(v) => setProject(v ?? "All projects")}
                    searchable
                    allowDeselect=${false}
                  />
                  <${Select}
                    label="Local type"
                    data=${options("kind", "All types")}
                    value=${kind}
                    onChange=${(v) => setKind(v ?? "All types")}
                    allowDeselect=${false}
                  />
                <//>
                <${Checkbox}
                  mt="md"
                  label="Show filing candidates only"
                  description="Includes proposed drafts and projects requiring a human-written report."
                  checked=${filingOnly}
                  onChange=${(e) => setFilingOnly(e.currentTarget.checked)}
                />
              <//>
              <${Group} justify="space-between" mt="xl" mb="md">
                <${Text} fw=${600} aria-live="polite"
                  >Showing ${visible.length} of ${entries.length} items<//
                >
                <${Text} size="sm" c="dimmed"
                  >Ordered by priority, then title<//
                >
              <//>
              ${visible.length ? html`<${SimpleGrid} cols=${{ base: 1, md: 2 }} spacing="md">${visible.map((item) => html`<${Finding} key=${item.id} item=${item} />`)}<//>` : html`<${Paper} withBorder p="xl" radius="md"><${Text}>No matching items. Try another search or clear the filters.<//><//>`}
            `
    }
    <${Text} component="footer" size="sm" c="dimmed" mt="xl"
      >Each card links to the original evidence. The inventory reflects reviewed
      candidates, not every upstream dependency or all possible reports.<//
    >
  <//>`;
}

mount(Report);
