'use client';

import { useEffect, useState } from 'react';
import type { ReactNode } from 'react';
import {
	Bot,
	Gauge,
	History,
	Route,
	Save,
	SlidersHorizontal,
} from 'lucide-react';
import Link from 'next/link';
import { api } from '@/lib/api';
import { toast } from '@/components/ui/sonner';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import {
	Card,
	CardContent,
	CardDescription,
	CardHeader,
	CardTitle,
} from '@/components/ui/card';
import {
	Select,
	SelectContent,
	SelectItem,
	SelectTrigger,
	SelectValue,
} from '@/components/ui/select';

export type AIBehaviorCfg = {
	confidence_threshold: number;
	context_window: number;
	persona_id: string;
	max_tokens: number;
	agent_mode: boolean;
	agent_max_steps: number;
	react_force_llm: boolean;
};

type PersonaSummary = {
	id: string;
	name: string;
	description: string;
};

export function AIBehaviorCard({
	value,
	onSaved,
}: {
	value: AIBehaviorCfg;
	onSaved: () => void;
}) {
	const [v, setV] = useState<AIBehaviorCfg>(value);
	const [personas, setPersonas] = useState<PersonaSummary[]>([]);
	const [busy, setBusy] = useState(false);

	useEffect(() => {
		api<PersonaSummary[]>('/personas')
			.then(setPersonas)
			.catch(() => {
				/* Persona picker is optional; keep the rest of settings usable. */
			});
	}, []);

	async function save() {
		setBusy(true);
		try {
			await api('/settings/ai', { method: 'PUT', body: JSON.stringify(v) });
			toast.success('AI 行为已保存');
			onSaved();
		} catch (e: any) {
			toast.error('保存失败', { description: e?.message });
		} finally {
			setBusy(false);
		}
	}

	const personaName =
		personas.find(
			(p) => p.id === ((v.persona_id || 'default').trim() || 'default'),
		)?.name || '默认客服人格';
	const selectedPersonaId = (v.persona_id || 'default').trim() || 'default';
	const selectedPersona = personas.find((p) => p.id === selectedPersonaId);
	const decisionMode = v.react_force_llm ? '每步走 LLM' : '规则快路径优先';

	return (
		<div className='grid min-h-0 gap-4 xl:grid-cols-[320px_minmax(0,1fr)]'>
			<aside className='rounded-lg border bg-background p-5 shadow-sm'>
				<div className='flex items-center gap-3'>
					<div className='flex h-10 w-10 items-center justify-center rounded-lg border bg-muted/40'>
						<SlidersHorizontal className='h-5 w-5 text-muted-foreground' />
					</div>
					<div>
						<h2 className='text-base font-semibold'>AI 行为</h2>
						<p className='mt-0.5 text-xs text-muted-foreground'>
							当前自动回复策略
						</p>
					</div>
				</div>

				<div className='mt-5 space-y-2'>
					<SummaryTile label='团队人格' value={personaName} />
					<SummaryTile
						label='客服模式'
						value={v.agent_mode ? 'ReAct 智能体' : '直接生成'}
					/>
					<SummaryTile label='设备决策' value={decisionMode} />
					<SummaryTile
						label='转人工阈值'
						value={String(v.confidence_threshold)}
					/>
				</div>

				<div className='mt-5 rounded-md border bg-muted/20 p-3 text-xs leading-6 text-muted-foreground'>
					人格决定“像谁说话”，业务规则只保留团队级约束；设备独立人格可在设备页覆盖。
				</div>
			</aside>

			<Card className='overflow-hidden rounded-lg border shadow-sm'>
				<CardHeader className='border-b p-5'>
					<CardTitle className='text-base'>回复策略</CardTitle>
					<CardDescription>
						配置生成回复前的语气、上下文窗口和执行方式。
					</CardDescription>
				</CardHeader>
				<CardContent className='space-y-6 p-5'>
					<section className='rounded-md border bg-muted/15 p-4'>
						<div className='grid gap-4 lg:grid-cols-[minmax(0,1fr)_360px] lg:items-center'>
							<div className='flex min-w-0 items-start gap-3'>
								<div className='flex h-9 w-9 shrink-0 items-center justify-center rounded-md border bg-background text-muted-foreground'>
									<Bot className='h-4 w-4' />
								</div>
								<div className='min-w-0'>
									<h3 className='text-sm font-semibold'>客服人格</h3>
									<p className='mt-1 text-xs leading-5 text-muted-foreground'>
										团队默认语气；设备页可单独覆盖。
									</p>
									{selectedPersona?.description && (
										<p className='mt-2 max-w-2xl truncate text-xs text-muted-foreground'>
											{selectedPersona.description}
										</p>
									)}
								</div>
							</div>
							<div className='space-y-2'>
								<Select
									value={selectedPersonaId}
									onValueChange={(persona_id) => setV({ ...v, persona_id })}>
									<SelectTrigger className='bg-background'>
										<SelectValue />
									</SelectTrigger>
									<SelectContent>
										{personas.map((p) => (
											<SelectItem key={p.id} value={p.id}>
												{p.name} · {p.id}
											</SelectItem>
										))}
										{personas.length === 0 && (
											<SelectItem value='default'>
												默认客服人格 · default
											</SelectItem>
										)}
									</SelectContent>
								</Select>
								<div className='flex items-center justify-between gap-3 text-xs text-muted-foreground'>
									<span className='truncate'>当前 ID：{selectedPersonaId}</span>
									<Link href='/personas' className='shrink-0 hover:text-foreground'>
										管理人格
									</Link>
								</div>
							</div>
						</div>
					</section>

					<section>
						<div className='mb-3'>
							<h3 className='text-sm font-semibold'>生成参数</h3>
							<p className='mt-1 text-xs text-muted-foreground'>
								控制何时转人工、读多少历史、单次生成多长。
							</p>
						</div>
						<div className='grid gap-3 lg:grid-cols-3'>
							<NumberField
								icon={<Gauge className='h-4 w-4' />}
								label='转人工阈值'
								description='低于该置信度时进入人工兜底。'
								value={v.confidence_threshold}
								onChange={(confidence_threshold) =>
									setV({ ...v, confidence_threshold })
								}
							/>
							<NumberField
								icon={<History className='h-4 w-4' />}
								label='历史消息数'
								description='生成回复前读取的对话窗口。'
								value={v.context_window}
								onChange={(context_window) => setV({ ...v, context_window })}
							/>
							<NumberField
								icon={<Route className='h-4 w-4' />}
								label='回复 token 上限'
								description='限制单次客服回复生成长度。'
								value={v.max_tokens}
								onChange={(max_tokens) => setV({ ...v, max_tokens })}
							/>
						</div>
					</section>

					<section>
						<div className='mb-3'>
							<h3 className='text-sm font-semibold'>执行方式</h3>
							<p className='mt-1 text-xs text-muted-foreground'>
								决定客服 agent 是否能调用工具，以及设备自动化如何选择节点。
							</p>
						</div>
						<div className='grid gap-3 xl:grid-cols-[minmax(0,1fr)_minmax(320px,0.75fr)]'>
							<div className='flex min-h-20 items-center justify-between gap-4 rounded-md border bg-muted/20 px-4 py-3'>
								<label className='flex min-w-0 items-start gap-3'>
									<input
										type='checkbox'
										checked={v.agent_mode}
										onChange={(e) =>
											setV({ ...v, agent_mode: e.target.checked })
										}
										className='mt-1'
									/>
									<span className='min-w-0'>
										<span className='block text-sm font-medium'>
											启用 ReAct 智能体
										</span>
										<span className='mt-1 block text-xs text-muted-foreground'>
											允许客服 agent 调用 kb_search、技能和 MCP 工具后再回复。
										</span>
									</span>
								</label>
								<div className='flex shrink-0 items-center gap-2'>
									<Label
										htmlFor='ams'
										className='m-0 text-xs text-muted-foreground'>
										最多推理步数
									</Label>
									<Input
										id='ams'
										type='number'
										value={String(v.agent_max_steps)}
										onChange={(e) =>
											setV({
												...v,
												agent_max_steps: Number(e.target.value),
											})
										}
										className='w-20 bg-background text-right'
									/>
								</div>
							</div>

							<div className='rounded-md border bg-muted/20 p-4'>
								<div className='flex items-center justify-between gap-3'>
									<div>
										<div className='text-sm font-medium'>设备 ReAct 决策模式</div>
										<p className='mt-1 text-xs text-muted-foreground'>
											控制设备自动化每一步如何选节点。
										</p>
									</div>
									<Select
										value={v.react_force_llm ? 'llm_only' : 'rule_first'}
										onValueChange={(val) =>
											setV({ ...v, react_force_llm: val === 'llm_only' })
										}>
										<SelectTrigger className='w-52 bg-background'>
											<SelectValue />
										</SelectTrigger>
										<SelectContent>
											<SelectItem value='rule_first'>规则快路径优先</SelectItem>
											<SelectItem value='llm_only'>每步走 LLM</SelectItem>
										</SelectContent>
									</Select>
								</div>
							</div>
						</div>
					</section>

					<div className='rounded-md border bg-muted/10 p-4'>
						<div className='text-sm font-medium'>设备执行约束</div>
						<p className='mt-1 text-xs leading-6 text-muted-foreground'>
							AI 仅决定要操作哪个节点，坐标始终由后端按节点 bounds
							解析；规则快路径优先会让常见 send-text 流程走缓存
							locator，命中失败再调 LLM。每步走 LLM 更适合调试新流程，但单次成本更高。
						</p>
					</div>

					<Button onClick={save} disabled={busy} className='w-full sm:w-auto'>
						<Save className='h-4 w-4' /> 保存 AI 行为
					</Button>
				</CardContent>
			</Card>
		</div>
	);
}

