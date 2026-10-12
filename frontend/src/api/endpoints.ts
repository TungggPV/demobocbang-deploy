import { config } from "../app/config";
import { apiClient } from "./client";
import type { AuthSession, Capabilities, Comment, CreatedShareLink, ExportArtifact, ExportFormat, Job, Meeting, MeetingStatus, MediaFile, Permission, Segment, ShareLink, Speaker, SpeakerSample, Summary, SummaryLanguage, SummaryTemplate, Transcript, Translation, User } from "../types/domain";

type Page = { limit?: number; offset?: number; q?: string; status?: MeetingStatus; source_type?: Meeting["source_type"] };
export type TranscriptMutation = { version_id: string; version_no: number; segment?: Segment };
const pageQuery = ({ limit, offset, q, status, source_type }: Page = {}) => {
  const query = new URLSearchParams();
  if (limit !== undefined) query.set("limit", String(limit));
  if (offset !== undefined) query.set("offset", String(offset));
  if (q) query.set("q", q);
  if (status) query.set("status", status);
  if (source_type) query.set("source_type", source_type);
  return query.size ? `?${query}` : "";
};

export const authApi = {
  login: (email: string, password: string) => apiClient.post<AuthSession>("/auth/login", { email, password }),
  register: (email: string, password: string, full_name: string) => apiClient.post<{ user: { id: string } }>("/auth/register", { email, password, full_name }),
  verifyOtp: (email: string, code: string) => apiClient.post<AuthSession>("/auth/verify-email-otp", { email, code }),
  resendOtp: (email: string) => apiClient.post<void>("/auth/resend-email-otp", { email }),
  refresh: (refresh_token?: string) => apiClient.post<AuthSession>("/auth/refresh", refresh_token ? { refresh_token } : undefined),
  logout: () => apiClient.post<void>("/auth/logout"),
  forgotPassword: (email: string) => apiClient.post<void>("/auth/forgot-password", { email }),
  resetPassword: (email: string, code: string, new_password: string) => apiClient.post<void>("/auth/reset-password", { email, code, new_password }),
  updateProfile: (full_name: string) => apiClient.patch<User>("/auth/me", { full_name }),
  googleUrl: () => "/auth/google/start",
};

