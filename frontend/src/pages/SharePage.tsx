import { sharesApi } from "../api/endpoints";
import { useState } from "react";
import { useAuth } from "../auth/AuthContext";
import { useAsync } from "../hooks/useAsync";
import { Button, Failure, Loading, statusLabel } from "../components/ui";
import { ThemeToggle } from "../components/ThemeToggle";
import { languageLabel } from "../utils/language";

/** Public links intentionally expose only the data returned by the capability-link API. */
export function SharePage({ token }: { token: string }) {
  const { user, ready } = useAuth();
  const access = useAsync(() => sharesApi.transcript(token), [token]);
  const [opening, setOpening] = useState(false);
  const [error, setError] = useState("");
  if (access.loading || !ready) return <div className="public-share"><ThemeToggle className="public-theme-toggle" /><Loading label="Đang mở…" /></div>;
  if (access.error || !access.data) return <div className="public-share"><ThemeToggle className="public-theme-toggle" /><Failure error={access.error ?? new Error("Link không hợp lệ hoặc đã hết hạn.")} retry={access.reload} /></div>;
  const { meeting, permission, transcript } = access.data;
  async function openWorkspace() { try { setOpening(true); setError(""); const redeemed = await sharesApi.redeem(token); window.location.assign(`/meetings/${redeemed.id}`); } catch (cause) { setError((cause as Error).message); setOpening(false); } }
  const next = `/share/${token}`;
  return <main className="public-share"><ThemeToggle className="public-theme-toggle" /><section className="public-share-card"><small>BÓC BĂNG</small><h1>{meeting.title}</h1>{meeting.description && <p>{meeting.description}</p>}<dl><div><dt>Trạng thái</dt><dd>{statusLabel(meeting.status)}</dd></div><div><dt>Quyền</dt><dd>{permissionLabel(permission)}</dd></div><div><dt>Ngôn ngữ</dt><dd>{languageLabel(meeting.language_mode)}</dd></div></dl>{permission !== "read" && <div className="share-access-action">{user ? <Button tone="dark" disabled={opening} onClick={() => void openWorkspace()}>{opening ? "Đang mở…" : permission === "edit" ? "Mở để chỉnh sửa" : "Mở để bình luận"}</Button> : <a className="btn btn-dark" href={`/login?next=${encodeURIComponent(next)}`}>Đăng nhập để dùng quyền được chia sẻ</a>}{error && <div className="form-error">{error}</div>}</div>}<section className="shared-transcript"><h2>Nội dung</h2>{transcript.segments.length ? transcript.segments.map((segment) => <article key={segment.logical_segment_id}><time>{formatTime(segment.start_ms)} – {formatTime(segment.end_ms)}</time><p>{segment.text}</p></article>) : <div className="notice">Chưa có nội dung.</div>}</section></section></main>;
}
function permissionLabel(permission: "read" | "comment" | "edit") { return ({ read: "Chỉ xem", comment: "Xem và bình luận", edit: "Chỉnh sửa" } as const)[permission]; }
function formatTime(milliseconds: number) { const seconds = Math.floor(milliseconds / 1000); return `${Math.floor(seconds / 60).toString().padStart(2, "0")}:${(seconds % 60).toString().padStart(2, "0")}`; }
