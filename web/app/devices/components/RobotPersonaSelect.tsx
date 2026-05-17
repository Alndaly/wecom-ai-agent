'use client';

import {
	Select,
	SelectContent,
	SelectItem,
	SelectTrigger,
	SelectValue,
} from '@/components/ui/select';

export type PersonaSummary = {
	id: string;
	name: string;
	description: string;
};

export function RobotPersonaSelect({
	value,
	personas,
	disabled,
	onChange,
}: {
	value: string | null;
	personas: PersonaSummary[];
	disabled?: boolean;
	onChange: (value: string) => void;
}) {
	return (
		<Select
			value={value ?? '__team_default__'}
			onValueChange={onChange}
			disabled={disabled}>
			<SelectTrigger className='h-8 w-fit bg-background'>
				<SelectValue />
			</SelectTrigger>
			<SelectContent>
				<SelectItem value='__team_default__'>跟随团队默认</SelectItem>
				{personas.map((p) => (
					<SelectItem key={p.id} value={p.id}>
						{p.name} · {p.id}
					</SelectItem>
				))}
			</SelectContent>
		</Select>
	);
}
