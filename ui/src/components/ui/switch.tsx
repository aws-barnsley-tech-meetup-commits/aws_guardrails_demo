import * as SwitchPrimitive from "@radix-ui/react-switch";
import type { ComponentProps } from "react";
import { cn } from "@/lib/utils";

export function Switch({ className, ...props }: ComponentProps<typeof SwitchPrimitive.Root>) {
  return (
    <SwitchPrimitive.Root
      className={cn(
        "peer inline-flex h-10 w-[4.5rem] shrink-0 cursor-pointer items-center rounded-sm border-2 border-ink p-1",
        "bg-surface-100 data-[state=checked]:bg-ink",
        "focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-ink",
        "disabled:cursor-not-allowed disabled:opacity-50",
        className,
      )}
      {...props}
    >
      <SwitchPrimitive.Thumb
        className={cn(
          "block size-6 rounded-sm bg-ink transition-transform",
          "data-[state=checked]:translate-x-8 data-[state=checked]:bg-surface-100",
        )}
      />
    </SwitchPrimitive.Root>
  );
}
