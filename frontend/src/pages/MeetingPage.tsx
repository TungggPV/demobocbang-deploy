import { forwardRef, useEffect, useImperativeHandle, useRef, useState } from "react";
import { useAuth } from "../auth/AuthContext";
import { MeetingSocket, type RealtimeEvent } from "../realtime/meetingSocket";
import { MicrophonePcmCapture } from "../realtime/microphonePcm";
import { capabilitiesApi, meetingsApi, type TranscriptMutation } from "../api/endpoints";
import { ApiError } from "../api/client";
import { useAsync } from "../hooks/useAsync";
import { can } from "../utils/permissions";
import { languageLabel } from "../utils/language";
import { formatTimestampInput, parseTimestampInput } from "../utils/time";
import type { Comment, Segment } from "../types/domain";
import { Button, Empty, Failure, Icon, Loading, PageHeading, Status } from "../components/ui";

type SegmentDraft = { text: string; startTime: string; endTime: string; speakerId: string };
type AudioPlayerHandle = { seek: (milliseconds: number) => void };
type MeetingTab = "info" | "comments" | "versions";
type RealtimeTranslation = { text: string; sourceLanguage: string; targetLanguage: string };
type EmailAttachmentChoice = "audio" | "transcript" | "summary" | "translation";

export function MeetingPage({ meetingId }: { meetingId: string }) {
  const { user } = useAuth();
  const meeting = useAsync(() => meetingsApi.get(meetingId), [meetingId]);
  const transcript = useAsync(() => meetingsApi.transcript(meetingId), [meetingId]);
  const media = useAsync(() => meetingsApi.media(meetingId), [meetingId]);
  const speakers = useAsync(() => meetingsApi.speakers(meetingId), [meetingId]);
  const capabilities = useAsync(capabilitiesApi.get, []);
  const comments = useAsync(() => meetingsApi.comments(meetingId), [meetingId]);
  const versions = useAsync(() => meetingsApi.versions(meetingId), [meetingId]);
  const [editing, setEditing] = useState<Segment>();
  const [draft, setDraft] = useState<SegmentDraft>({ text: "", startTime: "00:00", endTime: "00:00", speakerId: "" });
  const [notice, setNotice] = useState("");
  const [shareOpen, setShareOpen] = useState(false);
  const [renameOpen, setRenameOpen] = useState(false);
  const [pendingDelete, setPendingDelete] = useState<Segment>();
  const [pendingSplit, setPendingSplit] = useState<Segment>();
  const [mergeIds, setMergeIds] = useState<string[]>([]);
  const [pendingMerge, setPendingMerge] = useState<Segment[]>();
  const [activeSegmentId, setActiveSegmentId] = useState<string>();
  const [activeTab, setActiveTab] = useState<MeetingTab>("info");
  const [realtimeTranslations, setRealtimeTranslations] = useState<Record<string, RealtimeTranslation>>({});
  const [showRealtimeTranslations, setShowRealtimeTranslations] = useState(false);
  const player = useRef<AudioPlayerHandle>(null);

  useEffect(() => { if (editing) setDraft({ text: editing.text, startTime: formatTimestampInput(editing.start_ms), endTime: formatTimestampInput(editing.end_ms), speakerId: editing.speaker_id ?? "" }); }, [editing]);
  useEffect(() => { if (!activeSegmentId || editing) return; document.getElementById(`segment-${activeSegmentId}`)?.scrollIntoView({ block: "nearest", behavior: "smooth" }); }, [activeSegmentId, editing]);
  useEffect(() => { if (activeTab === "comments") void comments.reload(); }, [activeTab, comments.reload]);
  function applyMutation(result: TranscriptMutation, removedId?: string) {
    transcript.setData((current) => {
      if (!current) return current;
      return { ...current, version_id: result.version_id, version_no: result.version_no, segments: removedId ? current.segments.filter((segment) => segment.logical_segment_id !== removedId) : current.segments.map((segment) => segment.logical_segment_id === result.segment?.logical_segment_id ? result.segment : segment) };
    });
    void versions.reload();
  }
  async function save() {
    if (!editing) return;
    const text = draft.text.trim();
    if (!text) return setNotice("Nội dung đoạn ghi âm không được để trống.");
    try {
      const changedSpeaker = draft.speakerId || null;
      const body: { text?: string; start_ms?: number; end_ms?: number } = {};
      if (editing.text !== text) body.text = text;
      const timeChanged = draft.startTime !== formatTimestampInput(editing.start_ms) || draft.endTime !== formatTimestampInput(editing.end_ms);
      if (timeChanged) {
        const startMs = parseTimestampInput(draft.startTime); const endMs = parseTimestampInput(draft.endTime);
        if (startMs === undefined || endMs === undefined || endMs <= startMs) return setNotice("Nhập thời gian theo mm:ss hoặc hh:mm:ss; mốc kết thúc phải lớn hơn mốc bắt đầu.");
        body.start_ms = startMs; body.end_ms = endMs;
      }
      if (Object.keys(body).length) await meetingsApi.saveSegment(editing.logical_segment_id, body);
      if ((editing.speaker_id ?? null) !== changedSpeaker) await meetingsApi.setSegmentSpeaker(editing.logical_segment_id, changedSpeaker);
      if (!Object.keys(body).length && (editing.speaker_id ?? null) === changedSpeaker) { setEditing(undefined); return setNotice("Không có thay đổi mới."); }
      await transcript.reload(); await versions.reload(); setEditing(undefined); setNotice("Đã lưu thay đổi và tạo phiên bản transcript mới.");
    } catch (cause) { setNotice(meetingErrorMessage(cause)); }
  }
  async function mutate(action: () => Promise<TranscriptMutation>, success: string, removedId?: string) {
    try { const result = await action(); if (removedId) applyMutation(result, removedId); else { await transcript.reload(); await versions.reload(); } setNotice(success); }
    catch (cause) { setNotice(meetingErrorMessage(cause)); }
  }
  function toggleMerge(segment: Segment) {
    setMergeIds((current) => current.includes(segment.logical_segment_id) ? current.filter((id) => id !== segment.logical_segment_id) : [...current, segment.logical_segment_id]);
  }
  function prepareMerge() {
    const segments = transcript.data?.segments ?? [];
    const selected = segments.filter((item) => mergeIds.includes(item.logical_segment_id));
    if (selected.length < 2) return setNotice("Chọn ít nhất hai đoạn để gộp.");
    const indexes = selected.map((item) => segments.findIndex((segment) => segment.logical_segment_id === item.logical_segment_id));
    if (indexes.some((value, index) => index > 0 && value !== indexes[index - 1] + 1)) return setNotice("Các đoạn được chọn phải nằm liền nhau.");
    setPendingMerge(selected);
  }
  if (meeting.loading) return <Loading />;
  if (meeting.error || !meeting.data) return <Failure error={meeting.error ?? new Error("Không tìm thấy cuộc họp.")} retry={meeting.reload} />;
  const editable = can(meeting.data.permission, "edit"); const owner = meeting.data.permission === "owner";
  const realtimeTarget = meeting.data.language_mode === "auto" ? "auto" : meeting.data.language_mode === "vi" ? "en" : meeting.data.language_mode === "en" ? "vi" : undefined;
  const translationPairs = capabilities.data?.translation.language_pairs ?? [];
  const realtimeTranslationSupported = meeting.data.language_mode === "auto"
    ? [["vi", "en"], ["en", "vi"]].every(([source, target]) => translationPairs.some(([from, to]) => from === source && to === target))
    : Boolean(realtimeTarget && translationPairs.some(([source, target]) => source === meeting.data?.language_mode && target === realtimeTarget));
  const displayedSegments = transcript.data?.segments.slice().reverse() ?? [];
  const initialRecordingDurationMs = Math.max(
    meeting.data.duration_ms ?? 0,
    media.data?.[0]?.duration_ms ?? 0,
    ...(transcript.data?.segments.map((segment) => segment.end_ms) ?? [0]),
  );
  return <>
    <PageHeading eyebrow="BẢN GHI" title="Bản ghi" context={meeting.data.title} description={`${languageLabel(meeting.data.language_mode)} · ${meeting.data.source_type === "realtime" ? "Ghi âm" : "Tệp"}`} actions={<>{editable && <Button onClick={() => setRenameOpen(true)}>Đổi tên</Button>}{meeting.data.source_type === "upload" && <a className="btn btn-light" href={`/meetings/${meetingId}/upload`}>Tải tệp</a>}{owner && <Button tone="dark" onClick={() => setShareOpen(true)}>Chia sẻ</Button>}</>} />
    {notice && <div className="notice">{notice}</div>}
    <div className="meeting-layout"><section>
      {editable && meeting.data.source_type === "realtime" && meeting.data.status !== "completed" && capabilities.data?.stt.streaming && <RecordingPanel meetingId={meetingId} targetLanguage={realtimeTranslationSupported ? realtimeTarget : undefined} translationVisible={showRealtimeTranslations} onTranslationVisibleChange={setShowRealtimeTranslations} onTranslation={(logicalSegmentId, translation) => setRealtimeTranslations((current) => ({ ...current, [logicalSegmentId]: translation }))} onFinal={() => void transcript.reload()} onComplete={() => { void meeting.reload(); void transcript.reload(); void media.reload(); }} hasStartedBefore={transcript.data?.segments !== undefined && transcript.data.segments.length > 0} initialElapsedMs={initialRecordingDurationMs} />}
      {media.data?.[0] && <MediaPlayer ref={player} meetingId={meetingId} mediaId={media.data[0].id} filename={media.data[0].original_filename} segments={transcript.data?.segments ?? []} onActiveSegment={setActiveSegmentId} />}
      <div className="section-title"><h2>Nội dung</h2>{transcript.data && <Status>v{transcript.data.version_no}</Status>}</div>
      {mergeIds.length > 0 && <div className="merge-toolbar"><span>Đã chọn {mergeIds.length} đoạn liền nhau</span><div><Button onClick={() => setMergeIds([])}>Hủy</Button><Button tone="dark" disabled={mergeIds.length < 2} onClick={prepareMerge}>Gộp các đoạn</Button></div></div>}
      {transcript.loading && <Loading label="Đang tải transcript…" />}{transcript.error && <TranscriptState error={transcript.error} sourceType={meeting.data.source_type} uploadHref={`/meetings/${meetingId}/upload`} />}
      {displayedSegments.length ? <div className="transcript-list">{displayedSegments.map((segment) => <article className={`segment ${activeSegmentId === segment.logical_segment_id ? "is-playing" : ""}`} id={`segment-${segment.logical_segment_id}`} key={segment.logical_segment_id}>
        <div className="segment-meta"><strong>{speakers.data?.find((speaker) => speaker.id === segment.speaker_id)?.name ?? "Chưa xác định"}</strong><button className="segment-time" type="button" onClick={() => player.current?.seek(segment.start_ms)}>{formatTime(segment.start_ms)} – {formatTime(segment.end_ms)}</button>{segment.confidence !== undefined && <small>{Math.round(segment.confidence * 100)}% tin cậy</small>}</div>
        {editing?.logical_segment_id === segment.logical_segment_id ? <div className="editor"><label>Nội dung<textarea value={draft.text} onChange={(event) => setDraft({ ...draft, text: event.target.value })} /></label><div className="segment-time-fields"><label>Bắt đầu (mm:ss)<input inputMode="numeric" placeholder="01:15" value={draft.startTime} onChange={(event) => setDraft({ ...draft, startTime: event.target.value })} /></label><label>Kết thúc (mm:ss)<input inputMode="numeric" placeholder="02:30" value={draft.endTime} onChange={(event) => setDraft({ ...draft, endTime: event.target.value })} /></label></div><label>Người nói<select value={draft.speakerId} onChange={(event) => setDraft({ ...draft, speakerId: event.target.value })}><option value="">Chưa xác định</option>{speakers.data?.map((speaker) => <option key={speaker.id} value={speaker.id}>{speaker.name}</option>)}</select></label><div className="form-actions"><Button onClick={() => setEditing(undefined)}>Hủy</Button><Button tone="dark" onClick={() => void save()}>Lưu</Button></div></div> : <div className="segment-content"><p>{segment.text}</p>{showRealtimeTranslations && realtimeTranslations[segment.logical_segment_id] && <div className="realtime-translation"><small>BẢN DỊCH · {realtimeTranslations[segment.logical_segment_id].targetLanguage.toUpperCase()}</small><p>{realtimeTranslations[segment.logical_segment_id].text}</p></div>}{editable && <div className="inline-actions"><Button onClick={() => setEditing(segment)}>Sửa</Button><Button onClick={() => setPendingSplit(segment)}>Tách</Button><Button className={mergeIds.includes(segment.logical_segment_id) ? "is-selected" : ""} onClick={() => toggleMerge(segment)}>{mergeIds.includes(segment.logical_segment_id) ? "Đã chọn" : "Chọn gộp"}</Button><Button tone="danger" onClick={() => setPendingDelete(segment)}>Xóa</Button></div>}</div>}
      </article>)}</div> : null}
    </section><MeetingSidePanel activeTab={activeTab} setActiveTab={setActiveTab} meeting={meeting.data} segmentCount={transcript.data?.segments.length ?? 0} speakerCount={speakers.data?.length ?? 0} meetingId={meetingId} canComment={can(meeting.data.permission, "comment")} owner={owner} currentUserId={user?.id} comments={comments} versions={versions} /></div>
    {renameOpen && <RenameMeetingDialog title={meeting.data.title} close={() => setRenameOpen(false)} confirm={async (title) => { const updated = await meetingsApi.update(meetingId, { title }); meeting.setData(updated); setRenameOpen(false); setNotice("Đã đổi tên bản ghi."); }} />}{shareOpen && <ShareDialog meetingId={meetingId} close={() => setShareOpen(false)} />}{pendingSplit && <SplitDialog segment={pendingSplit} close={() => setPendingSplit(undefined)} confirm={(splitAt, first, second) => { const segment = pendingSplit; setPendingSplit(undefined); void mutate(() => meetingsApi.splitSegment(segment.logical_segment_id, splitAt, first, second), "Đã tách đoạn và tạo phiên bản mới."); }} />}{pendingMerge && <ConfirmDialog title={`Gộp ${pendingMerge.length} đoạn?`} detail="Các đoạn đã chọn sẽ được nối theo đúng thứ tự thời gian thành một đoạn." confirmLabel="Gộp đoạn" close={() => setPendingMerge(undefined)} confirm={() => { const selected = pendingMerge; setPendingMerge(undefined); setMergeIds([]); void mutate(() => meetingsApi.mergeSegments(selected.map((segment) => segment.logical_segment_id)), `Đã gộp ${selected.length} đoạn và tạo phiên bản mới.`); }} />}{pendingDelete && <ConfirmDialog title="Xóa đoạn transcript?" detail="Đoạn đã xóa sẽ không còn trong phiên bản transcript hiện hành." confirmLabel="Xóa đoạn" close={() => setPendingDelete(undefined)} confirm={() => { const segment = pendingDelete; setPendingDelete(undefined); void mutate(() => meetingsApi.deleteSegment(segment.logical_segment_id), "Đã xóa đoạn và tạo phiên bản mới.", segment.logical_segment_id); }} />}
  </>;
}

