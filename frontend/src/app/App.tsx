import { useAuth } from "../auth/AuthContext";
import { AppShell } from "../components/AppShell";
import { ErrorBoundary } from "../components/ui";
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
import { VoiceProfilesPage } from "../pages/VoiceProfilesPage";

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
  if (pathname.startsWith("/voice-profiles")) return { page: "voice-profiles" as const };
  return { page: pathname === "/settings" ? "settings" as const : "meetings" as const };
}

export function App() {
  const { user, ready } = useAuth();
  const current = route(window.location.pathname);
  if (current.page === "share") return <SharePage token={current.token} />;
  if (!ready) return (
    <div className="boot">
      <div className="boot-logo"><svg className="icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" style={{width:28,height:28}}><path d="M3 12h2m2-5v10m4-14v18m4-14v10m4-7v4m2-2h2" /></svg></div>
      <span>Đang tải…</span>
    </div>
  );
  if (!user) return <AuthPage initialMode={current.page === "auth" ? current.mode : "login"} />;
  return (
    <ErrorBoundary>
      <AppShell>{current.page === "voice-profiles" ? <VoiceProfilesPage /> : current.page === "meeting" ? <MeetingPage meetingId={current.id} /> : current.page === "upload" ? <UploadPage meetingId={current.id} /> : current.page === "speakers" ? <SpeakersPage meetingId={current.id} /> : current.page === "translation" ? <TranslationPage meetingId={current.id} /> : current.page === "summary" ? <SummaryPage meetingId={current.id} /> : current.page === "export-share" ? <ExportSharePage meetingId={current.id} /> : current.page === "settings" ? <SettingsPage /> : current.page === "create" ? <MeetingsPage createOnly initialSource={new URLSearchParams(window.location.search).get("mode") === "upload" ? "upload" : undefined} /> : <MeetingsPage />}</AppShell>
    </ErrorBoundary>
  );
}
