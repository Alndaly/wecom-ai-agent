export const API_BASE =
  process.env.NEXT_PUBLIC_API_BASE || "http://localhost:8000";
export const WS_BASE =
  process.env.NEXT_PUBLIC_WS_BASE || "ws://localhost:8000";

export function getToken(): string | null {
  if (typeof window === "undefined") return null;
  return localStorage.getItem("token");
}

export function setToken(t: string | null) {
  if (typeof window === "undefined") return;
  if (t) localStorage.setItem("token", t);
  else localStorage.removeItem("token");
}

export async function api<T = unknown>(
  path: string,
  init: RequestInit = {}
): Promise<T> {
  const token = getToken();
  const res = await fetch(`${API_BASE}${path}`, {
    ...init,
    headers: {
      "Content-Type": "application/json",
      ...(token ? { Authorization: `Bearer ${token}` } : {}),
      ...(init.headers || {}),
    },
  });
  if (!res.ok) {
    const text = await res.text();
    throw new Error(`${res.status} ${text}`);
  }
  if (res.status === 204) return undefined as T;
  return res.json() as Promise<T>;
}

/** Upload-flavored fetch: lets the browser set the multipart boundary. */
export async function apiForm<T = unknown>(
  path: string,
  form: FormData,
  init: RequestInit = {}
): Promise<T> {
  const token = getToken();
  const res = await fetch(`${API_BASE}${path}`, {
    method: "POST",
    body: form,
    ...init,
    headers: {
      ...(token ? { Authorization: `Bearer ${token}` } : {}),
      ...(init.headers || {}),
    },
  });
  if (!res.ok) {
    const text = await res.text();
    throw new Error(`${res.status} ${text}`);
  }
  return res.json() as Promise<T>;
}

export type Conversation = {
  id: number;
  robot_id: number;
  contact_id: number;
  mode: "ai" | "human" | "mixed";
  unread_count: number;
  last_message_at: string | null;
  last_message_preview: string | null;
  contact: {
    id: number;
    external_id: string;
    nickname: string;
    avatar: string | null;
    stage: string;
    tags_json: unknown[];
  };
};

export type Message = {
  id: number;
  conversation_id: number;
  direction: "in" | "out";
  sender_type: "customer" | "ai" | "human" | "system";
  sender_id: number | null;
  type: string;
  content: string;
  status: string | null;
  external_msg_id: string | null;
  task_id: number | null;
  created_at: string;
};

export type KnowledgeBase = {
  id: number;
  name: string;
  description: string;
  chunk_size: number;
  chunk_overlap: number;
  version: number;
  created_at: string;
};

export type KnowledgeDoc = {
  id: number;
  name: string;
  source: string;
  mime: string;
  status: "pending" | "processing" | "ready" | "failed";
  chunk_count: number;
  bytes: number;
  error: string | null;
  created_at: string;
  updated_at: string;
};

export type KBChunk = {
  id: number;
  doc_id: number;
  kb_id: number;
  ord: number;
  text: string;
};

export type Profile = {
  contact_id: number;
  team_id: number;
  summary: string;
  stage: string;
  updated_at: string;
};

export type Robot = {
  id: number;
  name: string;
  robot_id: string;
  status: string;
  current_page: string | null;
  last_seen_at: string | null;
  created_at: string;
};
