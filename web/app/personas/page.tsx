"use client";
import { useEffect, useState, useCallback } from "react";
import { Plus, Save, Trash2, Sparkles, Lock, Loader2 } from "lucide-react";
import { api } from "@/lib/api";
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
} from "@/components/ui/dialog";
import { cn } from "@/lib/utils";

// One persona summary as the list-API returns.
type PersonaSummary = {
  id: string;
  name: string;
  description: string;
  chars: number;
  protected: boolean;
};

// Full persona content for the editor view. `soul/memory/style` are the
// three editable markdown sections that get concatenated into the
// system prompt at decision time.
type PersonaDetail = {
  id: string;
  name: string;
  description: string;
  soul: string;
  memory: string;
  style: string;
};

// Which section tab is showing in the editor pane.
type SectionTab = "soul" | "memory" | "style";

const SECTION_TABS: { key: SectionTab; label: string; hint: string }[] = [
  { key: "soul", label: "灵魂 (soul)", hint: "我是谁 / 边界 / 被问真假时的处理" },
  { key: "memory", label: "记忆 (memory)", hint: "如何使用客户画像和历史记录,不要复读" },
  { key: "style", label: "风格 (style)", hint: "微信腔调 / 反 AI 痕迹清单" },
];

export default function PersonasPage() {
  const [list, setList] = useState<PersonaSummary[]>([]);
  const [activeId, setActiveId] = useState<string | null>(null);
  const [detail, setDetail] = useState<PersonaDetail | null>(null);
  const [tab, setTab] = useState<SectionTab>("soul");
  const [loading, setLoading] = useState(false);
  const [saving, setSaving] = useState(false);
  // Dirty flag so we don't pester the server with PUTs every keystroke
  // and so the operator can see at a glance whether their tab has unsaved
  // edits.
  const [dirty, setDirty] = useState(false);
  const [createOpen, setCreateOpen] = useState(false);

  const reloadList = useCallback(async () => {
    const rows = await api<PersonaSummary[]>("/personas");
    setList(rows);
    // Auto-pick the first persona on initial load. If the currently
    // selected id was deleted out from under us, switch to whatever the
    // first row is — never leave the editor pointing at a dead id.
    setActiveId((cur) => {
      if (cur && rows.some((r) => r.id === cur)) return cur;
      return rows[0]?.id ?? null;
    });
  }, []);

  useEffect(() => {
    reloadList().catch((e) =>
      toast.error("加载人格列表失败", { description: e?.message ?? String(e) })
    );
  }, [reloadList]);

  // Load detail when the selection changes. We always re-fetch from the
  // server (instead of stashing in memory) so a fresh selection reflects
  // the latest disk state in case multiple operators are editing.
  useEffect(() => {
    if (!activeId) {
      setDetail(null);
      return;
    }
    setLoading(true);
    setDirty(false);
    api<PersonaDetail>(`/personas/${encodeURIComponent(activeId)}`)
      .then((d) => setDetail(d))
      .catch((e) => toast.error("加载失败", { description: e?.message ?? String(e) }))
      .finally(() => setLoading(false));
  }, [activeId]);

  async function save() {
    if (!detail) return;
    setSaving(true);
    try {
      const updated = await api<PersonaDetail>(`/personas/${encodeURIComponent(detail.id)}`, {
        method: "PUT",
        body: JSON.stringify({
          name: detail.name,
          description: detail.description,
          soul: detail.soul,
          memory: detail.memory,
          style: detail.style,
        }),
      });
      setDetail(updated);
      setDirty(false);
      await reloadList();
      toast.success("已保存");
    } catch (e: any) {
      toast.error("保存失败", { description: e?.message ?? String(e) });
    } finally {
      setSaving(false);
    }
  }

  async function deleteCurrent() {
    if (!detail) return;
    if (detail.id === "default") return;
    if (!confirm(`确定删除人格「${detail.name}」(${detail.id})吗?此操作不可恢复。`)) return;
    try {
      await api(`/personas/${encodeURIComponent(detail.id)}`, { method: "DELETE" });
      toast.success("已删除");
      await reloadList();
    } catch (e: any) {
      toast.error("删除失败", { description: e?.message ?? String(e) });
    }
  }

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="flex items-center gap-2 text-2xl font-semibold">
            <Sparkles className="h-5 w-5" /> 人格管理
          </h1>
          <p className="text-sm text-muted-foreground">
            每个人格由 soul / memory / style 三段 markdown 组成,会拼到客服 agent 的 system prompt 最前面。可以为团队/设备配置不同人格。
          </p>
        </div>
        <Button onClick={() => setCreateOpen(true)}>
          <Plus className="h-4 w-4" />
          新建人格
        </Button>
      </div>

      <div className="grid grid-cols-12 gap-4">
        {/* Left rail: persona list */}
        <Card className="col-span-3">
          <CardHeader className="pb-2">
            <CardTitle className="text-sm">人格</CardTitle>
          </CardHeader>
          <CardContent className="space-y-1 p-2">
            {list.length === 0 && (
              <div className="px-2 py-4 text-xs text-muted-foreground">暂无人格</div>
            )}
            {list.map((p) => (
              <button
                key={p.id}
                type="button"
                onClick={() => setActiveId(p.id)}
                className={cn(
                  "flex w-full flex-col items-start gap-0.5 rounded px-3 py-2 text-left text-sm transition-colors",
                  activeId === p.id
                    ? "bg-accent text-accent-foreground"
                    : "hover:bg-accent/50"
                )}
              >
                <div className="flex w-full items-center gap-1.5">
                  <span className="truncate font-medium">{p.name}</span>
                  {p.protected && <Lock className="h-3 w-3 text-muted-foreground" />}
                </div>
                <div className="text-xs text-muted-foreground">{p.id} · {p.chars}字</div>
              </button>
            ))}
          </CardContent>
        </Card>

        {/* Right pane: editor */}
        <Card className="col-span-9">
          {!detail && (
            <CardContent className="flex h-64 items-center justify-center text-sm text-muted-foreground">
              {loading ? (
                <Loader2 className="h-4 w-4 animate-spin" />
              ) : (
                "选择左侧人格开始编辑"
              )}
            </CardContent>
          )}
          {detail && (
            <>
              <CardHeader>
                <div className="flex items-center justify-between gap-3">
                  <div className="grid flex-1 grid-cols-2 gap-3">
                    <div className="space-y-1">
                      <Label className="text-xs">名称</Label>
                      <Input
                        value={detail.name}
                        onChange={(e) => {
                          setDetail({ ...detail, name: e.target.value });
                          setDirty(true);
                        }}
                      />
                    </div>
                    <div className="space-y-1">
                      <Label className="text-xs">ID(只读)</Label>
                      <Input value={detail.id} disabled />
                    </div>
                  </div>
                  <div className="flex gap-2 pt-5">
                    <Button onClick={save} disabled={saving || !dirty}>
                      {saving ? <Loader2 className="h-4 w-4 animate-spin" /> : <Save className="h-4 w-4" />}
                      保存
                    </Button>
                    {detail.id !== "default" && (
                      <Button variant="destructive" onClick={deleteCurrent}>
                        <Trash2 className="h-4 w-4" />
                        删除
                      </Button>
                    )}
                  </div>
                </div>
                <div className="space-y-1">
                  <Label className="text-xs">描述</Label>
                  <Input
                    value={detail.description}
                    onChange={(e) => {
                      setDetail({ ...detail, description: e.target.value });
                      setDirty(true);
                    }}
                    placeholder="一句话描述这个人格的特点"
                  />
                </div>
              </CardHeader>
              <CardContent className="space-y-3">
                {/* Section tabs */}
                <div className="flex gap-1 border-b">
                  {SECTION_TABS.map((t) => (
                    <button
                      key={t.key}
                      type="button"
                      onClick={() => setTab(t.key)}
                      className={cn(
                        "border-b-2 px-3 py-2 text-sm transition-colors",
                        tab === t.key
                          ? "border-primary font-medium"
                          : "border-transparent text-muted-foreground hover:text-foreground"
                      )}
                    >
                      {t.label}
                    </button>
                  ))}
                </div>
                <p className="text-xs text-muted-foreground">
                  {SECTION_TABS.find((t) => t.key === tab)?.hint}
                </p>
                <Textarea
                  // Stretching the textarea so multi-page markdown is
                  // comfortable to edit. Mono font lines up easier with
                  // markdown indentation.
                  className="min-h-[420px] font-mono text-xs"
                  value={detail[tab]}
                  onChange={(e) => {
                    setDetail({ ...detail, [tab]: e.target.value });
                    setDirty(true);
                  }}
                />
                {dirty && (
                  <div className="text-xs text-amber-600">
                    有未保存的修改 — 切换到其它人格或刷新会丢失。
                  </div>
                )}
              </CardContent>
            </>
          )}
        </Card>
      </div>

      <CreateDialog
        open={createOpen}
        onOpenChange={setCreateOpen}
        onCreated={async (id) => {
          await reloadList();
          setActiveId(id);
        }}
      />
    </div>
  );
}

