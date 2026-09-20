import { Badge, Text } from "@mantine/core";

interface Props {
  meanCredit: number;
  nRuns: number;
}

export default function CreditBadge({ meanCredit, nRuns }: Props) {
  const color = meanCredit >= 0.7 ? "green" : meanCredit >= 0.4 ? "yellow" : "red";

  return (
    <Badge
      color={color}
      variant="outline"
      title={`Mean credit: ${(meanCredit * 100).toFixed(1)}% (${nRuns} runs)`}
      style={{ textTransform: "none" }}
    >
      {(meanCredit * 100).toFixed(0)}%
      <Text component="span" size="xs" c="dimmed" ml={4}>
        ({nRuns})
      </Text>
    </Badge>
  );
}
