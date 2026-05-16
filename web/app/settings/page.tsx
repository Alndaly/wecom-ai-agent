"use client";
import { useEffect, useMemo, useState } from "react";
import {
  AlertCircle,
  Bot,
  CheckCircle2,
  Database,
  FileText,
  Plus,
  Save,
  Server,
  SlidersHorizontal,
  Trash2,
  Wand2,
} from "lucide-react";
import { api } from "@/lib/api";
import { toast } from "@/components/ui/sonner";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Textarea } from "@/components/ui/textarea";
import { Badge } from "@/components/ui/badge";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import {
  Dialog,
  DialogContent,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { cn } from "@/lib/utils";

type LlmCfg = {
  provider: "mock" | "openai";
  model: string;
  api_key: string;
  base_url: string;
  temperature: number;
  supports_vision?: boolean;
  profiles?: ModelProfile[];
  active_profile?: string;
  fallback_profile?: string;
  fallback_enabled?: boolean;
};
type EmbedCfg = {
  provider: "mock" | "openai";
  model: string;
  api_key: string;
  base_url: string;
  dim: number;
  profiles?: ModelProfile[];
  active_profile?: string;
};
type ModelProfile = {
  id: string;
  name: string;
  provider: "mock" | "openai";
  model: string;
  api_key: string;
  base_url: string;
  temperature?: number;
  supports_vision?: boolean;
  dim?: number;
};
type RetrievalCfg = { top_k: number; min_score: number };
type AIBehaviorCfg = {
  confidence_threshold: number;
  context_window: number;
  default_prompt: string;
  max_tokens: number;
  agent_mode: boolean;
  agent_max_steps: number;
  react_force_llm: boolean;
};
type ParserCfg = {
  backend: "builtin" | "mineru_local" | "mineru_api";
  api_base: string;
  api_key: string;
  model_version: "vlm" | "pipeline";
  local_cmd: string;
  local_extra_args: string;
};
type InfraCfg = {
  vector_store: string;
  graph_store: string;
  milvus_uri: string;
  milvus_collection: string;
  neo4j_uri: string;
};
type ProbeResult = { ok: boolean; detail: string; latency_ms?: number; model?: string; dim?: number };
type SettingsSection = "models" | "parser" | "retrieval" | "behavior" | "infra";

const LLM_PRESETS: { label: string; provider: "openai" | "mock"; model: string; base_url: string }[] = [
  { label: "OpenAI · gpt-4o-mini", provider: "openai", model: "gpt-4o-mini", base_url: "https://api.openai.com/v1" },
  { label: "OpenAI · gpt-4o", provider: "openai", model: "gpt-4o", base_url: "https://api.openai.com/v1" },
  { label: "DeepSeek · deepseek-chat", provider: "openai", model: "deepseek-chat", base_url: "https://api.deepseek.com/v1" },
  { label: "通义 · qwen-plus", provider: "openai", model: "qwen-plus", base_url: "https://dashscope.aliyuncs.com/compatible-mode/v1" },
  { label: "智谱 · glm-4", provider: "openai", model: "glm-4", base_url: "https://open.bigmodel.cn/api/paas/v4" },
  { label: "Ollama 本地", provider: "openai", model: "llama3.1", base_url: "http://localhost:11434/v1" },
  { label: "内置 mock", provider: "mock", model: "mock", base_url: "" },
];

const EMBED_PRESETS: { label: string; provider: "openai" | "mock"; model: string; base_url: string; dim: number }[] = [
  { label: "OpenAI · text-embedding-3-small", provider: "openai", model: "text-embedding-3-small", base_url: "https://api.openai.com/v1", dim: 1536 },
  { label: "OpenAI · text-embedding-3-large", provider: "openai", model: "text-embedding-3-large", base_url: "https://api.openai.com/v1", dim: 3072 },
  { label: "DeepSeek (复用 OpenAI 兼容端)", provider: "openai", model: "text-embedding-3-small", base_url: "https://api.deepseek.com/v1", dim: 1536 },
  { label: "通义 · text-embedding-v3", provider: "openai", model: "text-embedding-v3", base_url: "https://dashscope.aliyuncs.com/compatible-mode/v1", dim: 1024 },
  { label: "Ollama · nomic-embed-text", provider: "openai", model: "nomic-embed-text", base_url: "http://localhost:11434/v1", dim: 768 },
  { label: "内置 mock (char-bigram)", provider: "mock", model: "mock", base_url: "", dim: 256 },
];

export default function SettingsPage() {
  const [llm, setLlm] = useState<LlmCfg | null>(null);
  const [embed, setEmbed] = useState<EmbedCfg | null>(null);
  const [retrieval, setRetrieval] = useState<RetrievalCfg | null>(null);
  const [ai, setAI] = useState<AIBehaviorCfg | null>(null);
  const [parser, setParser] = useState<ParserCfg | null>(null);
  const [infra, setInfra] = useState<InfraCfg | null>(null);
  const [initialLoading, setInitialLoading] = useState(true);
  const [section, setSection] = useState<SettingsSection>("models");
  const [modelPane, setModelPane] = useState<"llm" | "embedding">("llm");

  // Two distinct flows:
  //   - first mount → show a loading state until we know the values
  //   - after a save → silently refresh state; don't unmount the form
  //                    (otherwise input focus jumps, cards remount, and the
  //                    user perceives it as a full page refresh)
  async function reload(): Promise<void> {
    const data = await api<any>("/settings");
    setLlm(data.llm);
    setEmbed(data.embedding);
    setRetrieval(data.retrieval);
    setAI(data.ai);
    setParser(data.parser);
    setInfra(data.infra);
  }
  useEffect(() => {
    reload()
      .catch(console.error)
      .finally(() => setInitialLoading(false));
  }, []);

  if (initialLoading || !llm || !embed || !retrieval || !ai || !parser || !infra)
    return <p className="text-sm text-muted-foreground">加载中…</p>;

  const activeLlm = (llm.profiles || []).find((p) => p.id === llm.active_profile);
  const fallbackLlm = (llm.profiles || []).find((p) => p.id === llm.fallback_profile);
  const activeEmbed = (embed.profiles || []).find((p) => p.id === embed.active_profile);
  const nav: { id: SettingsSection; label: string; icon: any; detail: string }[] = [
    { id: "models", label: "模型", icon: Bot, detail: activeLlm?.name || llm.model },
    { id: "parser", label: "文档解析", icon: FileText, detail: parser.backend },
    { id: "retrieval", label: "检索", icon: Database, detail: `top ${retrieval.top_k}` },
    { id: "behavior", label: "AI 行为", icon: SlidersHorizontal, detail: `${ai.confidence_threshold}` },
    { id: "infra", label: "基础设施", icon: Server, detail: infra.vector_store },
  ];

  return (
    <div className="flex h-[calc(100vh-3rem)] min-h-[760px] flex-col gap-4 overflow-hidden">
      <div className="flex shrink-0 items-start justify-between gap-4 border-b pb-4">
        <div className="min-w-0">
          <h1 className="text-2xl font-semibold tracking-tight">系统设置</h1>
          <p className="mt-1 max-w-3xl text-sm text-muted-foreground">
            管理模型、入库解析、检索策略和自动回复行为。切换左侧模块后，只编辑当前模块。
          </p>
        </div>
        <div className="hidden grid-cols-3 gap-2 xl:grid">
          <SummaryChip label="主模型" value={activeLlm?.name || llm.model} />
          <SummaryChip label="兜底" value={llm.fallback_enabled && fallbackLlm ? fallbackLlm.name : "未启用"} muted={!llm.fallback_enabled} />
          <SummaryChip label="向量" value={activeEmbed?.name || embed.model} />
        </div>
      </div>

      <div className="grid min-h-0 flex-1 gap-4 lg:grid-cols-[260px_minmax(0,1fr)]">
        <aside className="min-h-0 overflow-auto rounded-lg border bg-background p-2 shadow-sm">
          {nav.map((item) => {
            const Icon = item.icon;
            const active = section === item.id;
            return (
              <button
                key={item.id}
                type="button"
                onClick={() => setSection(item.id)}
                className={cn(
                  "flex w-full items-center gap-3 rounded-md px-3 py-3 text-left transition-colors",
                  active ? "bg-slate-950 text-white dark:bg-slate-100 dark:text-slate-950" : "hover:bg-muted",
                )}
              >
                <Icon className="h-4 w-4 shrink-0" />
                <span className="min-w-0 flex-1">
                  <span className="block text-sm font-medium">{item.label}</span>
                  <span className={cn("mt-0.5 block truncate text-xs", active ? "text-white/70 dark:text-slate-950/65" : "text-muted-foreground")}>
                    {item.detail}
                  </span>
                </span>
              </button>
            );
          })}
        </aside>

        <div className="min-h-0 min-w-0 overflow-auto pr-1">
          {section === "models" && (
            <ModelHub
              pane={modelPane}
              onPaneChange={setModelPane}
              llm={llm}
              embed={embed}
              onSaved={reload}
            />
          )}
          {section === "parser" && <ParserCard value={parser} onSaved={reload} />}
          {section === "retrieval" && <RetrievalCard value={retrieval} onSaved={reload} />}
          {section === "behavior" && <AIBehaviorCard value={ai} onSaved={reload} />}
          {section === "infra" && <InfraCard value={infra} />}
        </div>
      </div>
    </div>
  );
}

function SummaryChip({ label, value, muted }: { label: string; value: string; muted?: boolean }) {
  return (
    <div
      className={cn(
        "min-w-[150px] rounded-lg border px-3 py-2 shadow-sm",
        muted ? "bg-muted/30 text-muted-foreground" : "bg-background",
      )}
    >
      <p className="text-[11px] font-medium text-muted-foreground">{label}</p>
      <p className="mt-0.5 max-w-[180px] truncate text-sm font-semibold">{value}</p>
    </div>
  );
}

function ModelHub({
  pane,
  onPaneChange,
  llm,
  embed,
  onSaved,
}: {
  pane: "llm" | "embedding";
  onPaneChange: (pane: "llm" | "embedding") => void;
  llm: LlmCfg;
  embed: EmbedCfg;
  onSaved: () => void;
}) {
  const activeLlm = (llm.profiles || []).find((p) => p.id === llm.active_profile);
  const fallbackLlm = (llm.profiles || []).find((p) => p.id === llm.fallback_profile);
  const activeEmbed = (embed.profiles || []).find((p) => p.id === embed.active_profile);

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center justify-between gap-3 rounded-lg border bg-background p-3 shadow-sm">
        <div className="flex rounded-md bg-muted p-1">
          <button
            type="button"
            onClick={() => onPaneChange("llm")}
            className={cn(
              "flex items-center gap-2 rounded px-3 py-2 text-sm font-medium transition-colors",
              pane === "llm" ? "bg-background shadow-sm" : "text-muted-foreground hover:text-foreground",
            )}
          >
            <Bot className="h-4 w-4" />
            LLM
          </button>
          <button
            type="button"
            onClick={() => onPaneChange("embedding")}
            className={cn(
              "flex items-center gap-2 rounded px-3 py-2 text-sm font-medium transition-colors",
              pane === "embedding" ? "bg-background shadow-sm" : "text-muted-foreground hover:text-foreground",
            )}
          >
            <Database className="h-4 w-4" />
            Embedding
          </button>
        </div>
        <div className="grid w-full gap-2 text-xs text-muted-foreground sm:w-auto sm:grid-cols-3">
          <StatusPill label="主模型" value={activeLlm?.name || llm.model} />
          <StatusPill label="二层兜底" value={llm.fallback_enabled && fallbackLlm ? fallbackLlm.name : "未启用"} muted={!llm.fallback_enabled} />
          <StatusPill label="向量模型" value={activeEmbed?.name || embed.model} />
        </div>
      </div>
      {pane === "llm" ? <LLMCard value={llm} onSaved={onSaved} /> : <EmbeddingCard value={embed} onSaved={onSaved} />}
    </div>
  );
}

