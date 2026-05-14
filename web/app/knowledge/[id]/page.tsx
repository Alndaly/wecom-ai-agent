"use client";
import { useCallback, useEffect, useRef, useState } from "react";
import { useParams } from "next/navigation";
import { Upload, Search, FileText, AlertCircle, RefreshCcw } from "lucide-react";
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

type SearchHit = {
  chunk_id: number;
  doc_id: number;
  kb_id: number;
  text: string;
  score: number;
};
type GraphFact = { src: string; rel: string; dst: string };

export default function KBDetailPage() {
  const params = useParams<{ id: string }>();
  const kbId = Number(params.id);
  const [kb, setKb] = useState<KnowledgeBase | null>(null);
  const [docs, setDocs] = useState<KnowledgeDoc[]>([]);

  const [pasteName, setPasteName] = useState("");
  const [pasteContent, setPasteContent] = useState("");
  const [busyPaste, setBusyPaste] = useState(false);
  const fileRef = useRef<HTMLInputElement | null>(null);

  const [q, setQ] = useState("");
  const [hits, setHits] = useState<SearchHit[]>([]);
  const [facts, setFacts] = useState<GraphFact[]>([]);
  const [searching, setSearching] = useState(false);

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
    const t = setInterval(reload, 2000); // simple poll for processing → ready
    return () => clearInterval(t);
  }, [reload]);

  async function uploadFile(file: File) {
    const fd = new FormData();
    fd.append("file", file);
    try {
      await apiForm(`/kb/${kbId}/docs`, fd);
      toast.success("已上传,正在解析…");
      reload();
    } catch (e: any) {
      toast.error("上传失败", { description: e?.message });
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
      toast.success("已粘贴,正在解析…");
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

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl font-semibold tracking-tight">
          {kb?.name || "知识库"}
        </h1>
        {kb && (
          <p className="mt-1 text-xs text-muted-foreground">
            {kb.description || "—"} · chunk_size={kb.chunk_size} · overlap={kb.chunk_overlap}
          </p>
        )}
      </div>

      <div className="grid gap-4 md:grid-cols-2">
        <Card>
          <CardHeader>
            <CardTitle className="text-base">上传文档</CardTitle>
          </CardHeader>
          <CardContent className="space-y-3">
            <input
              ref={fileRef}
              type="file"
              accept=".txt,.md,.pdf"
              className="hidden"
              onChange={(e) => {
                const f = e.target.files?.[0];
                if (f) uploadFile(f);
                if (e.target) e.target.value = "";
              }}
            />
            <Button variant="outline" onClick={() => fileRef.current?.click()}>
              <Upload className="h-4 w-4" /> 选择文件 (.txt / .md / .pdf)
            </Button>
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
                className="min-h-[100px]"
              />
              <Button type="submit" disabled={busyPaste || !pasteName.trim() || !pasteContent.trim()}>
                提交
              </Button>
            </form>
          </CardContent>
        </Card>
      </div>

      <Card>
        <CardHeader className="flex flex-row items-center justify-between">
          <CardTitle className="text-base">文档</CardTitle>
          <Button variant="ghost" size="icon" onClick={reload}>
            <RefreshCcw className="h-4 w-4" />
          </Button>
        </CardHeader>
        <CardContent className="p-0">
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead className="w-10">ID</TableHead>
                <TableHead>名称</TableHead>
                <TableHead>状态</TableHead>
                <TableHead>chunks</TableHead>
                <TableHead>bytes</TableHead>
                <TableHead>更新</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {docs.length === 0 && (
                <TableRow>
                  <TableCell colSpan={6} className="py-8 text-center text-sm text-muted-foreground">
                    暂无文档
                  </TableCell>
                </TableRow>
              )}
              {docs.map((d) => (
                <TableRow key={d.id}>
                  <TableCell>{d.id}</TableCell>
                  <TableCell className="flex items-center gap-1.5">
                    <FileText className="h-3.5 w-3.5 text-muted-foreground" />
                    {d.name}
                  </TableCell>
                  <TableCell>
                    <StatusBadge status={d.status} error={d.error} />
                  </TableCell>
                  <TableCell>{d.chunk_count}</TableCell>
                  <TableCell className="text-xs text-muted-foreground">{d.bytes}</TableCell>
                  <TableCell className="text-xs text-muted-foreground">
                    {formatFull(d.updated_at)}
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </CardContent>
      </Card>

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
              <Search className="h-4 w-4" /> 搜索
            </Button>
          </form>

          {hits.length === 0 && q && !searching && (
            <p className="text-xs text-muted-foreground">无命中</p>
          )}

          <div className="space-y-3">
            {hits.map((h, i) => (
              <div key={h.chunk_id} className="rounded-md border bg-card p-3 text-sm">
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
    </div>
  );
}

function StatusBadge({ status, error }: { status: string; error: string | null }) {
  if (status === "ready") return <Badge variant="success">ready</Badge>;
  if (status === "failed")
    return (
      <Badge variant="destructive" title={error ?? ""}>
        <AlertCircle className="mr-1 h-3 w-3" />
        failed
      </Badge>
    );
  return <Badge variant="secondary">{status}</Badge>;
}
