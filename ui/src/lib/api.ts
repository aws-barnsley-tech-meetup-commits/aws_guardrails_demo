import { mockAttack, mockPrompts } from "./mock";
import type { AttackResult, PromptOption, Settings } from "./types";

const API_URL = (import.meta.env.VITE_API_URL ?? "").replace(/\/+$/, "");
const API_KEY = import.meta.env.VITE_API_KEY ?? "";
export const MOCK = import.meta.env.VITE_MOCK === "true";

/** A failure the operator has to see: a non-200, or no answer at all. */
export class ApiError extends Error {
  status: number | null;

  constructor(status: number | null, message: string) {
    super(message);
    this.status = status;
  }
}

function requireBase(): string {
  if (!API_URL) {
    throw new ApiError(
      null,
      "VITE_API_URL is not set. Add it to ui/.env and restart.",
    );
  }
  return API_URL;
}

async function readError(res: Response): Promise<string> {
  const text = await res.text().catch(() => "");
  try {
    const body = JSON.parse(text);
    return body.message ?? body.error ?? text;
  } catch {
    return text || res.statusText;
  }
}

async function request(path: string, init: RequestInit): Promise<Response> {
  let res: Response;
  try {
    res = await fetch(`${requireBase()}${path}`, init);
  } catch (err) {
    if (err instanceof ApiError) throw err;
    // fetch rejects with a bare TypeError for CORS, DNS and offline alike.
    throw new ApiError(
      null,
      "No response from the API. Check the URL, the network and CORS.",
    );
  }
  if (!res.ok) throw new ApiError(res.status, await readError(res));
  return res;
}

export async function fetchPrompts(): Promise<PromptOption[]> {
  if (MOCK) return mockPrompts();
  const res = await request("/c1/prompts", { headers: authHeaders() });
  // GET /c1/prompts wraps the fixture in {"prompts": [...]}. POST /c1/attack
  // does not wrap its body, so only this route needs unwrapping.
  const body = await res.json();
  return Array.isArray(body) ? body : body.prompts;
}

export async function attack(
  prompt: string,
  settings: Settings,
): Promise<AttackResult> {
  if (MOCK) return mockAttack(prompt, settings);
  const res = await request("/c1/attack", {
    method: "POST",
    headers: {
      ...authHeaders(),
      "content-type": "application/json",
      "x-model-tier": settings.tier,
      "x-guardrail": settings.guardrail,
    },
    body: JSON.stringify({ prompt }),
  });
  return res.json();
}

function authHeaders(): Record<string, string> {
  return API_KEY ? { authorization: `Bearer ${API_KEY}` } : {};
}
