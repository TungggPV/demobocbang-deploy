export type MeetingStatus = "created" | "queued" | "recording" | "processing" | "ready" | "retrying" | "completed" | "failed";
export type JobStatus = "queued" | "processing" | "retrying" | "completed" | "failed";
export type Permission = "owner" | "edit" | "comment" | "read";
export type SummaryTemplate = "general" | "briefing" | "voting";
export type SummaryLanguage = "vi" | "en";
export type ExportFormat = "pdf" | "docx" | "txt" | "srt" | "vtt" | "json";

export interface User { id: string; email: string; full_name: string; avatar_url?: string | null; has_password?: boolean; is_email_verified: boolean }
export interface AuthSession { access_token: string; refresh_token?: string; user: User }
export interface Meeting {
  id: string; title: string; description?: string; status: MeetingStatus; permission: Permission;
  owner_id?: string; source_type: "realtime" | "upload"; language_mode: string; diarization_enabled: boolean;
  duration_ms?: number; created_at: string; updated_at: string;
}
export interface MediaFile { id: string; original_filename: string; size_bytes: number; duration_ms?: number; status: string }
export interface SpeakerSample { media_id: string; start_ms: number; end_ms: number }
export interface Speaker { id: string; meeting_id?: string; name: string; role?: string; organization?: string; is_identified: boolean; voice_profile_id?: string; match_confidence?: number }
export interface VoiceSampleItem { index: number; has_audio: boolean }
export interface VoiceProfile { id: string; user_id?: string; meeting_id?: string; name: string; sample_duration_ms: number; provider: string; sample_count: number; has_audio?: boolean; samples?: VoiceSampleItem[]; created_at: string; updated_at: string }
export interface Segment { logical_segment_id: string; start_ms: number; end_ms: number; text: string; speaker_id?: string; speaker_label?: string; confidence?: number }
export interface Transcript { version_id: string; version_no: number; language: string; segments: Segment[]; next_cursor?: string | null }
export interface Capabilities {
  stt: { provider: string; fallback_provider?: string; batch: boolean; streaming: boolean; speaker_identification?: boolean; diarization: boolean; languages: string[]; timestamps?: boolean; confidence?: boolean };
  translation: { languages: string[]; language_pairs?: [string, string][] }; summary_templates: SummaryTemplate[]; summary_languages: SummaryLanguage[]; exports: ExportFormat[];
}
export interface Job { id: string; status: JobStatus; progress?: number; error?: { code: string; message: string } }
export interface TranslationSegment { logical_segment_id: string; text: string }
export interface Translation { id: string; source_language: string; target_language: string; segments: TranslationSegment[]; created_at: string }
export interface SummaryVersion { version_no: number; content: Record<string, unknown>; created_at: string }
export interface Summary { id: string; template: SummaryTemplate; output_language: SummaryLanguage; current_version: SummaryVersion }
export interface Comment {
  id: string;
  parent_comment_id?: string | null;
  author_user_id?: string;
  author_id?: string;
  author_name?: string;
  author_avatar_url?: string;
  segment_id?: string | null;
  segment_logical_id?: string | null;
  content?: string;
  body?: string;
  created_at: string;
  updated_at?: string;
}
export interface ShareLink { id: string; token: string; permission: Exclude<Permission, "owner">; expires_at?: string }
export interface CreatedShareLink extends ShareLink { share_url: string }
export interface ExportArtifact { id: string; format: ExportFormat; filename?: string; status: JobStatus; job_id?: string }
export interface ApiErrorShape { code?: string; message?: string; detail?: string }
