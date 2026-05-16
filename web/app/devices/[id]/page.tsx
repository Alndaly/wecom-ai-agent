"use client";
import { useEffect, useMemo, useRef, useState } from "react";
import Link from "next/link";
import { useParams } from "next/navigation";
import {
  ArrowLeft,
  Activity,
  CheckCircle2,
  Copy,
  FileSearch,
  Loader2,
  MonitorUp,
  Radio,
  Send,
  ShieldAlert,
  Square,
  Smartphone,
  Trash2,
  Wand2,
  XCircle,
} from "lucide-react";
import { Textarea } from "@/components/ui/textarea";
import { api, type Robot, type RobotTaskLog } from "@/lib/api";
import { formatFull } from "@/lib/datetime";
import { useWebWs } from "@/lib/ws";
import { toast } from "@/components/ui/sonner";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from "@/components/ui/alert-dialog";
import type { RobotQueueItem, RobotQueueSnapshot } from "@/lib/api";

type ScreenFrame = {
  robot_id: string;
  image: string | null;
  mime: string;
  width: number | null;
  height: number | null;
  error: string | null;
  created_at: string;
};

type UiDump = {
  request_id: string | null;
  robot_id: string;
  current_page: string;
  reason: string;
  tree: string;
  path: string;
  created_at: string;
};

