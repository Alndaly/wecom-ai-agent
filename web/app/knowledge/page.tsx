"use client";
import { useEffect, useState } from "react";
import Link from "next/link";
import { Plus } from "lucide-react";
import { api, type KnowledgeBase } from "@/lib/api";
import { toast } from "@/components/ui/sonner";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Textarea } from "@/components/ui/textarea";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import {
  Dialog,
  DialogContent,
  DialogFooter,
  DialogHeader,
  DialogTitle,
  DialogTrigger,
} from "@/components/ui/dialog";

export default function KnowledgePage() {
  const [kbs, setKbs] = useState<KnowledgeBase[]>([]);
  const [open, setOpen] = useState(false);
  const [name, setName] = useState("");
  const [desc, setDesc] = useState("");
  const [creating, setCreating] = useState(false);

  async function reload() {
    setKbs(await api<KnowledgeBase[]>("/kb"));
  }
  useEffect(() => {
    reload().catch(console.error);
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
      reload();
    } catch (e: any) {
      toast.error("创建失败", { description: e?.message ?? String(e) });
    } finally {
      setCreating(false);
    }
  }

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <h1 className="text-2xl font-semibold tracking-tight">知识库</h1>
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
                  placeholder="例如:产品手册"
                  required
                />
              </div>
              <div className="space-y-2">
                <Label htmlFor="kb-desc">描述（也用作实体抽取的种子词,逗号分隔）</Label>
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

      {kbs.length === 0 ? (
        <Card>
          <CardContent className="py-12 text-center text-sm text-muted-foreground">
            暂无知识库,点击右上角创建
          </CardContent>
        </Card>
      ) : (
        <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
          {kbs.map((kb) => (
            <Link key={kb.id} href={`/knowledge/${kb.id}`}>
              <Card className="cursor-pointer transition-colors hover:bg-accent/50">
                <CardHeader>
                  <CardTitle className="text-base">{kb.name}</CardTitle>
                </CardHeader>
                <CardContent>
                  <p className="line-clamp-2 text-xs text-muted-foreground">
                    {kb.description || "—"}
                  </p>
                  <p className="mt-2 text-[11px] text-muted-foreground">
                    chunk_size={kb.chunk_size} · overlap={kb.chunk_overlap} · v{kb.version}
                  </p>
                </CardContent>
              </Card>
            </Link>
          ))}
        </div>
      )}
    </div>
  );
}
