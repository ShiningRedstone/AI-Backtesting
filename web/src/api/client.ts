import type { Issue } from "./types";

/** Every failure the UI can show: backend errors keep their kind/issues; network failures become "unavailable". */
export class ApiError extends Error {
  constructor(public status: number, public kind: string, message: string,
              public issues: Issue[] = [], public reason = "", public details = "") {
    super(message);
  }
}

async function request<T>(method: string, url: string, body?: unknown): Promise<T> {
  let res: Response;
  try {
    res = await fetch(url, {
      method, headers: body === undefined ? {} : { "Content-Type": "application/json" },
      body: body === undefined ? undefined : JSON.stringify(body),
    });
  } catch (e) {
    throw new ApiError(0, "unavailable", "The Munyun Lab backend is not reachable. Is `python -m edgelab.web` running?",
      [], "", String(e));
  }
  let data: unknown = null;
  try { data = await res.json(); } catch { /* non-JSON */ }
  if (!res.ok) {
    const err = (data as { error?: { kind?: string; message?: string; issues?: Issue[]; reason?: string; details?: string } } | null)?.error;
    throw new ApiError(res.status, err?.kind ?? "http", err?.message ?? `Request failed (${res.status})`,
      err?.issues ?? [], err?.reason ?? "", err?.details ?? "");
  }
  return data as T;
}

export const api = {
  get: <T>(url: string) => request<T>("GET", url),
  post: <T>(url: string, body: unknown = {}) => request<T>("POST", url, body),
};
