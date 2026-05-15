"use client";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Bot, Send, User2 } from "lucide-react";
import { api, type Conversation, type Message } from "@/lib/api";
import { formatClockTime, formatRelative } from "@/lib/datetime";
import { useWebWs } from "@/lib/ws";
import { toast } from "@/components/ui/sonner";
import { cn } from "@/lib/utils";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Textarea } from "@/components/ui/textarea";
import { ScrollArea } from "@/components/ui/scroll-area";
import { Separator } from "@/components/ui/separator";
import { Avatar, AvatarFallback } from "@/components/ui/avatar";

type Suggestion = { text: string; confidence: number; trace_id: string };
type KBChunk = { id: number; doc_id: number; kb_id: number; ord: number; text: string };

export default function WorkbenchPage() {
  const [convs, setConvs] = useState<Conversation[]>([]);
  const [activeId, setActiveId] = useState<number | null>(null);
  const [messages, setMessages] = useState<Message[]>([]);
  const [draft, setDraft] = useState("");
  const [sending, setSending] = useState(false);
  const [suggestions, setSuggestions] = useState<Record<number, Suggestion[]>>({});
  const [kbHits, setKbHits] = useState<Record<number, KBChunk[]>>({});
  const [memorySummary, setMemorySummary] = useState<string | null>(null);
  const scrollRef = useRef<HTMLDivElement | null>(null);
  const inputRef = useRef<HTMLTextAreaElement | null>(null);

  const active = useMemo(
    () => convs.find((c) => c.id === activeId) || null,
    [convs, activeId]
  );

  const reloadConvs = useCallback(async () => {
    const data = await api<Conversation[]>("/conversations");
    setConvs(data);
    setActiveId((cur) => cur ?? (data.length ? data[0].id : null));
  }, []);

  const reloadMessages = useCallback(async (cid: number) => {
    const data = await api<Message[]>(`/conversations/${cid}/messages?limit=100`);
    setMessages(data);
  }, []);

  useEffect(() => {
    reloadConvs().catch(console.error);
  }, [reloadConvs]);

  useEffect(() => {
    if (activeId == null) return;
    reloadMessages(activeId).catch(console.error);
    // pull profile summary
    const conv = convs.find((c) => c.id === activeId);
    if (conv) {
      api<{ summary?: string } | null>(`/memory/${conv.contact_id}`)
        .then((p) => setMemorySummary(p?.summary || null))
        .catch(() => setMemorySummary(null));
      // mark as read on the server (only if there are unread messages)
      if (conv.unread_count > 0) {
        api(`/conversations/${activeId}/read`, { method: "POST" }).catch(() => {});
      }
    }
  }, [activeId, convs, reloadMessages]);

  useEffect(() => {
    requestAnimationFrame(() => {
      const el = scrollRef.current;
      if (el) el.scrollTop = el.scrollHeight;
    });
  }, [messages]);

  useEffect(() => {
    const el = inputRef.current;
    if (!el) return;
    el.style.height = "0px";
    el.style.height = `${Math.min(el.scrollHeight, 176)}px`;
  }, [draft]);

  useWebWs(
    useCallback(
      (event, payload) => {
        if (event === "message.new") {
          if (payload.conversation_id === activeId) {
            setMessages((prev) =>
              prev.some((m) => m.id === payload.message.id) ? prev : [...prev, payload.message]
            );
            // operator is currently looking at this conversation → mark read
            // immediately so the unread badge does not flicker on.
            if (payload.message?.direction === "in") {
              api(`/conversations/${activeId}/read`, { method: "POST" }).catch(() => {});
            }
          }
          reloadConvs().catch(() => {});
        } else if (event === "message.updated") {
          if (payload.conversation_id === activeId) {
            setMessages((prev) =>
              prev.map((m) => (m.id === payload.message.id ? payload.message : m))
            );
          }
        } else if (event === "conversation.updated") {
          reloadConvs().catch(() => {});
        } else if (event === "ai.suggestion") {
          setSuggestions((prev) => ({
            ...prev,
            [payload.conversation_id]: payload.suggestions || [],
          }));
        } else if (event === "kb.hits") {
          const ids: number[] = payload.hit_ids || [];
          if (!ids.length) return;
          api<KBChunk[]>(`/kb/chunks/by-ids?ids=${ids.join(",")}`)
            .then((rows) =>
              setKbHits((prev) => ({ ...prev, [payload.conversation_id]: rows }))
            )
            .catch(() => {});
        }
      },
      [activeId, reloadConvs]
    )
  );

  async function send(text?: string) {
    const content = (text ?? draft).trim();
    if (!active || !content) return;
    setSending(true);
    try {
      const res = await api<{ message: Message }>(`/conversations/${active.id}/messages`, {
        method: "POST",
        body: JSON.stringify({ type: "text", content }),
      });
      setMessages((prev) =>
        prev.some((m) => m.id === res.message.id) ? prev : [...prev, res.message]
      );
      reloadConvs().catch(() => {});
      if (!text) setDraft("");
    } catch (e: any) {
      toast.error("发送失败", { description: e?.message ?? String(e) });
    } finally {
      setSending(false);
    }
  }

  async function changeMode(mode: "ai" | "human" | "mixed") {
    if (!active) return;
    await api(`/conversations/${active.id}`, {
      method: "PATCH",
      body: JSON.stringify({ mode }),
    });
    reloadConvs();
  }

  const activeSuggestions = active ? suggestions[active.id] || [] : [];
  const activeKbHits = active ? kbHits[active.id] || [] : [];

  return (
    <div className="grid h-full min-h-0 grid-cols-[18rem_minmax(0,1fr)_20rem] overflow-hidden">
      {/* left: conversations */}
      <div className="flex min-h-0 min-w-0 flex-col border-r bg-background">
        <div className="shrink-0 border-b px-4 py-3">
          <h2 className="text-sm font-semibold">会话</h2>
        </div>
        <ScrollArea className="min-h-0 flex-1">
          {convs.length === 0 && (
            <p className="px-4 py-6 text-xs text-muted-foreground">暂无会话</p>
          )}
          {convs.map((c) => {
            const initial = (c.contact.nickname || c.contact.external_id).slice(0, 1);
            const ts = c.last_message_at ? formatRelative(c.last_message_at) : "";
            // grid columns: avatar (auto) + content (1fr, can shrink); content
            // itself uses grid-cols-[minmax(0,1fr)_auto] for each row so the
            // text column is hard-capped and badges/timestamps never get
            // pushed off-screen.
            return (
              <button
                key={c.id}
                onClick={() => setActiveId(c.id)}
                className={cn(
                  "grid w-full grid-cols-[auto_minmax(0,1fr)] items-start gap-3 border-b px-3 py-3 text-left transition-colors",
                  activeId === c.id ? "bg-accent" : "hover:bg-accent/50"
                )}
              >
                <Avatar className="h-10 w-10 mt-0.5">
                  <AvatarFallback className="text-sm">{initial}</AvatarFallback>
                </Avatar>
                <div className="min-w-0">
                  <div className="grid grid-cols-[minmax(0,1fr)_auto] items-baseline gap-2">
                    <span className="truncate text-sm font-medium">
                      {c.contact.nickname || c.contact.external_id}
                    </span>
                    {ts && (
                      <span className="text-[10px] text-muted-foreground">
                        {ts}
                      </span>
                    )}
                  </div>
                  <div className="mt-1 grid grid-cols-[minmax(0,1fr)_auto] items-center gap-2">
                    <p className="truncate text-xs text-muted-foreground">
                      {c.last_message_preview || "—"}
                    </p>
                    {c.unread_count > 0 && (
                      <Badge
                        variant="destructive"
                        className="h-4 min-w-[18px] justify-center rounded-full px-1 text-[10px] leading-none"
                      >
                        {c.unread_count > 99 ? "99+" : c.unread_count}
                      </Badge>
                    )}
                  </div>
                </div>
              </button>
            );
          })}
        </ScrollArea>
      </div>

      {/* center: chat */}
      <div className="flex min-h-0 min-w-0 flex-col overflow-hidden">
        {!active && (
          <div className="flex flex-1 items-center justify-center text-sm text-muted-foreground">
            选择左侧会话开始
          </div>
        )}
        {active && (
          <>
            <div className="flex shrink-0 items-center justify-between border-b bg-background px-4 py-3">
              <div className="flex items-center gap-3">
                <Avatar className="h-8 w-8">
                  <AvatarFallback>
                    {(active.contact.nickname || active.contact.external_id).slice(0, 1)}
                  </AvatarFallback>
                </Avatar>
                <div>
                  <div className="text-sm font-medium">
                    {active.contact.nickname || active.contact.external_id}
                  </div>
                  <div className="text-xs text-muted-foreground">阶段: {active.contact.stage}</div>
                </div>
              </div>
              <Select value={active.mode} onValueChange={(v) => changeMode(v as any)}>
                <SelectTrigger className="w-28">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="ai">AI</SelectItem>
                  <SelectItem value="human">人工</SelectItem>
                  <SelectItem value="mixed">混合</SelectItem>
                </SelectContent>
              </Select>
            </div>

            <div ref={scrollRef} className="min-h-0 flex-1 overflow-y-auto">
              <div className="mx-auto flex min-h-full w-full max-w-4xl flex-col justify-end gap-3 px-5 py-4">
                {messages.map((m) => (
                  <MessageBubble key={m.id} m={m} />
                ))}
              </div>
            </div>

            <div className="shrink-0 border-t bg-background/95 px-4 py-3">
              <div className="mx-auto w-full max-w-4xl rounded-2xl border border-border/60 bg-background p-2 shadow-sm transition-shadow focus-within:border-border focus-within:shadow-md">
                <Textarea
                  ref={inputRef}
                  value={draft}
                  onChange={(e) => setDraft(e.target.value)}
                  onKeyDown={(e) => {
                    if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) {
                      e.preventDefault();
                      send();
                    }
                  }}
                  placeholder="输入消息"
                  rows={1}
                  className="max-h-44 min-h-[52px] resize-none overflow-y-auto border-0 bg-transparent px-3 py-3 text-sm leading-6 shadow-none focus-visible:ring-0 focus-visible:ring-offset-0"
                />
                <div className="flex justify-end px-1 pb-1">
                  <Button
                    size="icon"
                    onClick={() => send()}
                    disabled={sending || !draft.trim()}
                    className="h-8 w-8 rounded-full"
                    aria-label="发送"
                  >
                    <Send className="h-4 w-4" />
                  </Button>
                </div>
              </div>
            </div>
          </>
        )}
      </div>

      {/* right: panel */}
      <div className="flex min-h-0 min-w-0 flex-col overflow-hidden border-l bg-background">
        <ScrollArea className="min-h-0 flex-1">
          <div className="space-y-4 p-4">
            <Section title="客户">
              {active ? (
                <div className="space-y-1 text-sm">
                  <Row label="昵称" value={active.contact.nickname || "-"} />
                  <Row label="external_id" value={active.contact.external_id} mono />
                  <Row label="阶段" value={active.contact.stage} />
                </div>
              ) : (
                <p className="text-xs text-muted-foreground">未选择会话</p>
              )}
            </Section>
            <Separator />
            <Section title="AI 推荐">
              {!active && <p className="text-xs text-muted-foreground">未选择会话</p>}
              {active && activeSuggestions.length === 0 && (
                <p className="text-xs text-muted-foreground">暂无推荐</p>
              )}
              {active &&
                activeSuggestions.map((s, i) => (
                  <button
                    key={i}
                    onClick={() => send(s.text)}
                    className="block w-full rounded-md border bg-card p-2 text-left text-xs hover:bg-accent"
                  >
                    <div className="mb-1 flex items-center justify-between">
                      <Badge variant="secondary" className="text-[10px]">
                        置信度 {(s.confidence * 100).toFixed(0)}%
                      </Badge>
                    </div>
                    <p className="whitespace-pre-wrap leading-relaxed text-foreground">
                      {s.text}
                    </p>
                  </button>
                ))}
            </Section>
            <Separator />
            <Section title="长期记忆">
              {memorySummary ? (
                <p className="rounded-md bg-muted/50 p-2 text-xs leading-relaxed">
                  {memorySummary}
                </p>
              ) : (
                <p className="text-xs text-muted-foreground">暂无摘要</p>
              )}
            </Section>
            <Separator />
            <Section title="知识库命中">
              {activeKbHits.length === 0 && (
                <p className="text-xs text-muted-foreground">暂无命中</p>
              )}
              {activeKbHits.map((c, i) => (
                <div
                  key={c.id}
                  className="rounded-md border bg-card p-2 text-xs leading-relaxed"
                >
                  <div className="mb-1 flex items-center justify-between text-[10px] text-muted-foreground">
                    <span>#{i + 1}</span>
                    <span>chunk_id={c.id}</span>
                  </div>
                  <p className="line-clamp-5 break-all">{c.text}</p>
                </div>
              ))}
            </Section>
          </div>
        </ScrollArea>
      </div>
    </div>
  );
}

