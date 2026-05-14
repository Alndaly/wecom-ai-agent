"use client";

import { useTheme } from "next-themes";
import { Toaster as SonnerToaster, toast as sonnerToast } from "sonner";

type ToasterProps = React.ComponentProps<typeof SonnerToaster>;

/** App-level mount point — render once in the root layout. */
export function Toaster({ ...props }: ToasterProps) {
  const { theme = "system" } = useTheme();
  return (
    <SonnerToaster
      theme={theme as ToasterProps["theme"]}
      position="bottom-right"
      richColors
      closeButton
      toastOptions={{
        classNames: {
          toast:
            "group toast group-[.toaster]:bg-background group-[.toaster]:text-foreground group-[.toaster]:border-border group-[.toaster]:shadow-lg",
          description: "group-[.toast]:text-muted-foreground",
          actionButton:
            "group-[.toast]:bg-primary group-[.toast]:text-primary-foreground",
          cancelButton:
            "group-[.toast]:bg-muted group-[.toast]:text-muted-foreground",
        },
      }}
      {...props}
    />
  );
}

export { sonnerToast as toast };