function SummaryTile({ label, value }: { label: string; value: string }) {
	return (
		<div className='rounded-md border bg-muted/15 px-3 py-2'>
			<div className='text-[11px] font-medium text-muted-foreground'>{label}</div>
			<div className='mt-0.5 truncate text-sm font-semibold'>{value}</div>
		</div>
	);
}

function SettingBlock({
	icon,
	title,
	description,
	children,
}: {
	icon: ReactNode;
	title: string;
	description: string;
	children: ReactNode;
}) {
	return (
		<div className='space-y-3'>
			<div className='flex items-start gap-3'>
				<div className='mt-0.5 flex h-7 w-7 shrink-0 items-center justify-center rounded-md border bg-background text-muted-foreground'>
					{icon}
				</div>
				<div className='min-w-0'>
					<h3 className='text-sm font-semibold'>{title}</h3>
					<p className='mt-1 text-xs leading-5 text-muted-foreground'>
						{description}
					</p>
				</div>
			</div>
			{children}
		</div>
	);
}

function NumberField({
	icon,
	label,
	description,
	value,
	onChange,
}: {
	icon: ReactNode;
	label: string;
	description: string;
	value: number;
	onChange: (value: number) => void;
}) {
	return (
		<div className='space-y-3 rounded-md border bg-muted/15 p-4'>
			<div className='flex items-start gap-3'>
				<div className='mt-0.5 flex h-7 w-7 shrink-0 items-center justify-center rounded-md border bg-background text-muted-foreground'>
					{icon}
				</div>
				<div className='min-w-0'>
					<Label>{label}</Label>
					<p className='mt-1 text-xs leading-5 text-muted-foreground'>
						{description}
					</p>
				</div>
			</div>
			<Input
				type='number'
				value={String(value)}
				onChange={(e) => onChange(Number(e.target.value))}
				className='bg-background/80 text-base font-medium'
			/>
		</div>
	);
}
