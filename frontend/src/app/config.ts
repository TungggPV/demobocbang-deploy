const env = import.meta.env;
const host = typeof window !== "undefined" ? window.location.hostname : "localhost";
const isLocal =
  typeof window !== "undefined" &&
  (host === "localhost" ||
    host === "127.0.0.1" ||
    host.startsWith("192.168.") ||
    host.startsWith("10.") ||
    host.startsWith("172."));

const protocol = typeof window !== "undefined" ? window.location.protocol : "http:";

// In HTTPS mode: route through same-origin Vite proxy to prevent Mixed Content errors
// In HTTP mode: direct backend access
const isHttps = protocol === "https:";
const localApi = isHttps
  ? `${protocol}//${window.location.host}/api/v1`
  : `http://${host}:8000/api/v1`;
const localWs = isHttps
  ? `wss://${window.location.host}/api/v1`
  : `ws://${host}:8000/api/v1`;

export const config = {
  apiBaseUrl: (isLocal ? localApi : env.VITE_API_BASE_URL ?? localApi).replace(/\/$/, ""),
  wsBaseUrl: (isLocal ? localWs : env.VITE_WS_BASE_URL ?? localWs).replace(/\/$/, ""),
  maxUploadBytes: Number(env.VITE_MAX_UPLOAD_BYTES ?? 524_288_000),
  maxMediaDurationSeconds: Number(env.VITE_MAX_MEDIA_DURATION_SECONDS ?? 7_200),
};
