"use client";
import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { Bot, BookOpen, MessageSquare, Settings, Smartphone, Sparkles, LogOut } from "lucide-react";
import { setToken } from "@/lib/api";
import { Button } from "@/components/ui/button";
import { ThemeToggle } from "@/components/ThemeToggle";
import { cn } from "@/lib/utils";

const items = [
  { href: "/workbench", label: "工作台", icon: MessageSquare },
  { href: "/devices", label: "设备", icon: Smartphone },
  { href: "/knowledge", label: "知识库", icon: BookOpen },
  { href: "/personas", label: "人格", icon: Sparkles },
  { href: "/settings", label: "模型配置", icon: Settings },
];

export function Sidebar() {
  const path = usePathname();
  const router = useRouter();
  return (
    <aside className="flex w-52 flex-col border-r bg-background">
      <div className="flex items-center gap-2 px-4 py-4 border-b">
        <Bot className="h-5 w-5" />
        <span className="text-sm font-semibold">WeCom AI</span>
      </div>
      <nav className="flex-1 space-y-1 p-2">
        {items.map((it) => {
          const Icon = it.icon;
          const active = path?.startsWith(it.href);
          return (
            <Link
              key={it.href}
              href={it.href}
              className={cn(
                "flex items-center gap-2 rounded-md px-3 py-2 text-sm transition-colors",
                active
                  ? "bg-accent text-accent-foreground font-medium"
                  : "text-muted-foreground hover:bg-accent/50 hover:text-foreground"
              )}
            >
              <Icon className="h-4 w-4" />
              {it.label}
            </Link>
          );
        })}
      </nav>
      <div className="space-y-3 border-t p-3">
        <ThemeToggle />
        <Button
          variant="ghost"
          size="sm"
          className="w-full justify-start"
          onClick={() => {
            setToken(null);
            router.replace("/");
          }}
        >
          <LogOut className="h-4 w-4" />
          退出登录
        </Button>
      </div>
    </aside>
  );
}