function StatusPill({ label, value, muted }: { label: string; value: string; muted?: boolean }) {
  return (
    <div className={cn("rounded-md border px-3 py-2", muted ? "bg-muted/30" : "bg-background")}>
      <span className="block text-[11px]">{label}</span>
      <span className="block max-w-[160px] truncate font-medium text-foreground">{value}</span>
    </div>
  );
}

function LLMCard({ value, onSaved }: { value: LlmCfg; onSaved: () => void }) {
  // Never carry the masked "********" into form state — start api_key blank
  // and let the backend keep its saved value when we send empty.
  const [v, setV] = useState<LlmCfg>(() => normaliseLlm(value));
  const [busy, setBusy] = useState(false);
  const [probing, setProbing] = useState(false);
  const [probe, setProbe] = useState<ProbeResult | null>(null);
  const [addOpen, setAddOpen] = useState(false);
  const [editingId, setEditingId] = useState(value.active_profile || value.profiles?.[0]?.id || "default");

  async function save() {
    setBusy(true);
    try {
      await api("/settings/llm", { method: "PUT", body: JSON.stringify(v) });
      toast.success("LLM 配置已保存");
      setV((cur) => ({
        ...cur,
        api_key: "",
        profiles: (cur.profiles || []).map((p) => ({ ...p, api_key: "" })),
      }));
      onSaved();
    } catch (e: any) {
      toast.error("保存失败", { description: e?.message });
    } finally {
      setBusy(false);
    }
  }

  async function test() {
    setProbing(true);
    setProbe(null);
    try {
      const r = await api<ProbeResult>("/settings/test/llm", {
        method: "POST",
        body: JSON.stringify(v),
      });
      setProbe(r);
    } catch (e: any) {
      setProbe({ ok: false, detail: e?.message ?? String(e) });
    } finally {
      setProbing(false);
    }
  }

  function applyPreset(label: string) {
    const p = LLM_PRESETS.find((x) => x.label === label);
    if (!p) return;
    updateProfile(v, setV, editingId, { provider: p.provider, model: p.model, base_url: p.base_url });
  }

  const profiles = v.profiles || [];
  const active = profiles.find((p) => p.id === editingId) || profiles.find((p) => p.id === v.active_profile) || profiles[0];
  const hasSavedKey = active?.api_key === "********";
  function patchActive(patch: Partial<ModelProfile>) {
    updateProfile(v, setV, active?.id, patch);
  }
  function addProfile(profile?: Partial<ModelProfile>) {
    const id = `llm_${Date.now()}`;
    setV({
      ...v,
      profiles: [
        ...profiles,
        {
          id,
          name: profile?.name || "新模型",
          provider: profile?.provider || "openai",
          model: profile?.model || "gpt-4o-mini",
          base_url: profile?.base_url || "https://api.openai.com/v1",
          api_key: "",
          temperature: profile?.temperature ?? 0.3,
          supports_vision: profile?.supports_vision,
        },
      ],
    });
    setEditingId(id);
  }
  function removeActive() {
    if (profiles.length <= 1 || !active) return;
    const next = profiles.filter((p) => p.id !== active.id);
    setV({
      ...v,
      profiles: next,
      active_profile: v.active_profile === active.id ? next[0].id : v.active_profile,
      fallback_profile: v.fallback_profile === active.id ? "" : v.fallback_profile,
      fallback_enabled: v.fallback_profile === active.id ? false : v.fallback_enabled,
    });
    setEditingId(next[0].id);
  }

  return (
    <Card className="overflow-hidden rounded-lg shadow-sm">
      <CardHeader className="border-b p-4">
        <div className="flex flex-wrap items-start justify-between gap-3">
          <div>
            <CardTitle className="text-base">LLM（生成）</CardTitle>
            <CardDescription>配置主模型和二层兜底模型。左侧选模型，右侧编辑详情。</CardDescription>
          </div>
          <Button variant="outline" onClick={() => setAddOpen(true)}>
            <Plus className="h-4 w-4" /> 新增模型
          </Button>
        </div>
      </CardHeader>
      <CardContent className="p-0">
        <div className="grid min-h-[520px] xl:grid-cols-[320px_minmax(0,1fr)]">
          <div className="border-b bg-slate-50/70 p-4 dark:bg-slate-950/20 xl:border-b-0 xl:border-r">
            <div className="space-y-4">
              <ModelRoleSelect
                label="主模型"
                value={v.active_profile || ""}
                profiles={profiles}
                onChange={(id) => {
                  setV({ ...v, active_profile: id });
                  setEditingId(id);
                }}
              />
              <ModelRoleSelect
                label="二层兜底"
                value={v.fallback_profile || "__none"}
                profiles={profiles}
                allowNone
                onChange={(id) =>
                  setV({
                    ...v,
                    fallback_profile: id === "__none" ? "" : id,
                    fallback_enabled: id !== "__none",
                  })
                }
              />
              <div className="space-y-2">
                <div className="flex items-center justify-between">
                  <Label>模型列表</Label>
                  <Badge variant="secondary">{profiles.length}</Badge>
                </div>
                <ProfileList
                  profiles={profiles}
                  activeId={active?.id}
                  mainId={v.active_profile}
                  fallbackId={v.fallback_enabled ? v.fallback_profile : ""}
                  onSelect={setEditingId}
                />
                <Button
                  variant="outline"
                  className="w-full text-destructive hover:text-destructive"
                  onClick={removeActive}
                  disabled={profiles.length <= 1}
                >
                  <Trash2 className="h-4 w-4" /> 删除当前模型
                </Button>
              </div>
            </div>
          </div>

          <div className="space-y-5 p-5">
            <PresetSelect label="套用预设到当前模型" presets={LLM_PRESETS} onApply={applyPreset} />
            <div className="grid gap-4 sm:grid-cols-2">
              <Field label="名称" value={active?.name || ""} onChange={(x) => patchActive({ name: x })} />
              <ProviderSelect value={active?.provider} onChange={(x) => patchActive({ provider: x as any })} />
              <Field label="model" value={active?.model || ""} onChange={(x) => patchActive({ model: x })} />
              <Field
                label="temperature"
                value={String(active?.temperature ?? 0.3)}
                onChange={(x) => patchActive({ temperature: Number(x) })}
                type="number"
              />
              <Field
                label="base_url"
                value={active?.base_url || ""}
                onChange={(x) => patchActive({ base_url: x })}
                placeholder="https://api.openai.com/v1"
                full
              />
              <Field
                label={`api_key${hasSavedKey ? "（留空 = 使用已保存的 key）" : ""}`}
                value={active?.api_key === "********" ? "" : active?.api_key || ""}
                onChange={(x) => patchActive({ api_key: x })}
                placeholder={hasSavedKey ? "已配置，留空保持不变" : "sk-..."}
                type="password"
                full
              />
              <label className="flex items-start gap-2 sm:col-span-2 rounded-md border bg-muted/30 p-3 text-sm">
                <input
                  type="checkbox"
                  className="mt-0.5"
                  checked={!!active?.supports_vision}
                  onChange={(e) => patchActive({ supports_vision: e.target.checked })}
                />
                <div>
                  <div className="font-medium">该模型支持多模态（截图输入）</div>
                  <p className="mt-0.5 text-xs text-muted-foreground">
                    开启后，设备 ReAct agent 每一步会把当前屏幕截图作为 <code>image_url</code> 一起发给 LLM，
                    对图标按钮等无文本节点的判断更准。需要模型确实支持：gpt-4o / qwen-vl-plus / qwen-vl-max / glm-4v 等。
                    本地 gemma / 纯文本 qwen 等请保持关闭。
                  </p>
                </div>
              </label>
            </div>
            <ActionRow probe={probe} probing={probing} busy={busy} onTest={test} onSave={save} />
          </div>
        </div>
      </CardContent>
      <AddProfileDialog
        open={addOpen}
        onOpenChange={setAddOpen}
        title="新增 LLM 模型"
        presets={LLM_PRESETS}
        defaultName="新模型"
        onCreate={(profile) => addProfile(profile)}
      />
    </Card>
  );
}