export default function DeviceDetailPage() {
  const params = useParams<{ id: string }>();
  const id = Number(params.id);
  const [robot, setRobot] = useState<Robot | null>(null);
  const [loading, setLoading] = useState(true);
  const [streaming, setStreaming] = useState(false);
  const [streamBusy, setStreamBusy] = useState(false);
  const [frame, setFrame] = useState<ScreenFrame | null>(null);
  const [frameError, setFrameError] = useState<string | null>(null);
  const [lastCommandMessage, setLastCommandMessage] = useState<string | null>(null);
  const [dumping, setDumping] = useState(false);
  const [pendingDumpRequest, setPendingDumpRequest] = useState<string | null>(null);
  const [uiDump, setUiDump] = useState<UiDump | null>(null);
  const [logs, setLogs] = useState<RobotTaskLog[]>([]);
  const [queue, setQueue] = useState<RobotQueueSnapshot | null>(null);
  const [cancellingTaskId, setCancellingTaskId] = useState<number | null>(null);
  const [clearingLogs, setClearingLogs] = useState(false);
  const [clearLogsOpen, setClearLogsOpen] = useState(false);
  const [agentGoal, setAgentGoal] = useState("");
  const [agentMaxSteps, setAgentMaxSteps] = useState(8);
  const [agentSubmitting, setAgentSubmitting] = useState(false);

  useEffect(() => {
    if (!Number.isFinite(id)) return;
    setLoading(true);
    api<Robot>(`/robots/${id}`)
      .then(setRobot)
      .catch((e: any) => toast.error("加载设备失败", { description: e?.message ?? String(e) }))
      .finally(() => setLoading(false));
  }, [id]);

  useEffect(() => {
    if (!Number.isFinite(id)) return;
    const timer = setInterval(() => {
      api<Robot>(`/robots/${id}`)
        .then(setRobot)
        .catch(() => {});
    }, 5000);
    return () => clearInterval(timer);
  }, [id]);

  useEffect(() => {
    if (!Number.isFinite(id)) return;
    const loadLogs = () => {
      // Backend returns DESC by created_at — newest first, like a chat
      // / notification feed. We keep that order in the UI.
      api<RobotTaskLog[]>(`/robots/${id}/logs?limit=200`)
        .then(setLogs)
        .catch(() => {});
    };
    loadLogs();
    // WS pushes new entries in real time (see task.log handler); this poll
    // is just a 30s safety net for missed events.
    const timer = setInterval(loadLogs, 30_000);
    return () => clearInterval(timer);
  }, [id]);

  useEffect(() => {
    if (!Number.isFinite(id)) return;
    const loadQueue = () =>
      api<RobotQueueSnapshot>(`/robots/${id}/queue`)
        .then(setQueue)
        .catch(() => {});
    loadQueue();
    // 3s feels right — visible feedback when an operator clicks "执行" and
    // their task moves through the queue, without hammering the API.
    const timer = setInterval(loadQueue, 3_000);
    return () => clearInterval(timer);
  }, [id]);

  useEffect(() => {
    if (!streaming || frame || frameError) return;
    const timer = setTimeout(() => {
      setFrameError("尚未收到屏幕帧。请确认安卓端已升级到最新 APK、无障碍服务已重新开启，并且系统版本为 Android 11/API 30 及以上。");
    }, 8000);
    return () => clearTimeout(timer);
  }, [streaming, frame, frameError]);

  useEffect(() => {
    return () => {
      if (Number.isFinite(id) && streaming) {
        api(`/robots/${id}/screen/stop`, { method: "POST" }).catch(() => {});
      }
    };
  }, [id, streaming]);

  useWebWs((event, payload) => {
    if (event === "robot.status" && robot?.robot_id === payload.robot_id) {
      setRobot((prev) => (prev ? { ...prev, status: payload.status } : prev));
    }
    if (event === "robot.updated" && robot?.robot_id === payload.robot_id) {
      setRobot((prev) => (prev ? { ...prev, ...payload } : prev));
    }
    if (event === "device.screen_frame" && robot?.robot_id === payload.robot_id) {
      if (payload.error) {
        setFrameError(payload.error);
        toast.error("屏幕帧获取失败", { description: payload.error });
        return;
      }
      setFrameError(null);
      setFrame(payload);
    }
    if (event === "device.command_ack" && robot?.robot_id === payload.robot_id) {
      const message = payload.message ?? payload.command ?? "设备命令已确认";
      setLastCommandMessage(message);
      if (payload.ok === false) toast.error("设备命令失败", { description: message });
    }
    if (event === "device.ui_dump" && robot?.robot_id === payload.robot_id) {
      // Only show the dialog when *this* operator clicked "获取 UI 树".
      // ReAct-driven dumps (`reason: "react"`) and dumps initiated by other
      // operators also fan out on this WS, but we silently ignore them —
      // otherwise the dialog would pop up every step of an agent run.
      if (!pendingDumpRequest) return;
      if (payload.request_id !== pendingDumpRequest) return;
      setUiDump(payload);
      setDumping(false);
      setPendingDumpRequest(null);
      toast.success("UI 树已回传");
    }
    if (event === "task.log" && robot?.robot_id === payload.robot_id) {
      // Append the freshly-arrived log row (oldest at top, newest at bottom).
      // The polling effect will reconcile any missed rows every 30s; this
      // path is the one that makes the trace feel live during a ReAct run.
      const row: RobotTaskLog = {
        id: Number(payload.id ?? Date.now()),
        robot_id: 0,
        task_id: payload.task_id ?? null,
        level: payload.level ?? "info",
        message: payload.message ?? "",
        created_at: payload.created_at ?? new Date().toISOString(),
      };
      setLogs((prev) => {
        if (row.id && prev.some((p) => p.id === row.id)) return prev;
        // Newest on top — prepend.
        return [row, ...prev];
      });
    }
    if (event === "task.updated") {
      api<RobotQueueSnapshot>(`/robots/${id}/queue`).then(setQueue).catch(() => {});
    }
    if (event === "robot.logs_cleared" && robot?.robot_id === payload.robot_id) {
      setLogs([]);
    }
  });

  const screenLabel = useMemo(() => {
    if (!robot?.screen_width || !robot.screen_height) return "未知分辨率";
    return `${robot.screen_width} x ${robot.screen_height}`;
  }, [robot]);

  async function startStream() {
    if (!Number.isFinite(id)) return;
    setStreamBusy(true);
    try {
      await api(`/robots/${id}/screen/start`, { method: "POST" });
      setStreaming(true);
      setFrame(null);
      setFrameError(null);
      setLastCommandMessage("已下发开启实时屏幕命令");
      toast.success("实时屏幕已开启");
    } catch (e: any) {
      toast.error("开启失败", { description: e?.message ?? String(e) });
    } finally {
      setStreamBusy(false);
    }
  }

  async function stopStream() {
    if (!Number.isFinite(id)) return;
    setStreamBusy(true);
    try {
      await api(`/robots/${id}/screen/stop`, { method: "POST" });
      setStreaming(false);
      setLastCommandMessage("已下发关闭实时屏幕命令");
      toast.success("实时屏幕已关闭");
    } catch (e: any) {
      toast.error("关闭失败", { description: e?.message ?? String(e) });
    } finally {
      setStreamBusy(false);
    }
  }

  async function requestUiDump() {
    if (!Number.isFinite(id)) return;
    setDumping(true);
    try {
      const data = await api<{ request_id: string; dispatched: boolean }>(`/robots/${id}/ui-dump`, {
        method: "POST",
      });
      setPendingDumpRequest(data.request_id);
      toast.success("已请求采集 UI 树");
    } catch (e: any) {
      setDumping(false);
      toast.error("请求失败", { description: e?.message ?? String(e) });
    }
  }

  async function confirmClearLogs() {
    if (!Number.isFinite(id)) return;
    setClearingLogs(true);
    try {
      await api(`/robots/${id}/logs`, { method: "DELETE" });
      setLogs([]);
      toast.success("已清空任务日志");
      setClearLogsOpen(false);
    } catch (e: any) {
      toast.error("清空失败", { description: e?.message ?? String(e) });
    } finally {
      setClearingLogs(false);
    }
  }

  async function runAgentGoal() {
    if (!Number.isFinite(id)) return;
    const goal = agentGoal.trim();
    if (!goal) {
      toast.error("请先输入指令");
      return;
    }
    setAgentSubmitting(true);
    try {
      const res = await api<{ task_id: number; accepted: boolean }>(
        `/robots/${id}/agent/run`,
        {
          method: "POST",
          body: JSON.stringify({ goal, max_steps: agentMaxSteps }),
        }
      );
      toast.success(`已下发指令 #${res.task_id}`, {
        description: "ReAct agent 正在逐步执行，进度见下方日志",
      });
      setAgentGoal("");
    } catch (e: any) {
      toast.error("下发失败", { description: e?.message ?? String(e) });
    } finally {
      setAgentSubmitting(false);
    }
  }

  async function cancelTask(taskId: number) {
    if (!Number.isFinite(id)) return;
    setCancellingTaskId(taskId);
    try {
      await api(`/robots/${id}/tasks/${taskId}/cancel`, { method: "POST" });
      toast.success(`已请求中断任务 #${taskId}`);
      const latest = await api<RobotQueueSnapshot>(`/robots/${id}/queue`);
      setQueue(latest);
    } catch (e: any) {
      toast.error("中断失败", { description: e?.message ?? String(e) });
    } finally {
      setCancellingTaskId(null);
    }
  }

  if (loading) {
    return <div className="text-sm text-muted-foreground">加载中...</div>;
  }

  if (!robot) {
    return <div className="text-sm text-muted-foreground">设备不存在</div>;
  }

  return (
    <div className="flex h-[calc(100vh-3rem)] min-h-[760px] flex-col gap-4 overflow-hidden">
      <div className="flex shrink-0 items-center justify-between gap-4 border-b pb-4">
        <div className="min-w-0">
          <Button asChild variant="ghost" size="sm" className="-ml-3 mb-1 h-8 px-2 text-muted-foreground">
            <Link href="/devices">
              <ArrowLeft className="h-4 w-4" />
              返回设备
            </Link>
          </Button>
          <div className="flex min-w-0 items-center gap-3">
            <h1 className="truncate text-2xl font-semibold tracking-tight">{robot.name}</h1>
            <StatusBadge status={robot.status} />
          </div>
          <p className="mt-1 font-mono text-xs text-muted-foreground">{robot.robot_id}</p>
        </div>
        <div className="flex shrink-0 items-center gap-2">
          <Button
            variant="outline"
            disabled={dumping || robot.status !== "online"}
            onClick={requestUiDump}
            className="h-9"
          >
            {dumping ? <Loader2 className="h-4 w-4 animate-spin" /> : <FileSearch className="h-4 w-4" />}
            获取 UI 树
          </Button>
          {streaming ? (
            <Button variant="outline" disabled={streamBusy} onClick={stopStream} className="h-9">
              {streamBusy ? <Loader2 className="h-4 w-4 animate-spin" /> : <Square className="h-4 w-4" />}
              关闭实时屏幕
            </Button>
          ) : (
            <Button disabled={streamBusy || robot.status !== "online"} onClick={startStream} className="h-9">
              {streamBusy ? <Loader2 className="h-4 w-4 animate-spin" /> : <MonitorUp className="h-4 w-4" />}
              开启实时屏幕
            </Button>
          )}
        </div>
      </div>

      <div className="grid min-h-0 flex-1 gap-4 xl:grid-cols-[300px_minmax(520px,1fr)_400px]">
        <aside className="min-h-0 space-y-4 overflow-auto pr-1">
          <Card className="rounded-lg shadow-sm">
            <CardHeader className="border-b p-4">
              <CardTitle className="flex items-center gap-2 text-sm">
                <Smartphone className="h-4 w-4 text-muted-foreground" />
                设备信息
              </CardTitle>
            </CardHeader>
            <CardContent className="p-4">
              <div className="grid grid-cols-2 gap-3">
                <Metric label="页面" value={robot.current_page ?? "UNKNOWN"} />
                <Metric label="分辨率" value={screenLabel} />
              </div>
              <div className="mt-4 space-y-3 border-t pt-4 text-sm">
                <InfoRow label="设备类型" value={robot.device_type ?? "未知"} />
                <InfoRow label="设备名称" value={robot.device_name ?? "未知"} />
                <InfoRow label="厂商型号" value={[robot.manufacturer, robot.model].filter(Boolean).join(" ") || "未知"} />
                <InfoRow label="Android" value={robot.android_version ? `${robot.android_version} / API ${robot.sdk_int ?? "-"}` : "未知"} />
                <InfoRow label="Agent" value={robot.app_version ?? "未知"} />
                <InfoRow label="最近上线" value={robot.last_seen_at ? formatFull(robot.last_seen_at) : "-"} />
              </div>
            </CardContent>
          </Card>

          <Card className="rounded-lg shadow-sm">
            <CardHeader className="border-b p-4">
              <CardTitle className="flex items-center gap-2 text-sm">
                <Activity className="h-4 w-4 text-muted-foreground" />
                控制状态
              </CardTitle>
            </CardHeader>
            <CardContent className="space-y-3 p-4 text-sm">
              <StateRow label="实时屏幕" value={streaming ? "已开启" : "未开启"} active={streaming} />
              <StateRow label="屏幕帧" value={frame ? formatFull(frame.created_at) : "等待中"} />
              <StateRow label="最近命令" value={lastCommandMessage ?? "暂无"} />
              <StateRow
                label="队列"
                value={
                  queue == null
                    ? "—"
                    : queue.running
                      ? `运行中 ${queue.running.kind} #${queue.running.task_id} · 等待中 ${queue.depth}`
                      : queue.depth > 0
                        ? `等待中 ${queue.depth}`
                        : "空闲"
                }
                active={!!queue?.running || (queue?.depth ?? 0) > 0}
              />
            </CardContent>
          </Card>
          <TaskQueuePanel
            queue={queue}
            cancellingTaskId={cancellingTaskId}
            onCancel={cancelTask}
          />
        </aside>

        <main className="min-h-0">
          <Card className="flex h-full min-w-0 flex-col rounded-lg shadow-sm">
            <CardHeader className="flex-row items-center justify-between space-y-0 border-b p-4">
              <CardTitle className="flex items-center gap-2 text-sm">
                <MonitorUp className="h-4 w-4 text-muted-foreground" />
                实时屏幕
              </CardTitle>
              <div className="flex items-center gap-2">
                {frame && (
                  <span className="font-mono text-xs text-muted-foreground">
                    {frame.width} x {frame.height}
                  </span>
                )}
                {streaming && (
                  <Badge variant="secondary" className="gap-1.5">
                    <Radio className="h-3 w-3" />
                    接收中
                  </Badge>
                )}
              </div>
            </CardHeader>
            <CardContent className="flex min-h-0 flex-1 flex-col p-4">
              <div className="flex min-h-0 flex-1 items-center justify-center overflow-hidden rounded-lg border bg-slate-100 p-4 dark:bg-slate-900">
                {frame?.image ? (
                  <img
                    src={`data:${frame.mime};base64,${frame.image}`}
                    alt="设备实时屏幕"
                    className="h-full max-h-full max-w-full rounded-md object-contain shadow-sm"
                  />
                ) : (
                  <div className="max-w-sm text-center text-sm text-muted-foreground">
                    {frameError ?? (streaming ? "等待第一帧..." : "开启实时屏幕后会显示设备当前屏幕")}
                  </div>
                )}
              </div>
              <div className="mt-3 flex min-h-5 items-center justify-between gap-3 text-xs text-muted-foreground">
                <span>{frame ? formatFull(frame.created_at) : "无屏幕帧"}</span>
                {lastCommandMessage && <span className="truncate">{lastCommandMessage}</span>}
              </div>
            </CardContent>
          </Card>
        </main>

        <aside className="flex min-h-0 flex-col gap-4 overflow-hidden">
          <AgentCommandCard
            value={agentGoal}
            onChange={setAgentGoal}
            maxSteps={agentMaxSteps}
            onMaxStepsChange={setAgentMaxSteps}
            onSubmit={runAgentGoal}
            submitting={agentSubmitting}
            online={robot.status === "online"}
          />
          <TaskLogPanel
            logs={logs}
            onClear={() => setClearLogsOpen(true)}
            clearing={clearingLogs}
          />
        </aside>
      </div>

      <Dialog open={!!uiDump} onOpenChange={(o) => !o && setUiDump(null)}>
        <DialogContent className="flex max-h-[88vh] w-[min(1120px,calc(100vw-48px))] max-w-none flex-col overflow-hidden">
          <DialogHeader>
            <DialogTitle>当前 UI 树</DialogTitle>
            <DialogDescription>
              {uiDump ? `${uiDump.current_page} · ${formatFull(uiDump.created_at)}` : ""}
            </DialogDescription>
          </DialogHeader>
          {uiDump && (
            <div className="flex min-h-0 flex-1 flex-col space-y-3 overflow-hidden">
              <div className="grid min-w-0 gap-1 text-xs text-muted-foreground sm:grid-cols-[220px_1fr]">
                <span className="truncate">request_id: {uiDump.request_id ?? "manual"}</span>
                <span className="min-w-0 truncate">saved: {uiDump.path}</span>
              </div>
              <pre className="min-h-0 flex-1 overflow-auto rounded-md border bg-muted p-3 font-mono text-xs leading-relaxed whitespace-pre">
                {uiDump.tree}
              </pre>
            </div>
          )}
          <DialogFooter className="shrink-0">
            <Button
              variant="outline"
              onClick={() => {
                if (uiDump) navigator.clipboard.writeText(uiDump.tree).catch(() => {});
                toast.success("已复制 UI 树");
              }}
            >
              <Copy className="h-4 w-4" />
              复制
            </Button>
            <Button onClick={() => setUiDump(null)}>关闭</Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      <AlertDialog
        open={clearLogsOpen}
        onOpenChange={(o) => !o && !clearingLogs && setClearLogsOpen(false)}
      >
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>清空该设备的所有任务日志？</AlertDialogTitle>
            <AlertDialogDescription>
              任务本身保留，只删除步进记录（每个任务下的逐步日志）。此操作不可恢复。
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel disabled={clearingLogs}>取消</AlertDialogCancel>
            <AlertDialogAction
              disabled={clearingLogs}
              onClick={(e) => {
                e.preventDefault();
                confirmClearLogs();
              }}
              className="bg-destructive text-destructive-foreground hover:bg-destructive/90"
            >
              {clearingLogs ? "清空中…" : "确认清空"}
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </div>
  );
}

