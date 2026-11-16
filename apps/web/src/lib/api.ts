// Same-origin JSON client for the FastAPI backend (Caddy routes /api/* to it).
// Writes carry the HE-015 double-submit CSRF token; a stale token is refreshed once.

const CSRF_COOKIE = "__Host-csrf";
const CSRF_HEADER = "X-CSRF-Token";
const WRITE = new Set(["POST", "PUT", "PATCH", "DELETE"]);

export type ApiError = { code: string; message: string; details?: Record<string, unknown> };
export type ApiResult<T> =
  | { ok: true; status: number; data: T }
  | { ok: false; status: number; error: ApiError };

function readCookie(name: string): string | null {
  const hit = document.cookie.split("; ").find((c) => c.startsWith(`${name}=`));
  return hit ? decodeURIComponent(hit.slice(name.length + 1)) : null;
}

async function freshCsrf(): Promise<string> {
  const r = await fetch("/api/v1/auth/csrf", { credentials: "same-origin" });
  return ((await r.json()) as { csrf_token: string }).csrf_token;
}

async function send(path: string, method: string, body: unknown, csrf: string | null) {
  const headers: Record<string, string> = {};
  if (body !== undefined) headers["Content-Type"] = "application/json";
  if (csrf) headers[CSRF_HEADER] = csrf;
  return fetch(path, {
    method,
    headers,
    credentials: "same-origin",
    body: body === undefined ? undefined : JSON.stringify(body),
  });
}

export async function api<T>(path: string, method = "GET", body?: unknown): Promise<ApiResult<T>> {
  const isWrite = WRITE.has(method);
  let csrf = isWrite ? (readCookie(CSRF_COOKIE) ?? (await freshCsrf())) : null;
  let r = await send(path, method, body, csrf);
  if (isWrite && r.status === 403) {
    const err = await r.clone().json().catch(() => null);
    if (err?.error?.code === "csrf_failed") {
      csrf = await freshCsrf();
      r = await send(path, method, body, csrf);
    }
  }
  if (r.status === 204) return { ok: true, status: 204, data: undefined as T };
  const json = await r.json().catch(() => null);
  if (r.ok) return { ok: true, status: r.status, data: json as T };
  const error: ApiError = json?.error ?? { code: "network", message: "Something went wrong." };
  return { ok: false, status: r.status, error };
}

/** Only same-site absolute paths; anything else falls back to "/" (no open redirect). */
export function safeNext(next: string | null): string {
  return next && next.startsWith("/") && !next.startsWith("//") && !next.startsWith("/\\")
    ? next
    : "/";
}

export type Me = {
  id: string;
  email: string;
  name: string;
  role: string;
  timezone: string;
  quiet_start?: string | null; // "HH:MM" in `timezone`; null = off
  quiet_end?: string | null;
};

/** Where a failed admin-API call should send the user (401 → sign in, missing TOTP →
 *  /admin/2fa, both returning to `here`), or null to show the error in place. */
export function adminRedirect(r: { status: number; error: ApiError }, here: string): string | null {
  const next = `?next=${encodeURIComponent(here)}`;
  if (r.status === 401) return `/login${next}`;
  if (r.error.code === "totp_required" || r.error.code === "totp_enrolment_required")
    return `/admin/2fa${next}`;
  return null;
}