function EmbeddingCard({ value, onSaved }: { value: EmbedCfg; onSaved: () => void }) {
  const [v, setV] = useState<EmbedCfg>(() => normaliseEmbed(value));
  const [busy, setBusy] = useState(false);
  const [probing, setProbing] = useState(false);
  const [probe, setProbe] = useState<ProbeResult | null>(null);
  const [addOpen, setAddOpen] = useState(false);
  const [editingId, setEditingId] = useState(value.active_profile || value.profiles?.[0]?.id || "default");

  async function save() {
    setBusy(true);
    try {
      await api("/settings/embedding", { method: "PUT", body: JSON.stringify(v) });
      toast.success("Embedding 配置已保存");
      setV((cur) => ({
        ...cur,
        api_key: "",
        profiles: (cur.profiles || []).map((p) => ({ ...p, api_key: "" })),
      }));
      onSaved();
    } catch (e: any) {
      toast.error("保存失败", { description: e?.message });
    } finally {
      setBusy(false);
    }
  }
  async function test() {
    setProbing(true);
    setProbe(null);
    try {
      const r = await api<ProbeResult>("/settings/test/embedding", {
        method: "POST",
        body: JSON.stringify(v),
      });
      setProbe(r);
    } catch (e: any) {
      setProbe({ ok: false, detail: e?.message ?? String(e) });
    } finally {
      setProbing(false);
    }
  }

  function applyPreset(label: string) {
    const p = EMBED_PRESETS.find((x) => x.label === label);
    if (!p) return;
    updateProfile(v, setV, editingId, { provider: p.provider, model: p.model, base_url: p.base_url, dim: p.dim });
  }

  const profiles = v.profiles || [];
  const active = profiles.find((p) => p.id === editingId) || profiles.find((p) => p.id === v.active_profile) || profiles[0];
  const hasSavedKey = active?.api_key === "********";
  function patchActive(patch: Partial<ModelProfile>) {
    updateProfile(v, setV, active?.id, patch);
  }
  function addProfile(profile?: Partial<ModelProfile>) {
    const id = `emb_${Date.now()}`;
    setV({
      ...v,
      profiles: [
        ...profiles,
        {
          id,
          name: profile?.name || "新向量模型",
          provider: profile?.provider || "openai",
          model: profile?.model || "text-embedding-3-small",
          base_url: profile?.base_url || "https://api.openai.com/v1",
          api_key: "",
          dim: profile?.dim ?? 1536,
        },
      ],
    });
    setEditingId(id);
  }
  function removeActive() {
    if (profiles.length <= 1 || !active) return;
    const next = profiles.filter((p) => p.id !== active.id);
    setV({ ...v, profiles: next, active_profile: v.active_profile === active.id ? next[0].id : v.active_profile });
    setEditingId(next[0].id);
  }

  return (
    <Card className="overflow-hidden rounded-lg shadow-sm">
      <CardHeader className="border-b p-4">
        <div className="flex flex-wrap items-start justify-between gap-3">
          <div>
            <CardTitle className="text-base">Embedding（向量化）</CardTitle>
            <CardDescription>用于知识库入库和检索。切换维度后建议重建对应知识库向量。</CardDescription>
          </div>
          <Button variant="outline" onClick={() => setAddOpen(true)}>
            <Plus className="h-4 w-4" /> 新增向量模型
          </Button>
        </div>
      </CardHeader>
      <CardContent className="p-0">
        <div className="grid min-h-[500px] xl:grid-cols-[320px_minmax(0,1fr)]">
          <div className="border-b bg-slate-50/70 p-4 dark:bg-slate-950/20 xl:border-b-0 xl:border-r">
            <div className="space-y-4">
              <ModelRoleSelect
                label="当前使用"
                value={v.active_profile || ""}
                profiles={profiles}
                onChange={(id) => {
                  setV({ ...v, active_profile: id });
                  setEditingId(id);
                }}
              />
              <div className="space-y-2">
                <div className="flex items-center justify-between">
                  <Label>模型列表</Label>
                  <Badge variant="secondary">{profiles.length}</Badge>
                </div>
                <ProfileList
                  profiles={profiles}
                  activeId={active?.id}
                  mainId={v.active_profile}
                  onSelect={setEditingId}
                />
                <Button
                  variant="outline"
                  className="w-full text-destructive hover:text-destructive"
                  onClick={removeActive}
                  disabled={profiles.length <= 1}
                >
                  <Trash2 className="h-4 w-4" /> 删除当前模型
                </Button>
              </div>
            </div>
          </div>

          <div className="space-y-5 p-5">
            <PresetSelect label="套用预设到当前模型" presets={EMBED_PRESETS} onApply={applyPreset} />
            <div className="grid gap-4 sm:grid-cols-2">
              <Field label="名称" value={active?.name || ""} onChange={(x) => patchActive({ name: x })} />
              <ProviderSelect value={active?.provider} onChange={(x) => patchActive({ provider: x as any })} />
              <Field label="model" value={active?.model || ""} onChange={(x) => patchActive({ model: x })} />
              <Field
                label="dim"
                value={String(active?.dim ?? 1536)}
                onChange={(x) => patchActive({ dim: Number(x) })}
                type="number"
              />
              <Field
                label="base_url"
                value={active?.base_url || ""}
                onChange={(x) => patchActive({ base_url: x })}
                placeholder="https://api.openai.com/v1"
                full
              />
              <Field
                label={`api_key${hasSavedKey ? "（留空 = 使用已保存的 key）" : ""}`}
                value={active?.api_key === "********" ? "" : active?.api_key || ""}
                onChange={(x) => patchActive({ api_key: x })}
                placeholder={hasSavedKey ? "已配置，留空保持不变" : "sk-..."}
                type="password"
                full
              />
            </div>

            <ActionRow probe={probe} probing={probing} busy={busy} onTest={test} onSave={save} />
          </div>
        </div>
      </CardContent>
      <AddProfileDialog
        open={addOpen}
        onOpenChange={setAddOpen}
        title="新增向量模型"
        presets={EMBED_PRESETS}
        defaultName="新向量模型"
        onCreate={(profile) => addProfile(profile)}
      />
    </Card>
  );
}

