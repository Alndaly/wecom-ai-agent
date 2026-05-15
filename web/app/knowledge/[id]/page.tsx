"use client";
import { useCallback, useEffect, useRef, useState } from "react";
import { useParams, useRouter } from "next/navigation";
import Link from "next/link";
import {
  Upload,
  Search,
  FileText,
  AlertCircle,
  RefreshCcw,
  ArrowLeft,
  Trash2,
  Loader2,
} from "lucide-react";
import {
  api,
  apiForm,
  type KnowledgeBase,
  type KnowledgeDoc,
} from "@/lib/api";
import { formatFull } from "@/lib/datetime";
import { toast } from "@/components/ui/sonner";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import { Badge } from "@/components/ui/badge";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
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

type SearchHit = {
  chunk_id: number;
  doc_id: number;
  kb_id: number;
  text: string;
  score: number;
};
type GraphFact = { src: string; rel: string; dst: string };

type PendingDelete =
  | { kind: "kb" }
  | { kind: "doc"; doc: KnowledgeDoc };

export default function KBDetailPage() {
  const params = useParams<{ id: string }>();
  const router = useRouter();
  const kbId = Number(params.id);
  const [kb, setKb] = useState<KnowledgeBase | null>(null);
  const [docs, setDocs] = useState<KnowledgeDoc[]>([]);
  const [refreshing, setRefreshing] = useState(false);

  const [pasteName, setPasteName] = useState("");
  const [pasteContent, setPasteContent] = useState("");
  const [busyPaste, setBusyPaste] = useState(false);
  const [dragOver, setDragOver] = useState(false);
  const [uploading, setUploading] = useState(false);
  const fileRef = useRef<HTMLInputElement | null>(null);

  const [q, setQ] = useState("");
  const [hits, setHits] = useState<SearchHit[]>([]);
  const [facts, setFacts] = useState<GraphFact[]>([]);
  const [searching, setSearching] = useState(false);
  const [hasSearched, setHasSearched] = useState(false);

  const [pendingDelete, setPendingDelete] = useState<PendingDelete | null>(null);
  const [deleting, setDeleting] = useState(false);

  const reload = useCallback(async () => {
    const [kbRes, docsRes] = await Promise.all([
      api<KnowledgeBase>(`/kb/${kbId}`),
      api<KnowledgeDoc[]>(`/kb/${kbId}/docs`),
    ]);
    setKb(kbRes);
    setDocs(docsRes);
  }, [kbId]);

  useEffect(() => {
    reload().catch(console.error);
  }, [reload]);

  // Only poll while there's a doc that's still processing.
  const hasProcessing = docs.some(
    (d) => d.status === "pending" || d.status === "processing"
  );
  useEffect(() => {
    if (!hasProcessing) return;
    const t = setInterval(() => {
      reload().catch(console.error);
    }, 2000);
    return () => clearInterval(t);
  }, [hasProcessing, reload]);

  async function manualReload() {
    setRefreshing(true);
    try {
      await reload();
    } finally {
      setRefreshing(false);
    }
  }

  async function uploadFile(file: File) {
    const fd = new FormData();
    fd.append("file", file);
    setUploading(true);
    try {
      await apiForm(`/kb/${kbId}/docs`, fd);
      toast.success(`已上传 ${file.name}，正在解析…`);
      reload();
    } catch (e: any) {
      toast.error("上传失败", { description: e?.message });
    } finally {
      setUploading(false);
    }
  }

  async function uploadFiles(files: FileList | File[]) {
    const list = Array.from(files);
    for (const f of list) {
      await uploadFile(f);
    }
  }

  async function paste(e: React.FormEvent) {
    e.preventDefault();
    if (!pasteName.trim() || !pasteContent.trim()) return;
    setBusyPaste(true);
    try {
      const fd = new FormData();
      fd.append("name", pasteName);
      fd.append("content", pasteContent);
      await apiForm(`/kb/${kbId}/docs/paste`, fd);
      toast.success("已粘贴，正在解析…");
      setPasteName("");
      setPasteContent("");
      reload();
    } catch (e: any) {
      toast.error("失败", { description: e?.message });
    } finally {
      setBusyPaste(false);
    }
  }

  async function runSearch(e?: React.FormEvent) {
    e?.preventDefault();
    if (!q.trim()) return;
    setSearching(true);
    setHasSearched(true);
    try {
      const data = await api<{ hits: SearchHit[]; graph_facts: GraphFact[] }>(
        "/kb/search",
        { method: "POST", body: JSON.stringify({ query: q, top_k: 5 }) }
      );
      setHits(data.hits);
      setFacts(data.graph_facts);
    } catch (e: any) {
      toast.error("搜索失败", { description: e?.message });
    } finally {
      setSearching(false);
    }
  }

  async function confirmDelete() {
    if (!pendingDelete) return;
    setDeleting(true);
    try {
      if (pendingDelete.kind === "kb") {
        await api(`/kb/${kbId}`, { method: "DELETE" });
        toast.success("已删除知识库");
        router.push("/knowledge");
      } else {
        const doc = pendingDelete.doc;
        await api(`/kb/${kbId}/docs/${doc.id}`, { method: "DELETE" });
        toast.success(`已删除 ${doc.name}`);
        setPendingDelete(null);
        reload();
      }
    } catch (e: any) {
      toast.error("删除失败", { description: e?.message });
    } finally {
      setDeleting(false);
    }
  }

  const readyCount = docs.filter((d) => d.status === "ready").length;
  const failedCount = docs.filter((d) => d.status === "failed").length;

  return (
    <div className="space-y-6">
      {/* Header */}
      <div>
        <Link
          href="/knowledge"
          className="inline-flex items-center gap-1 text-xs text-muted-foreground transition-colors hover:text-foreground"
        >
          <ArrowLeft className="h-3.5 w-3.5" />
          返回知识库列表
        </Link>
        <div className="mt-2 flex items-start justify-between gap-4">
          <div className="min-w-0">
            <h1 className="truncate text-2xl font-semibold tracking-tight">
              {kb?.name || "知识库"}
            </h1>
            {kb && (
              <p className="mt-1 text-xs text-muted-foreground">
                {kb.description || "暂无描述"}
              </p>
            )}
            <div className="mt-2 flex flex-wrap items-center gap-2 text-[11px] text-muted-foreground">
              <Badge variant="secondary">{docs.length} 文档</Badge>
              <Badge variant="secondary">{readyCount} 就绪</Badge>
              {hasProcessing && (
                <Badge variant="secondary" className="gap-1">
                  <Loader2 className="h-3 w-3 animate-spin" />
                  处理中
                </Badge>
              )}
              {failedCount > 0 && (
                <Badge variant="destructive">{failedCount} 失败</Badge>
              )}
              {kb && (
                <span>
                  chunk={kb.chunk_size} · overlap={kb.chunk_overlap} · v{kb.version}
                </span>
              )}
            </div>
          </div>
          <Button
            variant="outline"
            size="sm"
            className="shrink-0 text-destructive hover:bg-destructive/10 hover:text-destructive"
            onClick={() => setPendingDelete({ kind: "kb" })}
          >
            <Trash2 className="h-3.5 w-3.5" />
            删除知识库
          </Button>
        </div>
      </div>

      {/* Upload */}
      <div className="grid gap-4 md:grid-cols-2">
        <Card className="flex flex-col">
          <CardHeader>
            <CardTitle className="text-base">上传文档</CardTitle>
          </CardHeader>
          <CardContent className="flex-1">
            <input
              ref={fileRef}
              type="file"
              accept=".txt,.md,.pdf"
              multiple
              className="hidden"
              onChange={(e) => {
                const files = e.target.files;
                if (files && files.length) uploadFiles(files);
                if (e.target) e.target.value = "";
              }}
            />
            <div
              role="button"
              tabIndex={0}
              onClick={() => fileRef.current?.click()}
              onKeyDown={(e) => {
                if (e.key === "Enter" || e.key === " ") fileRef.current?.click();
              }}
              onDragOver={(e) => {
                e.preventDefault();
                setDragOver(true);
              }}
              onDragLeave={() => setDragOver(false)}
              onDrop={(e) => {
                e.preventDefault();
                setDragOver(false);
                if (e.dataTransfer.files?.length) uploadFiles(e.dataTransfer.files);
              }}
              className={`flex h-full cursor-pointer flex-col items-center justify-center rounded-md border-2 border-dashed py-8 text-center transition-colors ${
                dragOver
                  ? "border-foreground bg-accent/50"
                  : "border-muted-foreground/25 hover:bg-accent/30"
              }`}
            >
              {uploading ? (
                <Loader2 className="mb-2 h-6 w-6 animate-spin text-muted-foreground" />
              ) : (
                <Upload className="mb-2 h-6 w-6 text-muted-foreground" />
              )}
              <p className="text-sm">
                {uploading ? "上传中…" : "点击或拖拽文件到此处"}
              </p>
              <p className="mt-1 text-[11px] text-muted-foreground">
                支持 .txt / .md / .pdf，可多选
              </p>
            </div>
          </CardContent>
        </Card>

        <Card>
          <CardHeader>
            <CardTitle className="text-base">粘贴文本</CardTitle>
          </CardHeader>
          <CardContent>
            <form onSubmit={paste} className="space-y-2">
              <Input
                placeholder="文档名称"
                value={pasteName}
                onChange={(e) => setPasteName(e.target.value)}
              />
              <Textarea
                placeholder="粘贴你的产品说明 / FAQ ..."
                value={pasteContent}
                onChange={(e) => setPasteContent(e.target.value)}
                className="min-h-[120px]"
              />
              <Button
                type="submit"
                disabled={busyPaste || !pasteName.trim() || !pasteContent.trim()}
              >
                {busyPaste ? "提交中…" : "提交"}
              </Button>
            </form>
          </CardContent>
        </Card>
      </div>

      {/* Documents */}
      <Card>
        <CardHeader className="flex flex-row items-center justify-between">
          <CardTitle className="text-base">文档</CardTitle>
          <Button
            variant="ghost"
            size="icon"
            onClick={manualReload}
            disabled={refreshing}
            aria-label="刷新"
          >
            <RefreshCcw className={`h-4 w-4 ${refreshing ? "animate-spin" : ""}`} />
          </Button>
        </CardHeader>
        <CardContent className="p-0">
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>名称</TableHead>
                <TableHead className="w-24">状态</TableHead>
                <TableHead className="w-20">分片</TableHead>
                <TableHead className="w-24">大小</TableHead>
                <TableHead className="w-44">更新时间</TableHead>
                <TableHead className="w-12"></TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {docs.length === 0 && (
                <TableRow>
                  <TableCell
                    colSpan={6}
                    className="py-10 text-center text-sm text-muted-foreground"
                  >
                    还没有文档，从上方上传或粘贴
                  </TableCell>
                </TableRow>
              )}
              {docs.map((d) => (
                <TableRow key={d.id}>
                  <TableCell className="max-w-[280px]">
                    <div className="flex items-center gap-1.5">
                      <FileText className="h-3.5 w-3.5 shrink-0 text-muted-foreground" />
                      <span className="truncate" title={d.name}>{d.name}</span>
                    </div>
                  </TableCell>
                  <TableCell>
                    <StatusBadge status={d.status} error={d.error} />
                  </TableCell>
                  <TableCell>{d.chunk_count}</TableCell>
                  <TableCell className="text-xs text-muted-foreground">
                    {formatBytes(d.bytes)}
                  </TableCell>
                  <TableCell className="text-xs text-muted-foreground">
                    {formatFull(d.updated_at)}
                  </TableCell>
                  <TableCell>
                    <Button
                      variant="ghost"
                      size="icon"
                      className="h-7 w-7 text-muted-foreground hover:text-destructive"
                      onClick={() => setPendingDelete({ kind: "doc", doc: d })}
                      aria-label="删除文档"
                    >
                      <Trash2 className="h-3.5 w-3.5" />
                    </Button>
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </CardContent>
      </Card>

      {/* Search */}
      <Card>
        <CardHeader>
          <CardTitle className="text-base">检索测试</CardTitle>
        </CardHeader>
        <CardContent>
          <form onSubmit={runSearch} className="mb-4 flex gap-2">
            <Input
              value={q}
              onChange={(e) => setQ(e.target.value)}
              placeholder="输入用户可能会问的问题"
              className="max-w-xl"
            />
            <Button type="submit" disabled={searching || !q.trim()}>
              <Search className="h-4 w-4" /> {searching ? "搜索中…" : "搜索"}
            </Button>
          </form>

          {hasSearched && hits.length === 0 && !searching && (
            <p className="text-xs text-muted-foreground">无命中</p>
          )}

          <div className="space-y-3">
            {hits.map((h, i) => (
              <div
                key={h.chunk_id}
                className="rounded-md border bg-card p-3 text-sm"
              >
                <div className="mb-2 flex items-center justify-between">
                  <Badge variant="secondary">
                    #{i + 1} · score {h.score.toFixed(2)}
                  </Badge>
                  <span className="text-[11px] text-muted-foreground">
                    chunk_id={h.chunk_id} · doc_id={h.doc_id}
                  </span>
                </div>
                <p className="whitespace-pre-wrap text-foreground">{h.text}</p>
              </div>
            ))}
          </div>

          {facts.length > 0 && (
            <>
              <h4 className="mt-6 mb-2 text-xs font-medium uppercase text-muted-foreground">
                关联实体（Graph）
              </h4>
              <ul className="space-y-1 text-xs">
                {facts.map((f, i) => (
                  <li key={i} className="font-mono text-muted-foreground">
                    {f.src} ─[{f.rel}]→ {f.dst}
                  </li>
                ))}
              </ul>
            </>
          )}
        </CardContent>
      </Card>

      <AlertDialog
        open={!!pendingDelete}
        onOpenChange={(o) => !o && !deleting && setPendingDelete(null)}
      >
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>
              {pendingDelete?.kind === "kb"
                ? `删除知识库 ${kb?.name}？`
                : `删除文档 ${pendingDelete?.kind === "doc" ? pendingDelete.doc.name : ""}？`}
            </AlertDialogTitle>
            <AlertDialogDescription>
              {pendingDelete?.kind === "kb"
                ? "此操作不可恢复。该知识库下所有文档、分片和向量都会被永久删除。"
                : "此操作不可恢复。该文档对应的分片和向量也会被一并删除。"}
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel disabled={deleting}>取消</AlertDialogCancel>
            <AlertDialogAction
              disabled={deleting}
              onClick={(e) => {
                e.preventDefault();
                confirmDelete();
              }}
              className="bg-destructive text-destructive-foreground hover:bg-destructive/90"
            >
              {deleting ? "删除中…" : "确认删除"}
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </div>
  );
}

function StatusBadge({ status, error }: { status: string; error: string | null }) {
  if (status === "ready") return <Badge variant="success">就绪</Badge>;
  if (status === "failed")
    return (
      <Badge variant="destructive" title={error ?? ""}>
        <AlertCircle className="mr-1 h-3 w-3" />
        失败
      </Badge>
    );
  if (status === "processing")
    return (
      <Badge variant="secondary" className="gap-1">
        <Loader2 className="h-3 w-3 animate-spin" />
        解析中
      </Badge>
    );
  return <Badge variant="secondary">{status}</Badge>;
}

function formatBytes(n: number): string {
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
  return `${(n / 1024 / 1024).toFixed(1)} MB`;
}