export const meetingsApi = {
  list: (page?: Page, signal?: AbortSignal) => apiClient.get<Meeting[]>(`/meetings${pageQuery(page)}`, { signal }),
  get: (id: string) => apiClient.get<Meeting>(`/meetings/${id}`),
  create: (body: Pick<Meeting, "title" | "description" | "source_type" | "language_mode" | "diarization_enabled">) => apiClient.post<Meeting>("/meetings", body),
  update: (id: string, body: Partial<Pick<Meeting, "title" | "description" | "language_mode" | "diarization_enabled">>) => apiClient.patch<Meeting>(`/meetings/${id}`, body),
  complete: (id: string) => apiClient.post<Meeting>(`/meetings/${id}/complete`),
  remove: (id: string) => apiClient.delete<void>(`/meetings/${id}`),
  uploadMedia: (id: string, file: File) => { const data = new FormData(); data.append("file", file); return apiClient.post<Job>(`/meetings/${id}/media`, data); },
  media: (id: string) => apiClient.get<MediaFile[]>(`/meetings/${id}/media`),
  playbackUrl: (meetingId: string, mediaId: string) => apiClient.post<{ url: string; expires_in_seconds: number }>(`/meetings/${meetingId}/media/${mediaId}/playback-url`),
  streamMedia: (meetingId: string, mediaId: string) => apiClient.getBlob(`/meetings/${meetingId}/media/${mediaId}/stream`),
  deleteMedia: (meetingId: string, mediaId: string) => apiClient.delete<void>(`/meetings/${meetingId}/media/${mediaId}`),
  transcript: (id: string, page?: Page) => apiClient.get<Transcript>(`/meetings/${id}/transcript${pageQuery(page)}`),
  versions: (id: string, page?: Page) => apiClient.get<Array<{ id: string; version_no: number; source: string; created_at: string }>>(`/meetings/${id}/transcript/versions${pageQuery(page)}`),
  saveSegment: (id: string, body: { text?: string; start_ms?: number; end_ms?: number }) => apiClient.patch<TranscriptMutation>(`/segments/${id}`, body),
  deleteSegment: (id: string) => apiClient.delete<TranscriptMutation>(`/segments/${id}`),
  splitSegment: (id: string, split_at_ms: number, first_text?: string, second_text?: string) => apiClient.post<TranscriptMutation>(`/segments/${id}/split`, { split_at_ms, first_text, second_text }),
  mergeSegments: (segment_ids: string[]) => apiClient.post<TranscriptMutation>("/segments/merge", { segment_ids }),
  speakers: (id: string) => apiClient.get<Speaker[]>(`/meetings/${id}/speakers`),
  updateSpeaker: (id: string, body: Partial<Speaker>) => apiClient.patch<Speaker>(`/speakers/${id}`, body),
  speakerSample: (id: string) => apiClient.get<SpeakerSample>(`/speakers/${id}/sample`),
  setSegmentSpeaker: (id: string, speaker_id: string | null) => apiClient.patch<TranscriptMutation>(`/segments/${id}/speaker`, { speaker_id }),
  createSpeaker: (meetingId: string, body: Pick<Speaker, "name"> & Partial<Pick<Speaker, "role" | "organization">>) => apiClient.post<Speaker>(`/meetings/${meetingId}/speakers`, body),
  addMember: (meetingId: string, email: string, permission: Exclude<Permission, "owner">, attachments: Array<"audio" | "transcript" | "summary" | "translation"> = []) => apiClient.post<void>(`/meetings/${meetingId}/members`, { email, permission, attachments }),
  translation: (id: string, source_language: string, target_language: string) => apiClient.post<Job>(`/meetings/${id}/translations`, { source_language, target_language }),
  translations: (id: string) => apiClient.get<Translation[]>(`/meetings/${id}/translations`),
  summary: (id: string, template: SummaryTemplate, output_language: SummaryLanguage) => apiClient.post<Job>(`/meetings/${id}/summaries`, { template, output_language }),
  summaries: (id: string) => apiClient.get<Summary[]>(`/meetings/${id}/summaries`),
  comments: (id: string, page?: Page) => apiClient.get<Comment[]>(`/meetings/${id}/comments${pageQuery(page)}`),
  addComment: (id: string, body: string, segment_logical_id?: string) => apiClient.post<Comment>(`/meetings/${id}/comments`, { body, segment_logical_id }),
  updateComment: (id: string, body: string) => apiClient.patch<Comment>(`/comments/${id}`, { body }),
  deleteComment: (id: string) => apiClient.delete<void>(`/comments/${id}`),
  export: (id: string, format: ExportFormat, options: { include_timestamps: boolean; include_speakers: boolean; include_translation: boolean; include_summary: boolean; content_mode?: "transcript" | "translation" | "bilingual" } = { include_timestamps: true, include_speakers: true, include_translation: false, include_summary: false, content_mode: "transcript" }) => apiClient.post<ExportArtifact>(`/meetings/${id}/exports`, { format, options }),
  getExport: (id: string) => apiClient.get<ExportArtifact>(`/exports/${id}`),
  exportDownloadUrl: (id: string) => `${config.apiBaseUrl}/exports/${id}/download`,
  downloadExport: (id: string) => apiClient.getBlob(`/exports/${id}/download`),
  createShare: (id: string, permission: Exclude<Permission, "owner">, expires_at?: string) => apiClient.post<CreatedShareLink>(`/meetings/${id}/shares`, { permission, expires_at }),
  shares: (id: string) => apiClient.get<ShareLink[]>(`/meetings/${id}/shares`),
};
export const capabilitiesApi = { get: () => apiClient.get<Capabilities>("/capabilities") };
export const jobsApi = { get: (id: string) => apiClient.get<Job>(`/jobs/${id}`) };
export const sharesApi = {
  resolve: (token: string) => apiClient.get<{ meeting: Meeting; permission: Exclude<Permission, "owner"> }>(`/shares/${token}`),
  transcript: (token: string) => apiClient.get<{ meeting: Meeting; permission: Exclude<Permission, "owner">; transcript: Transcript }>(`/shares/${token}/transcript`),
  redeem: (token: string) => apiClient.post<Meeting>(`/shares/${token}/redeem`),
};