function ParserCard({ value, onSaved }: { value: ParserCfg; onSaved: () => void }) {
  const hasSavedKey = value.api_key === "********";
  const [v, setV] = useState<ParserCfg>({ ...value, api_key: "" });
  const [busy, setBusy] = useState(false);
  const [probing, setProbing] = useState(false);
  const [probe, setProbe] = useState<ProbeResult | null>(null);

  async function save() {
    setBusy(true);
    try {
      await api("/settings/parser", { method: "PUT", body: JSON.stringify(v) });
      toast.success("文档解析配置已保存");
      setV((cur) => ({ ...cur, api_key: "" }));
      onSaved();
    } catch (e: any) {
      toast.error("保存失败", { description: e?.message });
    } finally {
      setBusy(false);
    }
  }

  async function test() {
    setProbing(true);
    setProbe(null);
    try {
      const r = await api<ProbeResult>("/settings/test/parser", {
        method: "POST",
        body: JSON.stringify(v),
      });
      setProbe(r);
    } catch (e: any) {
      setProbe({ ok: false, detail: e?.message ?? String(e) });
    } finally {
      setProbing(false);
    }
  }

  return (
    <Card className="rounded-lg shadow-sm">
      <CardHeader className="border-b p-4">
        <CardTitle className="text-base">文档解析（MinerU）</CardTitle>
        <CardDescription>
          上传 PDF、Office、图片文档时使用。默认内置解析零依赖，复杂版面可以切到 MinerU。
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-4 p-5">
        <div className="space-y-2">
          <Label>解析方式</Label>
          <Select
            value={v.backend}
            onValueChange={(x) => setV({ ...v, backend: x as ParserCfg["backend"] })}
          >
            <SelectTrigger>
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value="builtin">builtin（文本 + pypdf）</SelectItem>
              <SelectItem value="mineru_local">mineru_local（本地 CLI）</SelectItem>
              <SelectItem value="mineru_api">mineru_api（官方云端）</SelectItem>
            </SelectContent>
          </Select>
        </div>

        {v.backend === "mineru_local" && (
          <div className="grid gap-4 sm:grid-cols-2">
            <Field
              label="本地命令"
              value={v.local_cmd}
              onChange={(x) => setV({ ...v, local_cmd: x })}
              placeholder="mineru"
            />
            <Field
              label="额外参数"
              value={v.local_extra_args}
              onChange={(x) => setV({ ...v, local_extra_args: x })}
              placeholder="如 -b pipeline （CPU 模式）"
            />
          </div>
        )}

        {v.backend === "mineru_api" && (
          <div className="grid gap-4 sm:grid-cols-2">
            <Field
              label="API 地址"
              value={v.api_base}
              onChange={(x) => setV({ ...v, api_base: x })}
              placeholder="https://mineru.net/api/v4"
              full
            />
            <Field
              label={`API Token${hasSavedKey ? "（留空 = 使用已保存的 token）" : ""}`}
              value={v.api_key}
              onChange={(x) => setV({ ...v, api_key: x })}
              placeholder={hasSavedKey ? "已配置，留空保持不变" : "粘贴 Bearer Token"}
              type="password"
              full
            />
            <div className="space-y-2">
              <Label>模型版本</Label>
              <Select
                value={v.model_version}
                onValueChange={(x) =>
                  setV({ ...v, model_version: x as ParserCfg["model_version"] })
                }
              >
                <SelectTrigger>
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="vlm">vlm（推荐，精度更高）</SelectItem>
                  <SelectItem value="pipeline">pipeline（传统流水线）</SelectItem>
                </SelectContent>
              </Select>
            </div>
          </div>
        )}

        <ActionRow
          probe={probe}
          probing={probing}
          busy={busy}
          onTest={test}
          onSave={save}
        />
      </CardContent>
    </Card>
  );
}

