import { useEffect, useState } from "react";
import { capabilitiesApi, meetingsApi } from "../api/endpoints";
import { useAsync } from "../hooks/useAsync";
import { useJobMonitor } from "../state/useJobMonitor";
import { can } from "../utils/permissions";
import { Button, Empty, Failure, Loading, PageHeading, Status, statusLabel } from "../components/ui";

export function TranslationPage({ meetingId }: { meetingId: string }) {
  const meeting = useAsync(() => meetingsApi.get(meetingId), [meetingId]);
  const transcript = useAsync(() => meetingsApi.transcript(meetingId), [meetingId]);
  const translations = useAsync(() => meetingsApi.translations(meetingId), [meetingId]);
  const capabilities = useAsync(capabilitiesApi.get, []);
  const jobs = useJobMonitor();
  const [target, setTarget] = useState("en");
  const [error, setError] = useState("");

  useEffect(() => {
    if (jobs.job?.status === "completed") void translations.reload();
  }, [jobs.job?.status, translations.reload]);

  if (meeting.loading || transcript.loading) return <Loading />;
  if (meeting.error || !meeting.data) {
    return <Failure error={meeting.error ?? new Error("Không tìm thấy cuộc họp.")} retry={meeting.reload} />;
  }

  const editable = can(meeting.data.permission, "edit");
  const source = transcript.data?.language ?? meeting.data.language_mode;
  const available = capabilities.data?.translation.language_pairs
    ?.filter(([from]) => from === source)
    .map(([, to]) => to)
    ?? capabilities.data?.translation.languages.filter((language) => language !== source)
    ?? [];
  const latest = translations.data?.[0];
  const jobActive = ["queued", "processing", "retrying"].includes(jobs.job?.status ?? "");
  const originals = new Map(
    transcript.data?.segments.map((segment) => [segment.logical_segment_id, segment]) ?? [],
  );

  async function translate() {
    try {
      setError("");
      jobs.track(await meetingsApi.translation(meetingId, source, target));
    } catch (cause) {
      setError((cause as Error).message);
    }
  }

  return <>
    <PageHeading
      title="Bản dịch"
      context={meeting.data.title}
      actions={latest ? <a className="btn btn-light" href={`/meetings/${meetingId}/export-share?content=translation`}>Xuất bản dịch</a> : undefined}
    />
    <div className="translation-language-strip"><span>{source.toUpperCase()}</span><b>→</b><span>{(latest?.target_language ?? target).toUpperCase()}</span></div>
    {editable && <section className="translation-controls">
      <label className="field">Từ<input readOnly value={source.toUpperCase()} /></label>
      <label className="field">Sang<select value={target} onChange={(event) => setTarget(event.target.value)}>{available.map((language) => <option key={language} value={language}>{language.toUpperCase()}</option>)}</select></label>
      <Button tone="dark" disabled={!transcript.data?.segments.length || !available.length || !target || jobActive} onClick={() => void translate()}>{jobActive ? "Đang dịch…" : latest ? "Dịch lại" : "Dịch"}</Button>
    </section>}
    {error && <div className="form-error">{error}</div>}
    {jobs.job && <div className={`job-state ${jobs.job.status}`}>{statusLabel(jobs.job.status)}{jobs.job.progress !== undefined ? ` · ${jobs.job.progress}%` : ""}{jobs.job.error ? ` · ${jobs.job.error.message}` : ""}</div>}
    {translations.loading && <Loading />}
    {translations.error && <Failure error={translations.error} retry={translations.reload} />}
    {latest ? <>
      <div className="translation-meta"><Status>Phiên bản mới nhất</Status><span>{new Date(latest.created_at).toLocaleString("vi-VN")}</span></div>
      <section className="translation-pairs">{latest.segments.map((item) => <article key={item.logical_segment_id}><div><small>{formatTime(originals.get(item.logical_segment_id)?.start_ms)}</small><p>{originals.get(item.logical_segment_id)?.text ?? "Đoạn gốc không còn."}</p></div><div><small>{latest.target_language.toUpperCase()}</small><p>{item.text}</p></div></article>)}</section>
    </> : !translations.loading && <Empty title="Chưa có bản dịch" detail="Bấm Dịch để bắt đầu." />}
  </>;
}

function formatTime(value?: number) {
  if (value === undefined) return "--:--";
  const seconds = Math.floor(value / 1000);
  return `${Math.floor(seconds / 60).toString().padStart(2, "0")}:${(seconds % 60).toString().padStart(2, "0")}`;
}