function InfoRow({ label, value }: { label: string; value: string }) {
  return (
    <div className="grid grid-cols-[76px_1fr] gap-3">
      <div className="text-muted-foreground">{label}</div>
      <div className="min-w-0 break-words font-medium">{value}</div>
    </div>
  );
}

function StatusBadge({ status }: { status: string }) {
  const online = status === "online";
  return (
    <span
      className={`inline-flex h-6 items-center gap-1.5 rounded-md px-2 text-xs font-medium ${
        online
          ? "bg-emerald-50 text-emerald-700 ring-1 ring-emerald-200 dark:bg-emerald-950/30 dark:text-emerald-300 dark:ring-emerald-900"
          : "bg-slate-100 text-slate-600 ring-1 ring-slate-200 dark:bg-slate-900 dark:text-slate-300 dark:ring-slate-800"
      }`}
    >
      <span className={`h-1.5 w-1.5 rounded-full ${online ? "bg-emerald-500" : "bg-slate-400"}`} />
      {status}
    </span>
  );
}

function Metric({ label, value }: { label: string; value: string }) {
  return (
    <div className="rounded-lg border bg-muted/30 px-3 py-2">
      <div className="text-[11px] text-muted-foreground">{label}</div>
      <div className="mt-1 truncate font-mono text-sm font-semibold">{value}</div>
    </div>
  );
}

