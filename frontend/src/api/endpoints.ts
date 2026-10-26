import { config } from "../app/config";
import { apiClient } from "./client";
import type {
  AuthSession,
  Capabilities,
  Comment,
  CreatedShareLink,
  ExportArtifact,
  ExportFormat,
  Job,
  Meeting,
  MeetingBundle,
  MeetingRecording,
  MeetingSection,
  MeetingStatus,
  MediaFile,
  Permission,
  Segment,
  ShareLink,
  Speaker,
  SpeakerSample,
  Summary,
  SummaryLanguage,
  SummaryMode,
  SummaryScope,
  SummaryTemplate,
  Transcript,
  TranscriptPolish,
  Translation,
  User,
  VoiceProfile,
} from "../types/domain";

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
  me: () => apiClient.get<User>("/auth/me"),
  updateProfile: (full_name: string) => apiClient.patch<User>("/auth/me", { full_name }),
  updatePassword: (current_password: string | undefined, new_password: string) => apiClient.patch<User>("/auth/me/password", { current_password, new_password }),
  googleUrl: () => "/auth/google/start",
};

const bundleCache = new Map<string, { data: MeetingBundle; timestamp: number }>();
const BUNDLE_CACHE_TTL = 30_000;

let listCache: { key: string; data: Meeting[]; timestamp: number } | null = null;
const LIST_CACHE_TTL = 15_000;

