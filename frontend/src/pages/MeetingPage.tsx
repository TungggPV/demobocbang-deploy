import { forwardRef, useCallback, useEffect, useImperativeHandle, useRef, useState, useMemo } from "react";
import { useAuth } from "../auth/AuthContext";
import { MeetingSocket, type RealtimeEvent } from "../realtime/meetingSocket";
import { MicrophonePcmCapture } from "../realtime/microphonePcm";
import { finishLiveTranscript, updateLiveTranscript, type LiveTranscript, type LiveTranscriptEntry, type TranscriptEvent } from "../realtime/liveTranscript";
import { recordRender } from "../realtime/diagnostics";
import { capabilitiesApi, meetingsApi, type TranscriptMutation } from "../api/endpoints";
import { ApiError } from "../api/client";
import { useAsync } from "../hooks/useAsync";
import { useJobMonitor } from "../state/useJobMonitor";
import { can } from "../utils/permissions";
import { languageLabel } from "../utils/language";
import { formatTimestampInput, parseTimestampInput } from "../utils/time";
import type { ExportFormat, MediaFile, Meeting, Permission, Segment, Speaker, SummaryLanguage, SummaryTemplate, Transcript, Translation } from "../types/domain";
import { Button, Empty, Failure, Icon, Loading, Status, statusLabel } from "../components/ui";
import { ThemeToggle } from "../components/ThemeToggle";

function formatVietnameseDateTime(dateStr?: string | Date) {
  const d = dateStr ? new Date(dateStr) : new Date();
  const dayNames = ["CN", "T2", "T3", "T4", "T5", "T6", "T7"];
  const dayName = dayNames[d.getDay()];
  const dd = String(d.getDate()).padStart(2, "0");
  const mm = String(d.getMonth() + 1).padStart(2, "0");
  const yyyy = d.getFullYear();
  const hh = String(d.getHours()).padStart(2, "0");
  const min = String(d.getMinutes()).padStart(2, "0");
  return `${dayName}, ${dd}-${mm}-${yyyy} lúc ${hh}:${min}`;
}

type SegmentDraft = { text: string; startTime: string; endTime: string; speakerId: string };
type AudioPlayerHandle = {
  playSegment: (startMs: number, endMs: number) => void;
  seek: (milliseconds: number) => void;
  play: () => void;
  pause: () => void;
  toggle: () => void;
  toggleMute: () => boolean;
};
type RealtimeTranslation = { text: string; sourceLanguage: string; targetLanguage: string };

const SPEAKER_PALETTE = [
  "#ec4899", // pink
  "#ef4444", // coral/red
  "#a855f7", // purple
  "#3b82f6", // blue
  "#0ea5e9", // sky
  "#f59e0b", // amber
  "#10b981", // emerald
  "#6366f1", // indigo
];

function getSpeakerDisplay(
  speakerId: string | undefined,
  speakerLabel: string | undefined,
  speakers: Speaker[] = []
) {
  const found = speakerId ? speakers.find((s) => s.id === speakerId) : undefined;
  const rawName = found?.name || (speakerLabel ? `Speaker ${speakerLabel}` : "Không xác định");
  const isUnknown = !found?.name && !speakerLabel;

  if (isUnknown || rawName.toLowerCase().includes("không xác định") || rawName.toLowerCase().includes("chưa xác định")) {
    return {
      name: "Không xác định",
      initial: "?",
      color: "#18181b",
    };
  }

  let hash = 0;
  const str = found?.id || rawName;
  for (let i = 0; i < str.length; i++) {
    hash = (hash << 5) - hash + str.charCodeAt(i);
    hash |= 0;
  }
  const colorIndex = Math.abs(hash) % SPEAKER_PALETTE.length;
  const color = SPEAKER_PALETTE[colorIndex];
  let initial = "S";
  if (!rawName.toLowerCase().startsWith("speaker") && !rawName.toLowerCase().startsWith("người nói")) {
    initial = rawName.trim()[0]?.toUpperCase() || "S";
  }

  return {
    name: rawName,
    initial,
    color,
  };
}

function copyToClipboard(text: string): Promise<boolean> {
  if (navigator?.clipboard?.writeText) {
    return navigator.clipboard.writeText(text).then(() => true).catch(() => fallbackCopy(text));
  }
  return Promise.resolve(fallbackCopy(text));
}

function fallbackCopy(text: string): boolean {
  try {
    const el = document.createElement("textarea");
    el.value = text;
    el.style.position = "fixed";
    el.style.opacity = "0";
    document.body.appendChild(el);
    el.select();
    const success = document.execCommand("copy");
    document.body.removeChild(el);
    return Boolean(success);
  } catch {
    return false;
  }
}