function StateRow({ label, value, active = false }: { label: string; value: string; active?: boolean }) {
  return (
    <div className="flex items-start justify-between gap-3">
      <span className="shrink-0 text-muted-foreground">{label}</span>
      <span className={`min-w-0 break-words text-right font-medium ${active ? "text-emerald-700 dark:text-emerald-300" : ""}`}>
        {value}
      </span>
    </div>
  );
}

// ---- task log panel -------------------------------------------------------

type ParsedLog =
  | { kind: "react-step";       step: number; total: number; thought?: string; action?: string; args?: string }
  | { kind: "react-result";     step: number; ok: boolean; msg: string; elapsedMs?: number }
  | { kind: "react-final";      ok: boolean; summary: string; steps?: number }
  | { kind: "react-goal";       goal: string }
  | { kind: "plain";            text: string };

const RE_STEP = /^\[react\]\s+step\s+(\d+)\/(\d+)\s+thought=(.+?)(?:\s+action=(\S+))?(?:\s+args=(\{.*\}))?$/;
const RE_RESULT = /^\[react\]\s+step\s+(\d+)\s+→\s+ok=(True|False)\s+msg=(.+?)(?:\s+\((\d+)ms\))?$/;
const RE_FINAL = /^\[react\]\s+result\s+ok=(True|False)\s+steps=(\d+)\s+summary=(.*)$/;
const RE_GOAL = /^\[react\]\s+goal=(.+)$/;

