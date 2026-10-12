export type Permission = "read" | "comment" | "edit" | "owner";
export type MeetingStatus = "created" | "recording" | "processing" | "ready" | "completed" | "failed";
export type JobStatus = "queued" | "processing" | "retrying" | "completed" | "failed";
export type SummaryTemplate = "general" | "briefing" | "voting";
export type SummaryLanguage = "vi" | "en";
export type ExportFormat = "pdf" | "docx" | "txt" | "srt" | "vtt" | "json";

export interface User { id: string; email: string; full_name: string; avatar_url?: string | null; is_email_verified: boolean }
export interface AuthSession { access_token: string; refresh_token?: string; user: User }
export interface Meeting {
  id: string; title: string; description?: string; status: MeetingStatus; permission: Permission;
  owner_id: string; source_type: "realtime" | "upload"; language_mode: string;
  diarization_enabled: boolean; duration_ms?: number; created_at: string; updated_at: string;
}
export interface MediaFile { id: string; original_filename: string; size_bytes: number; duration_ms?: number; status: string }
export interface SpeakerSample { media_id: string; start_ms: number; end_ms: number }
export interface Speaker { id: string; meeting_id?: string; name: string; role?: string; organization?: string; is_identified: boolean }
export interface Segment { logical_segment_id: string; start_ms: number; end_ms: number; text: string; speaker_id?: string; speaker_label?: string; confidence?: number }
export interface Transcript { version_id: string; version_no: number; language: string; segments: Segment[]; next_cursor?: string | null }
export interface Capabilities {
  stt: { provider: string; batch: boolean; streaming: boolean; diarization: boolean; languages: string[]; timestamps?: boolean; confidence?: boolean };
  translation: { languages: string[]; language_pairs?: [string, string][] }; summary_templates: SummaryTemplate[]; summary_languages: SummaryLanguage[]; exports: ExportFormat[];
}
export interface Job { id: string; status: JobStatus; progress?: number; error?: { code: string; message: string } }
export interface Comment { id: string; meeting_id: string; segment_logical_id?: string; author_id: string; author: Pick<User, "id" | "full_name" | "avatar_url">; body: string; created_at: string; updated_at: string }
export interface ExportArtifact { id: string; meeting_id: string; transcript_version_id: string; format: ExportFormat; job_id?: string; status: JobStatus; filename?: string; content_type?: string; created_at: string }
export interface Translation { id: string; meeting_id: string; transcript_version_id: string; source_language: string; target_language: string; created_at: string; segments: Array<{ logical_segment_id: string; sequence: number; text: string }> }
export interface Summary { id: string; meeting_id: string; transcript_version_id: string; template: SummaryTemplate; output_language: SummaryLanguage; current_version: { id: string; version_no: number; content: Record<string, unknown>; provider: string; model: string; created_at: string } }
export interface ShareLink { id: string; meeting_id: string; permission: Exclude<Permission, "owner">; expires_at?: string | null; revoked_at?: string | null; created_at: string }
export interface CreatedShareLink extends ShareLink { token: string }
export interface ApiErrorShape { code?: string; message?: string; detail?: string }
