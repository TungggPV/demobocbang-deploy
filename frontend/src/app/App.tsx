import { useState, useEffect } from "react";
import { useAuth } from "../auth/AuthContext";
import { AppShell } from "../components/AppShell";
import { ErrorBoundary, Loading } from "../components/ui";
import { AuthPage } from "../pages/AuthPage";
import { ExportSharePage } from "../pages/ExportSharePage";
import { MeetingPage } from "../pages/MeetingPage";
import { MeetingsPage } from "../pages/MeetingsPage";
import { SettingsPage } from "../pages/SettingsPage";
import { SharePage } from "../pages/SharePage";
import { SpeakersPage } from "../pages/SpeakersPage";
import { SummaryPage } from "../pages/SummaryPage";
import { TranslationPage } from "../pages/TranslationPage";
import { UploadPage } from "../pages/UploadPage";
import { initLinkInterceptor } from "./router";

function route(pathname: string) {
  if (pathname === "/login") return { page: "auth" as const, mode: "login" as const };
  if (pathname === "/register") return { page: "auth" as const, mode: "register" as const };
  if (pathname === "/forgot-password") return { page: "auth" as const, mode: "forgot" as const };
  const share = pathname.match(/^\/share\/([^/]+)$/); if (share) return { page: "share" as const, token: share[1] };
  const speaker = pathname.match(/^\/meetings\/([^/]+)\/speakers$/); if (speaker) return { page: "speakers" as const, id: speaker[1] };
  const translation = pathname.match(/^\/meetings\/([^/]+)\/translation$/); if (translation) return { page: "translation" as const, id: translation[1] };
  const summary = pathname.match(/^\/meetings\/([^/]+)\/summary$/); if (summary) return { page: "summary" as const, id: summary[1] };
  const exportShare = pathname.match(/^\/meetings\/([^/]+)\/export-share$/); if (exportShare) return { page: "export-share" as const, id: exportShare[1] };
  const upload = pathname.match(/^\/meetings\/([^/]+)\/upload$/); if (upload) return { page: "upload" as const, id: upload[1] };
  if (pathname === "/meetings/create") return { page: "create" as const };
  const meeting = pathname.match(/^\/meetings\/([^/]+)$/); if (meeting) return { page: "meeting" as const, id: meeting[1] };
  if (pathname.startsWith("/voice-profiles")) return { page: "meetings" as const };
  return { page: pathname === "/settings" ? "settings" as const : "meetings" as const };
}

export function App() {
  const { user, ready } = useAuth();
  const [currentPath, setCurrentPath] = useState(() => window.location.pathname);
  const [currentSearch, setCurrentSearch] = useState(() => window.location.search);

  useEffect(() => {
    // Intercept normal anchor clicks to provide seamless SPA navigation
    const cleanupInterceptor = initLinkInterceptor();

    const handleLocationChange = () => {
      setCurrentPath(window.location.pathname);
      setCurrentSearch(window.location.search);
    };

    window.addEventListener("popstate", handleLocationChange);
    return () => {
      cleanupInterceptor?.();
      window.removeEventListener("popstate", handleLocationChange);
    };
  }, []);

  const current = route(currentPath);

  if (current.page === "share") return <SharePage token={current.token} />;
  if (!ready) {
    return (
      <div style={{ minHeight: "100vh", display: "flex", alignItems: "center", justifyContent: "center", background: "var(--bg, #08111f)" }}>
        <Loading label="Đang tải ứng dụng..." />
      </div>
    );
  }
  if (!user) return <AuthPage initialMode={current.page === "auth" ? current.mode : "login"} />;

  const isUploadMode = new URLSearchParams(currentSearch).get("mode") === "upload";

  return (
    <ErrorBoundary>
      <AppShell currentPath={currentPath}>
        {current.page === "meeting" ? (
          <MeetingPage key={current.id} meetingId={current.id} />
        ) : current.page === "upload" ? (
          <UploadPage key={current.id} meetingId={current.id} />
        ) : current.page === "speakers" ? (
          <SpeakersPage key={current.id} meetingId={current.id} />
        ) : current.page === "translation" ? (
          <TranslationPage key={current.id} meetingId={current.id} />
        ) : current.page === "summary" ? (
          <SummaryPage key={current.id} meetingId={current.id} />
        ) : current.page === "export-share" ? (
          <ExportSharePage key={current.id} meetingId={current.id} />
        ) : current.page === "settings" ? (
          <SettingsPage />
        ) : current.page === "create" ? (
          <MeetingsPage createOnly initialSource={isUploadMode ? "upload" : undefined} />
        ) : (
          <MeetingsPage />
        )}
      </AppShell>
    </ErrorBoundary>
  );
}
