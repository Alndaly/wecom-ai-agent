"use client";
import { useEffect, useState } from "react";
import { Save, Wand2, AlertCircle, CheckCircle2 } from "lucide-react";
import { api } from "@/lib/api";
import { toast } from "@/hooks/use-toast";
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

type LlmCfg = {
  provider: "mock" | "openai";
  model: string;
  api_key: string;
  base_url: string;
  temperature: number;
};
type EmbedCfg = {
  provider: "mock" | "openai";
  model: string;
  api_key: string;
  base_url: string;
  dim: number;
};
type RetrievalCfg = { top_k: number; min_score: number };
type AIBehaviorCfg = {
  confidence_threshold: number;
  context_window: number;
  default_prompt: string;
};
type InfraCfg = {
  vector_store: string;
  graph_store: string;
  milvus_uri: string;
  milvus_collection: string;
  neo4j_uri: string;
};
type ProbeResult = { ok: boolean; detail: string; latency_ms?: number; model?: string; dim?: number };

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
  const [infra, setInfra] = useState<InfraCfg | null>(null);
  const [loading, setLoading] = useState(true);

  async function reload() {
    setLoading(true);
    try {
      const data = await api<any>("/settings");
      setLlm(data.llm);
      setEmbed(data.embedding);
      setRetrieval(data.retrieval);
      setAI(data.ai);
      setInfra(data.infra);
    } finally {
      setLoading(false);
    }
  }
  useEffect(() => {
    reload().catch(console.error);
  }, []);

  if (loading || !llm || !embed || !retrieval || !ai || !infra)
    return <p className="text-sm text-muted-foreground">加载中…</p>;

  return (
    <div className="max-w-3xl space-y-6">
      <div>
        <h1 className="text-2xl font-semibold tracking-tight">模型配置</h1>
        <p className="mt-1 text-sm text-muted-foreground">
          这里的 LLM / Embedding 配置会即时生效（修改保存后,所有新对话和入库流程会立刻使用新配置）。
          基础设施（Milvus / Neo4j）通过 docker-compose 部署,在下方只读展示。
        </p>
      </div>

      <LLMCard value={llm} onSaved={reload} />
      <EmbeddingCard value={embed} onSaved={reload} />
      <RetrievalCard value={retrieval} onSaved={reload} />
      <AIBehaviorCard value={ai} onSaved={reload} />
      <InfraCard value={infra} />
    </div>
  );
}

