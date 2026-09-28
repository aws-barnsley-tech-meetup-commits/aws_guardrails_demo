import { Button } from "@/components/ui/button";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Textarea } from "@/components/ui/textarea";
import type { PromptOption } from "@/lib/types";

interface Props {
  prompts: PromptOption[];
  promptsError: string | null;
  selectedId: string;
  text: string;
  busy: boolean;
  onSelect: (id: string) => void;
  onText: (text: string) => void;
  onSend: () => void;
}

export function PromptInput({
  prompts,
  promptsError,
  selectedId,
  text,
  busy,
  onSelect,
  onText,
  onSend,
}: Props) {
  const canSend = !busy && text.trim().length > 0;

  return (
    <div className="flex items-stretch gap-6">
      <div className="w-[26rem] shrink-0">
        <Select value={selectedId} onValueChange={onSelect} disabled={prompts.length === 0}>
          <SelectTrigger aria-label="Prompt">
            <SelectValue placeholder={promptsError ? "Prompts failed to load" : "Pick a prompt"} />
          </SelectTrigger>
          <SelectContent>
            {prompts.map((p) => (
              <SelectItem key={p.id} value={p.id}>
                {p.label}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
        {promptsError && <p className="mt-2 text-body">{promptsError}</p>}
      </div>

      <Textarea
        aria-label="Prompt text"
        rows={3}
        className="flex-1"
        placeholder="Or type a prompt"
        value={text}
        onChange={(e) => onText(e.target.value)}
        onKeyDown={(e) => {
          if (e.key === "Enter" && (e.metaKey || e.ctrlKey) && canSend) {
            e.preventDefault();
            onSend();
          }
        }}
      />

      <Button onClick={onSend} disabled={!canSend}>
        Send
      </Button>
    </div>
  );
}
