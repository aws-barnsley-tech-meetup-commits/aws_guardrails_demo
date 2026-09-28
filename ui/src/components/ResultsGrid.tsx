import { Button } from "@/components/ui/button";
import type { AttackResult, PromptOption, Settings } from "@/lib/types";

export type Row =
  | { prompt: PromptOption; state: "pending" }
  | { prompt: PromptOption; state: "done"; data: AttackResult }
  | { prompt: PromptOption; state: "failed"; status: number | null; message: string };

const STATUS_WORD = { held: "held", broken: "broke", blocked: "blocked", error: "error" } as const;

function Cell({ row }: { row: Row }) {
  if (row.state === "pending") return <span className="text-ink-muted">pending</span>;
  if (row.state === "failed") {
    return <span>error: {row.status === null ? "no response" : row.status} {row.message}</span>;
  }
  const { data } = row;
  const detail =
    data.result === "broken"
      ? data.checks.filter((c) => c.broken).map((c) => c.label)
      : data.result === "blocked"
        ? data.firedFilters
        : [];
  return (
    <span>
      {STATUS_WORD[data.result]}
      {detail.length > 0 && (
        <span className={data.result === "blocked" ? "font-mono" : undefined}>: {detail.join(", ")}</span>
      )}
    </span>
  );
}

interface Props {
  rows: Row[];
  settings: Settings;
  running: boolean;
  onOpen: (row: Row) => void;
}

export function ResultsGrid({ rows, settings, running, onOpen }: Props) {
  const count = (result: AttackResult["result"]) =>
    rows.filter((r) => r.state === "done" && r.data.result === result).length;
  const errors = rows.filter((r) => r.state === "failed" || (r.state === "done" && r.data.result === "error")).length;
  const finished = rows.filter((r) => r.state !== "pending").length;

  // One purple per line: the count that tells the story for these settings.
  const focus = settings.guardrail === "on" ? "blocked" : "broken";
  const num = (n: number, key: "broken" | "blocked" | "held") => (
    <span className={key === focus ? "text-brand-purple" : undefined}>{n}</span>
  );

  return (
    <div className="flex min-h-0 flex-1 flex-col gap-4">
      <div className="flex shrink-0 items-baseline gap-6">
        <p className="text-body-lg">
          {num(count("held"), "held")} held · {num(count("broken"), "broken")} broke ·{" "}
          {num(count("blocked"), "blocked")} blocked
          {errors > 0 && ` · ${errors} failed`}
        </p>
        <p className="text-body text-ink-muted">
          {running ? `${finished} of ${rows.length} done` : `${rows.length} prompts`}
        </p>
      </div>

      <ul className="min-h-0 flex-1 overflow-auto bg-surface-200">
        {rows.map((row) => (
          <li key={row.prompt.id} className="border-b border-surface-100 last:border-b-0">
            <Button
              variant="outline"
              className="grid w-full grid-cols-[minmax(0,2fr)_minmax(0,3fr)] justify-items-start gap-8 border-0 px-8 py-4 text-left text-body whitespace-normal hover:bg-surface-100 disabled:opacity-100"
              disabled={row.state === "pending"}
              onClick={() => onOpen(row)}
            >
              <span>{row.prompt.label}</span>
              <Cell row={row} />
            </Button>
          </li>
        ))}
      </ul>
    </div>
  );
}