function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <div>
      <h3 className="mb-2 text-[11px] font-medium uppercase tracking-wider text-muted-foreground">
        {title}
      </h3>
      <div className="space-y-2">{children}</div>
    </div>
  );
}

function Row({ label, value, mono }: { label: string; value: string; mono?: boolean }) {
  return (
    <div className="grid grid-cols-[auto_minmax(0,1fr)] items-center gap-2 text-xs">
      <span className="text-muted-foreground">{label}</span>
      <span className={cn("truncate text-right text-foreground", mono && "font-mono")}>
        {value}
      </span>
    </div>
  );
}

function MessageBubble({ m }: { m: Message }) {
  const isOut = m.direction === "out";
  const isAI = m.sender_type === "ai";
  const statusLabel =
    isOut && m.status
      ? m.status === "sent"
        ? "已发送"
        : m.status === "failed"
        ? "发送失败"
        : "发送中…"
      : null;

  return (
    <div className={cn("flex", isOut ? "justify-end" : "justify-start")}>
      <div
        className={cn(
          "flex max-w-[min(78%,44rem)] flex-col gap-1",
          isOut ? "items-end" : "items-start"
        )}
      >
        {isOut && (
          <div className="flex items-center gap-1 text-[10px] text-muted-foreground">
            {isAI ? (
              <>
                <Bot className="h-3 w-3" /> AI
              </>
            ) : (
              <>
                <User2 className="h-3 w-3" /> 人工
              </>
            )}
          </div>
        )}
        <div
          className={cn(
            "rounded-2xl px-3 py-2 text-sm shadow-sm",
            isOut
              ? isAI
                ? "bg-blue-600 text-white"
                : "bg-emerald-600 text-white"
              : "bg-card border"
          )}
        >
          <p className="whitespace-pre-wrap break-words leading-6">{m.content}</p>
        </div>
        <div className="text-[10px] text-muted-foreground">
          {formatClockTime(m.created_at)}
          {statusLabel && <span> · {statusLabel}</span>}
        </div>
      </div>
    </div>
  );
}