export const meetingsApi = {
  sections: (id: string) => apiClient.get<MeetingSection[]>(`/meetings/${id}/sections`),
  renameSection: (meetingId: string, sectionId: string, title: string) => apiClient.patch<MeetingSection>(`/meetings/${meetingId}/sections/${sectionId}`, { title }),
  updateSection: (meetingId: string, sectionId: string, body: { title: string; start_ms: number; end_ms: number | null; expected_revision: string }) => apiClient.patch<MeetingSection>(`/meetings/${meetingId}/sections/${sectionId}`, body),
  createSection: (meetingId: string, body: { title: string; start_ms: number; expected_revision: string }) => apiClient.post<MeetingSection[]>(`/meetings/${meetingId}/sections`, body),
  deleteSection: (meetingId: string, sectionId: string, revision: string) => apiClient.delete<MeetingSection[]>(`/meetings/${meetingId}/sections/${sectionId}?expected_revision=${encodeURIComponent(revision)}`),
  editSummary: (id: string, content: Record<string, unknown>, expected_version_no?: number) => apiClient.patch<Summary>(`/summaries/${id}`, { content, expected_version_no }),
  invalidateBundle: (id?: string) => {
    if (id) bundleCache.delete(id);
    else bundleCache.clear();
  },
  invalidateList: () => {
    listCache = null;
  },
  list: async (page?: Page, signal?: AbortSignal, forceFresh = false) => {
    const key = pageQuery(page);
    const now = Date.now();
    if (!forceFresh && !signal && listCache && listCache.key === key && now - listCache.timestamp < LIST_CACHE_TTL) {
      return listCache.data;
    }
    const data = await apiClient.get<Meeting[]>(`/meetings${key}`, { signal });
    if (!signal) {
      listCache = { key, data, timestamp: Date.now() };
    }
    return data;
  },
  bundle: async (id: string, forceFresh = false) => {
    const cached = bundleCache.get(id);
    const now = Date.now();
    if (!forceFresh && cached && now - cached.timestamp < BUNDLE_CACHE_TTL) {
      return cached.data;
    }
    const data = await apiClient.get<MeetingBundle>(`/meetings/${id}/bundle`);
    bundleCache.set(id, { data, timestamp: Date.now() });
    return data;
  },
  get: (id: string) => apiClient.get<Meeting>(`/meetings/${id}`),
  create: async (body: Pick<Meeting, "title" | "description" | "source_type" | "language_mode" | "diarization_enabled">) => {
    listCache = null;
    return apiClient.post<Meeting>("/meetings", body);
  },
  update: async (id: string, body: Partial<Pick<Meeting, "title" | "description" | "language_mode" | "diarization_enabled">>) => {
    listCache = null;
    bundleCache.delete(id);
    return apiClient.patch<Meeting>(`/meetings/${id}`, body);
  },
  complete: async (id: string) => {
    listCache = null;
    bundleCache.delete(id);
    return apiClient.post<Meeting>(`/meetings/${id}/complete`);
  },
  remove: async (id: string) => {
    listCache = null;
    bundleCache.delete(id);
    return apiClient.delete<void>(`/meetings/${id}`);
  },
  uploadMedia: (id: string, file: File) => {
    bundleCache.delete(id);
    const data = new FormData();
    data.append("file", file);
    return apiClient.post<Job>(`/meetings/${id}/media`, data);
  },
  media: (id: string) => apiClient.get<MediaFile[]>(`/meetings/${id}/media`),
  playbackUrl: (meetingId: string, mediaId: string) => apiClient.post<{ url: string; expires_in_seconds: number }>(`/meetings/${meetingId}/media/${mediaId}/playback-url`),
  streamMedia: (meetingId: string, mediaId: string) => apiClient.getBlob(`/meetings/${meetingId}/media/${mediaId}/stream`),
  deleteMedia: (meetingId: string, mediaId: string) => apiClient.delete<void>(`/meetings/${meetingId}/media/${mediaId}`),
  transcript: (id: string, page?: Page) => apiClient.get<Transcript>(`/meetings/${id}/transcript${pageQuery(page)}`),
  versions: (id: string, page?: Page) => apiClient.get<Array<{ id: string; version_no: number; source: string; created_at: string }>>(`/meetings/${id}/transcript/versions${pageQuery(page)}`),
  saveSegment: (id: string, body: { text?: string; start_ms?: number; end_ms?: number }) => {
    bundleCache.clear();
    return apiClient.patch<TranscriptMutation>(`/segments/${id}`, body);
  },
  deleteSegment: (id: string) => {
    bundleCache.clear();
    return apiClient.delete<TranscriptMutation>(`/segments/${id}`);
  },
  splitSegment: (id: string, split_at_ms: number, first_text?: string, second_text?: string) => {
    bundleCache.clear();
    return apiClient.post<TranscriptMutation>(`/segments/${id}/split`, { split_at_ms, first_text, second_text });
  },
  mergeSegments: (segment_ids: string[]) => {
    bundleCache.clear();
    return apiClient.post<TranscriptMutation>("/segments/merge", {
      segment_ids,
      first_segment_id: segment_ids[0],
      second_segment_id: segment_ids[1],
    });
  },
  speakers: (id: string) => apiClient.get<Speaker[]>(`/meetings/${id}/speakers`),
  updateSpeaker: (id: string, body: Partial<Speaker>) => {
    bundleCache.clear();
    return apiClient.patch<Speaker>(`/speakers/${id}`, body);
  },
  deleteSpeaker: (id: string) => {
    bundleCache.clear();
    return apiClient.delete<void>(`/speakers/${id}`);
  },
  speakerSample: (id: string) => apiClient.get<SpeakerSample>(`/speakers/${id}/sample`),
  setSegmentSpeaker: (id: string, speaker_id: string | null) => {
    bundleCache.clear();
    return apiClient.patch<TranscriptMutation>(`/segments/${id}/speaker`, { speaker_id });
  },
  createSpeaker: (meetingId: string, body: Pick<Speaker, "name"> & Partial<Pick<Speaker, "role" | "organization" | "voice_profile_id">>) => apiClient.post<Speaker>(`/meetings/${meetingId}/speakers`, body),
  addMember: (meetingId: string, email: string, permission: Exclude<Permission, "owner">, attachments: Array<"audio" | "transcript" | "summary" | "translation"> = []) => apiClient.post<void>(`/meetings/${meetingId}/members`, { email, permission, attachments }),
  translation: (id: string, source_language: string, target_language: string) => apiClient.post<Job>(`/meetings/${id}/translations`, { source_language, target_language }),
  translations: (id: string) => apiClient.get<Translation[]>(`/meetings/${id}/translations`),
  summary: (
    id: string,
    param: SummaryTemplate | { scope?: SummaryScope; section_ids?: string[]; sections_revision?: string; mode?: SummaryMode; template?: SummaryTemplate; prompt?: string; output_language: SummaryLanguage },
    output_language?: SummaryLanguage
  ) => {
    if (typeof param === "string") {
      return apiClient.post<Job>(`/meetings/${id}/summaries`, { template: param, output_language });
    }
    return apiClient.post<Job>(`/meetings/${id}/summaries`, { mode: param.mode || (param.prompt ? "custom" : "template"), ...param });
  },
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
  redeem: (token: string) => apiClient.post<Meeting>(`/shares/${token}/redeem`),
};
let cachedCapabilities: Promise<Capabilities> | null = null;
export const capabilitiesApi = {
  get: () => {
    if (!cachedCapabilities) {
      cachedCapabilities = apiClient.get<Capabilities>("/capabilities").catch((err) => {
        cachedCapabilities = null;
        throw err;
      });
    }
    return cachedCapabilities;
  },
};
export const jobsApi = { get: (id: string) => apiClient.get<Job>(`/jobs/${id}`) };
export const recordingApi = {
  latest: (id: string, signal?: AbortSignal) => apiClient.get<MeetingRecording>(`/meetings/${id}/recording`, { signal }),
  prepare: (id: string) => apiClient.post<MeetingRecording>(`/meetings/${id}/recording`),
  download: (meetingId: string, mediaId: string) => apiClient.getBlob(`/meetings/${meetingId}/media/${mediaId}/download`),
};

