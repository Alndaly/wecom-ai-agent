"use client";
import { useEffect, useMemo, useRef, useState } from "react";
import Link from "next/link";
import { useParams } from "next/navigation";
import {
  ArrowLeft,
  CheckCircle2,
  Copy,
  FileSearch,
  Loader2,
  MonitorUp,
  Square,
  XCircle,
} from "lucide-react";
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
      api<RobotTaskLog[]>(`/robots/${id}/logs?limit=50`)
        .then(setLogs)
        .catch(() => {});
    };
    loadLogs();
    const timer = setInterval(loadLogs, 5000);
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
      if (pendingDumpRequest && payload.request_id && payload.request_id !== pendingDumpRequest) return;
      setUiDump(payload);
      setDumping(false);
      setPendingDumpRequest(null);
      toast.success("UI 树已回传");
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

  if (loading) {
    return <div className="text-sm text-muted-foreground">加载中...</div>;
  }

  if (!robot) {
    return <div className="text-sm text-muted-foreground">设备不存在</div>;
  }

  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div className="space-y-1">
          <Button asChild variant="ghost" size="sm" className="-ml-2">
            <Link href="/devices">
              <ArrowLeft className="h-4 w-4" />
              返回设备
            </Link>
          </Button>
          <div className="flex items-center gap-3">
            <h1 className="text-2xl font-semibold tracking-tight">{robot.name}</h1>
            <Badge variant={robot.status === "online" ? "success" : "secondary"}>{robot.status}</Badge>
          </div>
          <p className="font-mono text-xs text-muted-foreground">{robot.robot_id}</p>
        </div>
        <div className="flex gap-2">
          <Button
            variant="outline"
            disabled={dumping || robot.status !== "online"}
            onClick={requestUiDump}
          >
            {dumping ? <Loader2 className="h-4 w-4 animate-spin" /> : <FileSearch className="h-4 w-4" />}
            获取 UI 树
          </Button>
          {streaming ? (
            <Button variant="outline" disabled={streamBusy} onClick={stopStream}>
              {streamBusy ? <Loader2 className="h-4 w-4 animate-spin" /> : <Square className="h-4 w-4" />}
              关闭实时屏幕
            </Button>
          ) : (
            <Button disabled={streamBusy || robot.status !== "online"} onClick={startStream}>
              {streamBusy ? <Loader2 className="h-4 w-4 animate-spin" /> : <MonitorUp className="h-4 w-4" />}
              开启实时屏幕
            </Button>
          )}
        </div>
      </div>

      <div className="grid gap-4 lg:grid-cols-[280px_minmax(0,1fr)_360px]">
        <Card className="self-start">
          <CardHeader>
            <CardTitle className="text-base">设备信息</CardTitle>
          </CardHeader>
          <CardContent className="space-y-3 text-sm">
            <InfoRow label="设备类型" value={robot.device_type ?? "未知"} />
            <InfoRow label="设备名称" value={robot.device_name ?? "未知"} />
            <InfoRow label="厂商 / 型号" value={[robot.manufacturer, robot.model].filter(Boolean).join(" ") || "未知"} />
            <InfoRow label="Android" value={robot.android_version ? `${robot.android_version} / API ${robot.sdk_int ?? "-"}` : "未知"} />
            <InfoRow label="Agent 版本" value={robot.app_version ?? "未知"} />
            <InfoRow label="分辨率" value={screenLabel} />
            <InfoRow label="当前页面" value={robot.current_page ?? "UNKNOWN"} />
            <InfoRow label="最近上线" value={robot.last_seen_at ? formatFull(robot.last_seen_at) : "-"} />
          </CardContent>
        </Card>

        <Card className="min-w-0">
          <CardHeader>
            <CardTitle className="flex items-center justify-between gap-3 text-base">
              <span>实时屏幕</span>
              {streaming && <Badge variant="secondary">接收中</Badge>}
            </CardTitle>
          </CardHeader>
          <CardContent>
            <div className="flex min-h-[520px] items-center justify-center overflow-hidden rounded-md border bg-muted">
              {frame?.image ? (
                <img
                  src={`data:${frame.mime};base64,${frame.image}`}
                  alt="设备实时屏幕"
                  className="max-h-[72vh] max-w-full object-contain"
                />
              ) : (
                <div className="max-w-sm text-center text-sm text-muted-foreground">
                  {frameError ?? (streaming ? "等待第一帧..." : "开启实时屏幕后会显示设备当前屏幕")}
                </div>
              )}
            </div>
            {frame && (
              <div className="mt-2 text-xs text-muted-foreground">
                {frame.width} x {frame.height} · {formatFull(frame.created_at)}
              </div>
            )}
            {lastCommandMessage && (
              <div className="mt-2 text-xs text-muted-foreground">{lastCommandMessage}</div>
            )}
          </CardContent>
        </Card>

        <TaskLogPanel logs={logs} />
      </div>

      <Dialog open={!!uiDump} onOpenChange={(o) => !o && setUiDump(null)}>
        <DialogContent className="max-w-5xl">
          <DialogHeader>
            <DialogTitle>当前 UI 树</DialogTitle>
            <DialogDescription>
              {uiDump ? `${uiDump.current_page} · ${formatFull(uiDump.created_at)}` : ""}
            </DialogDescription>
          </DialogHeader>
          {uiDump && (
            <div className="space-y-3">
              <div className="flex flex-wrap gap-x-4 gap-y-1 text-xs text-muted-foreground">
                <span>request_id: {uiDump.request_id ?? "manual"}</span>
                <span>saved: {uiDump.path}</span>
              </div>
              <pre className="max-h-[60vh] overflow-auto rounded-md border bg-muted p-3 font-mono text-xs leading-relaxed">
                {uiDump.tree}
              </pre>
            </div>
          )}
          <DialogFooter>
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
    </div>
  );
}

function InfoRow({ label, value }: { label: string; value: string }) {
  return (
    <div className="grid grid-cols-[88px_1fr] gap-3">
      <div className="text-muted-foreground">{label}</div>
      <div className="min-w-0 break-words">{value}</div>
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

function TaskLogPanel({ logs }: { logs: RobotTaskLog[] }) {
  const scrollRef = useRef<HTMLDivElement | null>(null);
  // Auto-scroll to bottom when new entries arrive — only if user is already near bottom.
  useEffect(() => {
    const el = scrollRef.current;
    if (!el) return;
    const nearBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 80;
    if (nearBottom) el.scrollTop = el.scrollHeight;
  }, [logs.length]);

  return (
    <Card className="flex max-h-[78vh] min-h-[520px] flex-col self-start">
      <CardHeader className="flex flex-row items-center justify-between space-y-0 pb-3">
        <CardTitle className="text-base">任务日志</CardTitle>
        <Badge variant="secondary" className="font-mono">{logs.length}</Badge>
      </CardHeader>
      <CardContent ref={scrollRef} className="min-h-0 flex-1 overflow-auto px-0 pt-0">
        {logs.length === 0 ? (
          <div className="px-4 py-8 text-center text-sm text-muted-foreground">暂无日志</div>
        ) : (
          <ul className="space-y-px">
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
          <div className="mt-0.5 line-clamp-3 text-foreground">{parsed.goal}</div>
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
            <div className="mt-0.5 line-clamp-3 text-foreground">{parsed.summary}</div>
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
    <li className="border-b px-3 py-2 text-xs last:border-b-0">
      <div className="mb-1 flex items-center justify-between gap-2 text-[10.5px] font-mono text-muted-foreground">
        <span>{time}</span>
        {taskTag && <span>task {taskTag}</span>}
      </div>
      {body}
    </li>
  );
}