function parseLog(message: string): ParsedLog {
  let m;
  if ((m = message.match(RE_STEP))) {
    return {
      kind: "react-step",
      step: Number(m[1]),
      total: Number(m[2]),
      thought: stripQuotes(m[3]),
      action: m[4],
      args: m[5],
    };
  }
  if ((m = message.match(RE_RESULT))) {
    return {
      kind: "react-result",
      step: Number(m[1]),
      ok: m[2] === "True",
      msg: stripQuotes(m[3]),
      elapsedMs: m[4] ? Number(m[4]) : undefined,
    };
  }
  if ((m = message.match(RE_FINAL))) {
    return {
      kind: "react-final",
      ok: m[1] === "True",
      steps: Number(m[2]),
      summary: m[3],
    };
  }
  if ((m = message.match(RE_GOAL))) {
    return { kind: "react-goal", goal: stripQuotes(m[1]) };
  }
  return { kind: "plain", text: message };
}

function stripQuotes(s: string): string {
  s = s.trim();
  if ((s.startsWith("'") && s.endsWith("'")) || (s.startsWith('"') && s.endsWith('"'))) {
    return s.slice(1, -1);
  }
  return s;
}

function AgentCommandCard({
  value,
  onChange,
  maxSteps,
  onMaxStepsChange,
  onSubmit,
  submitting,
  online,
}: {
  value: string;
  onChange: (v: string) => void;
  maxSteps: number;
  onMaxStepsChange: (n: number) => void;
  onSubmit: () => void;
  submitting: boolean;
  online: boolean;
}) {
  return (
    <Card className="rounded-lg shadow-sm">
      <CardHeader className="flex-row items-center justify-between space-y-0 border-b p-4">
        <CardTitle className="flex items-center gap-2 text-sm">
          <Wand2 className="h-4 w-4 text-muted-foreground" />
          语义化指令
        </CardTitle>
        <span className="text-[10px] text-muted-foreground">ReAct agent</span>
      </CardHeader>
      <CardContent className="space-y-2 p-3">
        <Textarea
          value={value}
          onChange={(e) => onChange(e.target.value)}
          placeholder="用自然语言描述想让设备做什么，例如：&#10;&#10;- 打开「七月」的聊天并发送：你好&#10;- 给所有未读联系人发问候&#10;- 切到工作台 tab"
          rows={4}
          disabled={submitting}
          className="resize-none text-xs"
          onKeyDown={(e) => {
            if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) {
              e.preventDefault();
              onSubmit();
            }
          }}
        />
        <div className="flex items-center justify-between gap-2 text-xs text-muted-foreground">
          <label className="flex items-center gap-1.5">
            <span>最大步数</span>
            <input
              type="number"
              min={1}
              max={20}
              value={maxSteps}
              onChange={(e) => onMaxStepsChange(Math.max(1, Math.min(20, Number(e.target.value) || 8)))}
              className="h-6 w-14 rounded border bg-background px-1.5 font-mono text-xs"
              disabled={submitting}
            />
          </label>
          <Button
            size="sm"
            onClick={onSubmit}
            disabled={submitting || !online || !value.trim()}
            className="h-7"
          >
            {submitting ? (
              <Loader2 className="h-3.5 w-3.5 animate-spin" />
            ) : (
              <Send className="h-3.5 w-3.5" />
            )}
            <span className="ml-1">{online ? "执行" : "设备离线"}</span>
          </Button>
        </div>
      </CardContent>
    </Card>
  );
}