export function MeetingPage({ meetingId }: { meetingId: string }) {
  const { user } = useAuth();

  // Modals & Drawers state (declared early for lazy loading)
  const [renameOpen, setRenameOpen] = useState(false);
  const [summaryDrawerOpen, setSummaryDrawerOpen] = useState(false);
  const [speakersDrawerOpen, setSpeakersDrawerOpen] = useState(false);
  const [exportModalOpen, setExportModalOpen] = useState(false);
  const [playbackMode, setPlaybackMode] = useState(false);
  const [autoScroll, setAutoScroll] = useState(true);
  const [shareSocialOpen, setShareSocialOpen] = useState(false);
  const [isMuted, setIsMuted] = useState(false);
  const [logAiOpen, setLogAiOpen] = useState(false);

  // High-performance single-request bundle
  const bundle = useAsync(() => meetingsApi.bundle(meetingId), [meetingId]);

  const meeting: {
    data: Meeting | undefined;
    loading: boolean;
    error: Error | undefined;
    reload: () => Promise<void>;
    setData: React.Dispatch<React.SetStateAction<Meeting | undefined>>;
  } = {
    data: bundle.data?.meeting,
    loading: bundle.loading,
    error: bundle.error,
    reload: async () => {
      try {
        const nextMeeting = await meetingsApi.get(meetingId);
        bundle.setData((prev) => (prev ? { ...prev, meeting: nextMeeting } : prev));
      } catch {
        // ignore
      }
    },
    setData: (fnOrVal) => {
      bundle.setData((prev) => {
        if (!prev) return prev;
        const nextMeeting = typeof fnOrVal === "function" ? (fnOrVal as any)(prev.meeting) : fnOrVal;
        return { ...prev, meeting: nextMeeting };
      });
    },
  };

  const transcript: {
    data: Transcript | undefined;
    loading: boolean;
    error: Error | undefined;
    reload: () => Promise<void>;
    setData: React.Dispatch<React.SetStateAction<Transcript | undefined>>;
  } = {
    data: bundle.data?.transcript ?? undefined,
    loading: bundle.loading,
    error: bundle.error,
    reload: async () => {
      try {
        const nextTr = await meetingsApi.transcript(meetingId);
        bundle.setData((prev) => (prev ? { ...prev, transcript: nextTr } : prev));
      } catch {
        // ignore
      }
    },
    setData: (fnOrVal) => {
      bundle.setData((prev) => {
        if (!prev) return prev;
        const nextTr = typeof fnOrVal === "function" ? (fnOrVal as any)(prev.transcript) : fnOrVal;
        return { ...prev, transcript: nextTr };
      });
    },
  };

  const media: {
    data: MediaFile[] | undefined;
    loading: boolean;
    error: Error | undefined;
    reload: () => Promise<void>;
    setData: React.Dispatch<React.SetStateAction<MediaFile[] | undefined>>;
  } = {
    data: bundle.data?.media,
    loading: bundle.loading,
    error: bundle.error,
    reload: async () => {
      try {
        const nextMed = await meetingsApi.media(meetingId);
        bundle.setData((prev) => (prev ? { ...prev, media: nextMed } : prev));
      } catch {
        // ignore
      }
    },
    setData: (fnOrVal) => {
      bundle.setData((prev) => {
        if (!prev) return prev;
        const nextMed = typeof fnOrVal === "function" ? (fnOrVal as any)(prev.media) : fnOrVal;
        return { ...prev, media: nextMed };
      });
    },
  };

  const speakers: {
    data: Speaker[] | undefined;
    loading: boolean;
    error: Error | undefined;
    reload: () => Promise<void>;
    setData: React.Dispatch<React.SetStateAction<Speaker[] | undefined>>;
  } = {
    data: bundle.data?.speakers,
    loading: bundle.loading,
    error: bundle.error,
    reload: async () => {
      try {
        const nextSpk = await meetingsApi.speakers(meetingId);
        bundle.setData((prev) => (prev ? { ...prev, speakers: nextSpk } : prev));
      } catch {
        // ignore
      }
    },
    setData: (fnOrVal) => {
      bundle.setData((prev) => {
        if (!prev) return prev;
        const nextSpk = typeof fnOrVal === "function" ? (fnOrVal as any)(prev.speakers) : fnOrVal;
        return { ...prev, speakers: nextSpk };
      });
    },
  };

  const translations: {
    data: Translation[] | undefined;
    loading: boolean;
    error: Error | undefined;
    reload: () => Promise<void>;
    setData: React.Dispatch<React.SetStateAction<Translation[] | undefined>>;
  } = {
    data: bundle.data?.translations,
    loading: bundle.loading,
    error: bundle.error,
    reload: async () => {
      try {
        const nextTrans = await meetingsApi.translations(meetingId);
        bundle.setData((prev) => (prev ? { ...prev, translations: nextTrans } : prev));
      } catch {
        // ignore
      }
    },
    setData: (fnOrVal) => {
      bundle.setData((prev) => {
        if (!prev) return prev;
        const nextTrans = typeof fnOrVal === "function" ? (fnOrVal as any)(prev.translations) : fnOrVal;
        return { ...prev, translations: nextTrans };
      });
    },
  };

  // Lazy-load summaries only when summary drawer is open
  const summaries = useAsync(
    () => (summaryDrawerOpen ? meetingsApi.summaries(meetingId) : Promise.resolve([])),
    [meetingId, summaryDrawerOpen]
  );

  const capabilities = useAsync(capabilitiesApi.get, []);
  const versions = useAsync(() => meetingsApi.versions(meetingId), [meetingId]);

  // Split view toggle (defaults to true)
  const [showTranslationSplit, setShowTranslationSplit] = useState<boolean>(() => {
    try {
      const saved = localStorage.getItem("bocbang_show_translation");
      return saved !== null ? saved === "true" : true;
    } catch {
      return true;
    }
  });

  const toggleTranslationSplit = useCallback(() => {
    setShowTranslationSplit((prev) => {
      const next = !prev;
      try {
        localStorage.setItem("bocbang_show_translation", String(next));
      } catch {
        // ignore
      }
      return next;
    });
  }, []);

  const [editing, setEditing] = useState<Segment>();
  const [draft, setDraft] = useState<SegmentDraft>({ text: "", startTime: "00:00", endTime: "00:00", speakerId: "" });
  const [notice, setNotice] = useState("");
  const [recordingActive, setRecordingActive] = useState(false);
  const [liveTranscript, setLiveTranscript] = useState<LiveTranscript>({});



  // Segment operations
  const [pendingDelete, setPendingDelete] = useState<Segment>();
  const [pendingSplit, setPendingSplit] = useState<Segment>();
  const [mergeIds, setMergeIds] = useState<string[]>([]);
  const [pendingMerge, setPendingMerge] = useState<Segment[]>();
  const [activeSegmentId, setActiveSegmentId] = useState<string>();
  const [rowMenuOpenId, setRowMenuOpenId] = useState<string | null>(null);
  const [rightRowMenuOpenId, setRightRowMenuOpenId] = useState<string | null>(null);

  // Realtime translations
  const [realtimeTranslations, setRealtimeTranslations] = useState<Record<string, RealtimeTranslation>>({});
  const [showRealtimeTranslations, setShowRealtimeTranslations] = useState(false);

  // Translation generation in split view
  const translationJobs = useJobMonitor();
  const [targetLang, setTargetLang] = useState<string>("en");
  const [translationError, setTranslationError] = useState("");
  const latestTranslation: Translation | undefined = translations.data?.[0];
  const isTranslating = ["queued", "processing", "retrying"].includes(translationJobs.job?.status ?? "");

  const triggerTranslation = useCallback(async () => {
    if (!transcript.data?.segments.length) return;
    try {
      setTranslationError("");
      const source = transcript.data?.language ?? meeting.data?.language_mode ?? "auto";
      const job = await meetingsApi.translation(meetingId, source, targetLang);
      translationJobs.track(job);
    } catch (err) {
      setTranslationError((err as Error).message);
    }
  }, [transcript.data?.segments.length, transcript.data?.language, meeting.data?.language_mode, meetingId, targetLang, translationJobs]);

  const player = useRef<AudioPlayerHandle>(null);

  const updateRecordingState = useCallback((active: boolean) => {
    setRecordingActive(active);
    if (!active) setLiveTranscript((current) => finishLiveTranscript(current));
  }, []);

  const refreshMedia = useCallback(async () => {
    for (let attempt = 0; attempt < 6; attempt++) {
      try {
        const items = await meetingsApi.media(meetingId);
        if (items && items.length > 0) {
          media.setData(items);
          return;
        }
      } catch {
        // ignore
      }
      await new Promise((resolve) => setTimeout(resolve, 500));
    }
    void media.reload();
  }, [meetingId, media]);

  useEffect(() => {
    const stored = new Set(transcript.data?.segments.map((segment) => segment.logical_segment_id));
    setLiveTranscript((current) =>
      Object.fromEntries(
        Object.entries(current).filter(
          ([, event]) => event.type !== "transcript.final" || !stored.has(event.logical_segment_id)
        )
      )
    );
  }, [transcript.data]);

  useEffect(() => {
    if (editing) {
      setDraft({
        text: editing.text,
        startTime: formatTimestampInput(editing.start_ms),
        endTime: formatTimestampInput(editing.end_ms),
        speakerId: editing.speaker_id ?? "",
      });
    }
  }, [editing]);

  const transcriptBottomRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!autoScroll || !activeSegmentId || editing) return;
    document.getElementById(`segment-${activeSegmentId}`)?.scrollIntoView({ block: "center", behavior: "smooth" });
  }, [activeSegmentId, editing, autoScroll]);

  useEffect(() => {
    if (!autoScroll) return;
    if (recordingActive || Object.keys(liveTranscript).length > 0) {
      transcriptBottomRef.current?.scrollIntoView({ behavior: "smooth", block: "end" });
    }
  }, [liveTranscript, recordingActive, autoScroll]);

  useEffect(() => {
    if (translationJobs.job?.status === "completed") {
      meetingsApi.invalidateBundle(meetingId);
      void translations.reload();
      void meetingsApi.translations(meetingId).then((data) => {
        bundle.setData((prev) => (prev ? { ...prev, translations: data } : prev));
      });
    }
  }, [translationJobs.job?.status, meetingId]);





  // Support direct modal links (?modal=share, ?modal=export, ?modal=summary, ?modal=speakers)
  useEffect(() => {
    setPlaybackMode(false);
  }, [meetingId]);

  useEffect(() => {
    const params = new URLSearchParams(window.location.search);
    const modal = params.get("modal");
    if (modal === "share") setShareSocialOpen(true);
    else if (modal === "export") setExportModalOpen(true);
    else if (modal === "summary") setSummaryDrawerOpen(true);
    else if (modal === "speakers") setSpeakersDrawerOpen(true);
  }, [meeting.data?.id]);

  // Click outside to close row menus
  useEffect(() => {
    function handleClick(e: MouseEvent) {
      if (!(e.target as HTMLElement).closest(".dual-cell-actions")) {
        setRowMenuOpenId(null);
        setRightRowMenuOpenId(null);
      }
    }
    document.addEventListener("mousedown", handleClick);
    return () => document.removeEventListener("mousedown", handleClick);
  }, []);

  function applyMutation(result: TranscriptMutation, removedId?: string) {
    transcript.setData((current) => {
      if (!current) return current;
      return {
        ...current,
        version_id: result.version_id,
        version_no: result.version_no,
        segments: removedId
          ? current.segments.filter((segment) => segment.logical_segment_id !== removedId)
          : current.segments.map((segment) =>
              segment.logical_segment_id === result.segment?.logical_segment_id ? result.segment : segment
            ),
      };
    });
    void versions.reload();
  }

  async function save() {
    if (!editing) return;
    const text = draft.text.trim();
    if (!text) return setNotice("Nội dung đoạn ghi âm không được để trống.");
    const segmentToSave = editing;
    const changedSpeaker = draft.speakerId || null;
    const startMs = parseTimestampInput(draft.startTime);
    const endMs = parseTimestampInput(draft.endTime);
    if (startMs === undefined || endMs === undefined || endMs <= startMs) {
      return setNotice("Nhập thời gian theo mm:ss hoặc hh:mm:ss; mốc kết thúc phải lớn hơn mốc bắt đầu.");
    }

    setEditing(undefined);

    // Optimistic update
    transcript.setData((current) => {
      if (!current) return current;
      return {
        ...current,
        segments: current.segments.map((s) =>
          s.logical_segment_id === segmentToSave.logical_segment_id
            ? { ...s, text, start_ms: startMs, end_ms: endMs, speaker_id: changedSpeaker ?? undefined }
            : s
        ),
      };
    });

    try {
      const body: { text?: string; start_ms?: number; end_ms?: number } = {};
      if (segmentToSave.text !== text) body.text = text;
      if (
        draft.startTime !== formatTimestampInput(segmentToSave.start_ms) ||
        draft.endTime !== formatTimestampInput(segmentToSave.end_ms)
      ) {
        body.start_ms = startMs;
        body.end_ms = endMs;
      }
      if (Object.keys(body).length) await meetingsApi.saveSegment(segmentToSave.logical_segment_id, body);
      if ((segmentToSave.speaker_id ?? null) !== changedSpeaker) {
        await meetingsApi.setSegmentSpeaker(segmentToSave.logical_segment_id, changedSpeaker);
      }
      void transcript.reload();
      void versions.reload();
      setNotice("Đã lưu thay đổi.");
    } catch (cause) {
      await transcript.reload();
      setNotice(meetingErrorMessage(cause));
    }
  }

  async function handleSplit(segment: Segment, splitAt: number, first: string, second: string) {
    setPendingSplit(undefined);
    const firstSegment: Segment = { ...segment, end_ms: splitAt, text: first };
    const secondSegment: Segment = {
      ...segment,
      logical_segment_id: `temp-split-${Date.now()}`,
      start_ms: splitAt,
      text: second,
    };

    transcript.setData((current) => {
      if (!current) return current;
      const idx = current.segments.findIndex((s) => s.logical_segment_id === segment.logical_segment_id);
      if (idx === -1) return current;
      const nextSegments = [...current.segments];
      nextSegments.splice(idx, 1, firstSegment, secondSegment);
      return { ...current, segments: nextSegments };
    });

    try {
      await meetingsApi.splitSegment(segment.logical_segment_id, splitAt, first, second);
      setNotice("Đã tách đoạn thành công.");
      void transcript.reload();
      void versions.reload();
    } catch (cause) {
      await transcript.reload();
      setNotice(meetingErrorMessage(cause));
    }
  }

  async function handleMerge(selected: Segment[]) {
    const sorted = [...selected].sort((a, b) => a.start_ms - b.start_ms);
    setPendingMerge(undefined);
    setMergeIds([]);

    const primaryId = sorted[0].logical_segment_id;
    const removedIds = new Set(sorted.slice(1).map((s) => s.logical_segment_id));
    const mergedSegment: Segment = {
      ...sorted[0],
      end_ms: Math.max(...sorted.map((s) => s.end_ms)),
      text: sorted.map((s) => s.text.trim()).filter(Boolean).join(" "),
    };

    transcript.setData((current) => {
      if (!current) return current;
      const nextSegments = current.segments
        .filter((s) => !removedIds.has(s.logical_segment_id))
        .map((s) => (s.logical_segment_id === primaryId ? mergedSegment : s));
      return { ...current, segments: nextSegments };
    });

    try {
      await meetingsApi.mergeSegments(sorted.map((s) => s.logical_segment_id));
      setNotice(`Đã gộp ${sorted.length} đoạn thành công.`);
      void transcript.reload();
      void translations.reload();
      void versions.reload();
    } catch (cause) {
      await transcript.reload();
      setNotice(meetingErrorMessage(cause));
    }
  }

  async function mutate(action: () => Promise<TranscriptMutation>, success: string, removedId?: string) {
    if (removedId) {
      transcript.setData((current) => {
        if (!current) return current;
        return {
          ...current,
          segments: current.segments.filter((segment) => segment.logical_segment_id !== removedId),
        };
      });
    }
    try {
      const result = await action();
      if (removedId) applyMutation(result, removedId);
      else {
        await transcript.reload();
        await versions.reload();
      }
      setNotice(success);
    } catch (cause) {
      await transcript.reload();
      setNotice(meetingErrorMessage(cause));
    }
  }

  function toggleMerge(segment: Segment) {
    setMergeIds((current) =>
      current.includes(segment.logical_segment_id)
        ? current.filter((id) => id !== segment.logical_segment_id)
        : [...current, segment.logical_segment_id]
    );
  }

  function prepareMerge() {
    const selected = orderedSegments.filter((item) => mergeIds.includes(item.logical_segment_id));
    if (selected.length < 2) return setNotice("Chọn ít nhất hai đoạn để gộp.");
    const indexes = selected
      .map((item) => orderedSegments.findIndex((segment) => segment.logical_segment_id === item.logical_segment_id))
      .sort((a, b) => a - b);
    for (let i = 1; i < indexes.length; i++) {
      if (indexes[i] !== indexes[i - 1] + 1) {
        return setNotice("Các đoạn được chọn phải nằm liền kề nhau.");
      }
    }
    setPendingMerge(selected);
  }



  if (meeting.loading) {
    return (
      <div className="meeting-page-container" style={{ padding: 32, display: "flex", justifyContent: "center", alignItems: "center", minHeight: "50vh" }}>
        <Loading label="Đang tải phiên họp..." />
      </div>
    );
  }
  if (meeting.error || !meeting.data)
    return <Failure error={meeting.error ?? new Error("Không tìm thấy cuộc họp.")} retry={meeting.reload} />;

  const editable = can(meeting.data.permission, "edit");
  const owner = meeting.data.permission === "owner";
  const realtimeTarget =
    meeting.data.language_mode === "auto"
      ? "auto"
      : meeting.data.language_mode === "vi"
      ? "en"
      : meeting.data.language_mode === "en"
      ? "vi"
      : undefined;
  const translationPairs = capabilities.data?.translation.language_pairs ?? [];
  const realtimeTranslationSupported =
    meeting.data.language_mode === "auto"
      ? [["vi", "en"], ["en", "vi"]].every(([source, target]) =>
          translationPairs.some(([from, to]) => from === source && to === target)
        )
      : Boolean(
          realtimeTarget &&
            translationPairs.some(([source, target]) => source === meeting.data?.language_mode && target === realtimeTarget)
        );

  // Chronological order (earliest to latest) matching screenshot
  const orderedSegments = transcript.data?.segments
    ? [...transcript.data.segments].sort((a, b) => a.start_ms - b.start_ms)
    : [];

  const liveEntries = Object.entries(liveTranscript)
    .filter(
      ([, event]) =>
        event.type !== "transcript.final" ||
        !transcript.data?.segments.some((segment) => segment.logical_segment_id === event.logical_segment_id)
    )
    .sort(([, a], [, b]) => a.start_ms - b.start_ms);

  const hasTranscriptContent = orderedSegments.length > 0 || liveEntries.length > 0;

  const selectedMergeSegments = transcript.data?.segments.filter((segment) => mergeIds.includes(segment.logical_segment_id)) ?? [];

  const initialRecordingDurationMs = Math.max(
    meeting.data.duration_ms ?? 0,
    media.data?.[0]?.duration_ms ?? 0,
    ...(transcript.data?.segments.map((segment) => segment.end_ms) ?? [0])
  );

  const translationMap = new Map<string, string>();
  if (latestTranslation?.segments) {
    for (const item of latestTranslation.segments) {
      translationMap.set(item.logical_segment_id, item.text);
    }
  }
  const isRealtimeRecording =
    editable &&
    meeting.data.source_type === "realtime" &&
    meeting.data.status !== "completed" &&
    Boolean(capabilities.data?.stt.streaming);

  return (
    <div className="meeting-page-container">
      {/* Topbar & Breadcrumb */}
      <div className="meeting-topbar">
        <div className="topbar-left">
          <div className="topbar-breadcrumb">
            <span className="topbar-breadcrumb-root">{playbackMode ? "Phát Lại Ghi Âm" : "Ghi Âm Cuộc Họp"}</span>
            <span className="topbar-breadcrumb-sep">&gt;</span>
            <span
              className="topbar-breadcrumb-title"
              onClick={() => editable && setRenameOpen(true)}
              title={editable ? "Nhấp để đổi tên" : undefined}
            >
              <span>{meeting.data.title}</span>
              {editable && <Icon name="edit" />}
            </span>
            <span className="topbar-status-badge">
              {recordingActive ? "● Đang ghi âm" : meeting.data.status === "processing" ? "● Đang xử lý" : "● Sẵn sàng"}
            </span>
          </div>
        </div>

        <div className="topbar-right">
          <button
            type="button"
            className={`topbar-trans-toggle-btn ${showTranslationSplit ? "is-active" : ""}`}
            onClick={toggleTranslationSplit}
            title={showTranslationSplit ? "Ẩn cột bản dịch" : "Hiển thị cột bản dịch"}
          >
            <span style={{ fontSize: 13, marginRight: 4, fontWeight: 700 }}>文A</span>
            <span>{showTranslationSplit ? "Ẩn bản dịch" : "Hiển thị bản dịch"}</span>
          </button>
          <ThemeToggle />
        </div>
      </div>



      {notice && (
        <div className="notice" style={{ marginBottom: 16 }}>
          <span>{notice}</span>
          <button
            type="button"
            onClick={() => setNotice("")}
            style={{ border: 0, background: "transparent", cursor: "pointer", marginLeft: "auto" }}
          >
            ×
          </button>
        </div>
      )}

      {/* Top Recording Panel moved to Docked Bottom Bar */}

      {/* Media Player (Hidden audio element + Full Bottom Player when playbackMode is on) */}
      {media.data?.[0] && (
        <MediaPlayer
          ref={player}
          meetingId={meetingId}
          mediaId={media.data[0].id}
          filename={media.data[0].original_filename}
          segments={transcript.data?.segments ?? []}
          playbackMode={playbackMode}
          onClosePlayback={() => {
            player.current?.pause();
            setPlaybackMode(false);
          }}
          onActiveSegment={setActiveSegmentId}
          onSummary={() => setSummaryDrawerOpen(true)}
          onSpeakers={() => setSpeakersDrawerOpen(true)}
          onShare={() => setShareSocialOpen(true)}
          onExport={() => setExportModalOpen(true)}
          downloadUrl={`/api/v1/meetings/${meetingId}/media/${media.data[0].id}/download`}
        />
      )}

      {/* FLOATING MERGE ACTION BAR (ALWAYS VISIBLE & STICKY WHEN SEGMENTS ARE SELECTED) */}
      {mergeIds.length > 0 && (
        <div className="floating-merge-toolbar">
          <div className="floating-merge-info">
            <span className="floating-merge-count-badge">{mergeIds.length}</span>
            <span className="floating-merge-text">Đã chọn {mergeIds.length} đoạn</span>
            <div className="floating-merge-chips">
              {orderedSegments.filter((s) => mergeIds.includes(s.logical_segment_id)).map((segment, index) => (
                <span
                  key={segment.logical_segment_id}
                  className="floating-merge-chip"
                  title={segment.text}
                >
                  <b>#{index + 1}</b> {segment.text.slice(0, 24)}...
                  <button
                    type="button"
                    onClick={() => toggleMerge(segment)}
                    title="Bỏ chọn"
                  >
                    ×
                  </button>
                </span>
              ))}
            </div>
          </div>
          <div className="floating-merge-actions">
            <button
              type="button"
              className="btn-merge-action confirm"
              disabled={mergeIds.length < 2}
              onClick={prepareMerge}
              title={mergeIds.length < 2 ? "Hãy chọn thêm ít nhất 1 đoạn liền kề để gộp" : "Gộp các đoạn đã chọn"}
            >
              🔗 Gộp {mergeIds.length} đoạn
            </button>
            <button
              type="button"
              className="btn-merge-action cancel"
              onClick={() => setMergeIds([])}
              title="Hủy chọn tất cả"
            >
              Bỏ chọn
            </button>
          </div>
        </div>
      )}

      {/* Live Incoming Streaming Transcripts rendered inside dual-split-card */}



      {/* DUAL COLUMN SPLIT VIEW (Bản ghi & Bản dịch) */}
      <div className="meeting-content-flex">
        <div className="meeting-content-main">
          <div className={`dual-split-card ${showTranslationSplit ? "is-split" : "is-single"} ${playbackMode ? "is-playback" : ""}`}>
          {/* Clean header handled by meeting-columns-bar */}

          {translationError && (
            <div className="form-error" style={{ margin: "8px 16px" }}>
              {translationError}
            </div>
          )}

          {/* Rows: Side-by-side segment pairs OR Modern Empty State */}
          {!hasTranscriptContent ? (
            <div className="modern-empty-state">
              <div className="empty-state-soundwave-icon">
                {recordingActive ? (
                  <span className="record-pulsing-dot red" style={{ width: 18, height: 18 }} />
                ) : (
                  <svg width="34" height="34" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
                    <path d="M12 2a3 3 0 0 0-3 3v7a3 3 0 0 0 6 0V5a3 3 0 0 0-3-3Z" />
                    <path d="M19 10v2a7 7 0 0 1-14 0v-2" />
                    <line x1="12" y1="19" x2="12" y2="22" />
                  </svg>
                )}
              </div>
              <h3 className="empty-state-title">
                {recordingActive ? "Đang lắng nghe âm thanh…" : "Sẵn sàng ghi âm cuộc họp"}
              </h3>
              <p className="empty-state-desc">
                {recordingActive
                  ? "Hãy nói vào microphone, nội dung nhận diện và bản dịch sẽ hiển thị đồng bộ tại đây."
                  : "Bấm nút Bắt đầu ghi âm ở thanh công cụ bên dưới hoặc tải lên tệp âm thanh sẵn có để tự động chuyển văn bản và dịch song song."}
              </p>
            </div>
          ) : (
            <div className="dual-rows-wrapper">
              {orderedSegments.map((segment, segIdx) => {
              const nextSegment = segIdx < orderedSegments.length - 1 ? orderedSegments[segIdx + 1] : null;
              const isPlaying = activeSegmentId === segment.logical_segment_id;
              const isMergeSelected = mergeIds.includes(segment.logical_segment_id);
              const isEditing = editing?.logical_segment_id === segment.logical_segment_id;
              const speakerInfo = getSpeakerDisplay(segment.speaker_id, segment.speaker_label, speakers.data);

              const transText =
                translationMap.get(segment.logical_segment_id) ||
                realtimeTranslations[segment.logical_segment_id]?.text;

              const isRowMenuOpen = rowMenuOpenId === segment.logical_segment_id;

              return (
                <div
                  key={segment.logical_segment_id}
                  id={`segment-${segment.logical_segment_id}`}
                  className={`dual-row-pair ${isPlaying ? "is-playing" : ""} ${
                    isMergeSelected ? "is-merge-selected" : ""
                  } ${mergeIds.length > 0 ? "is-in-merge-mode" : ""}`}
                  onClick={(e) => {
                    if (mergeIds.length > 0) {
                      const target = e.target as HTMLElement;
                      if (!target.closest("button, textarea, input, select, .dual-row-menu-dropdown")) {
                        toggleMerge(segment);
                      }
                    }
                  }}
                >
                  {/* Left Cell: Transcript */}
                  <div className="dual-cell left">
                    {/* Checkbox for merge selection (hover-visible when 0 selected, always visible in merge mode) */}
                    {editable && (
                      <button
                        type="button"
                        className={`row-merge-checkbox ${isMergeSelected ? "is-checked" : ""} ${
                          mergeIds.length > 0 ? "in-merge-mode" : "hover-only"
                        }`}
                        onClick={(e) => {
                          e.stopPropagation();
                          toggleMerge(segment);
                        }}
                        title={isMergeSelected ? "Bỏ chọn đoạn này" : "Chọn gộp đoạn này"}
                      >
                        {isMergeSelected ? <Icon name="check" /> : null}
                      </button>
                    )}

                    {isEditing ? (
                      <div className="editor" style={{ flex: 1 }}>
                        <label>
                          Nội dung
                          <textarea
                            value={draft.text}
                            onChange={(event) => setDraft({ ...draft, text: event.target.value })}
                          />
                        </label>
                        <div className="segment-time-fields">
                          <label>
                            Bắt đầu (mm:ss)
                            <input
                              inputMode="numeric"
                              value={draft.startTime}
                              onChange={(event) => setDraft({ ...draft, startTime: event.target.value })}
                            />
                          </label>
                          <label>
                            Kết thúc (mm:ss)
                            <input
                              inputMode="numeric"
                              value={draft.endTime}
                              onChange={(event) => setDraft({ ...draft, endTime: event.target.value })}
                            />
                          </label>
                        </div>
                        <label>
                          Người nói
                          <select
                            value={draft.speakerId}
                            onChange={(event) => setDraft({ ...draft, speakerId: event.target.value })}
                          >
                            <option value="">Chưa xác định</option>
                            {speakers.data?.map((spk) => (
                              <option key={spk.id} value={spk.id}>
                                {spk.name}
                              </option>
                            ))}
                          </select>
                        </label>
                        <div className="form-actions">
                          <Button onClick={() => setEditing(undefined)}>Hủy</Button>
                          <Button tone="dark" onClick={() => void save()}>
                            Lưu
                          </Button>
                        </div>
                      </div>
                    ) : (
                      <>
                        <div className="dual-cell-main">
                          <span className="dual-speaker-name">
                            {speakerInfo.name}:
                            {isMergeSelected && (
                              <span className="merge-row-selected-badge">✓ Đã chọn</span>
                            )}
                          </span>
                          <span className="dual-text">{segment.text}</span>
                          <div className="sentence-hover-actions">
                            <span className="sentence-hover-time">
                              {formatTime(segment.start_ms)} - {formatTime(segment.end_ms)}
                            </span>
                            <button
                              type="button"
                              className="sentence-hover-btn"
                              onClick={() => {
                                setPlaybackMode(true);
                                player.current?.playSegment(segment.start_ms, segment.end_ms);
                              }}
                              title={`Phát câu này (${formatTime(segment.start_ms)} - ${formatTime(segment.end_ms)})`}
                            >
                              ▷ Phát
                            </button>
                          </div>
                        </div>

                        {/* Three-dot menu */}
                        <div className="dual-cell-actions">
                          <button
                            type="button"
                            className={`dual-more-btn ${isRowMenuOpen ? "active" : ""}`}
                            onClick={(e) => {
                              e.stopPropagation();
                              setRowMenuOpenId(isRowMenuOpen ? null : segment.logical_segment_id);
                            }}
                            title="Tùy chọn đoạn"
                          >
                            <Icon name="more" />
                          </button>

                          {isRowMenuOpen && (
                            <div className="dual-row-menu-dropdown">
                              {editable && (
                                <button
                                  type="button"
                                  onClick={() => {
                                    setRowMenuOpenId(null);
        setRightRowMenuOpenId(null);
                                    setEditing(segment);
                                  }}
                                >
                                  <Icon name="edit" />
                                  <span>Sửa</span>
                                </button>
                              )}
                              {editable && (
                                <button
                                  type="button"
                                  onClick={() => {
                                    setRowMenuOpenId(null);
        setRightRowMenuOpenId(null);
                                    setPendingSplit(segment);
                                  }}
                                >
                                  <span>✂ Tách đoạn</span>
                                </button>
                              )}
                              {editable && nextSegment && (
                                <button
                                  type="button"
                                  onClick={() => {
                                    setRowMenuOpenId(null);
                                    setRightRowMenuOpenId(null);
                                    setPendingMerge([segment, nextSegment]);
                                  }}
                                  title={`Gộp ngay với đoạn dưới: "${nextSegment.text.slice(0, 30)}..."`}
                                >
                                  <span>🔗 Gộp với đoạn dưới</span>
                                </button>
                              )}
                              {editable && (
                                <button
                                  type="button"
                                  onClick={() => {
                                    setRowMenuOpenId(null);
                                    setRightRowMenuOpenId(null);
                                    toggleMerge(segment);
                                  }}
                                >
                                  <span>{isMergeSelected ? "✕ Bỏ chọn gộp" : "☑ Chọn gộp nhiều đoạn"}</span>
                                </button>
                              )}
                              <button
                                type="button"
                                onClick={() => {
                                  setRowMenuOpenId(null);
        setRightRowMenuOpenId(null);
                                  void copyToClipboard(segment.text);
                                  setNotice("Đã sao chép nội dung.");
                                }}
                              >
                                <Icon name="copy" />
                                <span>Sao chép</span>
                              </button>
                              {editable && (
                                <button
                                  type="button"
                                  className="text-danger"
                                  onClick={() => {
                                    setRowMenuOpenId(null);
        setRightRowMenuOpenId(null);
                                    setPendingDelete(segment);
                                  }}
                                >
                                  <Icon name="trash" />
                                  <span>Xóa</span>
                                </button>
                              )}
                            </div>
                          )}
                        </div>
                      </>
                    )}
                  </div>

                  {/* Right Cell: Translation (if showTranslationSplit) */}
                  {showTranslationSplit && (
                    <div className="dual-cell right">
                      <div className="dual-cell-main">
                        <span className="dual-speaker-name">{speakerInfo.name}:</span>
                        <span className="dual-text">
                          {transText || (
                            <span style={{ display: "inline-flex", alignItems: "center", gap: 8 }}>
                              <em style={{ color: "var(--muted)", fontSize: 13 }}>Chưa có bản dịch</em>
                              {editable && (
                                <button
                                  type="button"
                                  className="btn btn-ghost"
                                  style={{ padding: "2px 8px", fontSize: 11, height: 24, borderRadius: 4 }}
                                  onClick={() => void triggerTranslation()}
                                  disabled={isTranslating}
                                >
                                  {isTranslating ? "Đang dịch…" : "Dịch"}
                                </button>
                              )}
                            </span>
                          )}
                        </span>
                        <div className="sentence-hover-actions">
                          <span className="sentence-hover-time">
                            {formatTime(segment.start_ms)} - {formatTime(segment.end_ms)}
                          </span>
                          <button
                            type="button"
                            className="sentence-hover-btn"
                            onClick={() => {
                              setPlaybackMode(true);
                              player.current?.playSegment(segment.start_ms, segment.end_ms);
                            }}
                            title={`Phát câu này (${formatTime(segment.start_ms)} - ${formatTime(segment.end_ms)})`}
                          >
                            ▷ Phát
                          </button>
                        </div>
                      </div>

                      <div className="dual-cell-actions">
                        <button
                          type="button"
                          className={`dual-more-btn ${rightRowMenuOpenId === segment.logical_segment_id ? "active" : ""}`}
                          title="Tùy chọn"
                          onClick={(e) => {
                            e.stopPropagation();
                            setRightRowMenuOpenId(
                              rightRowMenuOpenId === segment.logical_segment_id ? null : segment.logical_segment_id
                            );
                          }}
                        >
                          <Icon name="more" />
                        </button>

                        {rightRowMenuOpenId === segment.logical_segment_id && (
                          <div className="dual-row-menu-dropdown">
                            {editable && nextSegment && (
                              <button
                                type="button"
                                onClick={() => {
                                  setRowMenuOpenId(null);
                                  setRightRowMenuOpenId(null);
                                  setPendingMerge([segment, nextSegment]);
                                }}
                                title={`Gộp ngay với đoạn dưới: "${nextSegment.text.slice(0, 30)}..."`}
                              >
                                <span>🔗 Gộp với đoạn dưới</span>
                              </button>
                            )}
                            {editable && (
                              <button
                                type="button"
                                onClick={() => {
                                  setRowMenuOpenId(null);
                                  setRightRowMenuOpenId(null);
                                  toggleMerge(segment);
                                }}
                              >
                                <span>{isMergeSelected ? "✕ Bỏ chọn gộp" : "☑ Chọn gộp nhiều đoạn"}</span>
                              </button>
                            )}
                            {transText && (
                              <button
                                type="button"
                                onClick={() => {
                                  setRightRowMenuOpenId(null);
                                  void copyToClipboard(transText);
                                  setNotice("Đã sao chép bản dịch.");
                                }}
                              >
                                <Icon name="copy" />
                                <span>Sao chép bản dịch</span>
                              </button>
                            )}
                            <button
                              type="button"
                              onClick={() => {
                                setRightRowMenuOpenId(null);
                                player.current?.seek(segment.start_ms);
                              }}
                            >
                              <Icon name="play" />
                              <span>Phát đoạn này</span>
                            </button>
                          </div>
                        )}
                      </div>
                    </div>
                  )}
                </div>
              );
            })}

            {/* Realtime Streaming Live Entries inside the card */}
            {liveEntries.map(([key, event]) => (
              <div key={key} className="dual-row-pair is-live-streaming">
                <div className="dual-cell left">
                  <div className="dual-cell-main">
                    <span className="dual-speaker-name">
                      {event.speaker_name ?? (event.speaker_label ? `Người nói ${event.speaker_label}` : "Người nói")}:
                    </span>
                    <span className="dual-text">{event.text}</span>
                    <span
                      style={{
                        display: "inline-flex",
                        alignItems: "center",
                        gap: 5,
                        fontSize: 11,
                        color: "#ef4444",
                        fontWeight: 600,
                        marginLeft: 10,
                      }}
                    >
                      <span className="record-pulsing-dot red" style={{ width: 6, height: 6 }} />
                      Đang nhận diện…
                    </span>
                  </div>
                </div>
                {showTranslationSplit && (
                  <div className="dual-cell right">
                    <div className="dual-cell-main">
                      <span className="dual-speaker-name">Bản dịch:</span>
                      <span className="dual-text">
                        <em style={{ color: "var(--muted)", fontSize: 13 }}>Đang chờ hoàn tất câu để dịch…</em>
                      </span>
                    </div>
                  </div>
                )}
              </div>
            ))}
            <div
              ref={transcriptBottomRef}
              style={{ height: 0, minHeight: 0, pointerEvents: "none", scrollMarginBottom: 120 }}
            />
            </div>
          )}
        </div>
      </div>


    </div>

      {/* Slide-over Drawer: TÓM TẮT AI */}
      {summaryDrawerOpen && (
        <SummaryAiModal
          meetingId={meetingId}
          meetingTitle={meeting.data.title}
          hasTranscript={orderedSegments.length > 0}
          editable={editable}
          summaries={summaries}
          capabilities={capabilities.data}
          close={() => setSummaryDrawerOpen(false)}
        />
      )}

      {/* Wide Modal: QUẢN LÝ NGƯỜI NÓI (ẢNH 5) */}
      {speakersDrawerOpen && (
        <SpeakerManagementModal
          meetingId={meetingId}
          speakers={speakers}
          editable={editable}
          close={() => setSpeakersDrawerOpen(false)}
        />
      )}

      {/* Centered Modal: XUẤT NỘI DUNG (ẢNH 4) */}
      {exportModalOpen && (
        <ExportContentModal
          meetingId={meetingId}
          meetingTitle={meeting.data.title}
          mediaId={media.data?.[0]?.id}
          close={() => setExportModalOpen(false)}
        />
      )}

      {/* Centered Modal: CHIA SẺ (ẢNH 1) */}
      {shareSocialOpen && (
        <ShareSocialModal
          meetingTitle={meeting.data.title}
          close={() => setShareSocialOpen(false)}
        />
      )}

      {/* Dialogs */}
      {renameOpen && (
        <RenameMeetingDialog
          title={meeting.data.title}
          close={() => setRenameOpen(false)}
          confirm={async (title) => {
            const updated = await meetingsApi.update(meetingId, { title });
            meeting.setData(updated);
            bundle.setData((prev) => (prev ? { ...prev, meeting: updated } : prev));
            setRenameOpen(false);
            setNotice("Đã đổi tên bản ghi.");
            window.dispatchEvent(new CustomEvent("bocbang:refresh-meetings", { detail: updated }));
          }}
        />
      )}

      {pendingSplit && (
        <SplitDialog
          segment={pendingSplit}
          close={() => setPendingSplit(undefined)}
          confirm={(splitAt, first, second) => void handleSplit(pendingSplit, splitAt, first, second)}
        />
      )}

      {pendingMerge && (
        <ConfirmDialog
          title={`Gộp ${pendingMerge.length} đoạn?`}
          detail="Các đoạn đã chọn sẽ được nối theo đúng thứ tự thời gian thành một đoạn."
          confirmLabel="Gộp đoạn"
          close={() => setPendingMerge(undefined)}
          confirm={() => void handleMerge(pendingMerge)}
        />
      )}

      {pendingDelete && (
        <ConfirmDialog
          title="Xóa đoạn transcript?"
          detail="Đoạn đã xóa sẽ không còn trong phiên bản transcript hiện hành."
          confirmLabel="Xóa đoạn"
          close={() => setPendingDelete(undefined)}
          confirm={() => {
            const segment = pendingDelete;
            setPendingDelete(undefined);
            void mutate(
              () => meetingsApi.deleteSegment(segment.logical_segment_id),
              "Đã xóa đoạn và tạo phiên bản mới.",
              segment.logical_segment_id
            );
          }}
        />
      )}

      {/* DOCKED BOTTOM BAR: RECORDING (KHI ĐANG / CHUẨN BỊ GHI ÂM) HOẶC PLAYBACK (ẢNH 2 & 4) */}
      {(!playbackMode || !media.data?.[0]) && (
        isRealtimeRecording ? (
          <RecordingPanel
            meetingId={meetingId}
            targetLanguage={targetLang || (realtimeTranslationSupported ? realtimeTarget : "en")}
            translationVisible={showRealtimeTranslations}
            onTranslationVisibleChange={setShowRealtimeTranslations}
            onTranslation={(logicalSegmentId, translation) =>
              setRealtimeTranslations((current) => ({ ...current, [logicalSegmentId]: translation }))
            }
            onTranscript={(event) => setLiveTranscript((current) => updateLiveTranscript(current, event))}
            onResetTranscript={() =>
              setLiveTranscript((current) =>
                Object.fromEntries(Object.entries(current).filter(([, event]) => event.type === "transcript.final"))
              )
            }
            onFinal={() => {
              void transcript.reload();
              void speakers.reload();
              void versions.reload();
            }}
            onComplete={() => {
              void meeting.reload();
              void transcript.reload();
              void refreshMedia();
            }}
            onRecordingStateChange={updateRecordingState}
            hasStartedBefore={transcript.data?.segments !== undefined && transcript.data.segments.length > 0}
            hasAudio={Boolean(media.data?.[0])}
            initialElapsedMs={initialRecordingDurationMs}
            targetLang={targetLang}
            onTargetLangChange={setTargetLang}
            onPlayback={() => {
              if (media.data?.[0]) {
                setPlaybackMode(true);
                player.current?.play();
              } else {
                setNotice("Chưa có bản ghi âm để phát lại.");
              }
            }}
            playbackMode={playbackMode}
            onShare={() => setShareSocialOpen(true)}
            onSummary={() => setSummaryDrawerOpen(true)}
            onExport={() => setExportModalOpen(true)}
            onSpeakers={() => setSpeakersDrawerOpen(true)}
          />
        ) : (
        <div className="docked-bottom-bar-wrap">
        <div className="docked-bottom-bar">
          {/* Blue Playback pill button (ON THE LEFT - ONLY WHEN THERE IS RECORDING) */}
          {Boolean(media.data?.[0] || hasTranscriptContent) && (
            <button
              type="button"
              className="docked-play-pill"
              onClick={() => {
                if (media.data?.[0]) {
                  setPlaybackMode(true);
                  player.current?.play();
                } else {
                  setNotice("Chưa có bản ghi âm để phát lại.");
                }
              }}
              title="Phát lại bản ghi"
            >
              <Icon name="play" />
              <span>Phát lại bản ghi</span>
              <span className="docked-tooltip">Phát lại bản ghi</span>
            </button>
          )}

          <select
            className="docked-lang-pill"
            value={targetLang.toUpperCase()}
            onChange={(e) => setTargetLang(e.target.value.toLowerCase())}
            title="Ngôn ngữ đích"
            style={{
              width: 58,
              minWidth: 58,
              maxWidth: 58,
              height: 36,
              padding: "0 6px",
              borderRadius: 8,
              background: "#2a2b32",
              color: "#fff",
              border: "1px solid rgba(255,255,255,0.15)",
              fontWeight: 700,
              fontSize: 13,
              flexShrink: 0,
              textAlign: "center",
            }}
          >
            <option value="VI">VI</option>
            <option value="EN">EN</option>
          </select>

          {/* Share icon (Ảnh 1: Modal Chia sẻ) */}
          <button
            type="button"
            className="docked-icon-btn"
            onClick={() => setShareSocialOpen(true)}
            title="Chia sẻ cuộc họp"
          >
            <svg className="icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8">
              <path d="M4 12v8a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2v-8m-4-6-4-4-4 4m4-4v13" />
            </svg>
            <span className="docked-tooltip">Chia sẻ cuộc họp</span>
          </button>

          {/* AI Summary icon */}
          <button
            type="button"
            className="docked-icon-btn"
            onClick={() => setSummaryDrawerOpen(true)}
            title="Tóm tắt AI"
          >
            <Icon name="sparkles" />
            <span className="docked-tooltip">Tóm tắt AI</span>
          </button>

          {/* Export icon (Ảnh 4: Modal Xuất nội dung) */}
          <button
            type="button"
            className="docked-icon-btn"
            onClick={() => setExportModalOpen(true)}
            title="Xuất nội dung"
          >
            <svg className="icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8">
              <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z" />
              <polyline points="14 2 14 8 20 8" />
              <line x1="16" y1="13" x2="8" y2="13" />
              <line x1="16" y1="17" x2="8" y2="17" />
              <polyline points="10 9 9 9 8 9" />
            </svg>
            <span className="docked-tooltip">Xuất nội dung</span>
          </button>

          {/* Download audio icon */}
          {media.data?.[0] && (
            <a
              className="docked-icon-btn"
              href={`/api/v1/meetings/${meetingId}/media/${media.data[0].id}/download`}
              download={media.data[0].original_filename}
              title="Tải audio về máy"
            >
              <svg className="icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8">
                <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4m4-5 5 5 5-5m-5 5V3" />
              </svg>
              <span className="docked-tooltip">Tải audio về máy</span>
            </a>
          )}

          {/* Speaker icon (Ảnh 5: Modal Quản lý người nói) */}
          <button
            type="button"
            className="docked-icon-btn"
            onClick={() => setSpeakersDrawerOpen(true)}
            title="Quản lý người nói"
          >
            <Icon name="speaker" />
            <span className="docked-tooltip">Quản lý người nói</span>
          </button>
        </div>
      </div>
        )
      )}

      {/* FIXED VIEW LOG AI ON BOTTOM RIGHT (ẢNH 2 & ẢNH 4) */}
      {!playbackMode && (
      <button
        type="button"
        className="docked-view-log-btn-fixed"
        onClick={() => setLogAiOpen(true)}
        title="Xem nhật ký AI"
      >
        <span>⚡ View Log AI</span>
        <span className="docked-tooltip">Xem nhật ký AI</span>
      </button>
      )}

      {logAiOpen && (
        <LogAiModal
          meetingTitle={meeting.data.title}
          close={() => setLogAiOpen(false)}
        />
      )}
    </div>
  );
}

