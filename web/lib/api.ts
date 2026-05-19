export const API_BASE =
  process.env.NEXT_PUBLIC_API_BASE || "http://localhost:8000";
export const WS_BASE =
  process.env.NEXT_PUBLIC_WS_BASE || "ws://localhost:8000";

type AuthTokens = {
  access_token: string;
  refresh_token?: string | null;
};

let refreshPromise: Promise<string | null> | null = null;

export function getToken(): string | null {
  if (typeof window === "undefined") return null;
  return localStorage.getItem("token");
}

export function getRefreshToken(): string | null {
  if (typeof window === "undefined") return null;
  return localStorage.getItem("refresh_token");
}

export function setToken(t: string | null) {
  if (typeof window === "undefined") return;
  if (t) localStorage.setItem("token", t);
  else {
    localStorage.removeItem("token");
    localStorage.removeItem("refresh_token");
  }
}

export function setAuthTokens(tokens: AuthTokens | null) {
  if (typeof window === "undefined") return;
  if (!tokens) {
    setToken(null);
    return;
  }
  localStorage.setItem("token", tokens.access_token);
  if (tokens.refresh_token) localStorage.setItem("refresh_token", tokens.refresh_token);
}

export async function refreshAccessToken(): Promise<string | null> {
  if (refreshPromise) return refreshPromise;

  refreshPromise = (async () => {
    const refreshToken = getRefreshToken();
    if (!refreshToken) return null;

    const res = await fetch(`${API_BASE}/auth/refresh`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ refresh_token: refreshToken }),
    });
    if (!res.ok) {
      setToken(null);
      return null;
    }
    const tokens = (await res.json()) as AuthTokens;
    setAuthTokens(tokens);
    return tokens.access_token;
  })().finally(() => {
    refreshPromise = null;
  });

  return refreshPromise;
}

function buildJsonHeaders(init: RequestInit, token: string | null): HeadersInit {
  return {
    "Content-Type": "application/json",
    ...(token ? { Authorization: `Bearer ${token}` } : {}),
    ...(init.headers || {}),
  };
}

function buildFormHeaders(init: RequestInit, token: string | null): HeadersInit {
  return {
    ...(token ? { Authorization: `Bearer ${token}` } : {}),
    ...(init.headers || {}),
  };
}

async function parseResponse<T>(res: Response): Promise<T> {
  if (res.status === 204) return undefined as T;
  return res.json() as Promise<T>;
}

async function throwResponseError(res: Response): Promise<never> {
  const text = await res.text();
  throw new Error(`${res.status} ${text}`);
}

export async function api<T = unknown>(
  path: string,
  init: RequestInit = {}
): Promise<T> {
  let token = getToken();
  let res = await fetch(`${API_BASE}${path}`, {
    ...init,
    headers: buildJsonHeaders(init, token),
  });

  if (res.status === 401 && path !== "/auth/login" && path !== "/auth/refresh") {
    token = await refreshAccessToken();
    if (token) {
      res = await fetch(`${API_BASE}${path}`, {
        ...init,
        headers: buildJsonHeaders(init, token),
      });
    }
  }

  if (!res.ok) {
    await throwResponseError(res);
  }
  return parseResponse<T>(res);
}

/** Upload-flavored fetch: lets the browser set the multipart boundary. */
export async function apiForm<T = unknown>(
  path: string,
  form: FormData,
  init: RequestInit = {}
): Promise<T> {
  let token = getToken();
  let res = await fetch(`${API_BASE}${path}`, {
    method: "POST",
    body: form,
    ...init,
    headers: buildFormHeaders(init, token),
  });

  if (res.status === 401) {
    token = await refreshAccessToken();
    if (token) {
      res = await fetch(`${API_BASE}${path}`, {
        method: "POST",
        body: form,
        ...init,
        headers: buildFormHeaders(init, token),
      });
    }
  }

  if (!res.ok) {
    await throwResponseError(res);
  }
  return parseResponse<T>(res);
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
  media_json: Record<string, unknown> | null;
  status: string | null;
  feedback_status: string | null;
  feedback_trace_id: string | null;
  feedback_at: string | null;
  feedback_reply_task_ids: number[] | null;
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

export type RobotTaskLog = {
  id: number;
  robot_id: number;
  task_id: number | null;
  level: string;
  message: string;
  created_at: string;
};

export type RobotQueueItem = {
  kind: string;
  task_id: number;
  title: string;
  detail: string | null;
  priority: number;
  waited_ms: number;
  cancellable?: boolean;
  warning?: string | null;
};

export type RobotQueueSnapshot = {
  robot_id: string;
  running: RobotQueueItem | null;
  depth: number;
  pending: RobotQueueItem[];
};

export type Robot = {
  id: number;
  name: string;
  robot_id: string;
  status: string;
  current_page: string | null;
  device_type: string | null;
  device_name: string | null;
  manufacturer: string | null;
  model: string | null;
  android_version: string | null;
  sdk_int: number | null;
  app_version: string | null;
  screen_width: number | null;
  screen_height: number | null;
  last_seen_at: string | null;
  // null means "no per-device override; fall through to team default"
  persona_id: string | null;
  created_at: string;
};