// ---- Create dialog -------------------------------------------------------

function CreateDialog({
  open,
  onOpenChange,
  onCreated,
}: {
  open: boolean;
  onOpenChange: (v: boolean) => void;
  onCreated: (id: string) => void | Promise<void>;
}) {
  const [id, setId] = useState("");
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  // Pre-seed the new persona's sections by copying from the default
  // persona — saves the operator from staring at a blank textarea when
  // most teams only want to lightly tweak the default voice.
  const [copyFromDefault, setCopyFromDefault] = useState(true);
  const [busy, setBusy] = useState(false);

  // Reset every time the dialog opens so old draft state doesn't leak in.
  useEffect(() => {
    if (open) {
      setId("");
      setName("");
      setDescription("");
      setCopyFromDefault(true);
      setBusy(false);
    }
  }, [open]);

  async function submit() {
    if (!id.trim() || !name.trim()) {
      toast.error("ID 和名称都必填");
      return;
    }
    setBusy(true);
    try {
      let body: any = {
        id: id.trim().toLowerCase(),
        name: name.trim(),
        description: description.trim(),
      };
      if (copyFromDefault) {
        // Pull the default's sections and ship them as the new persona's
        // initial content. One round-trip extra, but it keeps the create
        // endpoint dumb and stateless (no "template_from" parameter).
        try {
          const def = await api<PersonaDetail>("/personas/default");
          body = { ...body, soul: def.soul, memory: def.memory, style: def.style };
        } catch {
          /* default missing — create empty */
        }
      }
      const created = await api<PersonaDetail>("/personas", {
        method: "POST",
        body: JSON.stringify(body),
      });
      toast.success("人格已创建");
      onOpenChange(false);
      await onCreated(created.id);
    } catch (e: any) {
      toast.error("创建失败", { description: e?.message ?? String(e) });
    } finally {
      setBusy(false);
    }
  }

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>新建人格</DialogTitle>
        </DialogHeader>
        <div className="space-y-3">
          <div className="space-y-1">
            <Label className="text-xs">ID</Label>
            <Input
              value={id}
              onChange={(e) => setId(e.target.value)}
              placeholder="如 sales_warm(小写字母数字 - _,最多 64 字符)"
            />
          </div>
          <div className="space-y-1">
            <Label className="text-xs">名称</Label>
            <Input
              value={name}
              onChange={(e) => setName(e.target.value)}
              placeholder="如 销售型温暖客服"
            />
          </div>
          <div className="space-y-1">
            <Label className="text-xs">描述(可选)</Label>
            <Input
              value={description}
              onChange={(e) => setDescription(e.target.value)}
              placeholder="一句话简介"
            />
          </div>
          <label className="flex items-center gap-2 text-sm">
            <input
              type="checkbox"
              checked={copyFromDefault}
              onChange={(e) => setCopyFromDefault(e.target.checked)}
            />
            从「默认人格」复制 soul / memory / style 作为起点
          </label>
        </div>
        <DialogFooter>
          <Button variant="ghost" onClick={() => onOpenChange(false)}>
            取消
          </Button>
          <Button onClick={submit} disabled={busy}>
            {busy ? <Loader2 className="h-4 w-4 animate-spin" /> : null}
            创建
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
