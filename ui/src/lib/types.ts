export type ModelTier = "high_volume" | "frontier";
export type GuardrailMode = "off" | "on";
export type Verdict = "held" | "broken" | "blocked" | "error";

export interface PromptOption {
  id: string;
  label: string;
  prompt: string;
}

export interface Check {
  id?: string;
  label: string;
  broken: boolean;
  reason: string;
}

/** Body of a 200 from POST /c1/attack. */
export interface AttackResult {
  result: Verdict;
  feedback: string;
  checks: Check[];
  firedFilters: string[];
  guardrailStage: "input" | "output" | null;
  botResponse: string;
  modelTier: string;
  modelId: string;
  guardrail: string;
  latencyMs: number;
}

export interface Settings {
  tier: ModelTier;
  guardrail: GuardrailMode;
}