function RetrievalCard({ value, onSaved }: { value: RetrievalCfg; onSaved: () => void }) {
  const [v, setV] = useState<RetrievalCfg>(value);
  const [busy, setBusy] = useState(false);
  async function save() {
    setBusy(true);
    try {
      await api("/settings/retrieval", { method: "PUT", body: JSON.stringify(v) });
      toast.success("检索参数已保存");
      onSaved();
    } catch (e: any) {
      toast.error("保存失败", { description: e?.message });
    } finally {
      setBusy(false);
    }
  }
  return (
    <Card className="rounded-lg shadow-sm">
      <CardHeader className="border-b p-4">
        <CardTitle className="text-base">检索参数</CardTitle>
        <CardDescription>
          控制知识库每次召回多少片段，以及低相关度内容是否进入回答上下文。
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-4 p-5">
        <div className="grid gap-4 sm:grid-cols-2">
          <Field
            label="召回片段数"
            value={String(v.top_k)}
            onChange={(x) => setV({ ...v, top_k: Number(x) })}
            type="number"
          />
          <Field
            label="最低相关分"
            value={String(v.min_score)}
            onChange={(x) => setV({ ...v, min_score: Number(x) })}
            type="number"
          />
        </div>
        <div className="rounded-md border bg-muted/30 p-3 text-xs text-muted-foreground">
          Mock 向量模型的分数通常偏低；真实 embedding 一般可以从 0.5 附近开始调。
        </div>
        <Button onClick={save} disabled={busy} className="w-full sm:w-auto">
          <Save className="h-4 w-4" /> 保存检索参数
        </Button>
      </CardContent>
    </Card>
  );
}

