import { useEffect, useState } from "react";
import { translateErrorMessage } from "../api/client";
import { capabilitiesApi, meetingsApi } from "../api/endpoints";
import { useAsync } from "../hooks/useAsync";
import { useJobMonitor } from "../state/useJobMonitor";
import type { ExportArtifact, ExportFormat } from "../types/domain";
import { Button, Failure, Loading, PageHeading, Panel, statusLabel } from "../components/ui";
import { ShareDialog } from "./MeetingPage";

const formats: Array<{
  value: ExportFormat;
  name: string;
  badge?: string;
  detail: string;
  desc: string;
}> = [
  { value: "pdf", name: "PDF", badge: "Khuyên dùng", detail: "Văn bản định dạng đẹp", desc: "Bố cục chuẩn in ấn, rõ ràng người nói và thời gian" },
  { value: "docx", name: "DOCX", detail: "Microsoft Word", desc: "Dễ dàng chỉnh sửa và lưu trữ trong Word" },
  { value: "txt", name: "TXT", detail: "Văn bản thuần", desc: "Gọn nhẹ, mở được trên mọi ứng dụng và thiết bị" },
  { value: "srt", name: "SRT", detail: "Phụ đề SubRip", desc: "Dành cho dựng phim Premiere, DaVinci, YouTube" },
  { value: "vtt", name: "VTT", detail: "Phụ đề WebVTT", desc: "Chuẩn phụ đề web quốc tế cho video HTML5" },
  { value: "json", name: "JSON", detail: "Dữ liệu cấu trúc", desc: "Dành cho lập trình viên tích hợp hệ thống" },
];