/* ═══════════════════════════════════════════
   SLIDE-OVER DRAWER: TÓM TẮT AI
═══════════════════════════════════════════ */
const summaryLabels: Record<SummaryTemplate, string> = {
  general: "Tóm tắt chung",
  briefing: "Họp giao ban",
  voting: "Họp biểu quyết",
};
const summaryLangLabels: Record<SummaryLanguage, string> = { vi: "Tiếng Việt", en: "English" };


function humanizeKey(key: string) {
  return key.replace(/_/g, " ").replace(/\b\w/g, (char) => char.toUpperCase());
}

function toSummaryText(content: Record<string, unknown>): string {
  return Object.entries(content)
    .map(([key, value]) => {
      const heading = humanizeKey(key);
      const text = typeof value === "object" && value !== null ? JSON.stringify(value, null, 2) : String(value);
      return `${heading}:\n${text}`;
    })
    .join("\n\n");
}

function SummaryValueRenderer({ value }: { value: unknown }) {
  if (value === null || value === undefined) return <span style={{ color: "var(--muted)" }}>—</span>;
  if (Array.isArray(value)) {
    return (
      <ul style={{ margin: "4px 0", paddingLeft: 20 }}>
        {value.map((item, index) => (
          <li key={index} style={{ marginBottom: 4 }}>
            <SummaryValueRenderer value={item} />
          </li>
        ))}
      </ul>
    );
  }
  if (typeof value === "object") {
    return (
      <div style={{ paddingLeft: 12, borderLeft: "2px solid rgba(255,255,255,0.1)" }}>
        {Object.entries(value as Record<string, unknown>).map(([k, v]) => (
          <div key={k} style={{ marginBottom: 6 }}>
            <strong style={{ color: "#94a3b8", fontSize: 12.5 }}>{humanizeKey(k)}: </strong>
            <SummaryValueRenderer value={v} />
          </div>
        ))}
      </div>
    );
  }
  return <span>{String(value)}</span>;
}

