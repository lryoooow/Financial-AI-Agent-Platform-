import * as Alert from "@radix-ui/react-alert-dialog";
import { createContext, useContext, useEffect, useRef, useState, type ReactNode } from "react";
import { isImeKeyEvent } from "../../lib/ime";

type Confirm = (title: string, description?: string) => Promise<boolean>;
const ConfirmContext = createContext<Confirm | null>(null);

export function ConfirmProvider({ children }: { children: ReactNode }) {
  const [dialog, setDialog] = useState<{ title: string; description: string } | null>(null);
  const pending = useRef<((value: boolean) => void) | null>(null);
  useEffect(() => () => pending.current?.(false), []);

  const finish = (value: boolean) => {
    pending.current?.(value);
    pending.current = null;
    setDialog(null);
  };
  const confirm: Confirm = (title, description = "") => new Promise((resolve) => {
    pending.current?.(false);
    pending.current = resolve;
    setDialog({ title, description });
  });

  return (
    <ConfirmContext.Provider value={confirm}>
      {children}
      <Alert.Root open={!!dialog} onOpenChange={(open) => { if (!open) finish(false); }}>
        <Alert.Portal>
          <Alert.Overlay className="fixed inset-0 z-[70] bg-black/45" />
          <Alert.Content className="fixed left-1/2 top-1/2 z-[71] w-[min(420px,calc(100%-32px))] -translate-1/2 rounded-lg border bg-popover p-5 shadow-[var(--shadow-popover)]"
            onEscapeKeyDown={(event) => { if (isImeKeyEvent(event)) event.preventDefault(); }}>
            <Alert.Title className="text-base">{dialog?.title}</Alert.Title>
            <Alert.Description className="mt-2 text-sm text-muted-foreground">{dialog?.description}</Alert.Description>
            <div className="mt-5 flex justify-end gap-2">
              <Alert.Cancel className="rounded-md border px-4 py-1.5" onClick={() => finish(false)}>取消</Alert.Cancel>
              <Alert.Action className="rounded-md bg-destructive px-4 py-1.5 text-destructive-foreground" onClick={() => finish(true)}>确认</Alert.Action>
            </div>
          </Alert.Content>
        </Alert.Portal>
      </Alert.Root>
    </ConfirmContext.Provider>
  );
}

export function useConfirm(): Confirm {
  const confirm = useContext(ConfirmContext);
  if (!confirm) throw new Error("ConfirmProvider is required");
  return confirm;
}
