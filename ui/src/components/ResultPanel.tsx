import type { ReactNode } from "react";
import type { AttackResult } from "@/lib/types";

export type Outcome =
  | { kind: "idle" }
  | { kind: "loading" }
  | { kind: "failed"; status: number | null; message: string }
  | { kind: "done"; data: AttackResult };

const CAUGHT: Record<string, string> = {
  input: "caught on the way in",
  output: "caught on the way out",
};

function Verdict({ children, sub }: { children: ReactNode; sub?: string }) {
  return (
    <div className="shrink-0 bg-surface-200 px-8 py-5">
      <p className="text-[clamp(3.25rem,8vh,6rem)] leading-none font-medium tracking-tight">{children}</p>
      {sub && <p className="mt-3 text-body-lg">{sub}</p>}
    </div>
  );
}

const accent = (word: string) => <span className="text-brand-purple">{word}</span>;

export function ResultPanel({ outcome }: { outcome: Outcome }) {
  if (outcome.kind === "idle") {
    return <Verdict sub="Pick a prompt and send it.">Ready</Verdict>;
  }
  if (outcome.kind === "loading") {
    return <Verdict sub="Waiting for the bot.">Running</Verdict>;
  }
  if (outcome.kind === "failed") {
    const status = outcome.status === null ? "No response" : `Status ${outcome.status}`;
    return <Verdict sub={`${status}: ${outcome.message}`}>Request failed</Verdict>;
  }

  const r = outcome.data;
  const brokenChecks = r.checks.filter((c) => c.broken);

  return (
    <div className="flex min-h-0 flex-1 flex-col gap-5">
      {r.result === "held" && <Verdict>Bot held</Verdict>}
      {r.result === "broken" && (
        <Verdict>
          Bot {accent("broke")} its rules
        </Verdict>
      )}
      {r.result === "blocked" && (
        <Verdict sub={r.guardrailStage ? CAUGHT[r.guardrailStage] : undefined}>
          Guardrail {accent("blocked")} it
        </Verdict>
      )}
      {r.result === "error" && <Verdict sub={r.feedback}>Error</Verdict>}

      {r.result === "blocked" && (
        <ul className="shrink-0 font-mono text-body-lg font-medium">
          {r.firedFilters.length > 0 ? (
            r.firedFilters.map((f) => <li key={f}>{f}</li>)
          ) : (
            <li className="font-sans">Filter detail unavailable.</li>
          )}
        </ul>
      )}

      {r.result === "broken" && (
        <ul className="shrink-0 space-y-2 text-body">
          {brokenChecks.map((c) => (
            <li key={c.label}>
              <span>{c.label}</span>
              {c.reason && <span> — {c.reason}</span>}
            </li>
          ))}
        </ul>
      )}

      <pre className="max-h-[40vh] min-h-0 overflow-auto bg-surface-200 px-6 py-4 font-mono text-body font-normal whitespace-pre-wrap">
        {r.botResponse}
      </pre>

      <p className="shrink-0 text-body text-ink-muted">
        <span className="font-mono">{r.modelId}</span> · guardrail {r.guardrail} · {r.latencyMs} ms
      </p>
    </div>
  );
}