function SummaryAiModal({
  meetingId,
  meetingTitle,
  hasTranscript,
  editable,
  summaries,
  capabilities,
  close,
}: {
  meetingId: string;
  meetingTitle: string;
  hasTranscript: boolean;
  editable: boolean;
  summaries: any;
  capabilities: any;
  close: () => void;
}) {
  const jobs = useJobMonitor();
  const [template, setTemplate] = useState<SummaryTemplate>("general");
  const [outputLang, setOutputLang] = useState<SummaryLanguage>("vi");
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");

  useEffect(() => {
    if (jobs.job?.status === "completed") {
      void summaries.reload();
    }
  }, [jobs.job?.status, summaries.reload]);

  const summary = summaries.data?.find(
    (item: any) => item.template === template && item.output_language === outputLang
  );

  async function generate() {
    try {
      setError("");
      setNotice("");
      jobs.track(await meetingsApi.summary(meetingId, template, outputLang));
    } catch (cause) {
      setError((cause as Error).message);
    }
  }

  async function copy() {
    if (!summary?.current_version?.content) return;
    try {
      await copyToClipboard(toSummaryText(summary.current_version.content));
      setNotice("Đã sao chép tóm tắt vào bộ nhớ tạm.");
    } catch {
      setError("Không thể sao chép nội dung.");
    }
  }

  const jobActive = ["queued", "processing", "retrying"].includes(jobs.job?.status ?? "");

  return (
    <div className="dialog-backdrop" onClick={close}>
      <div className="summary-modal-card" onClick={(e) => e.stopPropagation()}>
        <div className="summary-modal-header">
          <div className="summary-modal-title-group">
            <div className="summary-modal-icon-badge">
              <Icon name="sparkles" />
            </div>
            <div>
              <h3 className="summary-modal-title">Tóm tắt AI Cuộc họp</h3>
              <p className="summary-modal-subtitle">{meetingTitle}</p>
            </div>
          </div>
          <button type="button" className="summary-modal-close" onClick={close} aria-label="Đóng">
            ✕
          </button>
        </div>

        <div className="summary-modal-body">
          {/* Controls: Template selection & Language selection */}
          <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", flexWrap: "wrap", gap: 12 }}>
            <div className="summary-template-pills">
              {Object.entries(summaryLabels).map(([k, v]) => (
                <button
                  type="button"
                  key={k}
                  className={`summary-pill-btn ${template === k ? "is-active" : ""}`}
                  onClick={() => setTemplate(k as SummaryTemplate)}
                >
                  {v}
                </button>
              ))}
            </div>

            <div style={{ display: "flex", gap: 6, alignItems: "center" }}>
              <span style={{ fontSize: 13, color: "var(--muted)", marginRight: 4 }}>Ngôn ngữ:</span>
              <button
                type="button"
                className={`summary-pill-btn ${outputLang === "vi" ? "is-active" : ""}`}
                onClick={() => setOutputLang("vi")}
                style={{ padding: "4px 10px", fontSize: 12 }}
              >
                Tiếng Việt
              </button>
              <button
                type="button"
                className={`summary-pill-btn ${outputLang === "en" ? "is-active" : ""}`}
                onClick={() => setOutputLang("en")}
                style={{ padding: "4px 10px", fontSize: 12 }}
              >
                English
              </button>
            </div>
          </div>

          {/* Action Row */}
          <div style={{ display: "flex", gap: 10, alignItems: "center" }}>
            {editable && (
              <button
                type="button"
                className="export-btn-primary"
                disabled={!hasTranscript || jobActive}
                onClick={() => void generate()}
                style={{ flex: 1, height: 40 }}
              >
                {jobActive ? "Đang tạo tóm tắt…" : summary ? "Tạo lại tóm tắt" : "Tạo tóm tắt AI"}
              </button>
            )}
            {summary && (
              <>
                <button
                  type="button"
                  className="export-btn-secondary"
                  onClick={() => void copy()}
                  style={{ width: "auto", padding: "0 18px", height: 40, gap: 6 }}
                >
                  <Icon name="copy" /> Sao chép
                </button>
                <button
                  type="button"
                  className="export-btn-secondary"
                  onClick={() => {
                    if (!summary?.current_version?.content) return;
                    const txt = toSummaryText(summary.current_version.content);
                    const blob = new Blob([txt], { type: "text/plain;charset=utf-8" });
                    const url = URL.createObjectURL(blob);
                    const a = document.createElement("a");
                    a.href = url;
                    a.download = `${meetingTitle}_tom_tat.txt`;
                    a.click();
                    URL.revokeObjectURL(url);
                  }}
                  style={{ width: "auto", padding: "0 16px", height: 40, gap: 6 }}
                  title="Tải về tệp .txt"
                >
                  <Icon name="download" /> Xuất TXT
                </button>
              </>
            )}
          </div>

          {error && <div className="form-error">{error}</div>}
          {notice && <div className="notice">{notice}</div>}
          {jobs.job && (
            <div className={`job-state ${jobs.job.status}`}>
              Tóm tắt AI: {statusLabel(jobs.job.status)}
              {jobs.job.progress !== undefined ? ` · ${jobs.job.progress}%` : ""}
            </div>
          )}

          {/* Content */}
          {summaries.loading ? (
            <Loading label="Đang tải bản tóm tắt…" />
          ) : summary?.current_version?.content ? (
            <div className="summary-content-box">
              <div style={{ marginBottom: 14, display: "flex", gap: 8, alignItems: "center" }}>
                <Status>{summaryLabels[summary.template as SummaryTemplate]}</Status>
                <small style={{ color: "#94a3b8" }}>
                  {new Date(summary.current_version.created_at).toLocaleString("vi-VN")}
                </small>
              </div>

              {Object.entries(summary.current_version.content).map(([key, value]) => (
                <div key={key} style={{ marginBottom: 16 }}>
                  <h4 className="summary-section-heading">
                    {humanizeKey(key)}
                  </h4>
                  <SummaryValueRenderer value={value} />
                </div>
              ))}
            </div>
          ) : (
            <div style={{ textAlign: "center", padding: "40px 20px", color: "var(--muted)", background: "rgba(255,255,255,0.02)", borderRadius: 10, border: "1px dashed rgba(255,255,255,0.1)" }}>
              <p style={{ margin: 0, fontSize: 13.5 }}>
                Chưa có bản tóm tắt cho mẫu này. Nhấn <strong>"Tạo tóm tắt AI"</strong> để phân tích tự động.
              </p>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}


const MediaPlayer = forwardRef<
  AudioPlayerHandle,
  {
    meetingId: string;
    mediaId: string;
    filename: string;
    segments: Segment[];
    playbackMode: boolean;
    onClosePlayback?: () => void;
    onActiveSegment: (id?: string) => void;
    onSummary?: () => void;
    onSpeakers?: () => void;
    onShare?: () => void;
    onExport?: () => void;
    downloadUrl?: string;
  }
>(function MediaPlayer(
  {
    meetingId,
    mediaId,
    filename,
    segments,
    playbackMode,
    onClosePlayback,
    onActiveSegment,
    onSummary,
    onSpeakers,
    onShare,
    onExport,
    downloadUrl,
  },
  ref
) {
  const audio = useRef<HTMLAudioElement>(null);
  const [source, setSource] = useState("");
  const [isPlaying, setIsPlaying] = useState(false);
  const [isMuted, setIsMuted] = useState(false);
  const [currentTime, setCurrentTime] = useState(0);
  const [duration, setDuration] = useState(0);
  const [volume, setVolume] = useState(1);
  const [shuffle, setShuffle] = useState(false);
  const [playbackSpeed, setPlaybackSpeed] = useState(1);
  const stopAtMsRef = useRef<number | null>(null);

  const speeds = [0.75, 1, 1.25, 1.5, 2];
  function cyclePlaybackSpeed() {
    const nextIdx = (speeds.indexOf(playbackSpeed) + 1) % speeds.length;
    const nextSpeed = speeds[nextIdx];
    setPlaybackSpeed(nextSpeed);
    if (audio.current) audio.current.playbackRate = nextSpeed;
  }

  useImperativeHandle(
    ref,
    () => ({
      playSegment(startMs: number, endMs: number) {
        stopAtMsRef.current = endMs;
        const element = audio.current;
        if (!element) return;
        element.currentTime = startMs / 1000;
        void element.play();
        setIsPlaying(true);
      },
      seek(milliseconds) {
        stopAtMsRef.current = null;
        const element = audio.current;
        if (!element) return;
        element.currentTime = milliseconds / 1000;
        void element.play();
        setIsPlaying(true);
      },
      play() {
        stopAtMsRef.current = null;
        void audio.current?.play();
        setIsPlaying(true);
      },
      pause() {
        stopAtMsRef.current = null;
        audio.current?.pause();
        setIsPlaying(false);
      },
      toggle() {
        stopAtMsRef.current = null;
        if (!audio.current) return;
        if (audio.current.paused) {
          void audio.current.play();
          setIsPlaying(true);
        } else {
          audio.current.pause();
          setIsPlaying(false);
        }
      },
      toggleMute() {
        if (!audio.current) return false;
        audio.current.muted = !audio.current.muted;
        setIsMuted(audio.current.muted);
        return audio.current.muted;
      },
    }),
    []
  );

  useEffect(() => {
    let active = true;
    let objectUrl = "";
    setSource("");
    void meetingsApi
      .streamMedia(meetingId, mediaId)
      .then((blob) => {
        if (!blob.size) return;
        objectUrl = URL.createObjectURL(blob);
        if (active) setSource(objectUrl);
        else URL.revokeObjectURL(objectUrl);
      })
      .catch(() => {});
    return () => {
      active = false;
      if (objectUrl) URL.revokeObjectURL(objectUrl);
    };
  }, [meetingId, mediaId]);

  function syncTranscript() {
    const sec = audio.current?.currentTime ?? 0;
    setCurrentTime(sec);
    const milliseconds = sec * 1000;

    // When replaying a specific sentence, stop automatically at sentence end
    if (stopAtMsRef.current !== null && milliseconds >= stopAtMsRef.current) {
      audio.current?.pause();
      setIsPlaying(false);
      stopAtMsRef.current = null;
      return;
    }

    const segment = segments.find((item) => item.start_ms <= milliseconds && milliseconds < item.end_ms);
    onActiveSegment(segment?.logical_segment_id);
  }

  function handleSkip(deltaSec: number) {
    stopAtMsRef.current = null;
    if (!audio.current) return;
    const next = Math.max(0, Math.min(duration, (audio.current.currentTime || 0) + deltaSec));
    audio.current.currentTime = next;
    setCurrentTime(next);
  }

  function formatAudioTime(seconds: number) {
    if (!seconds || isNaN(seconds)) return "--:--";
    const m = Math.floor(seconds / 60);
    const s = Math.floor(seconds % 60);
    return `${String(m).padStart(2, "0")}:${String(s).padStart(2, "0")}`;
  }

  return (
    <>
      <audio
        ref={audio}
        preload="metadata"
        src={source || undefined}
        onTimeUpdate={syncTranscript}
        onLoadedMetadata={() => setDuration(audio.current?.duration ?? 0)}
        onDurationChange={() => setDuration(audio.current?.duration ?? 0)}
        onPlay={() => setIsPlaying(true)}
        onPause={() => setIsPlaying(false)}
        onEnded={() => {
          setIsPlaying(false);
          onActiveSegment(undefined);
        }}
        style={{ display: "none" }}
      />

      {/* FULL BOTTOM AUDIO PLAYER BAR (ẢNH 3 - REDESIGNED) */}
      {playbackMode && (
        <div className="full-bottom-audio-bar">
          <div className="bottom-player-track-line">
            <span className="player-track-time">{formatAudioTime(currentTime)}</span>
            <div className="player-slider-container">
              <input
                type="range"
                min={0}
                max={duration || 100}
                step={0.1}
                value={currentTime}
                onChange={(e) => {
                  stopAtMsRef.current = null;
                  const val = Number(e.target.value);
                  if (audio.current) audio.current.currentTime = val;
                  setCurrentTime(val);
                }}
                className="bottom-player-slider"
              />
              <div
                className="player-slider-fill"
                style={{ width: `${duration ? (currentTime / duration) * 100 : 0}%` }}
              />
            </div>
            <span className="player-track-time">{formatAudioTime(duration)}</span>
          </div>

          <div className="bottom-player-controls-row">
            {/* Left Column: Info & Speed */}
            <div className="player-left-col">
              <div className="player-audio-badge">
                <svg className="icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
                  <path d="M3 18v-6a9 9 0 0 1 18 0v6" />
                  <path d="M21 19a2 2 0 0 1-2 2h-1a2 2 0 0 1-2-2v-3a2 2 0 0 1 2-2h3zM3 19a2 2 0 0 0 2 2h1a2 2 0 0 0 2-2v-3a2 2 0 0 0-2-2H3z" />
                </svg>
              </div>
              <div className="player-info-text">
                <span className="player-label-now">Đang phát lại</span>
                <span className="player-filename" title={filename || "Bản ghi âm"}>
                  {filename || "Bản ghi âm cuộc họp"}
                </span>
              </div>
              <button
                type="button"
                className="player-speed-badge"
                onClick={cyclePlaybackSpeed}
                title="Thay đổi tốc độ phát"
              >
                {playbackSpeed}x
              </button>
            </div>

            {/* Center: Main Controls */}
            <div className="player-main-ctrls">
              <button
                type="button"
                className={`player-mini-btn ${shuffle ? "is-active" : ""}`}
                onClick={() => setShuffle(!shuffle)}
                title="Phát ngẫu nhiên"
              >
                🔀
              </button>
              <button
                type="button"
                className="player-arrow-btn"
                onClick={() => handleSkip(-10)}
                title="Lùi 10 giây"
              >
                <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
                  <path d="M11 19l-9-7 9-7v14zM22 19l-9-7 9-7v14z" />
                </svg>
              </button>
              <button
                type="button"
                className="player-circle-play-btn"
                onClick={() => {
                  if (!audio.current) return;
                  if (isPlaying) audio.current.pause();
                  else void audio.current.play();
                }}
                title={isPlaying ? "Tạm dừng" : "Phát"}
              >
                {isPlaying ? (
                  <svg width="16" height="16" viewBox="0 0 24 24" fill="currentColor">
                    <rect x="6" y="4" width="4" height="16" rx="1" />
                    <rect x="14" y="4" width="4" height="16" rx="1" />
                  </svg>
                ) : (
                  <svg width="16" height="16" viewBox="0 0 24 24" fill="currentColor" style={{ marginLeft: 2 }}>
                    <polygon points="6 3 20 12 6 21 6 3" />
                  </svg>
                )}
              </button>
              <button
                type="button"
                className="player-arrow-btn"
                onClick={() => handleSkip(10)}
                title="Tới 10 giây"
              >
                <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
                  <path d="M13 19l9-7-9-7v14zM2 19l9-7-9-7v14z" />
                </svg>
              </button>
            </div>

            {/* Right Column: Volume, Actions & Close */}
            <div className="player-right-col">
              <div className="player-vol-wrap">
                <button
                  type="button"
                  className="player-mini-btn"
                  onClick={() => {
                    if (!audio.current) return;
                    audio.current.muted = !audio.current.muted;
                    setIsMuted(audio.current.muted);
                  }}
                  title={isMuted ? "Bật âm thanh" : "Tắt tiếng"}
                >
                  {isMuted ? "🔇" : "🔊"}
                </button>
                <input
                  type="range"
                  min={0}
                  max={1}
                  step={0.02}
                  value={isMuted ? 0 : volume}
                  onChange={(e) => {
                    const val = Number(e.target.value);
                    setVolume(val);
                    if (audio.current) {
                      audio.current.volume = val;
                      audio.current.muted = val === 0;
                      setIsMuted(val === 0);
                    }
                  }}
                  className="player-vol-slider"
                />
              </div>

              {/* Action Cluster in Playback Bar */}
              <div className="playback-actions-cluster">
                {onSummary && (
                  <button
                    type="button"
                    className="player-action-icon-btn"
                    onClick={onSummary}
                    title="Tóm tắt AI cuộc họp"
                  >
                    <Icon name="sparkles" />
                  </button>
                )}
                {onSpeakers && (
                  <button
                    type="button"
                    className="player-action-icon-btn"
                    onClick={onSpeakers}
                    title="Quản lý người nói"
                  >
                    <Icon name="speaker" />
                  </button>
                )}
                {onShare && (
                  <button
                    type="button"
                    className="player-action-icon-btn"
                    onClick={onShare}
                    title="Chia sẻ cuộc họp"
                  >
                    <svg className="icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8">
                      <path d="M4 12v8a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2v-8m-4-6-4-4-4 4m4-4v13" />
                    </svg>
                  </button>
                )}
                {onExport && (
                  <button
                    type="button"
                    className="player-action-icon-btn"
                    onClick={onExport}
                    title="Xuất nội dung"
                  >
                    <svg className="icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8">
                      <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z" />
                      <polyline points="14 2 14 8 20 8" />
                      <line x1="16" y1="13" x2="8" y2="13" />
                      <line x1="16" y1="17" x2="8" y2="17" />
                      <polyline points="10 9 9 9 8 9" />
                    </svg>
                  </button>
                )}
                {downloadUrl && (
                  <a
                    className="player-action-icon-btn"
                    href={downloadUrl}
                    download={filename}
                    title="Tải tệp âm thanh"
                  >
                    <svg className="icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8">
                      <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4m4-5 5 5 5-5m-5 5V3" />
                    </svg>
                  </a>
                )}
              </div>

              {onClosePlayback && (
                <button
                  type="button"
                  className="player-close-pill-btn"
                  onClick={onClosePlayback}
                  title="Đóng thanh nghe lại, quay về thanh công cụ"
                >
                  <Icon name="close" />
                  <span>Đóng</span>
                </button>
              )}
            </div>
          </div>
        </div>
      )}
    </>
  );
});

function RecordingPanel({
  meetingId,
  targetLanguage,
  translationVisible,
  onTranslationVisibleChange,
  onTranslation,
  onTranscript,
  onResetTranscript,
  onFinal,
  onComplete,
  onRecordingStateChange,
  hasStartedBefore,
  hasAudio,
  initialElapsedMs,
  targetLang,
  onTargetLangChange,
  onPlayback,
  playbackMode,
  onShare,
  onSummary,
  onExport,
  onSpeakers,
}: {
  meetingId: string;
  targetLanguage?: string;
  translationVisible: boolean;
  onTranslationVisibleChange: (visible: boolean) => void;
  onTranslation: (logicalSegmentId: string, translation: RealtimeTranslation) => void;
  onTranscript: (event: TranscriptEvent) => void;
  onResetTranscript: () => void;
  onFinal: () => void;
  onComplete: () => void;
  onRecordingStateChange: (active: boolean) => void;
  hasStartedBefore?: boolean;
  hasAudio?: boolean;
  initialElapsedMs: number;
  targetLang: string;
  onTargetLangChange: (lang: string) => void;
  onPlayback?: () => void;
  playbackMode?: boolean;
  onShare: () => void;
  onSummary: () => void;
  onExport?: () => void;
  onSpeakers: () => void;
}) {
  const { accessToken } = useAuth();
  const socket = useRef<MeetingSocket | undefined>(undefined);
  const capture = useRef<MicrophonePcmCapture | undefined>(undefined);
  const startedAt = useRef<number | undefined>(undefined);
  const elapsedBeforePause = useRef(Math.floor(initialElapsedMs / 1000));
  const translationEnabled = useRef(true);
  const finalSegmentIds = useRef(new Set<string>());
  const finalTurnIds = useRef(new Set<string>());
  const translatedSegmentIds = useRef(new Set<string>());
  const [status, setStatus] = useState("Sẵn sàng ghi âm");
  const [translationStatus, setTranslationStatus] = useState("");
  const [fallbackWarning, setFallbackWarning] = useState("");
  const [state, setState] = useState<"idle" | "connecting" | "recording" | "paused" | "stopping" | "stopped" | "failed">(
    "idle"
  );
  const [elapsed, setElapsed] = useState(Math.floor(initialElapsedMs / 1000));

  useEffect(() => () => {
    void stop(true);
  }, []);

  useEffect(() => {
    const handleStartEvent = () => {
      if (state === "idle" || state === "failed") {
        void start();
      }
    };
    window.addEventListener("bocbang:start-recording", handleStartEvent);
    return () => window.removeEventListener("bocbang:start-recording", handleStartEvent);
  }, [state]);

  useEffect(() => {
    if (state === "recording" && targetLanguage && socket.current) {
      socket.current.enableTranslation(targetLanguage, missingTranslationIds());
    }
  }, [targetLanguage, state]);
  useEffect(
    () => onRecordingStateChange(state !== "idle" && state !== "stopped" && state !== "failed"),
    [state, onRecordingStateChange]
  );
  useEffect(() => {
    if (state !== "idle") return;
    const initialSeconds = Math.floor(initialElapsedMs / 1000);
    elapsedBeforePause.current = initialSeconds;
    setElapsed(initialSeconds);
  }, [initialElapsedMs, state]);
  useEffect(() => {
    if (state !== "recording") return;
    const timer = window.setInterval(
      () => setElapsed(elapsedBeforePause.current + Math.floor((Date.now() - (startedAt.current ?? Date.now())) / 1000)),
      1000
    );
    return () => window.clearInterval(timer);
  }, [state]);

  function missingTranslationIds() {
    return [...finalSegmentIds.current].filter((id) => !translatedSegmentIds.current.has(id));
  }

  async function releaseCapture(flush: boolean) {
    const current = capture.current;
    capture.current = undefined;
    if (current) await current.stop(flush);
  }

  function event(event: RealtimeEvent) {
    if (event.type === "session.ready") {
      onResetTranscript();
      void capture.current?.resume();
      setState("recording");
      setStatus("Đang ghi âm và nhận transcript trực tiếp.");
      setFallbackWarning(event.fallback_active ? "Đang chép lời dự phòng." : "");
      startedAt.current = Date.now();
      if (translationEnabled.current && targetLanguage) {
        socket.current?.enableTranslation(targetLanguage, missingTranslationIds());
      }
    }
    if (event.type === "transcript.partial") {
      if (!event.stream_segment_id || !finalTurnIds.current.has(event.stream_segment_id)) onTranscript(event);
    }
    if (event.type === "transcript.final") {
      if (event.stream_segment_id?.match(/(?:^|:)turn:\d+$/)) finalTurnIds.current.add(event.stream_segment_id);
      onTranscript(event);
      finalSegmentIds.current.add(event.logical_segment_id);
      onFinal();
    }
    if (event.type === "transcript.speaker_updated") onFinal();
    if (event.type === "translation.status") {
      setTranslationStatus(event.enabled ? "Đang dịch trực tiếp các đoạn đã được xác nhận." : "");
    }
    if (event.type === "translation.final") {
      translatedSegmentIds.current.add(event.logical_segment_id);
      onTranslation(event.logical_segment_id, {
        text: event.text,
        sourceLanguage: event.source_language,
        targetLanguage: event.target_language,
      });
      setTranslationStatus("Đã cập nhật bản dịch realtime.");
    }
    if (event.type === "translation.error") {
      setTranslationStatus(`Dịch realtime: ${event.message}`);
    }
    if (event.type === "meeting.status") {
      setState("stopped");
      setStatus("Đã kết thúc ghi âm.");
      onComplete();
    }
    if (event.type === "error") {
      setStatus(event.message || "Lỗi realtime.");
      if (event.retryable === false) {
        setState("failed");
        void releaseCapture(false);
      }
    }
  }

  async function start() {
    if (!accessToken) return;
    try {
      await releaseCapture(false);
      socket.current?.close();
      setState("connecting");
      setStatus("Đang xin quyền microphone và kết nối…");
      capture.current = await MicrophonePcmCapture.open((chunk) => socket.current?.sendAudio(chunk));
      socket.current = new MeetingSocket(meetingId, accessToken, event, (next) => {
        if (next === "reconnecting") {
          void capture.current?.pause();
          setStatus("Mất kết nối, đang thử kết nối lại…");
        }
        if (next === "closed") setState((current) => (current === "stopped" || current === "stopping" ? "stopped" : "failed"));
      });
      socket.current.connect();
    } catch (cause) {
      await releaseCapture(false);
      setState("failed");
      setStatus(
        cause instanceof DOMException && cause.name === "NotAllowedError"
          ? "Cần cấp quyền microphone để ghi âm."
          : (cause as Error).message
      );
    }
  }

  function toggleTranslation() {
    if (!targetLanguage) return;
    if (translationVisible) {
      translationEnabled.current = false;
      onTranslationVisibleChange(false);
      setTranslationStatus("");
      socket.current?.disableTranslation();
      return;
    }
    translationEnabled.current = true;
    onTranslationVisibleChange(true);
    setTranslationStatus("Đang bật dịch realtime…");
    socket.current?.enableTranslation(targetLanguage, missingTranslationIds());
  }

  async function pause() {
    await capture.current?.pause();
    socket.current?.command("recording.pause");
    elapsedBeforePause.current = elapsed;
    setState("paused");
    setStatus("Đã tạm dừng ghi âm.");
  }

  async function resume() {
    socket.current?.command("recording.resume");
    await capture.current?.resume();
    startedAt.current = Date.now();
    setState("recording");
    setStatus("Đang tiếp tục ghi âm.");
  }

  async function stop(silent = false) {
    if (silent) {
      socket.current?.close();
      await releaseCapture(false);
      return;
    }
    setState("stopping");
    setStatus("Đang hoàn tất transcript và kết thúc bản ghi…");
    await releaseCapture(true);
    socket.current?.command("recording.stop");
    try {
      await meetingsApi.complete(meetingId);
      setState("stopped");
      setStatus("Đã kết thúc ghi âm.");
      onComplete();
    } catch {
      // ignore
    }
  }

  async function finishExisting() {
    setState("stopping");
    setStatus("Đang kết thúc bản ghi…");
    try {
      await meetingsApi.complete(meetingId);
      setState("stopped");
      setStatus("Đã kết thúc ghi âm.");
      onComplete();
    } catch (cause) {
      setState("failed");
      setStatus(cause instanceof Error ? cause.message : "Không thể kết thúc bản ghi.");
    }
  }

  return (
    <div className="docked-bottom-bar-wrap">
      {state === "failed" && status && (
        <div
          style={{
            position: "absolute",
            bottom: "calc(100% + 12px)",
            left: "50%",
            transform: "translateX(-50%)",
            background: "#2f1515",
            border: "1px solid #ef4444",
            color: "#fca5a5",
            padding: "8px 16px",
            borderRadius: 8,
            fontSize: 13,
            maxWidth: 520,
            textAlign: "center",
            boxShadow: "0 6px 20px rgba(0,0,0,0.5)",
            zIndex: 100,
            whiteSpace: "normal",
            display: "flex",
            alignItems: "center",
            gap: 8,
          }}
        >
          <span>{status}</span>
          <button
            type="button"
            onClick={() => setStatus("")}
            style={{
              background: "none",
              border: "none",
              color: "#fca5a5",
              cursor: "pointer",
              fontSize: 16,
              lineHeight: 1,
              padding: "0 4px",
            }}
            title="Đóng thông báo"
          >
            ×
          </button>
        </div>
      )}
      <div className="docked-bottom-bar is-recording-bar">
        {(state === "idle" || state === "failed") && (
          <button
            type="button"
            className="docked-record-pill"
            onClick={() => void start()}
            title={state === "failed" ? "Kết nối lại" : hasStartedBefore ? "Tiếp tục ghi âm" : "Bắt đầu ghi âm"}
          >
            <span className="record-red-dot" />
            <span>{state === "failed" ? "Kết nối lại" : hasStartedBefore ? "Tiếp tục ghi âm" : "Bắt đầu ghi âm"}</span>
            <span className="docked-tooltip">Bắt đầu ghi âm</span>
          </button>
        )}

        {state === "connecting" && (
          <button type="button" className="docked-record-pill is-connecting" disabled>
            <span className="record-pulsing-dot red" />
            <span>Đang kết nối mic…</span>
          </button>
        )}

        {state === "recording" && (
          <div className="docked-record-active-group">
            <div className="docked-record-timer-badge">
              <span className="record-pulsing-dot red" />
              <span>{formatDuration(elapsed)}</span>
            </div>
            <button
              type="button"
              className="docked-record-action-btn pause"
              onClick={() => void pause()}
              title="Tạm dừng ghi âm"
            >
              <svg className="icon" viewBox="0 0 24 24" fill="currentColor">
                <rect x="6" y="4" width="4" height="16" rx="1" />
                <rect x="14" y="4" width="4" height="16" rx="1" />
              </svg>
              <span>Tạm dừng</span>
              <span className="docked-tooltip">Tạm dừng</span>
            </button>
            <button
              type="button"
              className="docked-record-action-btn stop"
              onClick={() => void stop()}
              title="Kết thúc và lưu bản ghi"
            >
              <svg className="icon" viewBox="0 0 24 24" fill="currentColor">
                <rect x="6" y="6" width="12" height="12" rx="2" />
              </svg>
              <span>Kết thúc</span>
              <span className="docked-tooltip">Kết thúc &amp; Lưu</span>
            </button>
          </div>
        )}

        {state === "paused" && (
          <div className="docked-record-active-group">
            <div className="docked-record-timer-badge is-paused">
              <span className="record-pulsing-dot amber" />
              <span>{formatDuration(elapsed)} (Tạm dừng)</span>
            </div>
            <button
              type="button"
              className="docked-record-action-btn resume"
              onClick={() => void resume()}
              title="Tiếp tục ghi âm"
            >
              <Icon name="play" />
              <span>Tiếp tục</span>
              <span className="docked-tooltip">Tiếp tục ghi âm</span>
            </button>
            <button
              type="button"
              className="docked-record-action-btn stop"
              onClick={() => void stop()}
              title="Kết thúc và lưu bản ghi"
            >
              <svg className="icon" viewBox="0 0 24 24" fill="currentColor">
                <rect x="6" y="6" width="12" height="12" rx="2" />
              </svg>
              <span>Kết thúc</span>
              <span className="docked-tooltip">Kết thúc &amp; Lưu</span>
            </button>
          </div>
        )}

        {state === "stopping" && (
          <button type="button" className="docked-record-pill is-connecting" disabled>
            <span>Đang hoàn tất bản ghi…</span>
          </button>
        )}

        {state === "stopped" && (
          <button type="button" className="docked-record-pill is-connecting" disabled>
            <span>Đã kết thúc</span>
          </button>
        )}

        {hasStartedBefore && state === "idle" && (
          <button
            type="button"
            className="docked-record-action-btn stop"
            onClick={() => void finishExisting()}
            title="Hoàn tất bản ghi hiện tại"
          >
            <Icon name="check" />
            <span>Hoàn tất</span>
            <span className="docked-tooltip">Hoàn tất bản ghi</span>
          </button>
        )}

        {/* Blue Playback pill button (ON THE LEFT - ONLY WHEN THERE IS RECORDING) */}
        {hasAudio && onPlayback && (state === "idle" || state === "stopped" || state === "failed") && (
          <button
            type="button"
            className="docked-play-pill"
            onClick={onPlayback}
            title="Phát lại bản ghi"
          >
            <Icon name="play" />
            <span>Phát lại bản ghi</span>
            <span className="docked-tooltip">Phát lại bản ghi</span>
          </button>
        )}

        {/* Target language selector */}
        <select
          className="docked-lang-pill"
          value={targetLang.toUpperCase()}
          onChange={(e) => onTargetLangChange(e.target.value.toLowerCase())}
          title="Ngôn ngữ dịch"
          style={{
            width: 58,
            minWidth: 58,
            maxWidth: 58,
            height: 36,
            padding: "0 6px",
            borderRadius: 8,
            background: "#2a2b32",
            color: "#fff",
            border: "1px solid rgba(255,255,255,0.15)",
            fontWeight: 700,
            fontSize: 13,
            flexShrink: 0,
            textAlign: "center",
          }}
        >
          <option value="VI">VI</option>
          <option value="EN">EN</option>
        </select>

        {/* Share icon */}
        <button
          type="button"
          className="docked-icon-btn"
          onClick={onShare}
          title="Chia sẻ cuộc họp"
        >
          <svg className="icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8">
            <path d="M4 12v8a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2v-8m-4-6-4-4-4 4m4-4v13" />
          </svg>
          <span className="docked-tooltip">Chia sẻ cuộc họp</span>
        </button>

        {/* AI Summary icon */}
        <button
          type="button"
          className="docked-icon-btn"
          onClick={onSummary}
          title="Tóm tắt AI"
        >
          <Icon name="sparkles" />
          <span className="docked-tooltip">Tóm tắt AI</span>
        </button>

        {/* Export icon */}
        {onExport && (
          <button
            type="button"
            className="docked-icon-btn"
            onClick={onExport}
            title="Xuất nội dung (PDF, Word, Audio)"
          >
            <svg className="icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8">
              <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z" />
              <polyline points="14 2 14 8 20 8" />
              <line x1="16" y1="13" x2="8" y2="13" />
              <line x1="16" y1="17" x2="8" y2="17" />
              <polyline points="10 9 9 9 8 9" />
            </svg>
            <span className="docked-tooltip">Xuất nội dung</span>
          </button>
        )}

        {/* Speaker management icon */}
        <button
          type="button"
          className="docked-icon-btn"
          onClick={onSpeakers}
          title="Quản lý người nói"
        >
          <Icon name="speaker" />
          <span className="docked-tooltip">Quản lý người nói</span>
        </button>
      </div>
    </div>
  );
}

function ConfirmDialog({
  title,
  detail,
  confirmLabel,
  close,
  confirm,
}: {
  title: string;
  detail: string;
  confirmLabel: string;
  close: () => void;
  confirm: () => void;
}) {
  return (
    <div className="dialog-backdrop" role="presentation">
      <section className="dialog" role="alertdialog" aria-modal="true" style={{ width: 440 }}>
        <h2>{title}</h2>
        <p>{detail}</p>
        <div className="dialog-actions">
          <Button onClick={close}>Hủy</Button>
          <Button tone="danger" onClick={confirm}>
            {confirmLabel}
          </Button>
        </div>
      </section>
    </div>
  );
}

function RenameMeetingDialog({
  title,
  close,
  confirm,
}: {
  title: string;
  close: () => void;
  confirm: (title: string) => Promise<void>;
}) {
  const [value, setValue] = useState(title);
  const [error, setError] = useState("");
  const [saving, setSaving] = useState(false);

  async function submit(event: React.FormEvent) {
    event.preventDefault();
    const next = value.trim();
    if (!next) return setError("Tên bản ghi không được để trống.");
    try {
      setSaving(true);
      setError("");
      await confirm(next);
    } catch (cause) {
      setError((cause as Error).message);
      setSaving(false);
    }
  }

  return (
    <div className="dialog-backdrop">
      <form className="dialog" onSubmit={submit} style={{ width: 440 }}>
        <button className="close" onClick={close} type="button">
          ×
        </button>
        <h2>Đổi tên bản ghi</h2>
        <label style={{ marginTop: 14 }}>
          Tên bản ghi
          <input autoFocus maxLength={200} value={value} onChange={(event) => setValue(event.target.value)} />
        </label>
        {error && <div className="form-error">{error}</div>}
        <div className="dialog-actions" style={{ marginTop: 20 }}>
          <Button onClick={close}>Hủy</Button>
          <Button type="submit" tone="dark" disabled={saving}>
            {saving ? "Đang lưu…" : "Lưu tên"}
          </Button>
        </div>
      </form>
    </div>
  );
}

function SplitDialog({
  segment,
  close,
  confirm,
}: {
  segment: Segment;
  close: () => void;
  confirm: (splitAt: number, first: string, second: string) => void;
}) {
  const words = segment.text.trim().split(/\s+/);
  const [splitIndex, setSplitIndex] = useState<number>(Math.max(1, Math.floor(words.length / 2)));
  const firstText = words.slice(0, splitIndex).join(" ").trim();
  const secondText = words.slice(splitIndex).join(" ").trim();
  const ratio = firstText.length / (firstText.length + secondText.length || 1);
  const estimatedSplitMs = Math.floor(segment.start_ms + (segment.end_ms - segment.start_ms) * ratio);
  const splitAtMs = Math.max(segment.start_ms + 1, Math.min(segment.end_ms - 1, estimatedSplitMs));

  return (
    <div className="dialog-backdrop" role="presentation">
      <section className="dialog" role="dialog" aria-modal="true" style={{ width: 600 }}>
        <h2>Tách bằng câu</h2>
        <p>Nhấp vào chữ bên dưới để chọn vị trí tách. Đoạn sẽ được chia làm hai.</p>
        <div className="split-text">
          {words.map((word, i) => (
            <span
              key={i}
              onClick={() => setSplitIndex(Math.max(1, i))}
              className={`${i >= splitIndex ? "second" : ""} ${i === splitIndex ? "split-point" : ""}`}
            >
              {word}
              {i < words.length - 1 ? " " : ""}
            </span>
          ))}
        </div>
        <div className="split-preview">
          <div>
            <strong>Đoạn 1</strong>
            <p>{firstText}</p>
          </div>
          <div>
            <strong>Đoạn 2</strong>
            <p>{secondText}</p>
          </div>
        </div>
        <div className="dialog-actions">
          <Button onClick={close}>Hủy</Button>
          <Button
            tone="dark"
            disabled={!firstText || !secondText}
            onClick={() => confirm(splitAtMs, firstText, secondText)}
          >
            Tách đoạn
          </Button>
        </div>
      </section>
    </div>
  );
}

function meetingErrorMessage(cause: unknown) {
  if (cause instanceof ApiError) {
    if (cause.code === "SEGMENTS_NOT_MERGEABLE") return "Các đoạn được chọn phải nằm liền nhau.";
    if (cause.code === "USER_NOT_FOUND") return "Email này chưa có tài khoản Bóc Băng.";
  }
  const message = cause instanceof Error ? cause.message : "Không thể thực hiện thao tác.";
  if (message === "Segments must be contiguous and have the same speaker.")
    return "Chỉ có thể gộp hai đoạn liền kề của cùng một người nói.";
  return message;
}

function formatTime(milliseconds?: number) {
  if (milliseconds === undefined) return "--:--";
  const seconds = Math.floor(milliseconds / 1000);
  return `${Math.floor(seconds / 60)
    .toString()
    .padStart(2, "0")}:${(seconds % 60).toString().padStart(2, "0")}`;
}

function formatDuration(seconds: number) {
  return `${Math.floor(seconds / 3600)
    .toString()
    .padStart(2, "0")}:${Math.floor((seconds % 3600) / 60)
    .toString()
    .padStart(2, "0")}:${(seconds % 60).toString().padStart(2, "0")}`;
}

/* ═══════════════════════════════════════════
   MODAL: VIEW LOG AI (ẢNH 4)
═══════════════════════════════════════════ */
function LogAiModal({ close, meetingTitle }: { close: () => void; meetingTitle: string }) {
  const logs = [
    { time: "10:14:02.12", level: "CONNECT", msg: `Phiên kết nối STT khởi tạo cho "${meetingTitle}"` },
    { time: "10:14:02.35", level: "AUDIO", msg: "Microphone PCM / Audio streaming active (16kHz, mono)" },
    { time: "10:14:03.90", level: "MODEL", msg: "Pipeline: Gemini Flash Realtime Engine initialized" },
    { time: "10:14:04.18", level: "STT", msg: "Segment chunk decoded (confidence: 0.985, latency: 118ms)" },
    { time: "10:14:04.22", level: "DIARIZATION", msg: "Voice embedding matched: Speaker 1" },
    { time: "10:14:07.45", level: "TRANSLATE", msg: "Realtime bilingual sync (VI <-> EN) active" },
    { time: "10:14:12.80", level: "SYNC", msg: "Audio-transcript timestamp synchronized" },
  ];

  return (
    <div className="dialog-backdrop" onClick={close}>
      <div
        className="dialog"
        style={{ width: 620, maxWidth: "94vw", background: "#111827", color: "#f9fafb" }}
        onClick={(e) => e.stopPropagation()}
      >
        <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", marginBottom: 12 }}>
          <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
            <span style={{ fontSize: 16 }}>⚡</span>
            <h2 style={{ margin: 0, fontSize: 16, color: "#fff" }}>AI Real-time Diagnostics Log</h2>
          </div>
          <button className="close" onClick={close} type="button" style={{ color: "#9ca3af" }}>
            ×
          </button>
        </div>
        <p style={{ margin: "0 0 14px", fontSize: 13, color: "#9ca3af" }}>
          Thông tin chi tiết về pipeline bóc băng, nhận diện người nói và độ trễ mô hình AI.
        </p>

        <div style={{ display: "flex", gap: 8, marginBottom: 14 }}>
          <span style={{ fontSize: 11, background: "rgba(16, 185, 129, 0.2)", color: "#34d399", padding: "3px 8px", borderRadius: 4, fontWeight: 600 }}>
            ● CONNECTED
          </span>
          <span style={{ fontSize: 11, background: "rgba(59, 130, 246, 0.2)", color: "#60a5fa", padding: "3px 8px", borderRadius: 4, fontWeight: 600 }}>
            Gemini STT
          </span>
          <span style={{ fontSize: 11, background: "rgba(245, 158, 11, 0.2)", color: "#fbbf24", padding: "3px 8px", borderRadius: 4, fontWeight: 600 }}>
            Latency: ~118ms
          </span>
        </div>

        <div
          style={{
            background: "#030712",
            border: "1px solid rgba(255,255,255,0.1)",
            borderRadius: 8,
            padding: 12,
            height: 260,
            overflowY: "auto",
            fontFamily: "monospace",
            fontSize: 12,
            lineHeight: 1.6,
          }}
        >
          {logs.map((item, idx) => (
            <div key={idx} style={{ display: "flex", gap: 8, marginBottom: 4 }}>
              <span style={{ color: "#6b7280" }}>[{item.time}]</span>
              <span
                style={{
                  color:
                    item.level === "CONNECT"
                      ? "#34d399"
                      : item.level === "MODEL"
                      ? "#a78bfa"
                      : item.level === "STT"
                      ? "#38bdf8"
                      : "#fbbf24",
                  fontWeight: 700,
                }}
              >
                [{item.level}]
              </span>
              <span style={{ color: "#e5e7eb" }}>{item.msg}</span>
            </div>
          ))}
        </div>

        <div className="dialog-actions" style={{ marginTop: 16 }}>
          <Button tone="dark" onClick={close}>
            Đóng
          </Button>
        </div>
      </div>
    </div>
  );
}


/* ═══════════════════════════════════════════
   MODAL: CHIA SẺ SOCIAL (ẢNH 1)
═══════════════════════════════════════════ */
function ShareSocialModal({ meetingTitle, close }: { meetingTitle: string; close: () => void }) {
  const [copied, setCopied] = useState(false);
  const shareUrl = window.location.href;

  function handleCopy() {
    void copyToClipboard(shareUrl).then(() => {
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    });
  }

  function openShare(type: string) {
    const encodedUrl = encodeURIComponent(shareUrl);
    const encodedTitle = encodeURIComponent(meetingTitle);
    let url = "";
    if (type === "facebook") url = `https://www.facebook.com/sharer/sharer.php?u=${encodedUrl}`;
    else if (type === "messenger") url = `https://www.facebook.com/dialog/send?link=${encodedUrl}&app_id=1091931238593&redirect_uri=${encodedUrl}`;
    else if (type === "x") url = `https://twitter.com/intent/tweet?url=${encodedUrl}&text=${encodedTitle}`;
    else if (type === "telegram") url = `https://t.me/share/url?url=${encodedUrl}&text=${encodedTitle}`;
    else if (type === "whatsapp") url = `https://api.whatsapp.com/send?text=${encodedTitle}%20${encodedUrl}`;
    else if (type === "viber") url = `viber://forward?text=${encodedTitle}%20${encodedUrl}`;
    if (url) window.open(url, "_blank", "noopener,noreferrer");
  }

  return (
    <div className="dialog-backdrop" onClick={close}>
      <div className="share-social-card" onClick={(e) => e.stopPropagation()}>
        <div className="share-social-header">
          <strong>Chia sẻ</strong>
          <button type="button" className="close" onClick={close} aria-label="Đóng">✕</button>
        </div>
        {copied && <div className="share-social-copied-toast">Đã sao chép liên kết vào bộ nhớ tạm!</div>}
        <div className="share-social-grid">
          <button type="button" className="share-social-btn" onClick={handleCopy}>
            <span className="share-social-icon copy-icon">🔗</span>
            <small>Copy Link</small>
          </button>
          <button type="button" className="share-social-btn" onClick={() => openShare("facebook")}>
            <span className="share-social-icon fb-icon">f</span>
            <small>Facebook</small>
          </button>
          <button type="button" className="share-social-btn" onClick={() => openShare("messenger")}>
            <span className="share-social-icon msg-icon">💬</span>
            <small>Messenger</small>
          </button>
          <button type="button" className="share-social-btn" onClick={() => openShare("x")}>
            <span className="share-social-icon x-icon">𝕏</span>
            <small>X</small>
          </button>
          <button type="button" className="share-social-btn" onClick={() => openShare("telegram")}>
            <span className="share-social-icon tg-icon">✈</span>
            <small>Telegram</small>
          </button>
          <button type="button" className="share-social-btn" onClick={() => openShare("whatsapp")}>
            <span className="share-social-icon wa-icon">📞</span>
            <small>WhatsApp</small>
          </button>
          <button type="button" className="share-social-btn" onClick={() => openShare("viber")}>
            <span className="share-social-icon vb-icon">📱</span>
            <small>Viber</small>
          </button>
        </div>
      </div>
    </div>
  );
}

/* ═══════════════════════════════════════════
   MODAL: XUẤT NỘI DUNG (ẢNH 4)
═══════════════════════════════════════════ */
function ExportContentModal({
  meetingId,
  meetingTitle,
  mediaId,
  close,
}: {
  meetingId: string;
  meetingTitle: string;
  mediaId?: string;
  close: () => void;
}) {
  const [loading, setLoading] = useState<string | null>(null);

  async function handleExport(format: "pdf" | "docx" | "audio") {
    if (format === "audio") {
      if (!mediaId) {
        alert("Bản ghi chưa có tệp âm thanh.");
        return;
      }
      window.location.href = `/api/v1/meetings/${meetingId}/media/${mediaId}/download`;
      close();
      return;
    }

    try {
      setLoading(format);
      const artifact = await meetingsApi.export(meetingId, format, {
        include_timestamps: true,
        include_speakers: true,
        include_translation: true,
        include_summary: false,
        content_mode: "bilingual",
      });
      const blob = await meetingsApi.downloadExport(artifact.id);
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = artifact.filename ?? `${meetingTitle}.${format === "docx" ? "docx" : "pdf"}`;
      a.click();
      setTimeout(() => URL.revokeObjectURL(url), 1000);
      close();
    } catch (e) {
      alert((e as Error).message);
    } finally {
      setLoading(null);
    }
  }

  return (
    <div className="dialog-backdrop" onClick={close}>
      <div className="export-content-modal" onClick={(e) => e.stopPropagation()}>
        <div className="export-content-header">
          <strong>Xuất nội dung</strong>
          <button type="button" className="close" onClick={close} aria-label="Đóng">✕</button>
        </div>
        <div className="export-content-buttons">
          <button
            type="button"
            className="export-btn-primary"
            onClick={() => void handleExport("pdf")}
            disabled={loading !== null}
          >
            {loading === "pdf" ? "Đang xuất PDF…" : "Xuất PDF"}
          </button>
          <button
            type="button"
            className="export-btn-secondary"
            onClick={() => void handleExport("docx")}
            disabled={loading !== null}
          >
            {loading === "docx" ? "Đang xuất DOC…" : "Xuất DOC"}
          </button>
          <button
            type="button"
            className="export-btn-secondary"
            onClick={() => void handleExport("audio")}
            disabled={loading !== null}
          >
            Xuất Audio
          </button>
        </div>
      </div>
    </div>
  );
}

/* ═══════════════════════════════════════════
   MODAL: QUẢN LÝ NGƯỜI NÓI (ẢNH 5)
═══════════════════════════════════════════ */
function SpeakerManagementModal({
  meetingId,
  speakers,
  editable,
  close,
}: {
  meetingId: string;
  speakers: any;
  editable: boolean;
  close: () => void;
}) {
  const [fullName, setFullName] = useState("");
  const [role, setRole] = useState("");
  const [organization, setOrganization] = useState("");
  const [loading, setLoading] = useState(false);

  // Search filter
  const [searchName, setSearchName] = useState("");
  const [searchRole, setSearchRole] = useState("");
  const [searchOrg, setSearchOrg] = useState("");

  const systemSpeakers = [
    { id: "sys-1", name: "test", role: "Trợ lý", organization: "Test" },
    { id: "sys-2", name: "admin", role: "Giám đốc", organization: "Test" },
    { id: "sys-3", name: "Phát 1", role: "Giám đốc hệ điều hành", organization: "TP" },
    { id: "sys-4", name: "Phát 1", role: "Giám đốc hệ điều hành", organization: "TP" },
    { id: "sys-5", name: "Administrator", role: "AdminDev", organization: "H2O" },
    { id: "sys-6", name: "Vũ (4)", role: "", organization: "" },
    { id: "sys-7", name: "Vũ (5)", role: "", organization: "" },
    { id: "sys-8", name: "Vũ (6)", role: "Nhân viên", organization: "Phòng BE" },
    { id: "sys-9", name: "Vũ (3)", role: "abc", organization: "1" },
    { id: "sys-10", name: "Vũ (2)", role: "abc", organization: "1" },
  ];

  const currentSpeakers: any[] = speakers.data ?? [];

  async function handleCreateSpeaker(e?: React.FormEvent) {
    if (e) e.preventDefault();
    if (!fullName.trim()) return;
    try {
      setLoading(true);
      const created = await meetingsApi.createSpeaker(meetingId, {
        name: fullName.trim(),
        role: role.trim() || undefined,
        organization: organization.trim() || undefined,
      });
      speakers.setData((cur: any) => [...(cur ?? []), created]);
      setFullName("");
      setRole("");
      setOrganization("");
    } catch (e) {
      alert((e as Error).message);
    } finally {
      setLoading(false);
    }
  }

  async function handleAddFromSystem(spk: { name: string; role?: string; organization?: string }) {
    try {
      setLoading(true);
      const created = await meetingsApi.createSpeaker(meetingId, {
        name: spk.name,
        role: spk.role || undefined,
        organization: spk.organization || undefined,
      });
      speakers.setData((cur: any) => [...(cur ?? []), created]);
    } catch (e) {
      alert((e as Error).message);
    } finally {
      setLoading(false);
    }
  }

  async function handleRemoveSpeaker(id: string) {
    try {
      await meetingsApi.deleteSpeaker(id);
      speakers.setData((cur: any) => cur?.filter((s: any) => s.id !== id));
    } catch (e) {
      alert((e as Error).message);
    }
  }

  const [page, setPage] = useState(1);
  const pageSize = 5;

  const filteredSystem = systemSpeakers.filter((s) => {
    if (searchName && !s.name.toLowerCase().includes(searchName.toLowerCase())) return false;
    if (searchRole && !s.role.toLowerCase().includes(searchRole.toLowerCase())) return false;
    if (searchOrg && !s.organization.toLowerCase().includes(searchOrg.toLowerCase())) return false;
    return true;
  });

  const totalPages = Math.max(1, Math.ceil(filteredSystem.length / pageSize));
  const pagedSystem = filteredSystem.slice((page - 1) * pageSize, page * pageSize);

  return (
    <div className="dialog-backdrop" onClick={close}>
      <div className="speaker-modal-wide" onClick={(e) => e.stopPropagation()}>
        <div className="speaker-modal-header">
          <strong>Quản lý người nói</strong>
          <button type="button" className="close" onClick={close} aria-label="Đóng">✕</button>
        </div>

        <div className="speaker-modal-grid">
          {/* Left Column */}
          <div className="speaker-modal-col left">
            <h4>Tạo người tham gia mới</h4>
            <div className="speaker-create-inputs">
              <label>
                <span>Tên đầy đủ <b style={{ color: "#ef4444" }}>*</b></span>
                <input
                  type="text"
                  placeholder="Nhập tên người tham gia"
                  value={fullName}
                  onChange={(e) => setFullName(e.target.value)}
                />
              </label>
              <label>
                <span>Chức danh</span>
                <input
                  type="text"
                  placeholder="Nhập chức danh"
                  value={role}
                  onChange={(e) => setRole(e.target.value)}
                />
              </label>
              <label>
                <span>Đơn vị công tác</span>
                <input
                  type="text"
                  placeholder="Nhập đơn vị công tác"
                  value={organization}
                  onChange={(e) => setOrganization(e.target.value)}
                />
              </label>
            </div>
            <div style={{ display: "flex", justifyContent: "flex-end", marginTop: 8 }}>
              <button
                type="button"
                className="speaker-add-btn"
                onClick={() => void handleCreateSpeaker()}
                disabled={!fullName.trim() || loading}
              >
                + Thêm người tham gia
              </button>
            </div>

            <h4 style={{ marginTop: 20 }}>Thêm người tham gia từ hệ thống</h4>
            <div className="speaker-search-bar">
              <input
                type="text"
                placeholder="Tên"
                value={searchName}
                onChange={(e) => setSearchName(e.target.value)}
              />
              <input
                type="text"
                placeholder="Chức Danh"
                value={searchRole}
                onChange={(e) => setSearchRole(e.target.value)}
              />
              <input
                type="text"
                placeholder="Đơn vị"
                value={searchOrg}
                onChange={(e) => setSearchOrg(e.target.value)}
              />
              <button type="button" className="speaker-search-btn">Tìm kiếm</button>
            </div>

            <div className="speaker-table-wrap">
              <table className="speaker-table">
                <thead>
                  <tr>
                    <th>Tên</th>
                    <th>Chức Danh</th>
                    <th>Đơn vị</th>
                    <th style={{ textAlign: "right" }}>Action</th>
                  </tr>
                </thead>
                <tbody>
                  {pagedSystem.map((spk, idx) => (
                    <tr key={idx}>
                      <td><strong>{spk.name}</strong></td>
                      <td>{spk.role || "—"}</td>
                      <td>{spk.organization || "—"}</td>
                      <td style={{ textAlign: "right" }}>
                        <button
                          type="button"
                          className="speaker-join-btn"
                          onClick={() => void handleAddFromSystem(spk)}
                          disabled={loading}
                        >
                          + Thêm vào cuộc họp
                        </button>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>

            {/* Pagination (Ảnh 5) */}
            <div className="speaker-pagination">
              <button
                type="button"
                onClick={() => setPage((p) => Math.max(1, p - 1))}
                disabled={page === 1}
                title="Trang trước"
              >
                &lt;
              </button>
              {Array.from({ length: totalPages }, (_, i) => i + 1).map((p) => (
                <button
                  key={p}
                  type="button"
                  className={page === p ? "active" : ""}
                  onClick={() => setPage(p)}
                >
                  {p}
                </button>
              ))}
              <button
                type="button"
                onClick={() => setPage((p) => Math.min(totalPages, p + 1))}
                disabled={page === totalPages}
                title="Trang sau"
              >
                &gt;
              </button>
            </div>
          </div>

          {/* Right Column */}
          <div className="speaker-modal-col right">
            <h4>Người đã tham gia</h4>
            <div className="speaker-table-wrap" style={{ minHeight: 380 }}>
              <table className="speaker-table">
                <thead>
                  <tr>
                    <th>Tên</th>
                    <th>Chức Danh</th>
                    <th>Đơn vị</th>
                    <th style={{ textAlign: "right" }}>Action</th>
                  </tr>
                </thead>
                <tbody>
                  {currentSpeakers.length === 0 ? (
                    <tr>
                      <td colSpan={4} style={{ textAlign: "center", padding: "60px 20px" }}>
                        <div style={{ color: "var(--muted)", fontSize: 32, marginBottom: 8 }}>📥</div>
                        <div style={{ color: "var(--muted)", fontSize: 13 }}>No data</div>
                      </td>
                    </tr>
                  ) : (
                    currentSpeakers.map((spk: any) => (
                      <tr key={spk.id}>
                        <td><strong>{spk.name}</strong></td>
                        <td>{spk.role || "—"}</td>
                        <td>{spk.organization || "—"}</td>
                        <td style={{ textAlign: "right" }}>
                          <button
                            type="button"
                            className="speaker-remove-btn"
                            onClick={() => void handleRemoveSpeaker(spk.id)}
                          >
                            Xóa
                          </button>
                        </td>
                      </tr>
                    ))
                  )}
                </tbody>
              </table>
            </div>
          </div>
        </div>

        <div className="speaker-modal-footer">
          <button type="button" className="btn btn-ghost" onClick={close}>Cancel</button>
          <button type="button" className="btn btn-primary-blue" onClick={close}>OK</button>
        </div>
      </div>
    </div>
  );
}
