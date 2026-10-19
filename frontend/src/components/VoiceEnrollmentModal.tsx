import { useState, useRef, useEffect } from "react";
import { Button, Icon } from "./ui";
import { translateErrorMessage } from "../api/client";
import { voiceProfilesApi } from "../api/endpoints";
import type { VoiceProfile } from "../types/domain";

interface VoiceEnrollmentModalProps {
  isOpen: boolean;
  onClose: () => void;
  onEnrolled: (profile: VoiceProfile) => void;
  meetingId?: string;
  profileId?: string;
  defaultName?: string;
  currentSampleCount?: number;
  targetSampleIndex?: number;
  isSpeakerMode?: boolean;
}

export function VoiceEnrollmentModal({
  isOpen,
  onClose,
  onEnrolled,
  meetingId,
  profileId,
  defaultName = "",
  currentSampleCount,
  targetSampleIndex,
  isSpeakerMode = false,
}: VoiceEnrollmentModalProps) {
  const [name, setName] = useState(defaultName);
  const [isRecording, setIsRecording] = useState(false);
  const [recordingSeconds, setRecordingSeconds] = useState(0);
  const [recordedDurationSeconds, setRecordedDurationSeconds] = useState<number | null>(null);
  const [audioBlob, setAudioBlob] = useState<Blob | null>(null);
  const [audioUrl, setAudioUrl] = useState<string | null>(null);
  const [isSubmitting, setIsSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [showUpload, setShowUpload] = useState(false);

  const mediaRecorderRef = useRef<MediaRecorder | null>(null);
  const chunksRef = useRef<Blob[]>([]);
  const timerRef = useRef<number | null>(null);
  const recordingStartedAtRef = useRef<number | null>(null);

  const sampleIndexToEnroll =
    targetSampleIndex ?? ((currentSampleCount !== undefined ? currentSampleCount + 1 : 1));

  useEffect(() => {
    if (defaultName) setName(defaultName);
  }, [defaultName]);

  useEffect(() => {
    return () => {
      if (timerRef.current) clearInterval(timerRef.current);
      if (audioUrl) URL.revokeObjectURL(audioUrl);
    };
  }, [audioUrl]);

  if (!isOpen) return null;

  const startRecording = async () => {
    setError(null);
    try {
      const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
      chunksRef.current = [];
      const recorder = new MediaRecorder(stream);
      mediaRecorderRef.current = recorder;
      setRecordedDurationSeconds(null);

      recorder.ondataavailable = (e) => {
        if (e.data.size > 0) chunksRef.current.push(e.data);
      };

      recorder.onstop = () => {
        stream.getTracks().forEach((track) => track.stop());
        const startedAt = recordingStartedAtRef.current;
        if (startedAt !== null) {
          setRecordedDurationSeconds((performance.now() - startedAt) / 1000);
        }
        recordingStartedAtRef.current = null;
        const blob = new Blob(chunksRef.current, { type: recorder.mimeType || "audio/webm" });
        setAudioBlob(blob);
        const url = URL.createObjectURL(blob);
        setAudioUrl(url);
      };

      recordingStartedAtRef.current = performance.now();
      recorder.start(100);
      setIsRecording(true);
      setRecordingSeconds(0);

      timerRef.current = window.setInterval(() => {
        const startedAt = recordingStartedAtRef.current;
        if (startedAt === null) return;
        const elapsed = (performance.now() - startedAt) / 1000;
        const rounded = Math.min(25, Math.floor(elapsed));
        setRecordingSeconds(rounded);
        if (elapsed >= 24.5) stopRecording();
      }, 250);
    } catch {
      setError("Không thể truy cập microphone. Vui lòng cấp quyền truy cập micro cho trình duyệt.");
    }
  };

  const stopRecording = () => {
    if (timerRef.current) {
      clearInterval(timerRef.current);
      timerRef.current = null;
    }
    if (mediaRecorderRef.current && mediaRecorderRef.current.state !== "inactive") {
      mediaRecorderRef.current.stop();
    }
    setIsRecording(false);
  };

  const handleFileUpload = (e: React.ChangeEvent<HTMLInputElement>) => {
    setError(null);
    const file = e.target.files?.[0];
    if (!file) return;
    setRecordedDurationSeconds(null);
    setAudioBlob(file);
    if (audioUrl) URL.revokeObjectURL(audioUrl);
    const url = URL.createObjectURL(file);
    setAudioUrl(url);
  };

  const handleResetAudio = () => {
    if (audioUrl) URL.revokeObjectURL(audioUrl);
    setAudioBlob(null);
    setAudioUrl(null);
    setRecordedDurationSeconds(null);
    setError(null);
  };

  const handleSave = async () => {
    if (!audioBlob) {
      setError("Vui lòng ghi âm mẫu giọng nói trước khi lưu.");
      return;
    }
    if (!profileId && (meetingId || isSpeakerMode) && !name.trim()) {
      setError("Vui lòng nhập tên người nói.");
      return;
    }

    let duration = recordedDurationSeconds;
    if (duration === null) {
      try {
        duration = await audioDurationSeconds(audioBlob);
      } catch {
        duration = null;
      }
    }
    if (duration !== null && duration < 10) {
      setError("Mẫu giọng cần dài ít nhất 10 giây để AI nhận diện chuẩn xác. Vui lòng thu lại.");
      return;
    }
    if (duration !== null && duration > 25) {
      setError("Mẫu giọng không được dài quá 25 giây.");
      return;
    }

    setIsSubmitting(true);
    setError(null);

    try {
      let profile: VoiceProfile;
      if (meetingId && profileId) {
        profile = await voiceProfilesApi.addMeetingGuestSample(meetingId, profileId, audioBlob);
      } else if (profileId) {
        profile = await voiceProfilesApi.addSample(profileId, audioBlob);
      } else if (meetingId) {
        profile = await voiceProfilesApi.enrollMeetingGuest(meetingId, name.trim(), audioBlob);
      } else if (isSpeakerMode) {
        profile = await voiceProfilesApi.create(name.trim(), audioBlob);
      } else {
        profile = await voiceProfilesApi.enrollMyProfile(audioBlob);
      }
      onEnrolled(profile);
      onClose();
    } catch (err: unknown) {
      const msg = err instanceof Error ? err.message : "Không thể lưu mẫu giọng nói.";
      setError(translateErrorMessage(undefined, msg));
    } finally {
      setIsSubmitting(false);
    }
  };

  const isAddSampleMode = Boolean(profileId);
  const targetLabel = isAddSampleMode
    ? `Thu âm Mẫu ${sampleIndexToEnroll} cho ${defaultName || name}`
    : "Thêm người nói mới";

  return (
    <div
      style={{
        position: "fixed",
        inset: 0,
        backgroundColor: "rgba(15, 23, 42, 0.6)",
        backdropFilter: "blur(4px)",
        display: "flex",
        alignItems: "center",
        justifyContent: "center",
        zIndex: 9999,
        padding: "16px",
      }}
    >
      <div
        style={{
          backgroundColor: "var(--surface, #ffffff)",
          color: "var(--text, #0f172a)",
          borderRadius: "16px",
          width: "100%",
          maxWidth: "500px",
          boxShadow: "0 25px 50px -12px rgba(0, 0, 0, 0.25)",
          border: "1px solid var(--border, #e2e8f0)",
          overflow: "hidden",
          animation: "modalFadeIn 0.2s ease-out",
        }}
      >
        {/* Header */}
        <div
          style={{
            padding: "18px 24px",
            borderBottom: "1px solid var(--border, #e2e8f0)",
            display: "flex",
            alignItems: "center",
            justifyContent: "space-between",
          }}
        >
          <div>
            <h2 style={{ fontSize: "17px", fontWeight: "700", margin: 0, display: "flex", alignItems: "center", gap: "8px" }}>
              <span>🎙️</span>
              <span>{targetLabel}</span>
            </h2>
            <p style={{ margin: "3px 0 0", fontSize: "13px", color: "var(--muted, #64748b)" }}>
              {isAddSampleMode
                ? `Mẫu ${sampleIndexToEnroll}/3 giúp AI nhận diện tốt hơn khi giọng nói thay đổi.`
                : "Thu âm mẫu 1 (10 - 25 giây) để AI nhận diện trong cuộc họp."}
            </p>
          </div>
          <button
            onClick={onClose}
            disabled={isSubmitting || isRecording}
            style={{
              background: "none",
              border: "none",
              cursor: "pointer",
              color: "var(--muted, #94a3b8)",
              padding: "6px",
              borderRadius: "8px",
              display: "flex",
              alignItems: "center",
            }}
            title="Đóng"
          >
            <Icon name="close" />
          </button>
        </div>

        {/* Content */}
        <div style={{ padding: "20px 24px" }}>
          {/* Name input (only for new speaker) */}
          {!profileId && (
            <div style={{ marginBottom: "18px" }}>
              <label
                style={{
                  display: "block",
                  fontSize: "13px",
                  fontWeight: "600",
                  marginBottom: "6px",
                  color: "var(--text, #1e293b)",
                }}
              >
                Tên người nói <span style={{ color: "#ef4444" }}>*</span>
              </label>
              <input
                type="text"
                value={name}
                onChange={(e) => setName(e.target.value)}
                placeholder="Ví dụ: Nguyễn Văn A, Giám đốc..."
                autoFocus
                disabled={isSubmitting || isRecording}
                style={{
                  width: "100%",
                  padding: "10px 14px",
                  border: "1.5px solid var(--border, #cbd5e1)",
                  borderRadius: "10px",
                  fontSize: "14px",
                  background: "var(--surface, #ffffff)",
                  color: "inherit",
                  boxSizing: "border-box",
                  outline: "none",
                  transition: "border-color 0.15s",
                }}
              />
            </div>
          )}

          {/* Reading prompt suggestion card */}
          <div
            style={{
              background: "var(--surface-soft, #f8fafc)",
              border: "1px solid var(--border, #e2e8f0)",
              borderRadius: "12px",
              padding: "14px 16px",
              marginBottom: "20px",
            }}
          >
            <div
              style={{
                fontSize: "12px",
                fontWeight: "600",
                color: "#6366f1",
                marginBottom: "5px",
                display: "flex",
                alignItems: "center",
                gap: "6px",
              }}
            >
              <span>💬</span> Đọc to đoạn thoại sau vào micro (10 - 20 giây):
            </div>
            <div
              style={{
                fontSize: "14px",
                lineHeight: "1.6",
                color: "var(--text, #334155)",
                fontStyle: "italic",
              }}
            >
              "Tôi là {name.trim() || "[Tên của bạn]"}, đây là mẫu giọng nói của tôi để hệ thống tự
              động nhận diện và gán tên trong các cuộc họp."
            </div>
          </div>

          {/* Recording / Audio Area */}
          {!showUpload ? (
            <div style={{ textAlign: "center", padding: "8px 0" }}>
              {/* Not yet recorded and not recording */}
              {!isRecording && !audioUrl && (
                <div>
                  <button
                    type="button"
                    onClick={startRecording}
                    style={{
                      display: "inline-flex",
                      alignItems: "center",
                      justifyContent: "center",
                      gap: "10px",
                      padding: "14px 28px",
                      backgroundColor: "#ef4444",
                      color: "#ffffff",
                      border: "none",
                      borderRadius: "999px",
                      fontSize: "15px",
                      fontWeight: "600",
                      cursor: "pointer",
                      boxShadow: "0 4px 14px rgba(239, 68, 68, 0.4)",
                      transition: "transform 0.15s, background-color 0.15s",
                    }}
                  >
                    <span style={{ fontSize: "18px" }}>🎙️</span>
                    <span>Bấm để bắt đầu nói</span>
                  </button>
                  <p style={{ fontSize: "12px", color: "var(--muted, #94a3b8)", marginTop: "10px" }}>
                    Thời lượng cần từ 10 đến 25 giây
                  </p>
                </div>
              )}

              {/* Is recording */}
              {isRecording && (
                <div>
                  {/* Pulsing indicator & timer */}
                  <div
                    style={{
                      display: "flex",
                      alignItems: "center",
                      justifyContent: "center",
                      gap: "12px",
                      marginBottom: "12px",
                    }}
                  >
                    <span
                      style={{
                        width: "14px",
                        height: "14px",
                        borderRadius: "50%",
                        backgroundColor: "#ef4444",
                        animation: "pulse 1.2s infinite",
                      }}
                    />
                    <span
                      style={{
                        fontSize: "24px",
                        fontWeight: "700",
                        color: "#ef4444",
                        fontVariantNumeric: "tabular-nums",
                      }}
                    >
                      0:{recordingSeconds < 10 ? `0${recordingSeconds}` : recordingSeconds} / 0:25s
                    </span>
                  </div>

                  {/* Visual progress bar */}
                  <div
                    style={{
                      width: "100%",
                      height: "8px",
                      backgroundColor: "var(--border, #e2e8f0)",
                      borderRadius: "999px",
                      overflow: "hidden",
                      marginBottom: "12px",
                    }}
                  >
                    <div
                      style={{
                        height: "100%",
                        width: `${Math.min(100, (recordingSeconds / 25) * 100)}%`,
                        backgroundColor: recordingSeconds >= 10 ? "#10b981" : "#f59e0b",
                        transition: "width 0.25s ease",
                      }}
                    />
                  </div>

                  {/* Helper message */}
                  <p
                    style={{
                      fontSize: "13px",
                      fontWeight: "500",
                      color: recordingSeconds >= 10 ? "#059669" : "#d97706",
                      marginBottom: "16px",
                    }}
                  >
                    {recordingSeconds < 10
                      ? `Cần nói thêm ít nhất ${10 - recordingSeconds} giây nữa…`
                      : "✓ Đã đủ độ dài! Bạn có thể bấm Dừng bất kỳ lúc nào."}
                  </p>

                  <button
                    type="button"
                    onClick={stopRecording}
                    style={{
                      display: "inline-flex",
                      alignItems: "center",
                      gap: "8px",
                      padding: "12px 24px",
                      backgroundColor: "#0f172a",
                      color: "#ffffff",
                      border: "none",
                      borderRadius: "999px",
                      fontSize: "14px",
                      fontWeight: "600",
                      cursor: "pointer",
                    }}
                  >
                    <span>⏹</span>
                    <span>Dừng thu âm</span>
                  </button>
                </div>
              )}

              {/* Recorded successfully, preview audio */}
              {audioUrl && !isRecording && (
                <div
                  style={{
                    background: "var(--surface-soft, #f8fafc)",
                    border: "1px solid var(--border, #e2e8f0)",
                    borderRadius: "12px",
                    padding: "16px",
                  }}
                >
                  <div
                    style={{
                      display: "flex",
                      alignItems: "center",
                      justifyContent: "space-between",
                      marginBottom: "10px",
                    }}
                  >
                    <span
                      style={{
                        fontSize: "13px",
                        fontWeight: "600",
                        color:
                          recordedDurationSeconds !== null && recordedDurationSeconds < 10
                            ? "#dc2626"
                            : "#059669",
                      }}
                    >
                      {recordedDurationSeconds !== null && recordedDurationSeconds < 10
                        ? `⚠️ Mẫu chỉ có ${recordedDurationSeconds.toFixed(1)}s (chưa đủ 10s)`
                        : `✓ Đã thu ${(recordedDurationSeconds ?? 0).toFixed(1)}s (Hợp lệ)`}
                    </span>

                    <button
                      type="button"
                      onClick={handleResetAudio}
                      disabled={isSubmitting}
                      style={{
                        background: "none",
                        border: "none",
                        color: "#6366f1",
                        fontSize: "13px",
                        fontWeight: "600",
                        cursor: "pointer",
                        textDecoration: "underline",
                      }}
                    >
                      ↺ Thu âm lại
                    </button>
                  </div>

                  <audio src={audioUrl} controls style={{ width: "100%", height: "40px" }} />
                </div>
              )}

              {/* Secondary link to switch to file upload */}
              {!isRecording && !audioUrl && (
                <div style={{ marginTop: "16px" }}>
                  <button
                    type="button"
                    onClick={() => setShowUpload(true)}
                    style={{
                      background: "none",
                      border: "none",
                      color: "var(--muted, #64748b)",
                      fontSize: "12px",
                      cursor: "pointer",
                      textDecoration: "underline",
                    }}
                  >
                    📁 Hoặc tải lên file âm thanh (.wav, .mp3, .m4a)
                  </button>
                </div>
              )}
            </div>
          ) : (
            /* Upload file mode */
            <div style={{ textAlign: "center", padding: "8px 0" }}>
              <input
                type="file"
                accept="audio/*"
                onChange={handleFileUpload}
                style={{ display: "none" }}
                id="voice-file-input"
                disabled={isSubmitting}
              />
              <label
                htmlFor="voice-file-input"
                style={{
                  display: "block",
                  padding: "24px 16px",
                  border: "2px dashed var(--border, #cbd5e1)",
                  borderRadius: "12px",
                  backgroundColor: "var(--surface-soft, #f8fafc)",
                  cursor: "pointer",
                }}
              >
                <div style={{ fontSize: "28px", marginBottom: "8px" }}>📁</div>
                <div style={{ fontSize: "14px", fontWeight: "600" }}>
                  Bấm để chọn file âm thanh mẫu
                </div>
                <div style={{ fontSize: "12px", color: "var(--muted, #94a3b8)", marginTop: "4px" }}>
                  Độ dài từ 10 đến 25 giây (.WAV, .MP3, .M4A)
                </div>
              </label>

              {audioUrl && (
                <div style={{ marginTop: "12px" }}>
                  <audio src={audioUrl} controls style={{ width: "100%", height: "40px" }} />
                </div>
              )}

              <div style={{ marginTop: "12px" }}>
                <button
                  type="button"
                  onClick={() => {
                    handleResetAudio();
                    setShowUpload(false);
                  }}
                  style={{
                    background: "none",
                    border: "none",
                    color: "#6366f1",
                    fontSize: "12px",
                    cursor: "pointer",
                    textDecoration: "underline",
                  }}
                >
                  🎙️ Quay lại ghi âm trực tiếp
                </button>
              </div>
            </div>
          )}

          {/* Error notice */}
          {error && (
            <div
              style={{
                marginTop: "16px",
                padding: "10px 14px",
                backgroundColor: "#fef2f2",
                border: "1px solid #fecaca",
                borderRadius: "8px",
                color: "#b91c1c",
                fontSize: "13px",
                display: "flex",
                alignItems: "center",
                gap: "8px",
              }}
            >
              <span>⚠️</span>
              <span>{error}</span>
            </div>
          )}
        </div>

        {/* Footer actions */}
        <div
          style={{
            padding: "14px 24px",
            backgroundColor: "var(--surface-soft, #f8fafc)",
            borderTop: "1px solid var(--border, #e2e8f0)",
            display: "flex",
            justifyContent: "flex-end",
            alignItems: "center",
            gap: "10px",
          }}
        >
          <Button tone="light" onClick={onClose} disabled={isSubmitting || isRecording}>
            Hủy bỏ
          </Button>

          <Button
            tone="dark"
            onClick={() => void handleSave()}
            disabled={
              !audioBlob ||
              isSubmitting ||
              isRecording ||
              (recordedDurationSeconds !== null && recordedDurationSeconds < 10) ||
              (!profileId && !name.trim())
            }
            style={{
              padding: "9px 20px",
              fontWeight: 600,
            }}
          >
            {isSubmitting ? "Đang lưu…" : "Lưu mẫu giọng"}
          </Button>
        </div>
      </div>
    </div>
  );
}

function audioDurationSeconds(blob: Blob): Promise<number> {
  return new Promise((resolve, reject) => {
    const url = URL.createObjectURL(blob);
    const audio = new Audio();
    audio.preload = "metadata";
    audio.onloadedmetadata = () => {
      const duration = audio.duration;
      URL.revokeObjectURL(url);
      if (Number.isFinite(duration)) resolve(duration);
      else reject(new Error("Invalid duration"));
    };
    audio.onerror = () => {
      URL.revokeObjectURL(url);
      reject(new Error("Invalid audio"));
    };
    audio.src = url;
  });
}
