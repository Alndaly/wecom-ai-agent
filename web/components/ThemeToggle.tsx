"use client";
import { useEffect, useState } from "react";
import { Monitor, Moon, Sun } from "lucide-react";
import { useTheme } from "next-themes";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
} from "@/components/ui/select";

const themeLabels: Record<string, string> = {
  system: "跟随系统",
  light: "浅色",
  dark: "深色",
};

export function ThemeToggle() {
  const { theme = "system", setTheme } = useTheme();
  const [mounted, setMounted] = useState(false);

  useEffect(() => {
    setMounted(true);
  }, []);

  const value = mounted ? theme : "system";
  const Icon = value === "dark" ? Moon : value === "light" ? Sun : Monitor;

  return (
    <div className="space-y-1">
      <p className="px-1 text-xs text-muted-foreground">外观</p>
      <Select value={value} onValueChange={setTheme}>
        <SelectTrigger className="h-8">
          <div className="flex items-center gap-2">
            <Icon className="h-4 w-4" />
            <span>{themeLabels[value]}</span>
          </div>
        </SelectTrigger>
        <SelectContent>
          <SelectItem value="system">跟随系统</SelectItem>
          <SelectItem value="light">浅色</SelectItem>
          <SelectItem value="dark">深色</SelectItem>
        </SelectContent>
      </Select>
    </div>
  );
}
