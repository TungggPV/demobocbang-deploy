import { useEffect, useState } from "react";
import { meetingsApi } from "../api/endpoints";
import { useAsync } from "../hooks/useAsync";
import type { Meeting, MeetingStatus } from "../types/domain";
import { Button, Empty, Failure, Icon, Loading, PageHeading, Status, statusLabel } from "../components/ui";

const statuses: Array<{ value: "" | MeetingStatus; label: string }> = [
  { value: "", label: "Tất cả trạng thái" },
  { value: "created", label: "Chưa bắt đầu" },
  { value: "recording", label: "Đang ghi âm" },
  { value: "processing", label: "Đang xử lý" },
  { value: "completed", label: "Hoàn tất" },
  { value: "failed", label: "Thất bại" },
];

export function MeetingsPage({ createOnly = false, initialSource }: { createOnly?: boolean; initialSource?: "realtime" | "upload" }) {
  return createOnly ? <CreateMeeting initialSource={initialSource} /> : <MeetingsOverview />;
}

function MeetingsOverview() {
  const [query, setQuery] = useState("");
  const [search, setSearch] = useState("");
  const [status, setStatus] = useState<"" | MeetingStatus>("");
  const [source, setSource] = useState<"" | Meeting["source_type"]>("");
  const [mediaMeeting, setMediaMeeting] = useState<Meeting>();
  const [pendingDelete, setPendingDelete] = useState<Meeting>();
  const statusInventory = useAsync(() => meetingsApi.list({ limit: 250 }), []);

  useEffect(() => {
    const timer = window.setTimeout(() => setSearch(query.trim()), 300);
    return () => window.clearTimeout(timer);
  }, [query]);

  const meetings = useAsync(
    () => meetingsApi.list({ limit: 50, q: search || undefined, status: status || undefined, source_type: source || undefined }),
    [search, status, source],
  );
  const filtering = Boolean(query || status || source);
  const visibleStatuses = statuses.filter(
    (item) => !item.value || statusInventory.data?.some((meeting) => meeting.status === item.value),
  );

  async function deleteMeeting() {
    if (!pendingDelete) return;
    try {
      await meetingsApi.remove(pendingDelete.id);
      setPendingDelete(undefined);
      await meetings.reload();
    } catch (cause) {
      setPendingDelete(undefined);
      window.alert((cause as Error).message);
    }
  }

  return <>
    <PageHeading
      eyebrow="CUỘC HỌP"
      title="Cuộc họp"
      actions={<div className="quick-actions">
        <a className="btn btn-dark" href="/meetings/create"><Icon name="meeting" /> Ghi âm mới</a>
        <a className="btn btn-light" href="/meetings/create?mode=upload"><Icon name="upload" /> Tải file lên</a>
      </div>}
    />
    <div className="history-tools meeting-tools">
      <label><Icon name="search" /><input value={query} onChange={(event) => setQuery(event.target.value)} placeholder="Tìm cuộc họp..." /></label>
      <select aria-label="Lọc trạng thái" value={status} onChange={(event) => setStatus(event.target.value as typeof status)}>{visibleStatuses.map((item) => <option key={item.value} value={item.value}>{item.label}</option>)}</select>
      <select aria-label="Lọc nguồn âm thanh" value={source} onChange={(event) => setSource(event.target.value as typeof source)}><option value="">Tất cả nguồn</option><option value="realtime">Ghi âm</option><option value="upload">Tải tệp</option></select>
    </div>
    {meetings.loading && <Loading />}
    {meetings.error && <Failure error={meetings.error} retry={meetings.reload} />}
    {meetings.data && meetings.data.length === 0 && (
      filtering
        ? <Empty title="Không tìm thấy" detail="Thử từ khóa hoặc bộ lọc khác." />
        : <div className="onboarding">
            <div className="onboarding-icon"><Icon name="meeting" /></div>
            <h2>Bắt đầu bóc băng ngay!</h2>
            <p>Ghi âm trực tiếp hoặc tải file âm thanh lên — AI sẽ tự động tạo transcript và tóm tắt cho bạn.</p>
            <div className="onboarding-actions">
              <a className="btn btn-dark" href="/meetings/create">
                <Icon name="meeting" /> Ghi âm cuộc họp
              </a>
              <a className="btn btn-light" href="/meetings/create?mode=upload">
                <Icon name="upload" /> Tải file âm thanh
              </a>
            </div>
          </div>
    )}
    {meetings.data && meetings.data.length > 0 && <div className="meeting-list">{meetings.data.map((meeting) => <MeetingRow key={meeting.id} meeting={meeting} onMedia={() => setMediaMeeting(meeting)} onDelete={() => setPendingDelete(meeting)} />)}</div>}
    {mediaMeeting && <MediaDialog meeting={mediaMeeting} close={() => setMediaMeeting(undefined)} />}
    {pendingDelete && <ConfirmDialog title="Xóa toàn bộ cuộc họp?" detail={`Tất cả transcript, tệp media, export và dữ liệu liên quan của “${pendingDelete.title}” sẽ bị xóa.`} confirmLabel="Xóa cuộc họp" close={() => setPendingDelete(undefined)} confirm={() => void deleteMeeting()} />}
  </>;
}