export const transcriptPolishApi = {
  latest: (id: string, signal?: AbortSignal) => apiClient.get<TranscriptPolish | null>(`/meetings/${id}/transcript/polish`, { signal }),
  create: (id: string) => apiClient.post<TranscriptPolish>(`/meetings/${id}/transcript/polish`),
  review: (id: string, jobId: string, body: { revision: number; segments: Array<{ logical_segment_id: string; text: string }> }, accept: boolean) => accept
    ? apiClient.post<TranscriptPolish>(`/meetings/${id}/transcript/polish/${jobId}/accept`, body)
    : apiClient.patch<TranscriptPolish>(`/meetings/${id}/transcript/polish/${jobId}`, body),
};

export const voiceLearningApi = {
  preview: (meetingId: string, segment_ids: string[]) => apiClient.postBlob(`/meetings/${meetingId}/voice-learning/preview`, { segment_ids }),
  learn: (meetingId: string, body: { segment_ids: string[]; name: string; profile_id?: string }) => apiClient.post<Job>(`/meetings/${meetingId}/voice-learning`, body),
};

export const sharesApi = {
  resolve: (token: string) => apiClient.get<{ meeting: Meeting; permission: Exclude<Permission, "owner"> }>(`/shares/${token}`),
  transcript: (token: string) => apiClient.get<{ meeting: Meeting; permission: Exclude<Permission, "owner">; transcript: Transcript }>(`/shares/${token}/transcript`),
  redeem: (token: string) => apiClient.post<Meeting>(`/shares/${token}/redeem`),
};

export const voiceProfilesApi = {
  list: () => apiClient.get<VoiceProfile[]>("/voice-profiles"),
  create: (name: string, audioFile: Blob | File) => {
    const data = new FormData();
    data.append("name", name);
    data.append("file", audioFile, audioFile instanceof File ? audioFile.name : "sample.webm");
    return apiClient.post<VoiceProfile>("/voice-profiles", data);
  },
  addSample: (profileId: string, audioFile: Blob | File) => {
    const data = new FormData();
    data.append("file", audioFile, audioFile instanceof File ? audioFile.name : "sample.webm");
    return apiClient.post<VoiceProfile>(`/voice-profiles/${profileId}/samples`, data);
  },
  delete: (profileId: string) => apiClient.delete<void>(`/voice-profiles/${profileId}`),
  deleteSample: (profileId: string, sampleIndex: number) => apiClient.delete<VoiceProfile>(`/voice-profiles/${profileId}/samples/${sampleIndex}`),
  getAudioBlob: (profileId: string, sampleIndex: number = 1) => apiClient.getBlob(`/voice-profiles/${profileId}/samples/${sampleIndex}/audio`),
  audioUrl: (profileId: string, sampleIndex: number = 1) => `${config.apiBaseUrl}/voice-profiles/${profileId}/samples/${sampleIndex}/audio`,
  getMyProfile: () => apiClient.get<VoiceProfile>("/voice-profiles/me"),
  enrollMyProfile: (audioFile: Blob | File) => {
    const data = new FormData();
    data.append("file", audioFile, audioFile instanceof File ? audioFile.name : "sample.webm");
    return apiClient.post<VoiceProfile>("/voice-profiles/me", data);
  },
  deleteMyProfile: () => apiClient.delete<void>("/voice-profiles/me"),
  listMeetingProfiles: (meetingId: string) => apiClient.get<VoiceProfile[]>(`/meetings/${meetingId}/voice-profiles`),
  enrollMeetingGuest: (meetingId: string, name: string, audioFile: Blob | File) => {
    const data = new FormData();
    data.append("name", name);
    data.append("file", audioFile, audioFile instanceof File ? audioFile.name : "sample.webm");
    return apiClient.post<VoiceProfile>(`/meetings/${meetingId}/voice-profiles`, data);
  },
  addMeetingGuestSample: (meetingId: string, profileId: string, audioFile: Blob | File) => {
    const data = new FormData();
    data.append("file", audioFile, audioFile instanceof File ? audioFile.name : "sample.webm");
    return apiClient.post<VoiceProfile>(`/meetings/${meetingId}/voice-profiles/${profileId}/samples`, data);
  },
  deleteMeetingGuest: (meetingId: string, profileId: string) => apiClient.delete<void>(`/meetings/${meetingId}/voice-profiles/${profileId}`),
};
