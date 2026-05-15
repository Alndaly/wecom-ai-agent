"use client";
import { useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import { Plus, Trash2, Database, ArrowRight } from "lucide-react";
import { api, type KnowledgeBase, type KnowledgeDoc } from "@/lib/api";
import { formatFull } from "@/lib/datetime";
import { toast } from "@/components/ui/sonner";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Textarea } from "@/components/ui/textarea";
import { Card, CardContent } from "@/components/ui/card";
import {
  Dialog,
  DialogContent,
  DialogFooter,
  DialogHeader,
  DialogTitle,
  DialogTrigger,
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

type KBWithStats = KnowledgeBase & {
  doc_count: number;
  ready_count: number;
};

export default function KnowledgePage() {
  const router = useRouter();
  const [kbs, setKbs] = useState<KBWithStats[]>([]);
  const [loading, setLoading] = useState(true);
  const [open, setOpen] = useState(false);
  const [name, setName] = useState("");
  const [desc, setDesc] = useState("");
  const [creating, setCreating] = useState(false);
  const [toDelete, setToDelete] = useState<KnowledgeBase | null>(null);
  const [deleting, setDeleting] = useState(false);

  async function reload() {
    const bases = await api<KnowledgeBase[]>("/kb");
    const enriched = await Promise.all(
      bases.map(async (kb) => {
        try {
          const docs = await api<KnowledgeDoc[]>(`/kb/${kb.id}/docs`);
          return {
            ...kb,
            doc_count: docs.length,
            ready_count: docs.filter((d) => d.status === "ready").length,
          } as KBWithStats;
        } catch {
          return { ...kb, doc_count: 0, ready_count: 0 } as KBWithStats;
        }
      })
    );
    setKbs(enriched);
  }
  useEffect(() => {
    reload()
      .catch(console.error)
      .finally(() => setLoading(false));
  }, []);

  async function create(e: React.FormEvent) {
    e.preventDefault();
    setCreating(true);
    try {
      await api("/kb", {
        method: "POST",
        body: JSON.stringify({ name, description: desc }),
      });
      setName("");
      setDesc("");
      setOpen(false);
      toast.success("已创建知识库");
      reload();
    } catch (e: any) {
      toast.error("创建失败", { description: e?.message ?? String(e) });
    } finally {
      setCreating(false);
    }
  }

  async function confirmDelete() {
    if (!toDelete) return;
    setDeleting(true);
    try {
      await api(`/kb/${toDelete.id}`, { method: "DELETE" });
      toast.success(`已删除知识库 ${toDelete.name}`);
      setToDelete(null);
      reload();
    } catch (e: any) {
      toast.error("删除失败", { description: e?.message });
    } finally {
      setDeleting(false);
    }
  }

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-semibold tracking-tight">知识库</h1>
          <p className="mt-1 text-xs text-muted-foreground">
            上传文档供 AI 检索回答，支持 .txt / .md / .pdf 与粘贴文本。
          </p>
        </div>
        <Dialog open={open} onOpenChange={setOpen}>
          <DialogTrigger asChild>
            <Button>
              <Plus className="h-4 w-4" />
              新建知识库
            </Button>
          </DialogTrigger>
          <DialogContent>
            <DialogHeader>
              <DialogTitle>新建知识库</DialogTitle>
            </DialogHeader>
            <form onSubmit={create} className="space-y-4">
              <div className="space-y-2">
                <Label htmlFor="kb-name">名称</Label>
                <Input
                  id="kb-name"
                  value={name}
                  onChange={(e) => setName(e.target.value)}
                  placeholder="例如：产品手册"
                  required
                />
              </div>
              <div className="space-y-2">
                <Label htmlFor="kb-desc">描述（也用作实体抽取的种子词，逗号分隔）</Label>
                <Textarea
                  id="kb-desc"
                  value={desc}
                  onChange={(e) => setDesc(e.target.value)}
                  placeholder="ProMax, 售后, 价格"
                />
              </div>
              <DialogFooter>
                <Button type="submit" disabled={creating || !name.trim()}>
                  {creating ? "创建中..." : "创建"}
                </Button>
              </DialogFooter>
            </form>
          </DialogContent>
        </Dialog>
      </div>

      {loading ? (
        <Card>
          <CardContent className="py-12 text-center text-sm text-muted-foreground">
            加载中…
          </CardContent>
        </Card>
      ) : kbs.length === 0 ? (
        <Card>
          <CardContent className="py-16 text-center">
            <Database className="mx-auto mb-3 h-8 w-8 text-muted-foreground" />
            <p className="text-sm text-muted-foreground">
              还没有知识库，点击右上角创建第一个。
            </p>
          </CardContent>
        </Card>
      ) : (
        <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
          {kbs.map((kb) => (
            <Card
              key={kb.id}
              className="group cursor-pointer transition-all hover:border-foreground/30 hover:shadow-sm"
              onClick={() => router.push(`/knowledge/${kb.id}`)}
            >
              <CardContent className="p-5">
                <div className="flex items-start justify-between gap-2">
                  <div className="min-w-0 flex-1">
                    <h3 className="truncate text-base font-medium">{kb.name}</h3>
                    <p className="mt-1 line-clamp-2 min-h-[2rem] text-xs text-muted-foreground">
                      {kb.description || "暂无描述"}
                    </p>
                  </div>
                  <Button
                    variant="ghost"
                    size="icon"
                    className="h-7 w-7 shrink-0 text-muted-foreground opacity-0 transition-opacity hover:text-destructive group-hover:opacity-100"
                    onClick={(e) => {
                      e.stopPropagation();
                      setToDelete(kb);
                    }}
                    aria-label="删除"
                  >
                    <Trash2 className="h-3.5 w-3.5" />
                  </Button>
                </div>

                <div className="mt-4 flex items-center justify-between border-t pt-3 text-[11px] text-muted-foreground">
                  <span>
                    {kb.ready_count}/{kb.doc_count} 文档就绪
                  </span>
                  <span>{formatFull(kb.created_at)}</span>
                </div>
                <div className="mt-2 flex items-center justify-between text-[11px] text-muted-foreground">
                  <span>
                    chunk={kb.chunk_size} · overlap={kb.chunk_overlap} · v{kb.version}
                  </span>
                  <ArrowRight className="h-3.5 w-3.5 opacity-0 transition-opacity group-hover:opacity-100" />
                </div>
              </CardContent>
            </Card>
          ))}
        </div>
      )}

      <AlertDialog
        open={!!toDelete}
        onOpenChange={(o) => !o && !deleting && setToDelete(null)}
      >
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>删除知识库 {toDelete?.name}？</AlertDialogTitle>
            <AlertDialogDescription>
              此操作不可恢复。该知识库下所有文档、分片和向量都会被永久删除。
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