function CreateMeeting({ initialSource }: { initialSource?: "realtime" | "upload" }) {
  const [title, setTitle] = useState("");
  const [description, setDescription] = useState("");
  const [language, setLanguage] = useState("vi");
  const [diarization, setDiarization] = useState(true);
  const [creating, setCreating] = useState<"realtime" | "upload">();
  const [error, setError] = useState("");

  async function create(sourceType: "realtime" | "upload") {
    if (!title.trim()) return;
    setCreating(sourceType);
    setError("");
    try {
      const meeting = await meetingsApi.create({
        title: title.trim(),
        description: description.trim(),
        source_type: sourceType,
        language_mode: language,
        diarization_enabled: diarization,
      });
      window.location.assign(sourceType === "realtime" ? `/meetings/${meeting.id}` : `/meetings/${meeting.id}/upload`);
    } catch (cause) {
      setError((cause as Error).message);
    } finally {
      setCreating(undefined);
    }
  }

  return <>
    <PageHeading eyebrow="TẠO MỚI" title={initialSource === "upload" ? "Tải bản ghi" : "Cuộc họp mới"} description="Đặt tên rồi chọn cách bắt đầu." />
    <section className="create-card compact-form">
      <label className="field full">Tên cuộc họp<input autoFocus placeholder="Họp kế hoạch tuần" value={title} onChange={(event) => setTitle(event.target.value)} /></label>
      <label className="field full">Ghi chú <span>(không bắt buộc)</span><textarea placeholder="Nội dung chính" value={description} onChange={(event) => setDescription(event.target.value)} /></label>
      <div className="field-grid">
        <label className="field">Ngôn ngữ<select value={language} onChange={(event) => setLanguage(event.target.value)}><option value="vi">Tiếng Việt</option><option value="en">English</option><option value="auto">Tự nhận diện Việt / Anh</option></select></label>
        <label className="field">Người nói<select value={String(diarization)} onChange={(event) => setDiarization(event.target.value === "true")}><option value="true">Tự phân biệt</option><option value="false">Không phân biệt</option></select></label>
      </div>
      {error && <div className="form-error">{error}</div>}
      <div className="form-actions">
        <a className="btn btn-ghost" href="/meetings">Hủy</a>
        <Button tone={initialSource === "upload" ? "dark" : "light"} disabled={Boolean(creating) || !title.trim()} onClick={() => void create("upload")}><Icon name="upload" />{creating === "upload" ? "Đang tạo…" : "Tải tệp"}</Button>
        <Button tone={initialSource === "upload" ? "light" : "dark"} disabled={Boolean(creating) || !title.trim()} onClick={() => void create("realtime")}><Icon name="meeting" />{creating === "realtime" ? "Đang tạo…" : "Ghi âm"}</Button>
      </div>
    </section>
  </>;
}

function MeetingRow({ meeting, onMedia, onDelete }: { meeting: Meeting; onMedia: () => void; onDelete: () => void }) {
  const date = new Intl.DateTimeFormat("vi-VN", { day: "2-digit", month: "2-digit", year: "numeric" }).format(new Date(meeting.updated_at));
  const duration = meeting.duration_ms ? ` · ${Math.max(1, Math.round(meeting.duration_ms / 60000))} phút` : "";
  return <article className="meeting-row manageable-meeting-row">
    <a className="meeting-row-link" href={`/meetings/${meeting.id}`} aria-label={`Mở cuộc họp ${meeting.title}`} />
    <div className="avatar">{meeting.title.slice(0, 2).toUpperCase()}</div>
    <div className="meeting-row-main"><strong>{meeting.title}</strong><p>{date} · {meeting.source_type === "realtime" ? "Ghi âm" : "Tệp"}{duration}</p></div>
    <Status value={meeting.status} />
    <div className="row-actions">
      {meeting.permission === "owner" && <Button className="row-secondary-action" onClick={onMedia}>Tệp</Button>}
      {meeting.permission === "owner" && <Button className="row-secondary-action" tone="danger" onClick={onDelete}>Xóa</Button>}
    </div>
  </article>;
}

function MediaDialog({ meeting, close }: { meeting: Meeting; close: () => void }) {
  const media = useAsync(() => meetingsApi.media(meeting.id), [meeting.id]);
  const [pending, setPending] = useState<{ id: string; name: string }>();

  async function remove() {
    if (!pending) return;
    try {
      await meetingsApi.deleteMedia(meeting.id, pending.id);
      setPending(undefined);
      await media.reload();
    } catch (cause) {
      window.alert((cause as Error).message);
      setPending(undefined);
    }
  }

  return <>
    <div className="dialog-backdrop"><section className="dialog" role="dialog" aria-modal="true" aria-labelledby="media-title"><button className="close" onClick={close} type="button" aria-label="Đóng">×</button><h2 id="media-title">Tệp của cuộc họp</h2><p>Xóa audio không xóa transcript đã tạo.</p>{media.loading && <Loading />}{media.error && <Failure error={media.error} retry={media.reload} />}{media.data?.length === 0 && <p>Không còn tệp media được lưu.</p>}{media.data?.map((item) => <div className="media-item" key={item.id}><div><strong>{item.original_filename}</strong><small>{(item.size_bytes / 1024 / 1024).toFixed(1)} MB · {statusLabel(item.status)}</small></div><Button tone="danger" onClick={() => setPending({ id: item.id, name: item.original_filename })}>Xóa tệp</Button></div>)}</section></div>
    {pending && <ConfirmDialog title="Xóa tệp audio?" detail={`Tệp “${pending.name}” sẽ bị xóa. Transcript được giữ lại theo chính sách demo.`} confirmLabel="Xóa tệp" close={() => setPending(undefined)} confirm={() => void remove()} />}
  </>;
}

function ConfirmDialog({ title, detail, confirmLabel, close, confirm }: { title: string; detail: string; confirmLabel: string; close: () => void; confirm: () => void }) {
  return <div className="dialog-backdrop"><section className="dialog" role="alertdialog" aria-modal="true" aria-labelledby="confirm-title"><h2 id="confirm-title">{title}</h2><p>{detail}</p><div className="dialog-actions"><Button onClick={close}>Hủy</Button><Button tone="danger" onClick={confirm}>{confirmLabel}</Button></div></section></div>;
}