function LLMCard({ value, onSaved }: { value: LlmCfg; onSaved: () => void }) {
  const [v, setV] = useState<LlmCfg>(value);
  const [busy, setBusy] = useState(false);
  const [probing, setProbing] = useState(false);
  const [probe, setProbe] = useState<ProbeResult | null>(null);

  async function save() {
    setBusy(true);
    try {
      await api("/settings/llm", { method: "PUT", body: JSON.stringify(v) });
      toast({ title: "LLM 配置已保存" });
      onSaved();
    } catch (e: any) {
      toast({ title: "保存失败", description: e?.message, variant: "destructive" });
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
    setV({ ...v, provider: p.provider, model: p.model, base_url: p.base_url });
  }

  return (
    <Card>
      <CardHeader>
        <CardTitle className="text-base">LLM（生成）</CardTitle>
        <CardDescription>
          兼容 OpenAI API 的服务都能直接用：OpenAI / DeepSeek / 通义 / 智谱 / 自部署 Ollama …
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-4">
        <div className="space-y-2">
          <Label>预设</Label>
          <Select onValueChange={applyPreset}>
            <SelectTrigger>
              <SelectValue placeholder="选择一个预设快速填写" />
            </SelectTrigger>
            <SelectContent>
              {LLM_PRESETS.map((p) => (
                <SelectItem key={p.label} value={p.label}>
                  {p.label}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
        </div>

        <div className="grid gap-4 sm:grid-cols-2">
          <div className="space-y-2">
            <Label>provider</Label>
            <Select
              value={v.provider}
              onValueChange={(x) => setV({ ...v, provider: x as any })}
            >
              <SelectTrigger>
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="openai">openai 兼容端</SelectItem>
                <SelectItem value="mock">mock</SelectItem>
              </SelectContent>
            </Select>
          </div>
          <Field
            label="model"
            value={v.model}
            onChange={(x) => setV({ ...v, model: x })}
          />
          <Field
            label="base_url"
            value={v.base_url}
            onChange={(x) => setV({ ...v, base_url: x })}
            placeholder="https://api.openai.com/v1"
            full
          />
          <Field
            label="api_key（留空则保持已保存的值）"
            value={v.api_key}
            onChange={(x) => setV({ ...v, api_key: x })}
            placeholder={v.api_key === "********" ? "已配置 ********" : "sk-..."}
            type="password"
            full
          />
          <Field
            label="temperature"
            value={String(v.temperature)}
            onChange={(x) => setV({ ...v, temperature: Number(x) })}
            type="number"
          />
        </div>

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

function EmbeddingCard({ value, onSaved }: { value: EmbedCfg; onSaved: () => void }) {
  const [v, setV] = useState<EmbedCfg>(value);
  const [busy, setBusy] = useState(false);
  const [probing, setProbing] = useState(false);
  const [probe, setProbe] = useState<ProbeResult | null>(null);

  async function save() {
    setBusy(true);
    try {
      await api("/settings/embedding", { method: "PUT", body: JSON.stringify(v) });
      toast({ title: "Embedding 配置已保存" });
      onSaved();
    } catch (e: any) {
      toast({ title: "保存失败", description: e?.message, variant: "destructive" });
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
    setV({ ...v, provider: p.provider, model: p.model, base_url: p.base_url, dim: p.dim });
  }

  return (
    <Card>
      <CardHeader>
        <CardTitle className="text-base">Embedding（向量化）</CardTitle>
        <CardDescription>
          切换 embedding 模型后,旧的向量库数据可能仍然按旧维度存在,建议为新模型创建新知识库。
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-4">
        <div className="space-y-2">
          <Label>预设</Label>
          <Select onValueChange={applyPreset}>
            <SelectTrigger>
              <SelectValue placeholder="选择一个预设快速填写" />
            </SelectTrigger>
            <SelectContent>
              {EMBED_PRESETS.map((p) => (
                <SelectItem key={p.label} value={p.label}>
                  {p.label}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
        </div>

        <div className="grid gap-4 sm:grid-cols-2">
          <div className="space-y-2">
            <Label>provider</Label>
            <Select
              value={v.provider}
              onValueChange={(x) => setV({ ...v, provider: x as any })}
            >
              <SelectTrigger>
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="openai">openai 兼容端</SelectItem>
                <SelectItem value="mock">mock</SelectItem>
              </SelectContent>
            </Select>
          </div>
          <Field
            label="model"
            value={v.model}
            onChange={(x) => setV({ ...v, model: x })}
          />
          <Field
            label="base_url"
            value={v.base_url}
            onChange={(x) => setV({ ...v, base_url: x })}
            placeholder="https://api.openai.com/v1"
            full
          />
          <Field
            label="api_key（留空则保持已保存的值）"
            value={v.api_key}
            onChange={(x) => setV({ ...v, api_key: x })}
            placeholder={v.api_key === "********" ? "已配置 ********" : "sk-..."}
            type="password"
            full
          />
          <Field
            label="dim"
            value={String(v.dim)}
            onChange={(x) => setV({ ...v, dim: Number(x) })}
            type="number"
          />
        </div>

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
      toast({ title: "检索参数已保存" });
      onSaved();
    } catch (e: any) {
      toast({ title: "保存失败", description: e?.message, variant: "destructive" });
    } finally {
      setBusy(false);
    }
  }
  return (
    <Card>
      <CardHeader>
        <CardTitle className="text-base">检索参数</CardTitle>
        <CardDescription>
          mock embedding 因为只是字符 bigram 哈希,score 上限大约 0.2; 真实 embedding 推荐
          min_score = 0.5 起步。
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-4">
        <div className="grid gap-4 sm:grid-cols-2">
          <Field
            label="top_k"
            value={String(v.top_k)}
            onChange={(x) => setV({ ...v, top_k: Number(x) })}
            type="number"
          />
          <Field
            label="min_score"
            value={String(v.min_score)}
            onChange={(x) => setV({ ...v, min_score: Number(x) })}
            type="number"
          />
        </div>
        <Button onClick={save} disabled={busy}>
          <Save className="h-4 w-4" /> 保存
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
      toast({ title: "AI 行为已保存" });
      onSaved();
    } catch (e: any) {
      toast({ title: "保存失败", description: e?.message, variant: "destructive" });
    } finally {
      setBusy(false);
    }
  }
  return (
    <Card>
      <CardHeader>
        <CardTitle className="text-base">AI 行为</CardTitle>
        <CardDescription>
          system prompt + 置信度阈值(混合模式下低于此值会转人工建议) + 历史窗口大小。
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-4">
        <div className="grid gap-4 sm:grid-cols-2">
          <Field
            label="confidence_threshold"
            value={String(v.confidence_threshold)}
            onChange={(x) => setV({ ...v, confidence_threshold: Number(x) })}
            type="number"
          />
          <Field
            label="context_window"
            value={String(v.context_window)}
            onChange={(x) => setV({ ...v, context_window: Number(x) })}
            type="number"
          />
        </div>
        <div className="space-y-2">
          <Label>default_prompt</Label>
          <Textarea
            value={v.default_prompt}
            onChange={(e) => setV({ ...v, default_prompt: e.target.value })}
            className="min-h-[120px]"
            placeholder="留空则使用后端默认提示词"
          />
        </div>
        <Button onClick={save} disabled={busy}>
          <Save className="h-4 w-4" /> 保存
        </Button>
      </CardContent>
    </Card>
  );
}

function InfraCard({ value }: { value: InfraCfg }) {
  return (
    <Card>
      <CardHeader>
        <CardTitle className="text-base">基础设施（只读）</CardTitle>
        <CardDescription>
          通过环境变量 / docker-compose 配置,改这些需要重启后端。
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-2 text-sm">
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
    <div className="flex items-center justify-between gap-3">
      <div className="flex-1">
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
      <div className="flex gap-2">
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
