import { Button } from "@/components/ui/button";
import { Switch } from "@/components/ui/switch";
import type { Settings } from "@/lib/types";

interface Props {
  settings: Settings;
  onChange: (settings: Settings) => void;
  onRunAll: () => void;
  busy: boolean;
  canRunAll: boolean;
}

/** The one row that must never scroll off screen. */
export function Controls({ settings, onChange, onRunAll, busy, canRunAll }: Props) {
  const frontier = settings.tier === "frontier";
  const guardrailOn = settings.guardrail === "on";

  return (
    <div className="flex items-center gap-x-12">
      <label className="flex cursor-pointer items-center gap-4 text-label">
        <Switch
          checked={frontier}
          onCheckedChange={(on) => onChange({ ...settings, tier: on ? "frontier" : "high_volume" })}
        />
        {/* Fixed width so the row does not shift when the label flips. */}
        <span className="inline-block min-w-[23ch]">
          {frontier ? "Frontier tier — Claude Sonnet 4.6" : "High-volume tier — DeepSeek V3.1"}
        </span>
      </label>

      <label className="flex cursor-pointer items-center gap-4 text-label">
        <Switch
          className="data-[state=checked]:border-brand-purple data-[state=checked]:bg-brand-purple"
          checked={guardrailOn}
          onCheckedChange={(on) => onChange({ ...settings, guardrail: on ? "on" : "off" })}
        />
        <span className="inline-block min-w-[9ch]">{guardrailOn ? "Guardrail on" : "Guardrail off"}</span>
      </label>

      <Button variant="outline" className="ml-auto" onClick={onRunAll} disabled={busy || !canRunAll}>
        Run all
      </Button>
    </div>
  );
}