function TaskQueuePanel({
  queue,
  cancellingTaskId,
  onCancel,
}: {
  queue: RobotQueueSnapshot | null;
  cancellingTaskId: number | null;
  onCancel: (taskId: number) => void;
}) {
  const pending = queue?.pending ?? [];
  const empty = !queue?.running && pending.length === 0;
  return (
    <Card className="rounded-lg shadow-sm">
      <CardHeader className="flex-row items-center justify-between space-y-0 border-b p-4">
        <CardTitle className="flex items-center gap-2 text-sm">
          <ShieldAlert className="h-4 w-4 text-muted-foreground" />
          任务队列
        </CardTitle>
        <Badge variant={empty ? "secondary" : "warning"} className="h-6 font-mono">
          {empty ? "idle" : `${(queue?.depth ?? 0) + (queue?.running ? 1 : 0)}`}
        </Badge>
      </CardHeader>
      <CardContent className="space-y-3 p-3">
        {queue?.running ? (
          <QueueRow
            item={queue.running}
            status="running"
            cancelling={cancellingTaskId === queue.running.task_id}
            onCancel={onCancel}
          />
        ) : (
          <div className="rounded-md border border-dashed px-3 py-2 text-xs text-muted-foreground">
            当前没有运行中的任务
          </div>
        )}
        {pending.length > 0 && (
          <div className="space-y-2">
            <div className="text-[10px] font-medium uppercase tracking-wider text-muted-foreground">
              等待中
            </div>
            {pending.map((item) => (
              <QueueRow
                key={item.task_id}
                item={item}
                status="pending"
                cancelling={cancellingTaskId === item.task_id}
                onCancel={onCancel}
              />
            ))}
          </div>
        )}
      </CardContent>
    </Card>
  );
}