function AIBehaviorCard({ value, onSaved }: { value: AIBehaviorCfg; onSaved: () => void }) {
  const [v, setV] = useState<AIBehaviorCfg>(value);
  const [busy, setBusy] = useState(false);
  async function save() {
    setBusy(true);
    try {
      await api("/settings/ai", { method: "PUT", body: JSON.stringify(v) });
      toast.success("AI 行为已保存");
      onSaved();
    } catch (e: any) {
      toast.error("保存失败", { description: e?.message });
    } finally {
      setBusy(false);
    }
  }
  return (
    <Card className="rounded-lg shadow-sm">
      <CardHeader className="border-b p-4">
        <CardTitle className="text-base">AI 行为</CardTitle>
        <CardDescription>
          控制客服助手的回复风格、置信度门槛、历史上下文窗口和智能体步数。
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-4 p-5">
        <div className="grid gap-4 sm:grid-cols-3">
          <Field
            label="转人工阈值"
            value={String(v.confidence_threshold)}
            onChange={(x) => setV({ ...v, confidence_threshold: Number(x) })}
            type="number"
          />
          <Field
            label="历史消息数"
            value={String(v.context_window)}
            onChange={(x) => setV({ ...v, context_window: Number(x) })}
            type="number"
          />
          <Field
            label="回复 token 上限"
            value={String(v.max_tokens)}
            onChange={(x) => setV({ ...v, max_tokens: Number(x) })}
            type="number"
          />
        </div>
        <div className="space-y-2">
          <Label>默认系统提示词</Label>
          <Textarea
            value={v.default_prompt}
            onChange={(e) => setV({ ...v, default_prompt: e.target.value })}
            className="min-h-[120px]"
            placeholder="留空则使用后端默认提示词"
          />
        </div>
        <div className="flex flex-wrap items-center justify-between gap-4 rounded-md border bg-muted/30 p-3 text-sm">
          <label className="flex items-center gap-2">
            <input
              type="checkbox"
              checked={v.agent_mode}
              onChange={(e) => setV({ ...v, agent_mode: e.target.checked })}
            />
            <span>启用 ReAct 智能体（可调用 kb_search / 技能 / MCP 工具）</span>
          </label>
          <div className="flex items-center gap-2">
            <Label htmlFor="ams" className="m-0">最多推理步数</Label>
            <Input
              id="ams"
              type="number"
              value={String(v.agent_max_steps)}
              onChange={(e) => setV({ ...v, agent_max_steps: Number(e.target.value) })}
              className="w-24"
            />
          </div>
        </div>

        <div className="space-y-3 rounded-md border bg-muted/30 p-3 text-sm">
          <div className="flex items-center justify-between gap-3">
            <div>
              <div className="font-medium">设备 ReAct 决策模式</div>
              <p className="mt-1 text-xs text-muted-foreground">
                AI 仅决定要操作哪个节点，坐标始终由后端按节点 bounds 解析，**不会让 AI 猜 x / y**。
              </p>
            </div>
            <Select
              value={v.react_force_llm ? "llm_only" : "rule_first"}
              onValueChange={(val) =>
                setV({ ...v, react_force_llm: val === "llm_only" })
              }
            >
              <SelectTrigger className="w-56">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="rule_first">
                  规则快路径优先（LLM 兜底）
                </SelectItem>
                <SelectItem value="llm_only">
                  完全由 AI 判断（每步走 LLM）
                </SelectItem>
              </SelectContent>
            </Select>
          </div>
          <p className="text-xs text-muted-foreground">
            ·「规则快路径优先」：常见 send-text 等流程走缓存 locator，命中失败再调 LLM —— 省成本、低延迟。
            <br />
            ·「完全由 AI 判断」：每步都把 UI 树（+ 可选截图）发给 LLM 决策 —— 适合调试新场景或非常规流程，单次成本高。
          </p>
        </div>

        <Button onClick={save} disabled={busy} className="w-full sm:w-auto">
          <Save className="h-4 w-4" /> 保存 AI 行为
        </Button>
      </CardContent>
    </Card>
  );
}

