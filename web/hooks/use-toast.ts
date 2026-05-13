"use client";
// Lightweight toast hook (shadcn-compatible API, simplified).
import { useCallback, useEffect, useState } from "react";
import type { ToastProps } from "@/components/ui/toast";

type ToastInput = {
  title?: React.ReactNode;
  description?: React.ReactNode;
  variant?: ToastProps["variant"];
  duration?: number;
};

type ToastItem = ToastInput & { id: string; open: boolean };

const listeners = new Set<(items: ToastItem[]) => void>();
let memory: ToastItem[] = [];

function emit() {
  for (const l of listeners) l(memory);
}

export function toast(input: ToastInput) {
  const id = Math.random().toString(36).slice(2);
  const item: ToastItem = { id, open: true, duration: 3000, ...input };
  memory = [item, ...memory].slice(0, 5);
  emit();
  setTimeout(() => {
    memory = memory.filter((t) => t.id !== id);
    emit();
  }, item.duration);
  return id;
}

export function useToast() {
  const [items, setItems] = useState<ToastItem[]>(memory);
  useEffect(() => {
    listeners.add(setItems);
    return () => {
      listeners.delete(setItems);
    };
  }, []);
  const dismiss = useCallback((id: string) => {
    memory = memory.filter((t) => t.id !== id);
    emit();
  }, []);
  return { toasts: items, toast, dismiss };
}