function QueueRow({
  item,
  status,
  cancelling,
  onCancel,
}: {
  item: RobotQueueItem;
  status: "running" | "pending";
  cancelling: boolean;
  onCancel: (taskId: number) => void;
}) {
  return (
    <div className="rounded-md border bg-background px-3 py-2">
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0">
          <div className="flex flex-wrap items-center gap-1.5">
            <Badge variant={status === "running" ? "success" : "outline"} className="h-5 px-1.5 text-[10px]">
              {status === "running" ? "运行中" : "等待"}
            </Badge>
            <span className="font-mono text-xs font-medium">#{item.task_id}</span>
            <span className="truncate text-xs text-muted-foreground">{queueKindLabel(item.kind)}</span>
          </div>
          <div className="mt-1 flex flex-wrap gap-2 font-mono text-[10.5px] text-muted-foreground">
            <span>priority={item.priority}</span>
            <span>wait={formatDuration(item.waited_ms)}</span>
          </div>
        </div>
        <Button
          variant="ghost"
          size="icon"
          disabled={cancelling || item.cancellable === false}
          onClick={() => onCancel(item.task_id)}
          className="h-7 w-7 shrink-0 text-muted-foreground hover:text-destructive"
          title="中断任务"
        >
          {cancelling ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Square className="h-3.5 w-3.5" />}
        </Button>
      </div>
    </div>
  );
}

function queueKindLabel(kind: string): string {
  if (kind === "send_text") return "发送消息";
  if (kind === "agent_goal") return "语义指令";
  return kind;
}

function formatDuration(ms: number): string {
  if (ms < 1000) return `${ms}ms`;
  const totalSec = Math.floor(ms / 1000);
  const min = Math.floor(totalSec / 60);
  const sec = totalSec % 60;
  if (min <= 0) return `${sec}s`;
  return `${min}m${sec.toString().padStart(2, "0")}s`;
}

function TaskLogPanel({
  logs,
  onClear,
  clearing,
}: {
  logs: RobotTaskLog[];
  onClear?: () => void;
  clearing?: boolean;
}) {
  const scrollRef = useRef<HTMLDivElement | null>(null);
  useEffect(() => {
    const el = scrollRef.current;
    if (!el) return;
    // Newest is on top. Pin to top only when the user is already near the
    // top — don't yank them back if they're scrolled down reading history.
    if (el.scrollTop < 120) el.scrollTop = 0;
  }, [logs.length]);

  return (
    <Card className="flex min-h-0 flex-col rounded-lg shadow-sm">
      <CardHeader className="flex-row items-center justify-between space-y-0 border-b p-4">
        <div className="flex items-center gap-2">
          <CardTitle className="text-sm">任务日志</CardTitle>
          <Badge variant="secondary" className="h-6 font-mono">{logs.length}</Badge>
        </div>
        {onClear && (
          <Button
            variant="ghost"
            size="sm"
            onClick={onClear}
            disabled={clearing || logs.length === 0}
            className="h-7 px-2 text-muted-foreground hover:text-destructive"
            title="清空该设备的所有日志"
          >
            {clearing ? (
              <Loader2 className="h-3.5 w-3.5 animate-spin" />
            ) : (
              <Trash2 className="h-3.5 w-3.5" />
            )}
            <span className="ml-1 text-xs">清空</span>
          </Button>
        )}
      </CardHeader>
      <CardContent ref={scrollRef} className="min-h-0 flex-1 overflow-auto p-0">
        {logs.length === 0 ? (
          <div className="px-4 py-8 text-center text-sm text-muted-foreground">暂无日志</div>
        ) : (
          <ul className="divide-y">
            {logs.map((log) => (
              <LogRow key={log.id} log={log} />
            ))}
          </ul>
        )}
      </CardContent>
    </Card>
  );
}

