import { useState } from "react";
import { meetingsApi, voiceProfilesApi } from "../api/endpoints";
import { useAsync } from "../hooks/useAsync";
import { can } from "../utils/permissions";
import { Button, Empty, Failure, Icon, Loading, PageHeading, Panel } from "../components/ui";
import type { VoiceProfile } from "../types/domain";

type SpeakerView = {
  id: string;
  meeting_id?: string;
  name: string;
  role?: string;
  organization?: string;
  is_identified: boolean;
  voice_profile_id?: string;
};

export function SpeakersPage({ meetingId }: { meetingId: string }) {
  const meeting = useAsync(() => meetingsApi.get(meetingId), [meetingId]);
  const speakers = useAsync(() => meetingsApi.speakers(meetingId), [meetingId]);
  const globalProfiles = useAsync(() => voiceProfilesApi.list(), []);

  const [draft, setDraft] = useState({
    name: "",
    role: "",
    organization: "",
    voice_profile_id: "",
  });
  const [error, setError] = useState("");
  const [creating, setCreating] = useState(false);
  const [createModalOpen, setCreateModalOpen] = useState(false);
  const [deletingSpeaker, setDeletingSpeaker] = useState<SpeakerView | null>(null);

  if (meeting.loading) return <Loading />;
  if (meeting.error || !meeting.data)
    return (
      <Failure
        error={meeting.error ?? new Error("Không tìm thấy cuộc họp.")}
        retry={meeting.reload}
      />
    );

  const editable = can(meeting.data.permission, "edit");

  function selectGlobalProfile(profileId: string) {
    if (!profileId) {
      setDraft((cur) => ({ ...cur, voice_profile_id: "" }));
      return;
    }
    const found = globalProfiles.data?.find((p) => p.id === profileId);
    if (found) {
      setDraft((cur) => ({
        ...cur,
        name: found.name,
        voice_profile_id: found.id,
      }));
    }
  }

  async function create(event: React.FormEvent) {
    event.preventDefault();
    if (!draft.name.trim()) return;
    try {
      setCreating(true);
      const item = await meetingsApi.createSpeaker(meetingId, {
        name: draft.name.trim(),
        role: draft.role.trim() || undefined,
        organization: draft.organization.trim() || undefined,
        voice_profile_id: draft.voice_profile_id || undefined,
      });
      speakers.setData((items) => [...(items ?? []), item]);
      setDraft({ name: "", role: "", organization: "", voice_profile_id: "" });
      setError("");
      setCreateModalOpen(false);
    } catch (cause) {
      setError((cause as Error).message);
    } finally {
      setCreating(false);
    }
  }

  async function handleDelete(speaker: SpeakerView) {
    try {
      await meetingsApi.deleteSpeaker(speaker.id);
      speakers.setData((items) => items?.filter((item) => item.id !== speaker.id));
      setDeletingSpeaker(null);
    } catch (cause) {
      setError((cause as Error).message);
    }
  }

  return (
    <>
      <PageHeading
        title="Người nói"
        context={meeting.data.title}
        description="Danh sách người nói được thiết lập riêng cho cuộc họp này. Các mẫu giọng dùng chung đã lưu sẽ được tự động nhận diện cho cuộc họp; bạn có thể tùy chỉnh tên, vai trò và đơn vị tại đây mà không ảnh hưởng mẫu giọng gốc."
        actions={
          editable ? (
            <Button
              tone="dark"
              onClick={() => {
                setError("");
                setCreateModalOpen(true);
              }}
            >
              <Icon name="plus" /> Thêm người nói
            </Button>
          ) : undefined
        }
      />

      <div className="speakers-layout">
        <section className="speaker-grid">
          {speakers.loading && <Loading />}
          {speakers.error && <Failure error={speakers.error} retry={speakers.reload} />}
          {speakers.data?.map((speaker) => (
            <SpeakerCard
              key={speaker.id}
              speaker={speaker}
              editable={editable}
              onSaved={(next) =>
                speakers.setData((items) =>
                  items?.map((item) => (item.id === next.id ? next : item))
                )
              }
              onDeleteRequest={() => setDeletingSpeaker(speaker)}
            />
          ))}
          {speakers.data && !speakers.data.length && (
            <Empty
              title="Chưa có người nói"
              detail="Người nói sẽ được tự động nhận diện từ mẫu giọng hoặc diarization khi ghi âm/tải tệp, hoặc bạn có thể thêm người nói thủ công."
              action={
                editable ? (
                  <Button
                    tone="dark"
                    onClick={() => {
                      setError("");
                      setCreateModalOpen(true);
                    }}
                  >
                    <Icon name="plus" /> Thêm người nói
                  </Button>
                ) : undefined
              }
            />
          )}
        </section>
      </div>

      {createModalOpen && (
        <div className="dialog-backdrop">
          <section className="dialog" role="dialog" style={{ maxWidth: 480 }}>
            <button
              className="close"
              onClick={() => {
                setCreateModalOpen(false);
                setError("");
              }}
              type="button"
            >
              ×
            </button>
            <h2>Thêm người nói</h2>
            <p className="muted" style={{ margin: "4px 0 16px" }}>
              Thêm người tham gia riêng cho cuộc họp này.
            </p>
            <form onSubmit={create}>
              {globalProfiles.data && globalProfiles.data.length > 0 && (
                <label className="field">
                  <span>Chọn từ mẫu giọng dùng chung</span>
                  <select
                    value={draft.voice_profile_id}
                    onChange={(e) => selectGlobalProfile(e.target.value)}
                    style={{
                      width: "100%",
                      padding: "8px 10px",
                      borderRadius: 6,
                      border: "1px solid var(--border)",
                      fontSize: 14,
                      background: "var(--surface)",
                    }}
                  >
                    <option value="">-- Nhập thủ công hoặc chọn mẫu giọng --</option>
                    {globalProfiles.data.map((p) => (
                      <option key={p.id} value={p.id}>
                        {p.name} ({p.sample_count} lần thu)
                      </option>
                    ))}
                  </select>
                </label>
              )}

              <label className="field">
                Tên *
                <input
                  required
                  value={draft.name}
                  onChange={(event) => setDraft({ ...draft, name: event.target.value })}
                  placeholder="Ví dụ: Nguyễn Văn A"
                  autoFocus
                />
              </label>
              <label className="field">
                Vai trò / Chức danh
                <input
                  value={draft.role}
                  onChange={(event) => setDraft({ ...draft, role: event.target.value })}
                  placeholder="Ví dụ: Chủ trì cuộc họp, Thư ký..."
                />
              </label>
              <label className="field">
                Đơn vị / Tổ chức
                <input
                  value={draft.organization}
                  onChange={(event) =>
                    setDraft({ ...draft, organization: event.target.value })
                  }
                  placeholder="Ví dụ: Ban Giám đốc, Phòng Kỹ thuật..."
                />
              </label>
              {error && <div className="form-error">{error}</div>}
              <div className="dialog-actions" style={{ marginTop: 16 }}>
                <Button onClick={() => setCreateModalOpen(false)}>Hủy</Button>
                <Button type="submit" tone="dark" disabled={creating || !draft.name.trim()}>
                  {creating ? "Đang thêm…" : "Thêm người nói"}
                </Button>
              </div>
            </form>
          </section>
        </div>
      )}

      {/* Confirmation modal for delete speaker */}
      {deletingSpeaker && (
        <div
          style={{
            position: "fixed",
            inset: 0,
            background: "rgba(0,0,0,0.5)",
            display: "flex",
            alignItems: "center",
            justifyContent: "center",
            zIndex: 9999,
          }}
        >
          <div
            style={{
              background: "#fff",
              borderRadius: 12,
              padding: 24,
              maxWidth: 420,
              width: "90%",
              boxShadow: "0 20px 25px -5px rgba(0,0,0,0.2)",
            }}
          >
            <h3 style={{ margin: "0 0 12px", fontSize: 18, color: "#0f172a" }}>
              Xác nhận xóa người nói
            </h3>
            <p style={{ margin: "0 0 20px", fontSize: 14, color: "#64748b", lineHeight: 1.5 }}>
              Bạn có chắc chắn muốn xóa người nói{" "}
              <strong>"{deletingSpeaker.name}"</strong> khỏi cuộc họp này? Thao tác này
              không làm ảnh hưởng đến mẫu giọng gốc.
            </p>
            <div style={{ display: "flex", justifyContent: "flex-end", gap: 10 }}>
              <Button tone="ghost" onClick={() => setDeletingSpeaker(null)}>
                Hủy
              </Button>
              <Button tone="danger" onClick={() => void handleDelete(deletingSpeaker)}>
                Xóa người nói
              </Button>
            </div>
          </div>
        </div>
      )}
    </>
  );
}