function TranscriptState({ error, sourceType, uploadHref }: { error: Error; sourceType: "realtime" | "upload"; uploadHref: string }) { const api = error as ApiError; const missing = api.status === 404; return <Empty title={missing ? "Chưa có transcript" : "Transcript chưa tải được"} detail={missing ? sourceType === "upload" ? "Tải tệp âm thanh để tạo transcript từ backend." : "Bắt đầu ghi âm để tạo transcript trực tiếp." : error.message} action={missing && sourceType === "upload" ? <a className="btn btn-dark" href={uploadHref}>Tải tệp âm thanh</a> : undefined} />; }
function MeetingSidePanel({ activeTab, setActiveTab, meeting, segmentCount, speakerCount, meetingId, canComment, owner, currentUserId, comments, versions }: { activeTab: MeetingTab; setActiveTab: (tab: MeetingTab) => void; meeting: { status: string; language_mode: string; duration_ms?: number }; segmentCount: number; speakerCount: number; meetingId: string; canComment: boolean; owner: boolean; currentUserId?: string; comments: { data?: Comment[]; setData: React.Dispatch<React.SetStateAction<Comment[] | undefined>>; loading: boolean; error?: Error; reload: () => Promise<void> }; versions: { data?: Array<{ id: string; version_no: number; source: string; created_at: string }>; loading: boolean; error?: Error } }) {
  const tabs: Array<[MeetingTab, string]> = [["info", "Thông tin"], ["comments", "Bình luận"], ["versions", "Phiên bản"]];
  return <aside className="meeting-side"><div className="meeting-tabs" role="tablist" aria-label="Chi tiết cuộc họp">{tabs.map(([id, label]) => <button key={id} type="button" role="tab" aria-selected={activeTab === id} className={activeTab === id ? "active" : ""} onClick={() => setActiveTab(id)}>{label}</button>)}</div><div className="meeting-tab-content" role="tabpanel">{activeTab === "info" && <MeetingContext meeting={meeting} segmentCount={segmentCount} speakerCount={speakerCount} />}{activeTab === "comments" && <CommentsPanel meetingId={meetingId} canComment={canComment} owner={owner} currentUserId={currentUserId} comments={comments} />}{activeTab === "versions" && <section className="panel"><h2>Phiên bản</h2><VersionList versions={versions} /></section>}</div></aside>;
}
const MediaPlayer = forwardRef<AudioPlayerHandle, { meetingId: string; mediaId: string; filename: string; segments: Segment[]; onActiveSegment: (id?: string) => void }>(function MediaPlayer({ meetingId, mediaId, filename, segments, onActiveSegment }, ref) {
  const audio = useRef<HTMLAudioElement>(null); const [source, setSource] = useState(""); const [error, setError] = useState(""); const [speed, setSpeed] = useState(1);
  useImperativeHandle(ref, () => ({ seek(milliseconds) { const element = audio.current; if (!element) return; element.currentTime = milliseconds / 1000; void element.play(); } }), []);
  useEffect(() => { let active = true; let objectUrl = ""; setSource(""); setError(""); void meetingsApi.streamMedia(meetingId, mediaId).then((blob) => { if (!blob.size) throw new Error("Băng ghi âm đang trống."); objectUrl = URL.createObjectURL(blob); if (active) setSource(objectUrl); else URL.revokeObjectURL(objectUrl); }).catch((cause: Error) => active && setError(cause.message)); return () => { active = false; if (objectUrl) URL.revokeObjectURL(objectUrl); }; }, [meetingId, mediaId]);
  function syncTranscript() { const milliseconds = (audio.current?.currentTime ?? 0) * 1000; const segment = segments.find((item) => item.start_ms <= milliseconds && milliseconds < item.end_ms); onActiveSegment(segment?.logical_segment_id); }
  function changeSpeed(next: number) { setSpeed(next); if (audio.current) audio.current.playbackRate = next; }
  return <section className="player"><div className="player-heading"><strong>Nghe lại băng · {filename}</strong>{source && <div className="player-tools"><label className="player-speed" title="Tốc độ phát"><Icon name="speed" /><select aria-label="Tốc độ phát" value={speed} onChange={(event) => changeSpeed(Number(event.target.value))}><option value={0.75}>0.75×</option><option value={1}>1×</option><option value={1.25}>1.25×</option><option value={1.5}>1.5×</option><option value={2}>2×</option></select></label><a className="player-icon-button" href={source} download={filename} aria-label="Tải băng ghi âm" title="Tải băng ghi âm"><Icon name="export" /></a></div>}</div>{error ? <div className="form-error">{error}</div> : source ? <audio ref={audio} controls controlsList="nodownload noplaybackrate" preload="metadata" src={source} onError={() => setError("Không thể tải băng ghi âm. Vui lòng tải lại trang và thử lại.")} onTimeUpdate={syncTranscript} onSeeking={syncTranscript} onEnded={() => onActiveSegment(undefined)}>Trình duyệt không hỗ trợ phát âm thanh.</audio> : <Loading label="Đang chuẩn bị audio…" />}</section>;
});
function MeetingContext({ meeting, segmentCount, speakerCount }: { meeting: { status: string; language_mode: string; duration_ms?: number }; segmentCount: number; speakerCount: number }) { return <section className="panel meeting-context"><h2>Thông tin phiên họp</h2><dl><div><dt>Trạng thái</dt><dd><Status value={meeting.status}>{meeting.status}</Status></dd></div><div><dt>Ngôn ngữ</dt><dd>{languageLabel(meeting.language_mode)}</dd></div><div><dt>Người nói</dt><dd>{speakerCount || "Chưa xác định"}</dd></div><div><dt>Phân đoạn</dt><dd>{segmentCount}</dd></div></dl></section>; }
function VersionList({ versions }: { versions: { data?: Array<{ id: string; version_no: number; source: string; created_at: string }>; loading: boolean; error?: Error } }) { if (versions.loading) return <Loading />; if (versions.error) return <p>Không tải được lịch sử.</p>; if (!versions.data?.length) return <p>Chưa có phiên bản transcript.</p>; return <ol className="versions">{versions.data.map((version) => <li key={version.id}>v{version.version_no} · {versionSourceLabel(version.source)} · {new Date(version.created_at).toLocaleString("vi-VN")}</li>)}</ol>; }
function ConfirmDialog({ title, detail, confirmLabel, close, confirm }: { title: string; detail: string; confirmLabel: string; close: () => void; confirm: () => void }) { return <div className="dialog-backdrop" role="presentation"><section className="dialog" role="alertdialog" aria-modal="true" aria-labelledby="confirm-title"><h2 id="confirm-title">{title}</h2><p>{detail}</p><div className="dialog-actions"><Button onClick={close}>Hủy</Button><Button tone="danger" onClick={confirm}>{confirmLabel}</Button></div></section></div>; }
function RenameMeetingDialog({ title, close, confirm }: { title: string; close: () => void; confirm: (title: string) => Promise<void> }) { const [value, setValue] = useState(title); const [error, setError] = useState(""); const [saving, setSaving] = useState(false); async function submit(event: React.FormEvent) { event.preventDefault(); const next = value.trim(); if (!next) return setError("Tên bản ghi không được để trống."); try { setSaving(true); setError(""); await confirm(next); } catch (cause) { setError((cause as Error).message); setSaving(false); } } return <div className="dialog-backdrop"><form className="dialog" onSubmit={submit}><button className="close" onClick={close} type="button">×</button><h2>Đổi tên bản ghi</h2><label>Tên bản ghi<input autoFocus maxLength={200} value={value} onChange={(event) => setValue(event.target.value)} /></label>{error && <div className="form-error">{error}</div>}<div className="dialog-actions"><Button onClick={close}>Hủy</Button><Button type="submit" tone="dark" disabled={saving}>{saving ? "Đang lưu…" : "Lưu tên"}</Button></div></form></div>; }
function SplitDialog({ segment, close, confirm }: { segment: Segment; close: () => void; confirm: (splitAt: number, first: string, second: string) => void }) {
  const words = segment.text.trim().split(/\s+/);
  const [splitIndex, setSplitIndex] = useState<number>(Math.max(1, Math.floor(words.length / 2)));

  const firstText = words.slice(0, splitIndex).join(" ").trim();
  const secondText = words.slice(splitIndex).join(" ").trim();
  
  const ratio = firstText.length / (firstText.length + secondText.length || 1);
  const estimatedSplitMs = Math.floor(segment.start_ms + (segment.end_ms - segment.start_ms) * ratio);
  const splitAtMs = Math.max(segment.start_ms + 1, Math.min(segment.end_ms - 1, estimatedSplitMs));

  return <div className="dialog-backdrop" role="presentation">
    <section className="dialog" role="dialog" aria-modal="true" aria-labelledby="split-title" style={{ width: 600 }}>
      <h2 id="split-title">Tách bằng câu</h2>
      <p>Nhấp vào chữ bên dưới để chọn vị trí tách. Đoạn sẽ được chia làm hai.</p>
      <div className="split-text">
        {words.map((word, i) => (
          <span 
            key={i} 
            onClick={() => setSplitIndex(Math.max(1, i))}
            className={`${i >= splitIndex ? "second" : ""} ${i === splitIndex ? "split-point" : ""}`}
          >
            {word}{i < words.length - 1 ? " " : ""}
          </span>
        ))}
      </div>
      <div className="split-preview">
        <div>
           <strong>Đoạn 1</strong>
           <p>{firstText}</p>
        </div>
        <div>
           <strong>Đoạn 2</strong>
           <p>{secondText}</p>
        </div>
      </div>
      <div className="dialog-actions">
        <Button onClick={close}>Hủy</Button>
        <Button tone="dark" disabled={!firstText || !secondText} onClick={() => confirm(splitAtMs, firstText, secondText)}>Tách đoạn</Button>
      </div>
    </section>
  </div>;
}
function ShareDialog({ meetingId, close }: { meetingId: string; close: () => void }) {
  const choices: Array<{ value: EmailAttachmentChoice; label: string; detail: string }> = [
    { value: "audio", label: "Băng ghi âm", detail: "Tệp âm thanh gốc" },
    { value: "transcript", label: "Nội dung bóc băng", detail: "Văn bản kèm thời gian và người nói" },
    { value: "summary", label: "Tóm tắt", detail: "Bản tóm tắt mới nhất" },
    { value: "translation", label: "Bản dịch", detail: "Bản dịch mới nhất" },
  ];
  const [email, setEmail] = useState("");
  const [permission, setPermission] = useState<"read" | "comment" | "edit">("read");
  const [attachments, setAttachments] = useState<EmailAttachmentChoice[]>(["transcript"]);
  const [message, setMessage] = useState("");
  const [success, setSuccess] = useState(false);
  const [sending, setSending] = useState(false);
  function toggleAttachment(value: EmailAttachmentChoice) {
    setAttachments((current) => current.includes(value) ? current.filter((item) => item !== value) : [...current, value]);
  }
  async function submit(event: React.FormEvent) {
    event.preventDefault();
    if (!email.trim()) return;
    try {
      setSending(true);
      setMessage("");
      await meetingsApi.addMember(meetingId, email.trim(), permission, attachments);
      setSuccess(true);
      setMessage(`Đã gửi lời mời${attachments.length ? ` cùng ${attachments.length} tệp đính kèm` : ""} qua email.`);
      setEmail("");
    } catch (cause) {
      setSuccess(false);
      setMessage(meetingErrorMessage(cause));
    } finally {
      setSending(false);
    }
  }
  return <div className="dialog-backdrop"><form className="dialog share-email-dialog" onSubmit={submit}>
    <button className="close" onClick={close} type="button">×</button>
    <h2>Chia sẻ qua email</h2>
    <p>Người nhận sẽ nhận liên kết truy cập và những tệp bạn chọn bên dưới.</p>
    <label>Email<input type="email" value={email} required onChange={(event) => setEmail(event.target.value)} /></label>
    <label>Quyền<select value={permission} onChange={(event) => setPermission(event.target.value as typeof permission)}><option value="read">Xem</option><option value="comment">Bình luận</option><option value="edit">Chỉnh sửa</option></select></label>
    <fieldset className="share-attachment-options"><legend>Đính kèm</legend>{choices.map((choice) => <label key={choice.value} className={attachments.includes(choice.value) ? "selected" : ""}><input type="checkbox" checked={attachments.includes(choice.value)} onChange={() => toggleAttachment(choice.value)} /><span><strong>{choice.label}</strong><small>{choice.detail}</small></span></label>)}</fieldset>
    <small className="muted">Băng ghi âm quá lớn có thể không gửi được qua email. Khi đó hãy bỏ chọn mục này.</small>
    {message && <div className={success ? "notice" : "form-error"}>{message}</div>}
    <Button type="submit" tone="dark" disabled={sending || !email.trim()}>{sending ? "Đang chuẩn bị và gửi…" : "Gửi email"}</Button>
  </form></div>;
}
function CommentsPanel({ meetingId, canComment, owner, currentUserId, comments }: { meetingId: string; canComment: boolean; owner: boolean; currentUserId?: string; comments: { data?: Comment[]; setData: React.Dispatch<React.SetStateAction<Comment[] | undefined>>; loading: boolean; error?: Error; reload: () => Promise<void> } }) { const [body, setBody] = useState(""); const [editing, setEditing] = useState<Comment>(); const [error, setError] = useState(""); const [submitting, setSubmitting] = useState(false); async function submit(event: React.FormEvent) { event.preventDefault(); const text = body.trim(); if (!text) return; try { setSubmitting(true); const comment = await meetingsApi.addComment(meetingId, text); comments.setData((items) => [...(items ?? []), comment]); setBody(""); setError(""); } catch (cause) { setError(meetingErrorMessage(cause)); } finally { setSubmitting(false); } } async function update() { if (!editing) return; const text = editing.body.trim(); if (!text) return setError("Bình luận không được để trống."); try { const comment = await meetingsApi.updateComment(editing.id, text); comments.setData((items) => items?.map((item) => item.id === comment.id ? comment : item)); setEditing(undefined); setError(""); } catch (cause) { setError(meetingErrorMessage(cause)); } } async function remove(id: string) { try { await meetingsApi.deleteComment(id); comments.setData((items) => items?.filter((item) => item.id !== id)); setError(""); } catch (cause) { setError(meetingErrorMessage(cause)); } } return <section className="panel"><h2>Bình luận</h2>{comments.loading && <Loading label="Đang tải bình luận…" />}{comments.error && <Failure error={comments.error} retry={comments.reload} />}{!comments.loading && !comments.error && !comments.data?.length && <p className="muted">Chưa có bình luận.</p>}{comments.data?.map((comment) => { const own = comment.author_id === currentUserId; return <div className="speaker" key={comment.id}>{editing?.id === comment.id ? <><textarea aria-label="Sửa bình luận" value={editing.body} onChange={(event) => setEditing({ ...editing, body: event.target.value })} /><Button onClick={() => void update()}>Lưu</Button><Button onClick={() => setEditing(undefined)}>Hủy</Button></> : <><strong>{comment.body}</strong><span>{new Date(comment.created_at).toLocaleString("vi-VN")}</span>{own && <Button onClick={() => setEditing(comment)}>Sửa</Button>}{(own || owner) && <Button tone="danger" onClick={() => void remove(comment.id)}>Xóa</Button>}</>}</div>; })}{canComment ? <form className="comments-form" onSubmit={submit}><textarea aria-label="Bình luận" placeholder="Viết bình luận…" value={body} required onChange={(event) => setBody(event.target.value)} /><Button type="submit" tone="dark" disabled={submitting || !body.trim()}>{submitting ? "Đang gửi…" : "Gửi bình luận"}</Button>{error && <div className="form-error">{error}</div>}</form> : <p>Bạn chỉ có quyền xem bình luận.</p>}</section>; }
function RecordingPanel({ meetingId, targetLanguage, translationVisible, onTranslationVisibleChange, onTranslation, onFinal, onComplete, hasStartedBefore, initialElapsedMs }: { meetingId: string; targetLanguage?: string; translationVisible: boolean; onTranslationVisibleChange: (visible: boolean) => void; onTranslation: (logicalSegmentId: string, translation: RealtimeTranslation) => void; onFinal: () => void; onComplete: () => void; hasStartedBefore?: boolean; initialElapsedMs: number }) {
  const { accessToken } = useAuth();
  const socket = useRef<MeetingSocket | undefined>(undefined);
  const capture = useRef<MicrophonePcmCapture | undefined>(undefined);
  const startedAt = useRef<number | undefined>(undefined);
  const elapsedBeforePause = useRef(Math.floor(initialElapsedMs / 1000));
  const translationEnabled = useRef(false);
  const finalSegmentIds = useRef(new Set<string>());
  const translatedSegmentIds = useRef(new Set<string>());
  const [status, setStatus] = useState("Sẵn sàng ghi âm");
  const [translationStatus, setTranslationStatus] = useState("");
  const [partial, setPartial] = useState("");
  const [state, setState] = useState<"idle" | "connecting" | "recording" | "paused" | "stopping" | "failed">("idle");
  const [elapsed, setElapsed] = useState(Math.floor(initialElapsedMs / 1000));

  useEffect(() => () => { void stop(true); }, []);
  useEffect(() => {
    if (state !== "idle") return;
    const initialSeconds = Math.floor(initialElapsedMs / 1000);
    elapsedBeforePause.current = initialSeconds;
    setElapsed(initialSeconds);
  }, [initialElapsedMs, state]);
  useEffect(() => {
    if (state !== "recording") return;
    const timer = window.setInterval(() => setElapsed(elapsedBeforePause.current + Math.floor((Date.now() - (startedAt.current ?? Date.now())) / 1000)), 1000);
    return () => window.clearInterval(timer);
  }, [state]);

  function missingTranslationIds() {
    return [...finalSegmentIds.current].filter((id) => !translatedSegmentIds.current.has(id));
  }

  async function releaseCapture(flush: boolean) {
    const current = capture.current;
    capture.current = undefined;
    if (current) await current.stop(flush);
  }

  function event(event: RealtimeEvent) {
    if (event.type === "session.ready") {
      void capture.current?.resume();
      setState("recording");
      setStatus("Đang ghi âm và nhận transcript trực tiếp.");
      startedAt.current = Date.now();
      if (translationEnabled.current && targetLanguage) {
        socket.current?.enableTranslation(targetLanguage, missingTranslationIds());
      }
    }
    if (event.type === "transcript.partial") setPartial(event.text);
    if (event.type === "transcript.final") {
      setPartial("");
      finalSegmentIds.current.add(event.logical_segment_id);
      onFinal();
    }
    if (event.type === "transcript.speaker_revision") onFinal();
    if (event.type === "translation.status") {
      setTranslationStatus(event.enabled ? "Đang dịch trực tiếp các đoạn đã được xác nhận." : "");
    }
    if (event.type === "translation.final") {
      translatedSegmentIds.current.add(event.logical_segment_id);
      onTranslation(event.logical_segment_id, {
        text: event.text,
        sourceLanguage: event.source_language,
        targetLanguage: event.target_language,
      });
      setTranslationStatus("Đã cập nhật bản dịch realtime.");
    }
    if (event.type === "translation.error") {
      setTranslationStatus(`Dịch realtime: ${event.message}`);
    }
    if (event.type === "meeting.status") {
      setState("stopping");
      setStatus("Đã kết thúc ghi âm.");
      onComplete();
    }
    if (event.type === "error") {
      setStatus(event.message || "Lỗi realtime.");
      if (event.retryable === false) {
        setState("failed");
        void releaseCapture(false);
      }
    }
  }

  async function start() {
    if (!accessToken) return;
    try {
      await releaseCapture(false);
      socket.current?.close();
      setState("connecting");
      setStatus("Đang xin quyền microphone và kết nối…");
      capture.current = await MicrophonePcmCapture.open((chunk) => socket.current?.sendAudio(chunk));
      socket.current = new MeetingSocket(meetingId, accessToken, event, (next) => {
        if (next === "reconnecting") {
          void capture.current?.pause();
          setStatus("Mất kết nối, đang thử kết nối lại…");
        }
        if (next === "closed") setState((current) => current === "stopping" ? current : "failed");
      });
      socket.current.connect();
    } catch (cause) {
      await releaseCapture(false);
      setState("failed");
      setStatus(cause instanceof DOMException && cause.name === "NotAllowedError" ? "Cần cấp quyền microphone để ghi âm." : (cause as Error).message);
    }
  }

  function toggleTranslation() {
    if (!targetLanguage) return;
    if (translationVisible) {
      translationEnabled.current = false;
      onTranslationVisibleChange(false);
      setTranslationStatus("");
      socket.current?.disableTranslation();
      return;
    }
    translationEnabled.current = true;
    onTranslationVisibleChange(true);
    setTranslationStatus("Đang bật dịch realtime…");
    socket.current?.enableTranslation(targetLanguage, missingTranslationIds());
  }

  async function pause() {
    await capture.current?.pause();
    socket.current?.command("recording.pause");
    elapsedBeforePause.current = elapsed;
    setState("paused");
    setStatus("Đã tạm dừng ghi âm.");
  }

  async function resume() {
    socket.current?.command("recording.resume");
    await capture.current?.resume();
    startedAt.current = Date.now();
    setState("recording");
    setStatus("Đang tiếp tục ghi âm.");
  }

  async function stop(silent = false) {
    if (silent) {
      socket.current?.close();
      await releaseCapture(false);
      return;
    }
    setState("stopping");
    setStatus("Đang hoàn tất transcript và bản dịch còn lại…");
    await releaseCapture(true);
    socket.current?.command("recording.stop");
  }

  async function finishExisting() {
    setState("stopping");
    setStatus("Đang kết thúc bản ghi…");
    try {
      await meetingsApi.complete(meetingId);
      setStatus("Đã kết thúc ghi âm.");
      onComplete();
    } catch (cause) {
      setState("failed");
      setStatus(cause instanceof Error ? cause.message : "Không thể kết thúc bản ghi.");
    }
  }

  const canToggleTranslation = state === "recording" || state === "paused" || state === "stopping";
  return <section className="panel recording-panel"><div className="recording-heading"><div><h2>Ghi âm</h2><p>{status}</p></div><strong className="recording-timer">{formatDuration(elapsed)}</strong></div>{partial && <p className="partial-segment">{partial}</p>}{translationStatus && <p className="realtime-translation-status">{translationStatus}</p>}<div className="recording-actions">{(state === "idle" || state === "failed") && <><Button tone="dark" onClick={() => void start()}>{state === "failed" ? "Kết nối lại" : (hasStartedBefore ? "Tiếp tục" : "Bắt đầu")}</Button>{hasStartedBefore && <Button tone="danger" onClick={() => void finishExisting()}>Kết thúc</Button>}</>}{state === "connecting" && <Button disabled>Đang kết nối…</Button>}{state === "recording" && <><Button onClick={() => void pause()}>Tạm dừng</Button><Button tone="danger" onClick={() => void stop()}>Kết thúc</Button></>}{state === "paused" && <><Button tone="dark" onClick={() => void resume()}>Tiếp tục</Button><Button tone="danger" onClick={() => void stop()}>Kết thúc</Button></>}{state === "stopping" && <Button disabled>Đang kết thúc…</Button>}{canToggleTranslation && <Button disabled={!targetLanguage} onClick={toggleTranslation}>{targetLanguage ? (translationVisible ? "Ẩn bản dịch" : "Dịch trực tiếp") : "Không thể dịch"}</Button>}</div></section>;
}
function meetingErrorMessage(cause: unknown) {
  if (cause instanceof ApiError) {
    if (cause.code === "SEGMENTS_NOT_MERGEABLE") return "Các đoạn được chọn phải nằm liền nhau trong nội dung hiện tại.";
    if (cause.code === "USER_NOT_FOUND") return "Email này chưa có tài khoản Bóc Băng. Người nhận cần đăng ký trước bằng đúng email.";
    if (cause.code === "EMAIL_DELIVERY_FAILED") return "Đã có lỗi khi gửi email. Vui lòng kiểm tra địa chỉ và thử lại.";
  }
  const message = cause instanceof Error ? cause.message : "Không thể thực hiện thao tác.";
  if (message === "Segments must be contiguous and have the same speaker.") return "Chỉ có thể gộp hai đoạn liền kề của cùng một người nói.";
  return message;
}

function versionSourceLabel(source: string) {
  return ({ stt: "Nhận dạng giọng nói", user_edit: "Chỉnh sửa thủ công", rollback: "Khôi phục" } as Record<string, string>)[source] ?? source;
}

function formatTime(milliseconds: number) { const seconds = Math.floor(milliseconds / 1000); return `${Math.floor(seconds / 60).toString().padStart(2, "0")}:${(seconds % 60).toString().padStart(2, "0")}`; } function formatDuration(seconds: number) { return `${Math.floor(seconds / 3600).toString().padStart(2, "0")}:${Math.floor((seconds % 3600) / 60).toString().padStart(2, "0")}:${(seconds % 60).toString().padStart(2, "0")}`; }
