import { config } from "../app/config";
import type { ApiErrorShape } from "../types/domain";

export class ApiError extends Error {
  constructor(public status: number, public code?: string, message = "Đã có lỗi xảy ra.") { super(message); }
}

let getAccessToken: () => string | null = () => null;
let onUnauthorized: () => Promise<boolean> = async () => false;
let refreshInFlight: Promise<boolean> | undefined;
export function configureApiAuth(token: () => string | null, refresh: () => Promise<boolean>) { getAccessToken = token; onUnauthorized = refresh; }

function refreshAccessToken(): Promise<boolean> {
  if (!refreshInFlight) {
    refreshInFlight = onUnauthorized().finally(() => { refreshInFlight = undefined; });
  }
  return refreshInFlight;
}

async function request<T>(path: string, init: RequestInit = {}, retry = true, responseType: "json" | "blob" = "json"): Promise<T> {
  // Auth endpoints must not try to refresh themselves. Otherwise an expired
  // refresh token can recursively call /auth/refresh after its own 401.
  const isPublicAuthRequest = path.startsWith("/auth/") && !path.startsWith("/auth/me");
  const token = isPublicAuthRequest ? null : getAccessToken();
  const headers = new Headers(init.headers);
  if (token) headers.set("Authorization", `Bearer ${token}`);
  if (init.body && !(init.body instanceof FormData)) headers.set("Content-Type", "application/json");
  const fetchOptions = { ...init, headers, credentials: "include" as const };
  let response: Response;
  try {
    response = await fetch(`${config.apiBaseUrl}${path}`, fetchOptions);
  } catch (cause) {
    if (init.signal?.aborted) throw cause;
    const method = init.method?.toUpperCase() ?? "GET";
    if (method !== "GET") {
      throw new ApiError(0, "NETWORK_ERROR", "Không thể kết nối máy chủ. Vui lòng thử lại.");
    }
    await new Promise((resolve) => window.setTimeout(resolve, 350));
    try {
      response = await fetch(`${config.apiBaseUrl}${path}`, fetchOptions);
    } catch (retryCause) {
      if (init.signal?.aborted) throw retryCause;
      throw new ApiError(0, "NETWORK_ERROR", "Không thể kết nối máy chủ. Vui lòng thử lại.");
    }
  }
  if (response.status === 401 && !isPublicAuthRequest && retry && await refreshAccessToken()) return request<T>(path, init, false, responseType);
  if (!response.ok) {
    const body = await response.json().catch(() => ({})) as ApiErrorShape;
    throw new ApiError(response.status, body.code, body.message ?? body.detail ?? "Không thể hoàn tất yêu cầu.");
  }
  if (response.status === 204) return undefined as T;
  return (responseType === "blob" ? response.blob() : response.json()) as Promise<T>;
}

export const apiClient = {
  get: <T>(path: string, init: RequestInit = {}) => request<T>(path, init),
  post: <T>(path: string, body?: unknown) => request<T>(path, { method: "POST", body: body instanceof FormData ? body : JSON.stringify(body) }),
  patch: <T>(path: string, body: unknown) => request<T>(path, { method: "PATCH", body: JSON.stringify(body) }),
  delete: <T>(path: string) => request<T>(path, { method: "DELETE" }),
  getBlob: (path: string) => request<Blob>(path, {}, true, "blob"),
};