function SpeakerCard({
  speaker,
  editable,
  onSaved,
  onDeleteRequest,
}: {
  speaker: SpeakerView;
  editable: boolean;
  onSaved: (speaker: SpeakerView) => void;
  onDeleteRequest: () => void;
}) {
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState({
    name: speaker.name,
    role: speaker.role ?? "",
    organization: speaker.organization ?? "",
  });
  const [error, setError] = useState("");
  const [playing, setPlaying] = useState(false);

  async function save() {
    try {
      onSaved(
        await meetingsApi.updateSpeaker(speaker.id, {
          name: draft.name,
          role: draft.role || undefined,
          organization: draft.organization || undefined,
        })
      );
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
      audio.addEventListener("loadedmetadata", () => {
        audio.currentTime = sample.start_ms / 1000;
        void audio.play();
      });
      audio.addEventListener("timeupdate", () => {
        if (audio.currentTime >= sample.end_ms / 1000) audio.pause();
      });
      audio.addEventListener(
        "pause",
        () => {
          URL.revokeObjectURL(source);
          setPlaying(false);
        },
        { once: true }
      );
      audio.addEventListener(
        "error",
        () => {
          URL.revokeObjectURL(source);
          setError("Không thể phát mẫu giọng.");
          setPlaying(false);
        },
        { once: true }
      );
    } catch (cause) {
      setError((cause as Error).message);
      setPlaying(false);
    }
  }

  return (
    <article className="speaker-card">
      <div className="speaker-card-top">
        <div className="speaker-avatar">{speaker.name.slice(0, 2).toUpperCase()}</div>
        <small className={speaker.is_identified ? "speaker-state identified" : "speaker-state"}>
          {speaker.is_identified ? "✓ Nhận diện từ mẫu giọng" : "Người nói cuộc họp"}
        </small>
      </div>
      {editing ? (
        <div className="speaker-card-form">
          <label className="field">
            Tên
            <input
              value={draft.name}
              onChange={(event) => setDraft({ ...draft, name: event.target.value })}
            />
          </label>
          <label className="field">
            Vai trò
            <input
              value={draft.role}
              onChange={(event) => setDraft({ ...draft, role: event.target.value })}
            />
          </label>
          <label className="field">
            Tổ chức
            <input
              value={draft.organization}
              onChange={(event) =>
                setDraft({ ...draft, organization: event.target.value })
              }
            />
          </label>
          {error && <div className="form-error">{error}</div>}
          <div style={{ display: "flex", gap: 8, marginTop: 10 }}>
            <Button tone="dark" onClick={() => void save()}>
              Lưu
            </Button>
            <Button tone="ghost" onClick={() => setEditing(false)}>
              Hủy
            </Button>
          </div>
        </div>
      ) : (
        <div className="speaker-card-info">
          <strong>{speaker.name}</strong>
          <span>{speaker.role ?? "Chưa có vai trò"}</span>
          {speaker.organization && <span>{speaker.organization}</span>}
          <div className="speaker-card-actions" style={{ marginTop: 12, display: "flex", flexWrap: "wrap", gap: 6 }}>
            <Button disabled={playing} onClick={() => void playSample()}>
              {playing ? "Đang phát…" : "Nghe giọng"}
            </Button>
            {editable && <Button onClick={() => setEditing(true)}>Sửa</Button>}
            {editable && (
              <Button tone="danger" onClick={onDeleteRequest}>
                Xóa
              </Button>
            )}
          </div>
          {error && <div className="form-error" style={{ marginTop: 8 }}>{error}</div>}
        </div>
      )}
    </article>
  );
}