function InfraCard({ value }: { value: InfraCfg }) {
  return (
    <Card className="rounded-lg shadow-sm">
      <CardHeader className="border-b p-4">
        <CardTitle className="text-base">基础设施（只读）</CardTitle>
        <CardDescription>
          通过环境变量 / docker-compose 配置,改这些需要重启后端。
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-2 p-5 text-sm">
        <KV k="vector_store" v={value.vector_store} />
        {value.vector_store === "milvus" && (
          <>
            <KV k="milvus_uri" v={value.milvus_uri} mono />
            <KV k="milvus_collection" v={value.milvus_collection} mono />
          </>
        )}
        <KV k="graph_store" v={value.graph_store} />
        {value.graph_store === "neo4j" && (
          <KV k="neo4j_uri" v={value.neo4j_uri} mono />
        )}
      </CardContent>
    </Card>
  );
}

function StatusLine({ label, value }: { label: string; value: string }) {
  return (
    <div className="grid grid-cols-[56px_minmax(0,1fr)] gap-2">
      <span className="text-muted-foreground">{label}</span>
      <span className="truncate font-medium">{value}</span>
    </div>
  );
}

function ModelRoleSelect({
  label,
  value,
  profiles,
  allowNone,
  onChange,
}: {
  label: string;
  value: string;
  profiles: ModelProfile[];
  allowNone?: boolean;
  onChange: (id: string) => void;
}) {
  return (
    <div className="space-y-2">
      <Label>{label}</Label>
      <Select value={value} onValueChange={onChange}>
        <SelectTrigger className="bg-background">
          <SelectValue />
        </SelectTrigger>
        <SelectContent>
          {allowNone && <SelectItem value="__none">不启用</SelectItem>}
          {profiles.map((p) => (
            <SelectItem key={p.id} value={p.id}>
              {p.name || p.id}
            </SelectItem>
          ))}
        </SelectContent>
      </Select>
    </div>
  );
}

function ProfileList({
  profiles,
  activeId,
  mainId,
  fallbackId,
  onSelect,
}: {
  profiles: ModelProfile[];
  activeId?: string;
  mainId?: string;
  fallbackId?: string;
  onSelect: (id: string) => void;
}) {
  return (
    <div className="max-h-[320px] space-y-1 overflow-auto pr-1">
      {profiles.map((p) => {
        const selected = p.id === activeId;
        return (
          <button
            key={p.id}
            type="button"
            onClick={() => onSelect(p.id)}
            className={cn(
              "w-full rounded-md border px-3 py-2 text-left transition-colors",
              selected ? "border-primary bg-primary/5" : "bg-background hover:bg-muted",
            )}
          >
            <div className="flex items-center justify-between gap-2">
              <span className="min-w-0 truncate text-sm font-medium">{p.name || p.id}</span>
              <span className="flex shrink-0 gap-1">
                {p.id === mainId && <Badge className="h-5 px-1.5 text-[10px]">主</Badge>}
                {p.id === fallbackId && <Badge variant="secondary" className="h-5 px-1.5 text-[10px]">兜底</Badge>}
              </span>
            </div>
            <div className="mt-1 flex min-w-0 items-center gap-2 text-xs text-muted-foreground">
              <span className="truncate font-mono">{p.model || "未填写 model"}</span>
              {typeof p.dim === "number" && <span className="shrink-0">dim {p.dim}</span>}
            </div>
          </button>
        );
      })}
    </div>
  );
}

function ProviderSelect({
  value,
  onChange,
}: {
  value?: "mock" | "openai";
  onChange: (value: "mock" | "openai") => void;
}) {
  return (
    <div className="space-y-2">
      <Label>provider</Label>
      <Select value={value} onValueChange={(x) => onChange(x as "mock" | "openai")}>
        <SelectTrigger>
          <SelectValue />
        </SelectTrigger>
        <SelectContent>
          <SelectItem value="openai">OpenAI 兼容端</SelectItem>
          <SelectItem value="mock">mock</SelectItem>
        </SelectContent>
      </Select>
    </div>
  );
}

function PresetSelect({
  label,
  presets,
  value,
  onApply,
}: {
  label: string;
  presets: { label: string }[];
  value?: string;
  onApply: (label: string) => void;
}) {
  return (
    <div className="space-y-2">
      <Label>{label}</Label>
      <Select value={value} onValueChange={onApply}>
        <SelectTrigger>
          <SelectValue placeholder="选择一个预设快速填写" />
        </SelectTrigger>
        <SelectContent>
          {presets.map((p) => (
            <SelectItem key={p.label} value={p.label}>
              {p.label}
            </SelectItem>
          ))}
        </SelectContent>
      </Select>
    </div>
  );
}

