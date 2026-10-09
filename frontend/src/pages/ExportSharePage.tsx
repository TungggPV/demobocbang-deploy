import { useEffect, useState } from "react";
import { capabilitiesApi, meetingsApi } from "../api/endpoints";
import { useAsync } from "../hooks/useAsync";
import { useJobMonitor } from "../state/useJobMonitor";
import type { ExportArtifact, ExportFormat, Permission } from "../types/domain";
import { Button, Failure, Loading, PageHeading, Panel, statusLabel } from "../components/ui";

const formats: Array<{ value: ExportFormat; name: string; detail: string }> = [
  { value: "pdf", name: "PDF", detail: "Tài liệu trình bày đẹp" },
  { value: "docx", name: "DOCX", detail: "Có thể chỉnh sửa" },
  { value: "txt", name: "TXT", detail: "Văn bản thuần" },
  { value: "srt", name: "SRT", detail: "Phụ đề video" },
  { value: "vtt", name: "VTT", detail: "Phụ đề web" },
  { value: "json", name: "JSON", detail: "Dữ liệu có cấu trúc" },
];

export function ExportSharePage({ meetingId }: { meetingId: string }) {
  const requestedContent = new URLSearchParams(window.location.search).get("content");
  const meeting = useAsync(() => meetingsApi.get(meetingId), [meetingId]);
  const transcript = useAsync(() => meetingsApi.transcript(meetingId), [meetingId]);
  const translations = useAsync(() => meetingsApi.translations(meetingId), [meetingId]);
  const capabilities = useAsync(capabilitiesApi.get, []);
  const jobs = useJobMonitor();
  const [format, setFormat] = useState<ExportFormat>("pdf");
  const [contentMode, setContentMode] = useState<"transcript" | "translation" | "bilingual">(requestedContent === "translation" ? "translation" : requestedContent === "bilingual" ? "bilingual" : "transcript");
  const [options, setOptions] = useState({ include_timestamps: true, include_speakers: true, include_summary: false });
  const [artifact, setArtifact] = useState<ExportArtifact>();
  const [permission, setPermission] = useState<Exclude<Permission, "owner">>("read");
  const [shareUrl, setShareUrl] = useState("");
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");

  useEffect(() => {
    if (jobs.job?.status !== "completed" || !artifact || artifact.job_id !== jobs.job.id) return;
    void meetingsApi.getExport(artifact.id).then(setArtifact).catch((cause: Error) => setError(cause.message));
  }, [artifact?.id, artifact?.job_id, jobs.job?.id, jobs.job?.status]);

  if (meeting.loading) return <Loading />;
  if (meeting.error || !meeting.data) return <Failure error={meeting.error ?? new Error("Không tìm thấy cuộc họp.")} retry={meeting.reload} />;
  const currentMeeting = meeting.data;
  const owner = currentMeeting.permission === "owner";
  const hasTranscript = Boolean(transcript.data?.segments.length);
  const hasTranslation = Boolean(translations.data?.[0]?.segments.length);
  const translationRequired = contentMode !== "transcript";
  const jobActive = ["queued", "processing", "retrying"].includes(jobs.job?.status ?? "");

  async function createExport() {
    if (!hasTranscript) return setError("Cần có nội dung bản ghi trước khi xuất tệp.");
    try {
      setError(""); setNotice("");
      if (translationRequired && !hasTranslation) return setError("Cuộc họp chưa có bản dịch. Hãy tạo bản dịch trước khi xuất.");
      const next = await meetingsApi.export(meetingId, format, { ...options, include_translation: translationRequired, content_mode: contentMode });
      setArtifact(next);
      if (next.job_id) jobs.track({ id: next.job_id, status: next.status });
    } catch (cause) { setError((cause as Error).message); }
  }

  async function download() {
    if (!artifact) return;
    try {
      setError("");
      const blob = await meetingsApi.downloadExport(artifact.id);
      const href = URL.createObjectURL(blob);
      const anchor = document.createElement("a");
      anchor.href = href;
      anchor.download = artifact.filename ?? `${currentMeeting.title}.${artifact.format}`;
      anchor.click();
      window.setTimeout(() => URL.revokeObjectURL(href), 1000);
    } catch (cause) { setError((cause as Error).message); }
  }

  async function share() {
    try {
      setError(""); setNotice("");
      const link = await meetingsApi.createShare(meetingId, permission);
      setShareUrl(`${window.location.origin}/share/${link.token}`);
    } catch (cause) { setError((cause as Error).message); }
  }

  async function copyShareLink() {
    try {
      await navigator.clipboard.writeText(shareUrl);
      setNotice("Đã sao chép liên kết chia sẻ.");
      setError("");
    } catch { setError("Không thể sao chép liên kết trong trình duyệt này."); }
  }

  return <>
    <PageHeading eyebrow="XUẤT" title="Xuất & chia sẻ" context={currentMeeting.title} />
    {notice && <div className="notice">{notice}</div>}
    {error && <div className="form-error">{error}</div>}
    <div className="export-layout">
      <Panel>
        <h2>Xuất tệp</h2>
        <p className="muted">Chọn định dạng và phần dữ liệu cần đưa vào tệp.</p>
        <label className="field export-content-mode">Nội dung<select value={contentMode} onChange={(event) => setContentMode(event.target.value as typeof contentMode)}><option value="transcript">Nội dung bóc băng</option><option value="translation">Chỉ bản dịch</option><option value="bilingual">Bản gốc và bản dịch</option></select></label>
        {translationRequired && !translations.loading && !hasTranslation && <div className="form-error">Chưa có bản dịch. <a href={`/meetings/${meetingId}/translation`}>Tạo bản dịch trước</a>.</div>}
        <div className="export-formats">{formats.filter((item) => capabilities.data?.exports.includes(item.value)).map((item) => <button type="button" className={`export-format ${format === item.value ? "selected" : ""}`} onClick={() => setFormat(item.value)} key={item.value}><strong>{item.name}</strong><span>{item.detail}</span></button>)}</div>
        <div className="export-options">{Object.entries(options).map(([key, checked]) => <label key={key}><input type="checkbox" checked={checked} onChange={(event) => setOptions({ ...options, [key]: event.target.checked })} />{optionLabel(key)}</label>)}</div>
        {!transcript.loading && !hasTranscript && <p>Cần có nội dung bản ghi trước khi xuất tệp.</p>}
        <Button tone="dark" disabled={transcript.loading || translations.loading || !hasTranscript || (translationRequired && !hasTranslation) || jobActive || !capabilities.data?.exports.includes(format)} onClick={() => void createExport()}>{jobActive ? "Đang tạo tệp…" : `Tạo ${format.toUpperCase()}`}</Button>
        {jobs.job && <div className={`job-state ${jobs.job.status}`}>{statusLabel(jobs.job.status)}{jobs.job.progress !== undefined ? ` · ${jobs.job.progress}%` : ""}{jobs.job.error ? ` · ${jobs.job.error.message}` : ""}</div>}
        {artifact?.status === "completed" && <Button onClick={() => void download()}>Tải {artifact.filename ?? artifact.format.toUpperCase()}</Button>}
      </Panel>
      <Panel>
        <h2>Chia sẻ bằng liên kết</h2>
        <p>{owner ? "Người mở link sẽ nhận đúng quyền bạn chọn sau khi đăng nhập." : "Chỉ chủ sở hữu có thể tạo liên kết."}</p>
        {owner && <><label className="field">Quyền<select value={permission} onChange={(event) => setPermission(event.target.value as typeof permission)}><option value="read">Chỉ xem</option><option value="comment">Xem và bình luận</option><option value="edit">Chỉnh sửa</option></select></label><Button onClick={() => void share()}>Tạo liên kết</Button>{shareUrl && <div className="share-link"><input readOnly value={shareUrl} /><Button onClick={() => void copyShareLink()}>Sao chép</Button></div>}</>}
      </Panel>
    </div>
  </>;
}

function optionLabel(value: string) {
  return ({ include_timestamps: "Kèm mốc thời gian", include_speakers: "Kèm tên người nói", include_translation: "Kèm bản dịch mới nhất", include_summary: "Kèm tóm tắt mới nhất" } as Record<string, string>)[value];
}
