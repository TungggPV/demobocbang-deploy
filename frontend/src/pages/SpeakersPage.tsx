import { useState } from "react";
import { meetingsApi } from "../api/endpoints";
import { useAsync } from "../hooks/useAsync";
import { can } from "../utils/permissions";
import { Button, Empty, Failure, Loading, PageHeading, Panel } from "../components/ui";

type SpeakerView = { id: string; meeting_id?: string; name: string; role?: string; organization?: string; is_identified: boolean };

export function SpeakersPage({ meetingId }: { meetingId: string }) {
  const meeting = useAsync(() => meetingsApi.get(meetingId), [meetingId]);
  const speakers = useAsync(() => meetingsApi.speakers(meetingId), [meetingId]);
  const [draft, setDraft] = useState({ name: "", role: "", organization: "" });
  const [error, setError] = useState("");
  const [creating, setCreating] = useState(false);

  if (meeting.loading) return <Loading />;
  if (meeting.error || !meeting.data) return <Failure error={meeting.error ?? new Error("Không tìm thấy cuộc họp.")} retry={meeting.reload} />;
  const editable = can(meeting.data.permission, "edit");

  async function create(event: React.FormEvent) {
    event.preventDefault();
    if (!draft.name.trim()) return;
    try {
      setCreating(true);
      const item = await meetingsApi.createSpeaker(meetingId, { name: draft.name.trim(), role: draft.role.trim() || undefined, organization: draft.organization.trim() || undefined });
      speakers.setData((items) => [...(items ?? []), item]);
      setDraft({ name: "", role: "", organization: "" });
      setError("");
    } catch (cause) {
      setError((cause as Error).message);
    } finally {
      setCreating(false);
    }
  }

  return <>
    <PageHeading title="Người nói" context={meeting.data.title} />
    <div className="speakers-layout">
      <section className="speaker-grid">
        {speakers.loading && <Loading />}
        {speakers.error && <Failure error={speakers.error} retry={speakers.reload} />}
        {speakers.data?.map((speaker) => <SpeakerCard key={speaker.id} speaker={speaker} editable={editable} onSaved={(next) => speakers.setData((items) => items?.map((item) => item.id === next.id ? next : item))} />)}
        {speakers.data && !speakers.data.length && <Empty title="Chưa có người nói" detail="Người nói sẽ được tạo từ diarization hoặc bạn có thể thêm thủ công." />}
      </section>
      {editable && <Panel className="speaker-create"><h2>Thêm người nói</h2><form onSubmit={create}>
        <label className="field">Tên<input required value={draft.name} onChange={(event) => setDraft({ ...draft, name: event.target.value })} /></label>
        <label className="field">Vai trò<input value={draft.role} onChange={(event) => setDraft({ ...draft, role: event.target.value })} /></label>
        <label className="field">Tổ chức<input value={draft.organization} onChange={(event) => setDraft({ ...draft, organization: event.target.value })} /></label>
        {error && <div className="form-error">{error}</div>}
        <Button type="submit" tone="dark" disabled={creating || !draft.name.trim()}>{creating ? "Đang thêm…" : "Thêm người nói"}</Button>
      </form></Panel>}
    </div>
  </>;
}

function SpeakerCard({ speaker, editable, onSaved }: { speaker: SpeakerView; editable: boolean; onSaved: (speaker: SpeakerView) => void }) {
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState({ name: speaker.name, role: speaker.role ?? "", organization: speaker.organization ?? "" });
  const [error, setError] = useState("");
  const [playing, setPlaying] = useState(false);

  async function save() {
    try {
      onSaved(await meetingsApi.updateSpeaker(speaker.id, { name: draft.name, role: draft.role || undefined, organization: draft.organization || undefined }));
      setEditing(false);
    } catch (cause) {
      setError((cause as Error).message);
    }
  }

  async function playSample() {
    if (!speaker.meeting_id) return setError("Không xác định được cuộc họp của người nói.");
    try {
      setError("");
      setPlaying(true);
      const sample = await meetingsApi.speakerSample(speaker.id);
      const blob = await meetingsApi.streamMedia(speaker.meeting_id, sample.media_id);
      const source = URL.createObjectURL(blob);
      const audio = new Audio(source);
      audio.addEventListener("loadedmetadata", () => { audio.currentTime = sample.start_ms / 1000; void audio.play(); });
      audio.addEventListener("timeupdate", () => { if (audio.currentTime >= sample.end_ms / 1000) audio.pause(); });
      audio.addEventListener("pause", () => { URL.revokeObjectURL(source); setPlaying(false); }, { once: true });
      audio.addEventListener("error", () => { URL.revokeObjectURL(source); setError("Không thể phát mẫu giọng."); setPlaying(false); }, { once: true });
    } catch (cause) {
      setError((cause as Error).message);
      setPlaying(false);
    }
  }

  return <article className="speaker-card">
    <div className="speaker-card-top"><div className="speaker-avatar">{speaker.name.slice(0, 2).toUpperCase()}</div><small className={speaker.is_identified ? "speaker-state identified" : "speaker-state"}>{speaker.is_identified ? "Đã đặt tên" : "Chưa xác minh"}</small></div>
    {editing ? <div className="speaker-card-form">
      <label className="field">Tên<input value={draft.name} onChange={(event) => setDraft({ ...draft, name: event.target.value })} /></label>
      <label className="field">Vai trò<input value={draft.role} onChange={(event) => setDraft({ ...draft, role: event.target.value })} /></label>
      <label className="field">Tổ chức<input value={draft.organization} onChange={(event) => setDraft({ ...draft, organization: event.target.value })} /></label>
      {error && <div className="form-error">{error}</div>}
      <Button tone="dark" onClick={() => void save()}>Lưu</Button><Button tone="ghost" onClick={() => setEditing(false)}>Hủy</Button>
    </div> : <div className="speaker-card-info">
      <strong>{speaker.name}</strong><span>{speaker.role ?? "Chưa có vai trò"}</span>{speaker.organization && <span>{speaker.organization}</span>}
      <div className="speaker-card-actions"><Button disabled={playing} onClick={() => void playSample()}>{playing ? "Đang phát…" : "Nghe giọng"}</Button>{editable && <Button onClick={() => setEditing(true)}>Sửa</Button>}</div>
      {error && <div className="form-error">{error}</div>}
    </div>}
  </article>;
}
