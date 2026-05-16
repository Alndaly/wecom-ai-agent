'use client';
import { useEffect, useState } from 'react';
import Link from 'next/link';
import { Copy, Eye, FileSearch, Loader2, Plus, Trash2 } from 'lucide-react';
import { api, type Robot } from '@/lib/api';
import { formatFull } from '@/lib/datetime';
import { useWebWs } from '@/lib/ws';
import { toast } from '@/components/ui/sonner';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Badge } from '@/components/ui/badge';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import {
	Table,
	TableBody,
	TableCell,
	TableHead,
	TableHeader,
	TableRow,
} from '@/components/ui/table';
import {
	Dialog,
	DialogContent,
	DialogDescription,
	DialogFooter,
	DialogHeader,
	DialogTitle,
} from '@/components/ui/dialog';
import {
	AlertDialog,
	AlertDialogAction,
	AlertDialogCancel,
	AlertDialogContent,
	AlertDialogDescription,
	AlertDialogFooter,
	AlertDialogHeader,
	AlertDialogTitle,
} from '@/components/ui/alert-dialog';

export default function DevicesPage() {
	const [robots, setRobots] = useState<Robot[]>([]);
	const [name, setName] = useState('');
	const [creating, setCreating] = useState(false);
	const [justCreated, setJustCreated] = useState<{
		robot: Robot;
		token: string;
	} | null>(null);
	const [toDelete, setToDelete] = useState<Robot | null>(null);
	const [deleting, setDeleting] = useState(false);
	const [dumpingRobotId, setDumpingRobotId] = useState<number | null>(null);
	const [pendingDumpRequest, setPendingDumpRequest] = useState<string | null>(
		null,
	);
	const [uiDump, setUiDump] = useState<{
		request_id: string | null;
		robot_id: string;
		current_page: string;
		reason: string;
		tree: string;
		path: string;
		created_at: string;
	} | null>(null);

	async function reload() {
		setRobots(await api<Robot[]>('/robots'));
	}
	useEffect(() => {
		reload();
	}, []);

	useWebWs((event, payload) => {
		if (event === 'robot.status') {
			setRobots((prev) =>
				prev.map((r) =>
					r.robot_id === payload.robot_id
						? { ...r, status: payload.status }
						: r,
				),
			);
		}
		if (event === 'robot.updated') {
			setRobots((prev) =>
				prev.map((r) =>
					r.robot_id === payload.robot_id ? { ...r, ...payload } : r,
				),
			);
		}
		if (event === 'device.ui_dump') {
			if (
				pendingDumpRequest &&
				payload.request_id &&
				payload.request_id !== pendingDumpRequest
			)
				return;
			setUiDump(payload);
			setDumpingRobotId(null);
			setPendingDumpRequest(null);
			toast.success('UI 树已回传', {
				description: `${payload.robot_id} · ${payload.current_page ?? 'UNKNOWN'}`,
			});
		}
	});

	async function create(e: React.FormEvent) {
		e.preventDefault();
		if (!name.trim()) return;
		setCreating(true);
		try {
			const data = await api<{ robot: Robot; token: string }>('/robots', {
				method: 'POST',
				body: JSON.stringify({ name }),
			});
			setJustCreated(data);
			setName('');
			reload();
		} catch (e: any) {
			toast.error('创建失败', { description: e?.message ?? String(e) });
		} finally {
			setCreating(false);
		}
	}

	async function confirmDelete() {
		if (!toDelete) return;
		setDeleting(true);
		try {
			await api(`/robots/${toDelete.id}`, { method: 'DELETE' });
			setToDelete(null);
			reload();
			toast.success(`已删除设备 ${toDelete.name}`);
		} catch (e: any) {
			toast.error('删除失败', { description: e?.message ?? String(e) });
		} finally {
			setDeleting(false);
		}
	}

	async function requestUiDump(robot: Robot) {
		setDumpingRobotId(robot.id);
		try {
			const data = await api<{ request_id: string; dispatched: boolean }>(
				`/robots/${robot.id}/ui-dump`,
				{ method: 'POST' },
			);
			setPendingDumpRequest(data.request_id);
			toast.success('已请求采集 UI 树', {
				description: '请保持目标应用在手机前台，回传后会自动弹出。',
			});
		} catch (e: any) {
			setDumpingRobotId(null);
			toast.error('请求失败', { description: e?.message ?? String(e) });
		}
	}

	return (
		<div className='space-y-6'>
			<div className='flex items-center justify-between'>
				<h1 className='text-2xl font-semibold tracking-tight'>设备管理</h1>
			</div>

			<Card>
				<CardHeader>
					<CardTitle className='text-base'>新建设备</CardTitle>
				</CardHeader>
				<CardContent>
					<form onSubmit={create} className='flex gap-2'>
						<Input
							value={name}
							onChange={(e) => setName(e.target.value)}
							placeholder='设备名称（例如：北京-办公-01）'
							className='max-w-sm'
						/>
						<Button type='submit' disabled={creating || !name.trim()}>
							<Plus className='h-4 w-4' />
							新建
						</Button>
					</form>
				</CardContent>
			</Card>

			<Card>
				<CardHeader>
					<CardTitle className='text-base'>设备列表</CardTitle>
				</CardHeader>
				<CardContent className='p-0'>
					<Table>
						<TableHeader>
							<TableRow>
								<TableHead>ID</TableHead>
								<TableHead>名称</TableHead>
								<TableHead>robot_id</TableHead>
								<TableHead>状态</TableHead>
								<TableHead>最近上线</TableHead>
								<TableHead className='w-28'></TableHead>
							</TableRow>
						</TableHeader>
						<TableBody>
							{robots.length === 0 && (
								<TableRow>
									<TableCell
										colSpan={6}
										className='py-10 text-center text-sm text-muted-foreground'>
										暂无设备
									</TableCell>
								</TableRow>
							)}
							{robots.map((r) => (
								<TableRow key={r.id}>
									<TableCell>{r.id}</TableCell>
									<TableCell>
										<Link
											href={`/devices/${r.id}`}
											className='font-medium hover:underline'>
											{r.name}
										</Link>
									</TableCell>
									<TableCell className='font-mono text-xs'>
										{r.robot_id}
									</TableCell>
									<TableCell>
										<Badge
											variant={r.status === 'online' ? 'success' : 'secondary'}>
											{r.status}
										</Badge>
									</TableCell>
									<TableCell className='text-xs text-muted-foreground'>
										{r.last_seen_at ? formatFull(r.last_seen_at) : '—'}
									</TableCell>
									<TableCell className='text-right'>
										<div className='flex justify-end gap-1'>
											<Button
												asChild
												variant='ghost'
												size='icon'
												title='查看设备详情'>
												<Link href={`/devices/${r.id}`}>
													<Eye className='h-4 w-4' />
												</Link>
											</Button>
											<Button
												variant='ghost'
												size='icon'
												onClick={() => setToDelete(r)}>
												<Trash2 className='h-4 w-4 text-destructive' />
											</Button>
										</div>
									</TableCell>
								</TableRow>
							))}
						</TableBody>
					</Table>
				</CardContent>
			</Card>

			<Dialog
				open={!!justCreated}
				onOpenChange={(o) => !o && setJustCreated(null)}>
				<DialogContent>
					<DialogHeader>
						<DialogTitle>设备已创建</DialogTitle>
						<DialogDescription>
							token 只显示这一次，请立即复制保存到 Android
							端，离开后将无法再次查看。
						</DialogDescription>
					</DialogHeader>
					{justCreated && (
						<div className='space-y-3 text-sm'>
							<div>
								<p className='mb-1 text-xs text-muted-foreground'>robot_id</p>
								<code className='block rounded bg-muted px-2 py-1 font-mono text-xs'>
									{justCreated.robot.robot_id}
								</code>
							</div>
							<div>
								<p className='mb-1 text-xs text-muted-foreground'>token</p>
								<code className='block break-all rounded bg-muted px-2 py-1 font-mono text-xs'>
									{justCreated.token}
								</code>
							</div>
						</div>
					)}
					<DialogFooter>
						<Button
							variant='outline'
							onClick={() => {
								if (justCreated)
									navigator.clipboard
										.writeText(justCreated.token)
										.catch(() => {});
								toast.success('已复制 token 到剪贴板');
							}}>
							复制 token
						</Button>
						<Button onClick={() => setJustCreated(null)}>我已保存</Button>
					</DialogFooter>
				</DialogContent>
			</Dialog>

			<Dialog open={!!uiDump} onOpenChange={(o) => !o && setUiDump(null)}>
				<DialogContent className='max-w-5xl'>
					<DialogHeader>
						<DialogTitle>当前 UI 树</DialogTitle>
						<DialogDescription>
							{uiDump
								? `${uiDump.robot_id} · ${uiDump.current_page} · ${formatFull(uiDump.created_at)}`
								: ''}
						</DialogDescription>
					</DialogHeader>
					{uiDump && (
						<div className='space-y-3'>
							<div className='flex flex-wrap gap-x-4 gap-y-1 text-xs text-muted-foreground'>
								<span>request_id: {uiDump.request_id ?? 'manual'}</span>
								<span>reason: {uiDump.reason}</span>
								<span>saved: {uiDump.path}</span>
							</div>
							<pre className='max-h-[60vh] overflow-auto rounded-md border bg-muted p-3 font-mono text-xs leading-relaxed break-all'>
								{uiDump.tree}
							</pre>
						</div>
					)}
					<DialogFooter>
						<Button
							variant='outline'
							onClick={() => {
								if (uiDump)
									navigator.clipboard.writeText(uiDump.tree).catch(() => {});
								toast.success('已复制 UI 树');
							}}>
							<Copy className='h-4 w-4' />
							复制
						</Button>
						<Button onClick={() => setUiDump(null)}>关闭</Button>
					</DialogFooter>
				</DialogContent>
			</Dialog>

			<AlertDialog
				open={!!toDelete}
				onOpenChange={(o) => !o && !deleting && setToDelete(null)}>
				<AlertDialogContent>
					<AlertDialogHeader>
						<AlertDialogTitle>删除设备 {toDelete?.name}？</AlertDialogTitle>
						<AlertDialogDescription>
							此操作不可恢复。该设备的 token 立即失效，下次启动需要重新生成。
							历史会话和消息不会被删除。
						</AlertDialogDescription>
					</AlertDialogHeader>
					<AlertDialogFooter>
						<AlertDialogCancel disabled={deleting}>取消</AlertDialogCancel>
						<AlertDialogAction
							disabled={deleting}
							onClick={(e) => {
								e.preventDefault(); // keep dialog open while we wait for the request
								confirmDelete();
							}}
							className='bg-destructive text-destructive-foreground hover:bg-destructive/90'>
							{deleting ? '删除中…' : '确认删除'}
						</AlertDialogAction>
					</AlertDialogFooter>
				</AlertDialogContent>
			</AlertDialog>
		</div>
	);
}
