import { useEffect, useRef, useState } from "react";
import { Controls } from "@/components/Controls";
import { PromptInput } from "@/components/PromptInput";
import { ResultPanel, type Outcome } from "@/components/ResultPanel";
import { ResultsGrid, type Row } from "@/components/ResultsGrid";
import { Button } from "@/components/ui/button";
import { ApiError, attack, fetchPrompts, MOCK } from "@/lib/api";
import type { PromptOption, Settings } from "@/lib/types";

const CONCURRENCY = 3;

function failure(err: unknown): { status: number | null; message: string } {
  if (err instanceof ApiError) return { status: err.status, message: err.message };
  return { status: null, message: err instanceof Error ? err.message : String(err) };
}

export default function App() {
  const [prompts, setPrompts] = useState<PromptOption[]>([]);
  const [promptsError, setPromptsError] = useState<string | null>(null);
  const [selectedId, setSelectedId] = useState("");
  const [text, setText] = useState("");
  const [settings, setSettings] = useState<Settings>({ tier: "high_volume", guardrail: "off" });

  const [outcome, setOutcome] = useState<Outcome>({ kind: "idle" });
  // Identifies the request behind `outcome`, so the panel remounts per request.
  const [outcomeKey, setOutcomeKey] = useState("idle");
  const [rows, setRows] = useState<Row[] | null>(null);
  const [gridSettings, setGridSettings] = useState<Settings>(settings);
  const [view, setView] = useState<"single" | "grid">("single");
  const [running, setRunning] = useState(false);
  const [sending, setSending] = useState(false);

  // Only the latest run may write to state; an older one is dropped.
  const runId = useRef(0);

  useEffect(() => {
    fetchPrompts()
      .then(setPrompts)
      .catch((err) => {
        const { status, message } = failure(err);
        setPromptsError(`Could not load prompts${status ? ` (${status})` : ""}: ${message}`);
      });
  }, []);

  const busy = running || sending;

  function select(id: string) {
    setSelectedId(id);
    const chosen = prompts.find((p) => p.id === id);
    if (chosen) setText(chosen.prompt);
  }

  function edit(value: string) {
    setText(value);
    // Once the text drifts from the picked prompt, the dropdown no longer describes it.
    const chosen = prompts.find((p) => p.id === selectedId);
    if (chosen && chosen.prompt !== value) setSelectedId("");
  }

  async function send() {
    const id = ++runId.current;
    setSending(true);
    setView("single");
    setOutcomeKey(`send-${id}`);
    setOutcome({ kind: "loading" });
    try {
      const data = await attack(text, settings);
      if (id === runId.current) setOutcome({ kind: "done", data });
    } catch (err) {
      if (id === runId.current) setOutcome({ kind: "failed", ...failure(err) });
    } finally {
      if (id === runId.current) setSending(false);
    }
  }

  async function runAll() {
    const id = ++runId.current;
    const snapshot = { ...settings };
    const list = prompts;
    setGridSettings(snapshot);
    setRows(list.map((prompt) => ({ prompt, state: "pending" })));
    setView("grid");
    setRunning(true);

    const fill = (index: number, row: Row) => {
      if (id !== runId.current) return;
      setRows((current) => current && current.map((r, i) => (i === index ? row : r)));
    };

    let next = 0;
    const worker = async () => {
      while (next < list.length && id === runId.current) {
        const index = next++;
        const prompt = list[index];
        try {
          fill(index, { prompt, state: "done", data: await attack(prompt.prompt, snapshot) });
        } catch (err) {
          fill(index, { prompt, state: "failed", ...failure(err) });
        }
      }
    };
    await Promise.all(Array.from({ length: Math.min(CONCURRENCY, list.length) }, worker));
    if (id === runId.current) setRunning(false);
  }

  function openRow(row: Row) {
    if (row.state === "pending") return;
    setText(row.prompt.prompt);
    setSelectedId(row.prompt.id);
    setOutcomeKey(`grid-${runId.current}-${row.prompt.id}`);
    setOutcome(
      row.state === "done"
        ? { kind: "done", data: row.data }
        : { kind: "failed", status: row.status, message: row.message },
    );
    setView("single");
  }

  return (
    <main className="mx-auto flex h-screen max-w-[1920px] flex-col gap-6 p-16">
      <header className="flex shrink-0 items-baseline justify-between gap-8">
        <h1 className="text-[clamp(1.75rem,2.2vw,2.5rem)] leading-tight font-extrabold">
          Enforcing AI Safety Controls with Amazon Bedrock Guardrails - Demo
        </h1>
        {MOCK && <span className="text-body text-ink-muted">sample data</span>}
      </header>

      <Controls
        settings={settings}
        onChange={setSettings}
        onRunAll={runAll}
        busy={busy}
        canRunAll={prompts.length > 0}
      />

      <PromptInput
        prompts={prompts}
        promptsError={promptsError}
        selectedId={selectedId}
        text={text}
        busy={busy}
        onSelect={select}
        onText={edit}
        onSend={send}
      />

      {view === "grid" && rows ? (
        <ResultsGrid rows={rows} settings={gridSettings} running={running} onOpen={openRow} />
      ) : (
        <div className="flex min-h-0 flex-1 flex-col gap-4">
          <ResultPanel key={outcomeKey} outcome={outcome} />
          {rows && (
            <Button variant="outline" className="shrink-0 self-start" onClick={() => setView("grid")}>
              Back to results grid
            </Button>
          )}
        </div>
      )}
    </main>
  );
}
