import { Button, Group } from "@mantine/core";

export function ChartLegend({
  items,
  selected,
  onToggle,
  onHover,
}: {
  items: { id: string; label: string; color: string }[];
  selected: Set<string>;
  onToggle: (id: string) => void;
  onHover: (id: string | null) => void;
}) {
  return (
    <Group gap="xs" mt="xs">
      {items.map((item) => (
        <Button
          key={item.id}
          size="xs"
          styles={{ root: { maxWidth: "100%", height: "auto", minHeight: 30 }, label: { whiteSpace: "normal" } }}
          variant={selected.has(item.id) ? "filled" : "light"}
          aria-pressed={selected.has(item.id)}
          onClick={() => onToggle(item.id)}
          onMouseEnter={() => onHover(item.id)}
          onMouseLeave={() => onHover(null)}
          onFocus={() => onHover(item.id)}
          onBlur={() => onHover(null)}
          leftSection={
            <span
              aria-hidden="true"
              style={{ width: 10, height: 10, background: item.color, display: "inline-block" }}
            />
          }
        >
          {item.label}
        </Button>
      ))}
    </Group>
  );
}

export function toggleLegendItem(current: Set<string>, id: string): Set<string> {
  const next = new Set(current);
  if (next.has(id)) next.delete(id);
  else next.add(id);
  return next;
}
