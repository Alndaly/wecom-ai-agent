"use client";
import { useEffect, useMemo, useState } from "react";
import Link from "next/link";
import { useParams } from "next/navigation";
import { ArrowLeft, Copy, FileSearch, Loader2, MonitorUp, Square } from "lucide-react";
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

      <div className="grid gap-4 lg:grid-cols-[320px_1fr]">
        <Card>
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

        <Card>
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
      </div>

      <div className="rounded-lg border bg-background">
        <div className="border-b px-4 py-3">
          <h2 className="text-sm font-semibold">任务日志</h2>
        </div>
        <div className="max-h-80 overflow-auto">
          {logs.length === 0 ? (
            <div className="px-4 py-6 text-sm text-muted-foreground">暂无日志</div>
          ) : (
            <div className="divide-y">
              {logs.map((log) => (
                <div key={log.id} className="grid grid-cols-[88px_1fr_auto] gap-3 px-4 py-2 text-sm">
                  <div className="text-muted-foreground">{new Date(log.created_at).toLocaleTimeString()}</div>
                  <div className={log.level === "error" ? "text-destructive" : ""}>{log.message}</div>
                  <div className="text-xs text-muted-foreground">task #{log.task_id ?? "-"}</div>
                </div>
              ))}
            </div>
          )}
        </div>
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
