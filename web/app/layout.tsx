import "./globals.css";
import type { Metadata } from "next";
import { Toaster } from "@/components/toaster";

export const metadata: Metadata = {
  title: "WeCom AI Agent",
  description: "AI + 人工协同的企微私域运营平台",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="zh-CN" suppressHydrationWarning>
      <body>
        {children}
        <Toaster />
      </body>
    </html>
  );
}