function AddProfileDialog({
  open,
  onOpenChange,
  title,
  presets,
  defaultName,
  onCreate,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  title: string;
  presets: ({ label: string; provider: "openai" | "mock"; model: string; base_url: string; dim?: number })[];
  defaultName: string;
  onCreate: (profile: Partial<ModelProfile>) => void;
}) {
  const [name, setName] = useState(defaultName);
  const [presetLabel, setPresetLabel] = useState(presets[0]?.label || "");
  const preset = useMemo(
    () => presets.find((p) => p.label === presetLabel) || presets[0],
    [presetLabel, presets],
  );

  function create() {
    if (!preset) return;
    onCreate({
      name: name.trim() || defaultName,
      provider: preset.provider,
      model: preset.model,
      base_url: preset.base_url,
      dim: preset.dim,
    });
    setName(defaultName);
    setPresetLabel(presets[0]?.label || "");
    onOpenChange(false);
  }

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>{title}</DialogTitle>
        </DialogHeader>
        <div className="space-y-4 py-2">
          <Field label="名称" value={name} onChange={setName} />
          <PresetSelect label="从预设开始" presets={presets} value={presetLabel} onApply={setPresetLabel} />
          {preset && (
            <div className="rounded-md border bg-muted/30 p-3 text-xs">
              <StatusLine label="provider" value={preset.provider} />
              <StatusLine label="model" value={preset.model} />
              <StatusLine label="base" value={preset.base_url || "mock"} />
            </div>
          )}
        </div>
        <DialogFooter>
          <Button variant="outline" onClick={() => onOpenChange(false)}>取消</Button>
          <Button onClick={create}>
            <Plus className="h-4 w-4" /> 新增
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

function normaliseLlm(value: LlmCfg): LlmCfg {
  const profiles = (value.profiles?.length ? value.profiles : [{
    id: value.active_profile || "default",
    name: "默认模型",
    provider: value.provider,
    model: value.model,
    api_key: value.api_key || "",
    base_url: value.base_url,
    temperature: value.temperature,
    supports_vision: value.supports_vision,
  }]).map((p) => ({ ...p, api_key: p.api_key === "********" ? "********" : "" }));
  return {
    ...value,
    api_key: "",
    profiles,
    active_profile: value.active_profile || profiles[0].id,
    fallback_profile: value.fallback_profile || "",
    fallback_enabled: Boolean(value.fallback_enabled && value.fallback_profile),
  };
}

function normaliseEmbed(value: EmbedCfg): EmbedCfg {
  const profiles = (value.profiles?.length ? value.profiles : [{
    id: value.active_profile || "default",
    name: "默认向量模型",
    provider: value.provider,
    model: value.model,
    api_key: value.api_key || "",
    base_url: value.base_url,
    dim: value.dim,
  }]).map((p) => ({ ...p, api_key: p.api_key === "********" ? "********" : "" }));
  return {
    ...value,
    api_key: "",
    profiles,
    active_profile: value.active_profile || profiles[0].id,
  };
}

function updateProfile<T extends { profiles?: ModelProfile[] }>(
  value: T,
  setValue: (v: T) => void,
  profileId: string | undefined,
  patch: Partial<ModelProfile>,
) {
  const profiles = value.profiles || [];
  const targetId = profileId || profiles[0]?.id;
  setValue({
    ...value,
    profiles: profiles.map((p) => p.id === targetId ? { ...p, ...patch } : p),
  });
}

function ActionRow({
  probe,
  probing,
  busy,
  onTest,
  onSave,
}: {
  probe: ProbeResult | null;
  probing: boolean;
  busy: boolean;
  onTest: () => void;
  onSave: () => void;
}) {
  return (
    <div className="flex flex-col gap-3 border-t pt-4 sm:flex-row sm:items-center sm:justify-between">
      <div className="min-w-0 flex-1">
        {probe && (
          <div
            className={`flex items-start gap-2 rounded-md border p-2 text-xs ${
              probe.ok ? "border-emerald-200 bg-emerald-50" : "border-red-200 bg-red-50"
            }`}
          >
            {probe.ok ? (
              <CheckCircle2 className="h-4 w-4 text-emerald-600" />
            ) : (
              <AlertCircle className="h-4 w-4 text-red-600" />
            )}
            <div className="min-w-0 flex-1">
              <p className="font-medium">{probe.ok ? "测试通过" : "测试失败"}</p>
              <p className="text-muted-foreground break-words">{probe.detail}</p>
              <div className="mt-1 flex flex-wrap gap-1">
                {probe.model && (
                  <Badge variant="secondary" className="text-[10px]">
                    model={probe.model}
                  </Badge>
                )}
                {typeof probe.latency_ms === "number" && (
                  <Badge variant="secondary" className="text-[10px]">
                    {probe.latency_ms}ms
                  </Badge>
                )}
                {typeof probe.dim === "number" && (
                  <Badge variant="secondary" className="text-[10px]">
                    dim={probe.dim}
                  </Badge>
                )}
              </div>
            </div>
          </div>
        )}
      </div>
      <div className="flex shrink-0 gap-2">
        <Button variant="outline" onClick={onTest} disabled={probing}>
          <Wand2 className="h-4 w-4" /> 测试
        </Button>
        <Button onClick={onSave} disabled={busy}>
          <Save className="h-4 w-4" /> 保存
        </Button>
      </div>
    </div>
  );
}

function Field({
  label,
  value,
  onChange,
  type,
  placeholder,
  full,
}: {
  label: string;
  value: string;
  onChange: (x: string) => void;
  type?: string;
  placeholder?: string;
  full?: boolean;
}) {
  return (
    <div className={`space-y-2 ${full ? "sm:col-span-2" : ""}`}>
      <Label>{label}</Label>
      <Input
        type={type}
        value={value}
        placeholder={placeholder}
        onChange={(e) => onChange(e.target.value)}
      />
    </div>
  );
}

function KV({ k, v, mono }: { k: string; v: string; mono?: boolean }) {
  return (
    <div className="flex items-center justify-between gap-3">
      <span className="text-muted-foreground">{k}</span>
      <span className={mono ? "font-mono text-xs" : "text-foreground"}>{v}</span>
    </div>
  );
}
