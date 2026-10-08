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

/** ADR-77: the last answer per GET url, so a page shows its previous data at once when it is opened again while
 * the fresh answer loads (always re-fetched). Any write (POST) empties it, so nothing older than the last change of
 * this browser session is ever shown, and only until the fresh answer arrives. */
const lastAnswers = new Map<string, unknown>();
const MAX_CACHED = 80;
export const viewCache = {
  get: (url: string): unknown => lastAnswers.get(url),
  has: (url: string): boolean => lastAnswers.has(url),
  put: (url: string, data: unknown) => {
    lastAnswers.delete(url);
    lastAnswers.set(url, data);
    if (lastAnswers.size > MAX_CACHED) lastAnswers.delete(lastAnswers.keys().next().value as string);
  },
  clear: () => lastAnswers.clear(),
};

export const api = {
  get: <T>(url: string) => request<T>("GET", url),
  post: <T>(url: string, body: unknown = {}) => { viewCache.clear(); return request<T>("POST", url, body); },
  put: <T>(url: string, body: unknown) => request<T>("PUT", url, body),                  // ADR-111: saved chart state
};
