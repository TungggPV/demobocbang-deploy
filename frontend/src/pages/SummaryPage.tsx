import { useEffect, useState } from "react";
import { capabilitiesApi, meetingsApi } from "../api/endpoints";
import { useAsync } from "../hooks/useAsync";
import { useJobMonitor } from "../state/useJobMonitor";
import { can } from "../utils/permissions";
import type { SummaryLanguage, SummaryTemplate } from "../types/domain";
import { Button, Empty, Failure, Loading, PageHeading, Status, statusLabel } from "../components/ui";

const labels: Record<SummaryTemplate, string> = {
  general: "Tóm tắt chung",
  briefing: "Họp giao ban",
  voting: "Họp biểu quyết",
};
const languageLabels: Record<SummaryLanguage, string> = { vi: "Tiếng Việt", en: "English" };

export function SummaryPage({ meetingId }: { meetingId: string }) {
  const meeting = useAsync(() => meetingsApi.get(meetingId), [meetingId]);
  const transcript = useAsync(() => meetingsApi.transcript(meetingId), [meetingId]);
  const summaries = useAsync(() => meetingsApi.summaries(meetingId), [meetingId]);
  const capabilities = useAsync(capabilitiesApi.get, []);
  const jobs = useJobMonitor();
  const [template, setTemplate] = useState<SummaryTemplate>("general");
  const [outputLanguage, setOutputLanguage] = useState<SummaryLanguage>("vi");
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");

  useEffect(() => {
    const transcriptLanguage = meeting.data?.language_mode;
    setOutputLanguage(transcriptLanguage === "en" ? "en" : "vi");
  }, [meeting.data?.language_mode, meetingId]);
  useEffect(() => {
    if (jobs.job?.status === "completed") void summaries.reload();
  }, [jobs.job?.status, summaries.reload]);

  if (meeting.loading || transcript.loading) return <Loading />;
  if (meeting.error || !meeting.data) {
    return <Failure error={meeting.error ?? new Error("Không tìm thấy cuộc họp.")} retry={meeting.reload} />;
  }

  const editable = can(meeting.data.permission, "edit");
  const hasTranscript = Boolean(transcript.data?.segments.length);
  const summary = summaries.data?.find(
    (item) => item.template === template && item.output_language === outputLanguage,
  );
  const availableLanguages = capabilities.data?.summary_languages.length
    ? capabilities.data.summary_languages
    : (["vi", "en"] as SummaryLanguage[]);

  async function generate() {
    try {
      setError("");
      setNotice("");
      jobs.track(await meetingsApi.summary(meetingId, template, outputLanguage));
    } catch (cause) {
      setError((cause as Error).message);
    }
  }

  async function copy() {
    if (!summary) return;
    try {
      await navigator.clipboard.writeText(toText(summary.current_version.content));
      setNotice("Đã sao chép tóm tắt.");
      setError("");
    } catch {
      setError("Không thể sao chép nội dung trong trình duyệt này.");
    }
  }

  return <>
    <PageHeading
      eyebrow="TÓM TẮT"
      title="Tóm tắt"
      context={meeting.data.title}
    />
    <section className="summary-controls">
      <label className="field">Mẫu
        <select value={template} onChange={(event) => setTemplate(event.target.value as SummaryTemplate)}>
          {(capabilities.data?.summary_templates.length
            ? capabilities.data.summary_templates
            : Object.keys(labels) as SummaryTemplate[]
          ).map((item) => <option key={item} value={item}>{labels[item]}</option>)}
        </select>
      </label>
      <label className="field">Ngôn ngữ
        <select value={outputLanguage} onChange={(event) => setOutputLanguage(event.target.value as SummaryLanguage)}>
          {availableLanguages.map((language) => <option key={language} value={language}>{languageLabels[language]}</option>)}
        </select>
      </label>
      {editable && <Button
        tone="dark"
        disabled={!hasTranscript || !capabilities.data?.summary_templates.includes(template) || ["queued", "processing", "retrying"].includes(jobs.job?.status ?? "")}
        onClick={() => void generate()}
      >{summary ? "Tạo lại" : "Tạo tóm tắt"}</Button>}
      {summary && <Button onClick={() => void copy()}>Sao chép</Button>}
    </section>
    {editable && !hasTranscript && <div className="notice">Cần có transcript trước khi tạo tóm tắt.</div>}
    {notice && <div className="notice">{notice}</div>}
    {error && <div className="form-error">{error}</div>}
    {jobs.job && <div className={`job-state ${jobs.job.status}`}>
      Tóm tắt AI: {statusLabel(jobs.job.status)}
      {jobs.job.progress !== undefined ? ` · ${jobs.job.progress}%` : ""}
      {jobs.job.error ? ` · ${jobs.job.error.message}` : ""}
    </div>}
    {summaries.loading && <Loading />}
    {summaries.error && <Failure error={summaries.error} retry={summaries.reload} />}
    {summary ? <div className="summary-layout">
      <section className="summary-document">
        <div className="summary-document-meta">
          <Status>{labels[summary.template]}</Status>
          <span>{languageLabels[summary.output_language]} · v{summary.current_version.version_no} · {new Date(summary.current_version.created_at).toLocaleString("vi-VN")}</span>
        </div>
        {Object.entries(summary.current_version.content).map(([key, value], index) => <article className="summary-section" key={key}>
          <span>{String(index + 1).padStart(2, "0")}</span>
          <div><h2>{humanize(key)}</h2><SummaryValue value={value} /></div>
        </article>)}
      </section>
    </div> : !summaries.loading && <Empty title="Chưa có tóm tắt" detail="Chọn mẫu và bấm Tạo tóm tắt." />}
  </>;
}

function SummaryValue({ value }: { value: unknown }) {
  if (Array.isArray(value)) return <ul>{value.map((item, index) => <li key={index}>{typeof item === "string" ? item : JSON.stringify(item)}</li>)}</ul>;
  if (value && typeof value === "object") return <pre>{JSON.stringify(value, null, 2)}</pre>;
  return <p>{String(value ?? "")}</p>;
}

const sectionLabels: Record<string, string> = {
  overview: "Tổng quan",
  summary: "Tóm tắt",
  key_points: "Ý chính",
  decisions: "Quyết định",
  action_items: "Công việc cần làm",
  risks: "Rủi ro & lưu ý",
  risks_notes: "Rủi ro & lưu ý",
  attendees: "Thành phần tham dự",
  participants: "Thành phần tham dự",
  next_steps: "Bước tiếp theo",
  voting_results: "Kết quả biểu quyết",
  notes: "Ghi chú bổ sung",
  discussion: "Nội dung thảo luận",
};
function humanize(key: string) { return sectionLabels[key] ?? key.replace(/[_-]+/g, " ").replace(/\b\w/g, (character) => character.toUpperCase()); }
function toText(content: Record<string, unknown>) { return Object.entries(content).map(([key, value]) => `${humanize(key)}\n${Array.isArray(value) ? value.map(String).join("\n") : typeof value === "object" ? JSON.stringify(value, null, 2) : String(value)}`).join("\n\n"); }