export function ExportSharePage({ meetingId }: { meetingId: string }) {
  const requestedContent = new URLSearchParams(window.location.search).get("content");
  const meeting = useAsync(() => meetingsApi.get(meetingId), [meetingId]);
  const transcript = useAsync(() => meetingsApi.transcript(meetingId), [meetingId]);
  const translations = useAsync(() => meetingsApi.translations(meetingId), [meetingId]);
  const capabilities = useAsync(capabilitiesApi.get, []);
  const jobs = useJobMonitor();
  const [format, setFormat] = useState<ExportFormat>("pdf");
  const [contentMode, setContentMode] = useState<"transcript" | "translation" | "bilingual">(
    requestedContent === "translation" ? "translation" : requestedContent === "bilingual" ? "bilingual" : "transcript"
  );
  const [options, setOptions] = useState({ include_timestamps: true, include_speakers: true, include_summary: false });
  const [artifact, setArtifact] = useState<ExportArtifact>();
  const [emailOpen, setEmailOpen] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");

  useEffect(() => {
    if (jobs.job?.status !== "completed" || !artifact || artifact.job_id !== jobs.job.id) return;
    void meetingsApi.getExport(artifact.id).then(setArtifact).catch((cause: Error) => setError(cause.message));
  }, [artifact?.id, artifact?.job_id, jobs.job?.id, jobs.job?.status]);

  if (meeting.loading) return <Loading />;
  if (meeting.error || !meeting.data) return <Failure error={meeting.error ?? new Error("Không tìm thấy cuộc họp.")} retry={meeting.reload} />;
  const currentMeeting = meeting.data;
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

  return (
    <>
      <PageHeading eyebrow="XUẤT DỮ LIỆU" title="Xuất tệp & Báo cáo" context={currentMeeting.title} />
      {notice && <div className="notice">{notice}</div>}
      {error && <div className="form-error">{error}</div>}
      <div className="export-layout">
        <Panel>
          <h2>Cấu hình xuất tệp</h2>
          <p className="muted">Chọn định dạng, phần nội dung và các thông tin cần đưa vào tài liệu.</p>

          <div className="export-section-title">1. Chọn định dạng tệp</div>
          <div className="export-formats">
            {formats
              .filter((item) => capabilities.data?.exports.includes(item.value))
              .map((item) => (
                <button
                  type="button"
                  className={`export-format ${format === item.value ? "selected" : ""}`}
                  onClick={() => setFormat(item.value)}
                  key={item.value}
                >
                  <div className="export-format-header">
                    <strong>{item.name}</strong>
                    {item.badge && <span className="export-format-badge">{item.badge}</span>}
                  </div>
                  <span className="export-format-detail">{item.detail}</span>
                  <span className="export-format-desc">{item.desc}</span>
                </button>
              ))}
          </div>

          <div className="export-section-title">2. Phần nội dung cần xuất</div>
          <label className="field export-content-mode">
            Nội dung
            <select value={contentMode} onChange={(event) => setContentMode(event.target.value as typeof contentMode)}>
              <option value="transcript">Nội dung bóc băng gốc</option>
              <option value="translation">Chỉ xuất bản dịch</option>
              <option value="bilingual">Song ngữ (Bản gốc + Bản dịch)</option>
            </select>
          </label>
          {translationRequired && !translations.loading && !hasTranslation && (
            <div className="form-error">
              Chưa có bản dịch. <a href={`/meetings/${meetingId}/translation`}>Tạo bản dịch trước</a>.
            </div>
          )}

          <div className="export-section-title" style={{ marginTop: 16 }}>3. Tùy chọn hiển thị</div>
          <div className="export-options">
            {Object.entries(options).map(([key, checked]) => (
              <label key={key}>
                <input
                  type="checkbox"
                  checked={checked}
                  onChange={(event) => setOptions({ ...options, [key]: event.target.checked })}
                />
                {optionLabel(key)}
              </label>
            ))}
          </div>

          {!transcript.loading && !hasTranscript && <p>Cần có nội dung bản ghi trước khi xuất tệp.</p>}

          <Button
            tone="dark"
            disabled={
              transcript.loading ||
              translations.loading ||
              !hasTranscript ||
              (translationRequired && !hasTranslation) ||
              jobActive ||
              !capabilities.data?.exports.includes(format)
            }
            onClick={() => void createExport()}
          >
            {jobActive ? "Đang xử lý & tạo tệp…" : `Tạo tệp ${format.toUpperCase()}`}
          </Button>

          {jobs.job && (
            <div className={`job-state ${jobs.job.status}`}>
              {statusLabel(jobs.job.status)}
              {jobs.job.progress !== undefined ? ` · ${jobs.job.progress}%` : ""}
              {jobs.job.error ? ` · ${translateErrorMessage(jobs.job.error.code, jobs.job.error.message)}` : ""}
            </div>
          )}

          {artifact?.status === "completed" && (
            <div className="export-ready-card">
              <div className="export-ready-info">
                <div className="export-ready-icon">{artifact.format}</div>
                <div className="export-ready-meta">
                  <strong>{artifact.filename ?? `${currentMeeting.title}.${artifact.format}`}</strong>
                  <span>Tệp đã sẵn sàng tải xuống</span>
                </div>
              </div>
              <Button tone="dark" onClick={() => void download()}>
                Tải về máy tính
              </Button>
            </div>
          )}
        </Panel>

        <Panel className="export-side-details">
          <h2>Thông tin bản ghi</h2>
          <p className="muted">Chi tiết bản ghi sẽ được đóng gói vào tài liệu xuất.</p>
          <dl>
            <dt>Tên bản ghi</dt>
            <dd>{currentMeeting.title}</dd>
            <dt>Ngôn ngữ</dt>
            <dd>
              {currentMeeting.language_mode === "vi"
                ? "Tiếng Việt"
                : currentMeeting.language_mode === "en"
                ? "Tiếng Anh"
                : "Tự động nhận diện"}
            </dd>
            <dt>Phân đoạn</dt>
            <dd>{transcript.data?.segments.length ?? 0} đoạn</dd>
            <dt>Phương thức</dt>
            <dd>{currentMeeting.source_type === "realtime" ? "Ghi âm trực tiếp" : "Tải tệp âm thanh"}</dd>
          </dl>

          <div className="export-side-email-box">
            <p>Cần gửi trực tiếp tài liệu đính kèm qua hòm thư cho đồng nghiệp?</p>
            <Button onClick={() => setEmailOpen(true)}>Gửi tài liệu qua Email</Button>
          </div>
        </Panel>
      </div>

      {emailOpen && <ShareDialog meetingId={meetingId} title={currentMeeting.title} close={() => setEmailOpen(false)} />}
    </>
  );
}

function optionLabel(value: string) {
  return (
    {
      include_timestamps: "Kèm mốc thời gian [mm:ss]",
      include_speakers: "Kèm tên người nói",
      include_translation: "Kèm bản dịch mới nhất",
      include_summary: "Kèm phần tóm tắt nội dung",
    } as Record<string, string>
  )[value];
}
