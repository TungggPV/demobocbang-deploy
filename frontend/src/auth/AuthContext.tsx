import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState } from "react";
import { ApiError, configureApiAuth } from "../api/client";
import { authApi } from "../api/endpoints";
import { config } from "../app/config";
import type { AuthSession, User } from "../types/domain";

type AuthContextValue = {
  user: User | null; ready: boolean; login(email: string, password: string): Promise<void>;
  register(email: string, password: string, name: string): Promise<void>; verifyOtp(email: string, code: string): Promise<void>;
  logout(): Promise<void>; googleLogin(): Promise<void>; updateProfile(fullName: string): Promise<void>; accessToken: string | null;
};
const AuthContext = createContext<AuthContextValue | undefined>(undefined);
const KEY = "bocbang.session";

function parseSession(raw: string | null): AuthSession | null {
  try {
    const value = JSON.parse(raw ?? "null") as Partial<AuthSession> | null;
    return value?.access_token && value.user ? value as AuthSession : null;
  } catch {
    return null;
  }
}

function readSession(): AuthSession | null {
  const persisted = parseSession(localStorage.getItem(KEY));
  if (persisted) return persisted;

  // Migrate users who were signed in before sessions became persistent.
  const legacy = parseSession(sessionStorage.getItem(KEY));
  if (legacy) {
    localStorage.setItem(KEY, JSON.stringify(legacy));
    sessionStorage.removeItem(KEY);
  }
  return legacy;
}

function storeSession(next: AuthSession | null) {
  sessionStorage.removeItem(KEY);
  if (next) localStorage.setItem(KEY, JSON.stringify(next));
  else localStorage.removeItem(KEY);
}

function consumeOAuthRefreshToken(): string | null {
  const refreshToken = new URLSearchParams(window.location.hash.slice(1)).get("refresh_token");
  if (refreshToken) window.history.replaceState(null, "", window.location.pathname + window.location.search);
  return refreshToken;
}
export function AuthProvider({ children }: { children: React.ReactNode }) {
  const [session, setSession] = useState<AuthSession | null>(readSession);
  const sessionRef = useRef(session);
  const [oauthRefreshToken] = useState<string | null>(consumeOAuthRefreshToken);
  const [ready, setReady] = useState(!oauthRefreshToken);
  const persist = useCallback((next: AuthSession | null) => {
    sessionRef.current = next;
    setSession(next);
    storeSession(next);
  }, []);
  const refresh = useCallback(async () => {
    try {
      const next = await authApi.refresh(sessionRef.current?.refresh_token);
      persist(next);
      return true;
    } catch (error) {
      // Only end the session when the refresh token is actually rejected.
      // A temporary network/backend error must not sign the user out.
      if (error instanceof ApiError && (error.status === 401 || error.status === 403)) persist(null);
      return false;
    }
  }, [persist]);
  useEffect(() => {
    const syncSession = (event: StorageEvent) => {
      if (event.storageArea !== localStorage || event.key !== KEY) return;
      const next = parseSession(event.newValue);
      sessionRef.current = next;
      setSession(next);
    };
    window.addEventListener("storage", syncSession);
    return () => window.removeEventListener("storage", syncSession);
  }, []);
  useEffect(() => { if (oauthRefreshToken) void authApi.refresh(oauthRefreshToken).then((next) => { persist(next); const target = sessionStorage.getItem("bocbang.auth.next"); if (target?.startsWith("/") && !target.startsWith("//")) { sessionStorage.removeItem("bocbang.auth.next"); window.location.assign(target); } }).catch(() => persist(null)).finally(() => setReady(true)); }, [oauthRefreshToken, persist]);
  // Configure during render so child effects cannot issue an unauthenticated
  // request before this provider's useEffect has run.
  configureApiAuth(() => sessionRef.current?.access_token ?? null, refresh);
  const value = useMemo<AuthContextValue>(() => ({
    user: session?.user ?? null, ready, accessToken: session?.access_token ?? null,
    login: async (email, password) => persist(await authApi.login(email, password)),
    register: async (email, password, name) => { await authApi.register(email, password, name); },
    verifyOtp: async (email, code) => persist(await authApi.verifyOtp(email, code)),
    logout: async () => { try { await authApi.logout(); } finally { persist(null); } },
    updateProfile: async (fullName) => { if (!sessionRef.current) return; const user = await authApi.updateProfile(fullName); persist({ ...sessionRef.current, user }); },
    googleLogin: async () => {
      window.location.assign(`${config.apiBaseUrl}${authApi.googleUrl()}`);
    },
  }), [persist, ready, session?.user]);
  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}
export function useAuth() { const value = useContext(AuthContext); if (!value) throw new Error("useAuth must be inside AuthProvider"); return value; }