function LogRow({ log }: { log: RobotTaskLog }) {
  const parsed = useMemo(() => parseLog(log.message), [log.message]);
  const time = useMemo(
    () => new Date(log.created_at).toLocaleTimeString(undefined, { hour12: false }),
    [log.created_at]
  );
  const taskTag = log.task_id != null ? `#${log.task_id}` : null;
  const isError = log.level === "error";
  const isWarn = log.level === "warn";

  let body: React.ReactNode;
  switch (parsed.kind) {
    case "react-goal":
      body = (
        <div className="rounded-md bg-amber-50 px-2 py-1.5 dark:bg-amber-950/30">
          <div className="text-[10px] font-semibold uppercase tracking-wider text-amber-700 dark:text-amber-400">
            目标
          </div>
          <div className="mt-0.5 text-foreground">{parsed.goal}</div>
        </div>
      );
      break;
    case "react-step":
      body = (
        <div className="space-y-1">
          <div className="flex flex-wrap items-center gap-1.5">
            <Badge variant="outline" className="h-5 px-1.5 font-mono text-[10px]">
              step {parsed.step}/{parsed.total}
            </Badge>
            {parsed.action && (
              <Badge className="h-5 bg-blue-600 px-1.5 font-mono text-[10px] text-white hover:bg-blue-600">
                {parsed.action}
              </Badge>
            )}
          </div>
          {parsed.thought && (
            <div className="line-clamp-3 text-muted-foreground">{parsed.thought}</div>
          )}
          {parsed.args && (
            <pre className="overflow-hidden text-ellipsis rounded bg-muted px-1.5 py-1 font-mono text-[10.5px] leading-tight">
              {parsed.args.length > 200 ? parsed.args.slice(0, 200) + "…" : parsed.args}
            </pre>
          )}
        </div>
      );
      break;
    case "react-result":
      body = (
        <div className="flex items-start gap-1.5">
          {parsed.ok ? (
            <CheckCircle2 className="mt-0.5 h-3.5 w-3.5 shrink-0 text-emerald-600" />
          ) : (
            <XCircle className="mt-0.5 h-3.5 w-3.5 shrink-0 text-destructive" />
          )}
          <div className="min-w-0 flex-1">
            <span className="font-mono text-[10.5px] text-muted-foreground">
              step {parsed.step} →{" "}
            </span>
            <span className={parsed.ok ? "" : "text-destructive"}>{parsed.msg}</span>
            {parsed.elapsedMs != null && (
              <span className="ml-1 text-[10.5px] text-muted-foreground">
                ({parsed.elapsedMs}ms)
              </span>
            )}
          </div>
        </div>
      );
      break;
    case "react-final":
      body = (
        <div
          className={`rounded-md px-2 py-1.5 ${
            parsed.ok
              ? "bg-emerald-50 dark:bg-emerald-950/30"
              : "bg-red-50 dark:bg-red-950/30"
          }`}
        >
          <div className="flex items-center gap-1.5">
            {parsed.ok ? (
              <CheckCircle2 className="h-3.5 w-3.5 text-emerald-600" />
            ) : (
              <XCircle className="h-3.5 w-3.5 text-destructive" />
            )}
            <span
              className={`text-[10px] font-semibold uppercase tracking-wider ${
                parsed.ok
                  ? "text-emerald-700 dark:text-emerald-400"
                  : "text-destructive"
              }`}
            >
              {parsed.ok ? "完成" : "失败"} · {parsed.steps} 步
            </span>
          </div>
          {parsed.summary && (
            <div className="mt-0.5 text-foreground">{parsed.summary}</div>
          )}
        </div>
      );
      break;
    default:
      body = (
        <div
          className={`break-words leading-snug ${
            isError ? "text-destructive" : isWarn ? "text-amber-600" : ""
          }`}
        >
          {parsed.text}
        </div>
      );
  }

  return (
    <li className="px-4 py-3 text-xs transition-colors hover:bg-muted/40">
      <div className="mb-1.5 flex items-center justify-between gap-2 text-[10.5px] font-mono text-muted-foreground">
        <span>{time}</span>
        {taskTag && <span>{taskTag}</span>}
      </div>
      {body}
    </li>
  );
}
