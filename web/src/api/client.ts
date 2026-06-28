// =============================================================================
// web/src/api/client.ts
// -----------------------------------------------------------------------------
// Responsible for: The single fetch wrapper + typed helpers the SPA uses to talk to
//                  the relay's read-only JSON API. Centralizes credentials, JSON
//                  parsing, and error shaping so screens just call getPortfolio() etc.
// Role in project: The client half of the SPA<->relay seam (docs/dashboard-api-contract.md).
// Why raw fetch (no data library): the API is a handful of GETs + two auth POSTs read
//                  once per screen. The lightest thing that works is a thin wrapper —
//                  no react-query/swr until caching/refetch complexity actually warrants
//                  it (build seams, not futures).
// Same-origin cookies: requests carry the session cookie automatically (single host in
//                  prod, Vite proxy in dev), so credentials:"same-origin" is enough.
// =============================================================================

import type {
  Comment,
  DisciplinesData,
  LoginResult,
  LogoutResult,
  Me,
  Portfolio,
  ProjectDetail,
  ReportDetail,
  SchedulingData,
  ShowcaseData,
} from "./types";

/** A failed API call. `status` lets callers branch (e.g. 401 → route to login). */
export class ApiError extends Error {
  status: number;
  constructor(status: number, message: string) {
    super(message);
    this.name = "ApiError";
    this.status = status;
  }
}

async function apiFetch<T>(path: string, init?: RequestInit): Promise<T> {
  // same-origin: the browser attaches the session cookie; we never send a bearer here.
  const res = await fetch(path, {
    credentials: "same-origin",
    headers: { Accept: "application/json", ...(init?.headers ?? {}) },
    ...init,
  });
  if (!res.ok) {
    // Surface the status so the caller can act (401 → login). The body may carry an
    // {error} string; fall back to the status text when it does not parse.
    let message = res.statusText;
    try {
      const body = await res.json();
      if (body && typeof body.error === "string") message = body.error;
    } catch {
      /* non-JSON error body — keep statusText */
    }
    throw new ApiError(res.status, message);
  }
  return (await res.json()) as T;
}

// --- Reads ------------------------------------------------------------------

export const getMe = () => apiFetch<Me>("/api/me");
export const getPortfolio = () => apiFetch<Portfolio>("/api/portfolio");
export const getProject = (name: string) =>
  apiFetch<ProjectDetail>(`/api/projects/${encodeURIComponent(name)}`);
export const getReport = (id: number | string) =>
  apiFetch<ReportDetail>(`/api/reports/${encodeURIComponent(String(id))}`);
export const getScheduling = () => apiFetch<SchedulingData>("/api/scheduling");
export const getDisciplines = () => apiFetch<DisciplinesData>("/api/disciplines");
// Public, no-login: 404s (ApiError.status === 404) when the relay's Showcase is disabled,
// which the Showcase screen turns into a clean "not available" note.
export const getShowcase = () => apiFetch<ShowcaseData>("/api/showcase");

// --- Auth (the only writes in 4a) -------------------------------------------

export const login = (key: string) =>
  apiFetch<LoginResult>("/api/login", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ key }),
  });

export const logout = () =>
  apiFetch<LogoutResult>("/api/logout", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: "{}",
  });

// Post a comment on a report (the only user-authored write). Same-origin cookie + JSON,
// like login/logout; the server attributes it to the session identity. Returns the created
// comment (the read-path shape) so the caller can append it without a refetch.
export const postComment = (reportId: number, body: string) =>
  apiFetch<Comment>(`/api/reports/${encodeURIComponent(String(reportId))}/comments`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ body }),
  });
