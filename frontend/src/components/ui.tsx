import { Component, type ButtonHTMLAttributes, type ErrorInfo, type ReactNode } from "react";
export type IconName = "brand" | "plus" | "upload" | "search" | "meeting" | "history" | "profile" | "summary" | "speaker" | "translate" | "export" | "download" | "back" | "settings" | "close" | "trash" | "check" | "play" | "speed" | "volume" | "moon" | "sun" | "logout" | "lock" | "chevron-up" | "edit-profile" | "eye" | "eye-off" | "sidebar" | "chevron-left" | "chevron-right" | "more" | "copy" | "edit" | "message" | "sparkles" | "arrow-up";
export function Icon({ name, label }: { name: IconName; label?: string }) { const paths: Record<IconName, ReactNode> = { brand: <><path d="M3 12h2m2-5v10m4-14v18m4-14v10m4-7v4m2-2h2" /></>, plus: <><path d="M12 5v14M5 12h14" /></>, upload: <><path d="M12 16V4m0 0 4 4m-4-4-4 4M5 15v4h14v-4" /></>, search: <><circle cx="10.5" cy="10.5" r="5.5" /><path d="m15 15 4 4" /></>, meeting: <><rect x="4" y="5" width="16" height="14" rx="2" /><path d="M8 3v4m8-4v4M7 11h10m-10 4h6" /></>, history: <><path d="M4 12a8 8 0 1 0 2.3-5.6L4 8.7M4 4v4.7h4.7M12 7v5l3 2" /></>, profile: <><circle cx="12" cy="8" r="3.5" /><path d="M4.5 20c.8-3.6 3.2-5.5 7.5-5.5s6.7 1.9 7.5 5.5" /></>, summary: <><path d="M5 4h14v16H5zM8 8h8M8 12h8M8 16h5" /></>, speaker: <><circle cx="9" cy="8" r="3" /><path d="M3.5 19c.6-3.3 2.4-5 5.5-5m5-4h6m-3-3v6" /></>, translate: <><path d="M5 5h9M9.5 3v2m-2 0c0 4-1.7 6.8-4.5 8.5m2.5-3c1.1 1.4 2.4 2.5 4 3.3M15 19l3-8 3 8m-4.7-3h3.4" /></>, export: <><path d="M12 3v11m0 0 4-4m-4 4-4-4M5 15v5h14v-5" /></>, download: <><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4m-7-5-5 5-5-5m5 5V3" /></>, back: <path d="m14 5-7 7 7 7" />, settings: <><circle cx="12" cy="12" r="3" /><path d="M12 3v2m0 14v2M21 12h-2M5 12H3m15.4-6.4-1.4 1.4M7 17l-1.4 1.4m12.8 0L17 17M7 7 5.6 5.6" /></>, close: <path d="m6 6 12 12M18 6 6 18" />, trash: <><path d="M5 7h14m-9-3h4m-7 3 1 13h8l1-13M10 11v5m4-5v5" /></>, check: <path d="m5 12 4 4L19 6" />, play: <path d="m9 6 9 6-9 6z" />, speed: <><path d="M4 16a8 8 0 1 1 16 0" /><path d="m12 16 4-5" /><circle cx="12" cy="16" r="1" /></>, volume: <><path d="M5 10h3l4-4v12l-4-4H5z" /><path d="M16 9c1 .8 1.5 1.8 1.5 3S17 14.2 16 15" /></>, moon: <path d="M20.5 15.2A8.5 8.5 0 0 1 8.8 3.5 8.5 8.5 0 1 0 20.5 15.2Z" />, sun: <><circle cx="12" cy="12" r="3.5" /><path d="M12 2v2m0 16v2M4.9 4.9l1.4 1.4m11.4 11.4 1.4 1.4M2 12h2m16 0h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4" /></>, logout: <><path d="M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4m7 14 5-5-5-5m5 5H9" /></>, lock: <><rect x="5" y="11" width="14" height="10" rx="2" /><path d="M8 11V7a4 4 0 0 1 8 0v4" /></>, "chevron-up": <path d="m18 15-6-6-6 6" />, "edit-profile": <><circle cx="9" cy="8" r="3" /><path d="M3.5 19c.6-3.3 2.4-5 5.5-5" /><path d="m16 13 3 3-4 1 1-4z" /></>, eye: <><path d="M2 12s3-7 10-7 10 7 10 7-3 7-10 7-10-7-10-7Z" /><circle cx="12" cy="12" r="3" /></>, "eye-off": <><path d="M9.88 9.88a3 3 0 1 0 4.24 4.24" /><path d="M10.73 5.08A10.43 10.43 0 0 1 12 5c7 0 10 7 10 7a13.16 13.16 0 0 1-1.67 2.68" /><path d="M6.61 6.61A13.526 13.526 0 0 0 2 12s3 7 10 7a9.74 9.74 0 0 0 5.39-1.61" /><line x1="2" y1="2" x2="22" y2="22" /></>, sidebar: <><rect x="3" y="3" width="18" height="18" rx="2" /><path d="M9 3v18" /></>, "chevron-left": <path d="m15 18-6-6 6-6" />, "chevron-right": <path d="m9 18 6-6-6-6" />, more: <><circle cx="12" cy="12" r="1.5" /><circle cx="19" cy="12" r="1.5" /><circle cx="5" cy="12" r="1.5" /></>, copy: <><rect width="13" height="13" x="9" y="9" rx="2" /><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1" /></>, edit: <><path d="M11 4H4a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-7" /><path d="M18.5 2.5a2.121 2.121 0 0 1 3 3L12 15l-4 1 1-4 9.5-9.5z" /></>, message: <path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z" />, sparkles: <><path d="m12 3-1.9 5.8a2 2 0 0 1-1.3 1.3L3 12l5.8 1.9a2 2 0 0 1 1.3 1.3L12 21l1.9-5.8a2 2 0 0 1 1.3-1.3L21 12l-5.8-1.9a2 2 0 0 1-1.3-1.3z" /><path d="M5 3v4M3 5h4" /></>, "arrow-up": <path d="M12 19V5m-7 7 7-7 7 7" /> }; return <svg className="icon" viewBox="0 0 24 24" role={label ? "img" : undefined} aria-label={label} aria-hidden={label ? undefined : true} fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round">{paths[name]}</svg>; }
export function Button({ tone = "light", className = "", children, ...props }: ButtonHTMLAttributes<HTMLButtonElement> & { tone?: "light" | "dark" | "danger" | "ghost" }) { return <button className={`btn btn-${tone} ${className}`} type="button" {...props}>{children}</button>; }
export function PageHeading({ eyebrow, title, context, description, actions }: { eyebrow?: string; title: string; context?: string; description?: string; actions?: ReactNode }) { return <div className="heading"><div className="heading-copy">{eyebrow && <small className="kicker">{eyebrow}</small>}<h1><span>{title}</span>{context && <span className="heading-record-inline" title={context}><Icon name="meeting" /><span>{context}</span></span>}</h1>{description && <div className="heading-meta"><p>{description}</p></div>}</div>{actions && <div className="heading-actions">{actions}</div>}</div>; }
export function Panel({ className = "", children }: { className?: string; children: ReactNode }) { return <section className={`panel ${className}`}>{children}</section>; }
export function Loading({ label = "Đang tải dữ liệu…" }: { label?: string }) { return <div className="state"><span className="spinner" />{label}</div>; }
export function Failure({ error, retry }: { error: Error; retry?: () => void }) { return <div className="state state-error"><strong>Có lỗi xảy ra</strong><span>{error.message}</span>{retry && <Button onClick={retry}>Thử lại</Button>}</div>; }
export function Empty({ title, detail, action }: { title: string; detail: string; action?: ReactNode }) { return <div className="state"><strong>{title}</strong><span>{detail}</span>{action}</div>; }
const statusLabels: Record<string, string> = {
  created: "Đang ghi âm", queued: "Đang chờ", recording: "Đang ghi âm", processing: "Đang xử lý", ready: "Đang ghi âm", retrying: "Đang thử lại", completed: "Hoàn tất", failed: "Thất bại",
};
export function statusLabel(value?: string) { return value ? (statusLabels[value] ?? value) : ""; }
export function Status({ children, value }: { children?: ReactNode; value?: string }) { return <span className={`status ${value ?? ""}`}>{value ? statusLabel(value) : children}</span>; }

export class ErrorBoundary extends Component<{ children: ReactNode; fallback?: ReactNode }, { hasError: boolean; error?: Error }> {
  constructor(props: { children: ReactNode; fallback?: ReactNode }) {
    super(props);
    this.state = { hasError: false };
  }
  static getDerivedStateFromError(error: Error) {
    return { hasError: true, error };
  }
  componentDidCatch(error: Error, info: ErrorInfo) {
    console.error("ErrorBoundary caught:", error, info);
  }
  render() {
    if (this.state.hasError) {
      return this.props.fallback ?? (
        <div className="state state-error" style={{ margin: "40px auto", maxWidth: 480, textAlign: "center", padding: 24 }}>
          <strong>Đã xảy ra sự cố hiển thị</strong>
          <span>{this.state.error?.message || "Không thể tải trang lúc này."}</span>
          <div style={{ marginTop: 16 }}>
            <Button tone="dark" onClick={() => window.location.reload()}>Tải lại trang</Button>
          </div>
        </div>
      );
    }
    return this.props.children;
  }
}
