import { useState, useEffect } from "react";
import { translateErrorMessage } from "../api/client";
import { voiceProfilesApi } from "../api/endpoints";
import { useAsync } from "../hooks/useAsync";
import { Button, Empty, Failure, Icon, Loading, PageHeading } from "../components/ui";
import { VoiceEnrollmentModal } from "../components/VoiceEnrollmentModal";
import type { VoiceProfile } from "../types/domain";

// Avatar colors
const AVATAR_COLORS = [
  "linear-gradient(135deg, #6366f1 0%, #4f46e5 100%)",
  "linear-gradient(135deg, #0ea5e9 0%, #0284c7 100%)",
  "linear-gradient(135deg, #10b981 0%, #059669 100%)",
  "linear-gradient(135deg, #f59e0b 0%, #d97706 100%)",
  "linear-gradient(135deg, #ec4899 0%, #db2777 100%)",
  "linear-gradient(135deg, #8b5cf6 0%, #7c3aed 100%)",
];

function getAvatarColor(name: string): string {
  let hash = 0;
  for (let i = 0; i < name.length; i++) {
    hash = name.charCodeAt(i) + ((hash << 5) - hash);
  }
  return AVATAR_COLORS[Math.abs(hash) % AVATAR_COLORS.length];
}

function formatTimestamp(isoString?: string): string {
  if (!isoString) return "";
  try {
    const d = new Date(isoString);
    if (isNaN(d.getTime())) return "";
    const hours = String(d.getHours()).padStart(2, "0");
    const minutes = String(d.getMinutes()).padStart(2, "0");
    const day = String(d.getDate()).padStart(2, "0");
    const month = String(d.getMonth() + 1).padStart(2, "0");
    const year = d.getFullYear();
    return `${hours}:${minutes} · ${day}/${month}/${year}`;
  } catch {
    return "";
  }
}

// Pause other audios when one starts playing
function pauseOtherAudios(current: HTMLAudioElement) {
  document.querySelectorAll("audio").forEach((el) => {
    if (el !== current && !el.paused) {
      el.pause();
    }
  });
}

// Global cache for sample audio object URLs to prevent re-fetching
const audioBlobCache = new Map<string, string>();

function SampleAudioBar({
  profileId,
  sampleIndex,
  onDurationLoaded,
  onAudioMissing,
}: {
  profileId: string;
  sampleIndex: number;
  onDurationLoaded?: (durationSec: number) => void;
  onAudioMissing?: (profileId: string) => void;
}) {
  const cacheKey = `${profileId}_${sampleIndex}`;
  const [audioUrl, setAudioUrl] = useState<string | null>(audioBlobCache.get(cacheKey) ?? null);
  const [loading, setLoading] = useState(!audioBlobCache.has(cacheKey));
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (audioBlobCache.has(cacheKey)) {
      setAudioUrl(audioBlobCache.get(cacheKey)!);
      setLoading(false);
      return;
    }

    let active = true;
    setLoading(true);
    setError(null);

    voiceProfilesApi
      .getAudioBlob(profileId, sampleIndex)
      .then((blob) => {
        if (!active) return;
        const url = URL.createObjectURL(blob);
        audioBlobCache.set(cacheKey, url);
        setAudioUrl(url);
        setLoading(false);
      })
      .catch((err: unknown) => {
        if (!active) return;
        const msg = err instanceof Error ? err.message : "Không tìm thấy file âm thanh.";
        setError(translateErrorMessage(undefined, msg));
        setLoading(false);
        if (onAudioMissing) {
          onAudioMissing(profileId);
        }
      });

    return () => {
      active = false;
    };
  }, [profileId, sampleIndex, cacheKey, onAudioMissing]);

  if (loading) {
    return (
      <div
        style={{
          display: "flex",
          alignItems: "center",
          gap: "8px",
          padding: "8px 4px",
          fontSize: "12px",
          color: "var(--muted, #64748b)",
        }}
      >
        <span className="spinner" style={{ width: 14, height: 14 }} />
        <span>Đang tải file âm thanh lần thu {sampleIndex}…</span>
      </div>
    );
  }

  if (error) {
    return (
      <div
        style={{
          padding: "6px 10px",
          fontSize: "12px",
          color: "#b91c1c",
          backgroundColor: "#fef2f2",
          border: "1px solid #fecaca",
          borderRadius: "8px",
          marginTop: "4px",
        }}
      >
        ⚠️ {error}
      </div>
    );
  }

  if (!audioUrl) return null;

  return (
    <audio
      controls
      controlsList="nodownload noplaybackrate"
      preload="metadata"
      src={audioUrl}
      onPlay={(e) => pauseOtherAudios(e.currentTarget)}
      onLoadedMetadata={(e) => {
        if (onDurationLoaded && Number.isFinite(e.currentTarget.duration)) {
          onDurationLoaded(e.currentTarget.duration);
        }
      }}
      style={{
        width: "100%",
        height: "38px",
        borderRadius: "999px",
        display: "block",
        marginTop: "4px",
      }}
    >
      Trình duyệt không hỗ trợ phát âm thanh.
    </audio>
  );
}

function RecordedSampleItem({
  profile,
  slotIndex,
  onDeleteRequest,
  onAudioMissing,
}: {
  profile: VoiceProfile;
  slotIndex: number;
  onDeleteRequest: (profile: VoiceProfile, slotIndex: number) => void;
  onAudioMissing?: (profileId: string) => void;
}) {
  const [duration, setDuration] = useState<number | null>(null);

  return (
    <div className="voice-sample-player-box">
      <div
        style={{
          display: "flex",
          alignItems: "center",
          justifyContent: "space-between",
          flexWrap: "wrap",
          gap: "8px",
        }}
      >
        <div style={{ display: "flex", alignItems: "center", gap: "8px", flexWrap: "wrap" }}>
          <span style={{ fontSize: "14px" }}>🎙️</span>
          <strong style={{ fontSize: "13px" }}>Lần thu {slotIndex}</strong>
          {duration !== null && (
            <span
              style={{
                fontSize: "11px",
                fontWeight: 600,
                color: "var(--text, #0f172a)",
                backgroundColor: "var(--surface, #ffffff)",
                border: "1px solid var(--border, #cbd5e1)",
                padding: "1px 7px",
                borderRadius: "6px",
              }}
            >
              ⏱️ {duration.toFixed(1)}s
            </span>
          )}
          <span
            style={{
              fontSize: "11px",
              color: "var(--muted, #64748b)",
              display: "inline-flex",
              alignItems: "center",
              gap: "4px",
            }}
          >
            <span>🕒</span> {formatTimestamp(profile.updated_at || profile.created_at)}
          </span>
          <span
            style={{
              fontSize: "11px",
              fontWeight: 600,
              color: "#059669",
              backgroundColor: "#ecfdf5",
              padding: "1px 7px",
              borderRadius: "999px",
              border: "1px solid #a7f3d0",
            }}
          >
            ✓ Đã lưu
          </span>
        </div>

        {/* Delete sample button */}
        <button
          type="button"
          onClick={() => onDeleteRequest(profile, slotIndex)}
          style={{
            background: "none",
            border: "none",
            cursor: "pointer",
            color: "var(--muted, #94a3b8)",
            padding: "3px 8px",
            borderRadius: "6px",
            display: "inline-flex",
            alignItems: "center",
            gap: "4px",
            fontSize: "12px",
            transition: "all 0.15s ease",
          }}
          title={`Xóa lần thu ${slotIndex}`}
          onMouseEnter={(e) => {
            e.currentTarget.style.color = "#ef4444";
            e.currentTarget.style.backgroundColor = "#fef2f2";
          }}
          onMouseLeave={(e) => {
            e.currentTarget.style.color = "var(--muted, #94a3b8)";
            e.currentTarget.style.backgroundColor = "transparent";
          }}
        >
          <Icon name="trash" />
          <span style={{ fontSize: "11px", fontWeight: 500 }}>Xóa lần thu</span>
        </button>
      </div>

      {/* Dedicated Audio Player Bar */}
      <SampleAudioBar
        profileId={profile.id}
        sampleIndex={slotIndex}
        onDurationLoaded={setDuration}
        onAudioMissing={onAudioMissing}
      />
    </div>
  );
}

export function VoiceProfilesPage() {
  const profilesQuery = useAsync(() => voiceProfilesApi.list(), []);
  const [profiles, setProfiles] = useState<VoiceProfile[]>([]);
  const [search, setSearch] = useState("");
  const [createModalOpen, setCreateModalOpen] = useState(false);
  const [sampleModalTarget, setSampleModalTarget] = useState<{
    profile: VoiceProfile;
    targetIndex: number;
  } | null>(null);
  const [deletingId, setDeletingId] = useState<string | null>(null);
  const [confirmDeleteProfile, setConfirmDeleteProfile] = useState<VoiceProfile | null>(null);
  const [confirmDeleteSample, setConfirmDeleteSample] = useState<{
    profile: VoiceProfile;
    sampleIndex: number;
  } | null>(null);
  const [deletingSample, setDeletingSample] = useState(false);

  useEffect(() => {
    if (profilesQuery.data) {
      setProfiles(profilesQuery.data);
    }
  }, [profilesQuery.data]);

  const handleDeleteProfile = async (profile: VoiceProfile) => {
    setDeletingId(profile.id);
    try {
      await voiceProfilesApi.delete(profile.id);
      setProfiles((prev) => prev.filter((p) => p.id !== profile.id));
      setConfirmDeleteProfile(null);

      // Clean cached audio URLs for this profile
      for (let idx = 1; idx <= 3; idx++) {
        const k = `${profile.id}_${idx}`;
        const u = audioBlobCache.get(k);
        if (u) {
          URL.revokeObjectURL(u);
          audioBlobCache.delete(k);
        }
      }
    } catch (cause) {
      alert(translateErrorMessage(undefined, (cause as Error).message) || "Không thể xóa người nói.");
    } finally {
      setDeletingId(null);
    }
  };

  const handleConfirmDeleteSample = async () => {
    if (!confirmDeleteSample) return;
    const { profile, sampleIndex } = confirmDeleteSample;
    setDeletingSample(true);

    try {
      const updated = await voiceProfilesApi.deleteSample(profile.id, sampleIndex);

      // Invalidate cached audio URLs for this profile
      for (let idx = 1; idx <= 3; idx++) {
        const k = `${profile.id}_${idx}`;
        const u = audioBlobCache.get(k);
        if (u) {
          URL.revokeObjectURL(u);
          audioBlobCache.delete(k);
        }
      }

      if (profile.sample_count <= 1) {
        // Only remaining sample was deleted, profile is gone
        setProfiles((prev) => prev.filter((p) => p.id !== profile.id));
      } else {
        setProfiles((prev) => prev.map((p) => (p.id === updated.id ? updated : p)));
      }
      setConfirmDeleteSample(null);
    } catch (err: unknown) {
      alert(translateErrorMessage(undefined, (err as Error).message) || "Không thể xóa lần thu này.");
    } finally {
      setDeletingSample(false);
    }
  };

  const handleAudioMissing = (profileId: string) => {
    // Automatically purge broken profile from UI and delete from DB
    setProfiles((prev) => prev.filter((p) => p.id !== profileId));
    void voiceProfilesApi.delete(profileId).catch(() => {});
  };

  const filtered = profiles.filter((p) =>
    p.name.toLowerCase().includes(search.trim().toLowerCase())
  );

  const totalSamples = profiles.reduce((sum, p) => sum + (p.sample_count || 1), 0);

  return (
    <>
      <PageHeading
        title="Mẫu giọng"
        description="Đăng ký mẫu giọng để hệ thống tự động nhận diện và gán tên người phát biểu trong mọi cuộc họp."
        actions={
          <Button
            tone="dark"
            onClick={() => setCreateModalOpen(true)}
            style={{
              display: "inline-flex",
              alignItems: "center",
              gap: "8px",
              padding: "10px 18px",
              fontSize: "14px",
              fontWeight: 600,
              borderRadius: "10px",
            }}
          >
            <Icon name="plus" />
            Thêm người nói mới
          </Button>
        }
      />

      <div style={{ marginTop: 20 }}>
        {/* Search and summary bar */}
        <div
          style={{
            display: "flex",
            justifyContent: "space-between",
            alignItems: "center",
            flexWrap: "wrap",
            gap: "12px",
            marginBottom: "20px",
          }}
        >
          <div style={{ display: "flex", alignItems: "center", gap: "12px" }}>
            <span style={{ fontSize: "18px" }}>🎙️</span>
            <strong style={{ fontSize: "15px" }}>Danh sách người nói</strong>
            {profiles.length > 0 && (
              <span
                style={{
                  fontSize: "12px",
                  fontWeight: 600,
                  padding: "3px 10px",
                  borderRadius: "999px",
                  background: "var(--surface-soft, #f1f5f9)",
                  color: "var(--muted, #64748b)",
                  border: "1px solid var(--border, #e2e8f0)",
                }}
              >
                {profiles.length} người · {totalSamples} mẫu giọng
              </span>
            )}
          </div>

          {profiles.length > 0 && (
            <div style={{ width: "280px" }}>
              <input
                type="text"
                placeholder="🔍 Tìm theo tên người nói…"
                value={search}
                onChange={(e) => setSearch(e.target.value)}
                style={{
                  width: "100%",
                  padding: "9px 14px",
                  borderRadius: "10px",
                  border: "1px solid var(--border, #cbd5e1)",
                  fontSize: "13px",
                  background: "var(--surface, #ffffff)",
                  color: "inherit",
                  boxSizing: "border-box",
                }}
              />
            </div>
          )}
        </div>

        {profilesQuery.loading && <Loading label="Đang tải danh sách mẫu giọng…" />}
        {profilesQuery.error && (
          <Failure error={profilesQuery.error} retry={profilesQuery.reload} />
        )}

        {!profilesQuery.loading && !profilesQuery.error && (
          <>
            {filtered.length > 0 ? (
              /* STRICT 2-COLUMN GRID (responsive to 1 col on mobile) */
              <div className="voice-profiles-grid">
                {filtered.map((profile) => {
                  const initials = profile.name
                    .split(/\s+/)
                    .map((w) => w[0])
                    .join("")
                    .slice(0, 2)
                    .toUpperCase();

                  const count = Math.max(1, profile.sample_count || 1);
                  const isFull = count >= 3;

                  return (
                    <article key={profile.id} className="voice-profile-card">
                      {/* Top row: Avatar, Name, Progress Badge & Delete icon */}
                      <div
                        style={{
                          display: "flex",
                          alignItems: "center",
                          justifyContent: "space-between",
                          gap: "12px",
                        }}
                      >
                        <div
                          style={{
                            display: "flex",
                            alignItems: "center",
                            gap: "12px",
                            minWidth: 0,
                          }}
                        >
                          <div
                            style={{
                              width: "44px",
                              height: "44px",
                              borderRadius: "12px",
                              background: getAvatarColor(profile.name),
                              color: "#ffffff",
                              display: "flex",
                              alignItems: "center",
                              justifyContent: "center",
                              fontSize: "16px",
                              fontWeight: 700,
                              flexShrink: 0,
                              boxShadow: "0 2px 6px rgba(0,0,0,0.12)",
                            }}
                          >
                            {initials}
                          </div>

                          <div style={{ minWidth: 0 }}>
                            <strong
                              style={{
                                fontSize: "16px",
                                fontWeight: 700,
                                display: "block",
                                overflow: "hidden",
                                textOverflow: "ellipsis",
                                whiteSpace: "nowrap",
                                color: "var(--text, #0f172a)",
                              }}
                              title={profile.name}
                            >
                              {profile.name}
                            </strong>
                            <div
                              style={{
                                display: "flex",
                                alignItems: "center",
                                gap: "8px",
                                marginTop: "3px",
                              }}
                            >
                              <span
                                style={{
                                  fontSize: "11px",
                                  fontWeight: 600,
                                  padding: "2px 8px",
                                  borderRadius: "999px",
                                  background: isFull ? "#ecfdf5" : "var(--surface-soft, #f1f5f9)",
                                  color: isFull ? "#059669" : "var(--muted, #64748b)",
                                  border: isFull ? "1px solid #a7f3d0" : "1px solid var(--border, #e2e8f0)",
                                }}
                              >
                                {isFull ? "✓ Đã đủ 3/3 mẫu" : `Đã thu ${count}/3 mẫu`}
                              </span>
                              <small
                                style={{
                                  color: "var(--muted, #64748b)",
                                  fontSize: "11.5px",
                                }}
                              >
                                {(profile.sample_duration_ms / 1000).toFixed(1)}s tổng thời lượng
                              </small>
                            </div>
                          </div>
                        </div>

                        {/* Delete speaker button */}
                        <button
                          type="button"
                          onClick={() => setConfirmDeleteProfile(profile)}
                          disabled={deletingId === profile.id}
                          style={{
                            background: "none",
                            border: "none",
                            cursor: "pointer",
                            color: "var(--muted, #94a3b8)",
                            padding: "6px",
                            borderRadius: "8px",
                            display: "flex",
                            alignItems: "center",
                            transition: "color 0.15s",
                          }}
                          title="Xóa người nói này"
                          onMouseEnter={(e) => (e.currentTarget.style.color = "#ef4444")}
                          onMouseLeave={(e) =>
                            (e.currentTarget.style.color = "var(--muted, #94a3b8)")
                          }
                        >
                          <Icon name="trash" />
                        </button>
                      </div>

                      {/* 3-bar progress indicator */}
                      <div
                        style={{
                          display: "grid",
                          gridTemplateColumns: "1fr 1fr 1fr",
                          gap: "6px",
                          marginTop: "-4px",
                        }}
                      >
                        {[1, 2, 3].map((slotIdx) => (
                          <div
                            key={slotIdx}
                            style={{
                              height: "4px",
                              borderRadius: "999px",
                              backgroundColor:
                                slotIdx <= count
                                  ? "#10b981"
                                  : "var(--border, #e2e8f0)",
                              transition: "background-color 0.2s",
                            }}
                          />
                        ))}
                      </div>

                      {/* Separator */}
                      <div
                        style={{
                          height: "1px",
                          backgroundColor: "var(--border, #f1f5f9)",
                        }}
                      />

                      {/* INDIVIDUAL RECORDING SESSIONS (LẦN THU 1, 2, 3) */}
                      <div style={{ display: "flex", flexDirection: "column", gap: "10px" }}>
                        {[1, 2, 3].map((slotIndex) => {
                          const hasSample = slotIndex <= count;
                          const isNextSlot = slotIndex === count + 1;

                          if (hasSample) {
                            // Enrolled sample with timestamp, duration & individual audio player bar
                            return (
                              <RecordedSampleItem
                                key={slotIndex}
                                profile={profile}
                                slotIndex={slotIndex}
                                onDeleteRequest={(p, idx) =>
                                  setConfirmDeleteSample({ profile: p, sampleIndex: idx })
                                }
                                onAudioMissing={handleAudioMissing}
                              />
                            );
                          }

                          if (isNextSlot) {
                            // Slot ready to be recorded
                            return (
                              <button
                                key={slotIndex}
                                type="button"
                                onClick={() =>
                                  setSampleModalTarget({
                                    profile,
                                    targetIndex: slotIndex,
                                  })
                                }
                                style={{
                                  display: "flex",
                                  alignItems: "center",
                                  justifyContent: "space-between",
                                  padding: "12px 16px",
                                  borderRadius: "12px",
                                  border: "1.5px dashed var(--border, #cbd5e1)",
                                  backgroundColor: "var(--surface, #ffffff)",
                                  cursor: "pointer",
                                  textAlign: "left",
                                  color: "#6366f1",
                                  transition: "all 0.15s ease",
                                }}
                                onMouseEnter={(e) => {
                                  e.currentTarget.style.borderColor = "#6366f1";
                                  e.currentTarget.style.backgroundColor = "rgba(99, 102, 241, 0.04)";
                                }}
                                onMouseLeave={(e) => {
                                  e.currentTarget.style.borderColor = "var(--border, #cbd5e1)";
                                  e.currentTarget.style.backgroundColor = "var(--surface, #ffffff)";
                                }}
                              >
                                <div style={{ display: "flex", alignItems: "center", gap: "10px" }}>
                                  <span style={{ fontSize: "16px" }}>🎙️</span>
                                  <div>
                                    <strong style={{ fontSize: "13.5px", display: "block" }}>
                                      Thu âm Lần {slotIndex}
                                    </strong>
                                    <small
                                      style={{
                                        color: "var(--muted, #64748b)",
                                        fontSize: "12px",
                                      }}
                                    >
                                      Bổ sung mẫu giọng để AI nhận diện chuẩn xác hơn
                                    </small>
                                  </div>
                                </div>
                                <span
                                  style={{
                                    fontSize: "12px",
                                    fontWeight: 600,
                                    backgroundColor: "rgba(99, 102, 241, 0.1)",
                                    color: "#4f46e5",
                                    padding: "5px 12px",
                                    borderRadius: "8px",
                                  }}
                                >
                                  + Thu mẫu
                                </span>
                              </button>
                            );
                          }

                          // Future slot (waiting)
                          return (
                            <div
                              key={slotIndex}
                              style={{
                                display: "flex",
                                alignItems: "center",
                                justifyContent: "space-between",
                                padding: "10px 14px",
                                borderRadius: "12px",
                                border: "1px dashed var(--border, #e2e8f0)",
                                backgroundColor: "var(--surface-soft, #f8fafc)",
                                opacity: 0.5,
                              }}
                            >
                              <div style={{ display: "flex", alignItems: "center", gap: "8px" }}>
                                <span style={{ fontSize: "13px" }}>🔒</span>
                                <span style={{ fontSize: "13px", color: "var(--muted, #94a3b8)" }}>
                                  Lần thu {slotIndex} (Cần thu lần {slotIndex - 1} trước)
                                </span>
                              </div>
                            </div>
                          );
                        })}
                      </div>
                    </article>
                  );
                })}
              </div>
            ) : profiles.length > 0 ? (
              <Empty
                title="Không tìm thấy người nói"
                detail={`Không có người nào khớp với từ khóa "${search}".`}
                action={
                  <Button tone="light" onClick={() => setSearch("")}>
                    Xóa tìm kiếm
                  </Button>
                }
              />
            ) : (
              /* Empty state */
              <div
                style={{
                  padding: "60px 24px",
                  textAlign: "center",
                  background: "var(--surface, #ffffff)",
                  borderRadius: "16px",
                  border: "1.5px dashed var(--border, #cbd5e1)",
                  marginTop: "16px",
                  boxShadow: "0 2px 10px rgba(0, 0, 0, 0.02)",
                }}
              >
                <div
                  style={{
                    width: "64px",
                    height: "64px",
                    borderRadius: "50%",
                    backgroundColor: "#f5f3ff",
                    color: "#6366f1",
                    display: "flex",
                    alignItems: "center",
                    justifyContent: "center",
                    fontSize: "28px",
                    margin: "0 auto 16px",
                  }}
                >
                  🎙️
                </div>
                <h3
                  style={{
                    fontSize: "19px",
                    fontWeight: 700,
                    margin: "0 0 8px",
                    color: "var(--text, #0f172a)",
                  }}
                >
                  Chưa có mẫu giọng người nói
                </h3>
                <p
                  style={{
                    color: "var(--muted, #64748b)",
                    fontSize: "14px",
                    maxWidth: "460px",
                    margin: "0 auto 24px",
                    lineHeight: 1.6,
                  }}
                >
                  Thêm mẫu giọng (10 - 25 giây) cho từng thành viên để hệ thống tự động nhận diện và
                  hiển thị tên người phát biểu trong các cuộc họp.
                </p>
                <Button
                  tone="dark"
                  onClick={() => setCreateModalOpen(true)}
                  style={{
                    padding: "11px 24px",
                    fontSize: "14px",
                    fontWeight: 600,
                    borderRadius: "10px",
                    display: "inline-flex",
                    alignItems: "center",
                    gap: "8px",
                  }}
                >
                  <Icon name="plus" />
                  Thêm người nói đầu tiên
                </Button>
              </div>
            )}
          </>
        )}
      </div>

      {/* Modal create speaker (Sample 1) */}
      <VoiceEnrollmentModal
        isOpen={createModalOpen}
        onClose={() => setCreateModalOpen(false)}
        onEnrolled={(newProfile) => {
          setProfiles((prev) => [newProfile, ...prev]);
        }}
        isSpeakerMode={true}
      />

      {/* Modal add sample 2 or 3 */}
      {sampleModalTarget && (
        <VoiceEnrollmentModal
          isOpen={true}
          onClose={() => setSampleModalTarget(null)}
          onEnrolled={(updated) => {
            // Invalidate cache for new sample
            const cacheKey = `${updated.id}_${sampleModalTarget.targetIndex}`;
            const oldUrl = audioBlobCache.get(cacheKey);
            if (oldUrl) URL.revokeObjectURL(oldUrl);
            audioBlobCache.delete(cacheKey);

            setProfiles((prev) => prev.map((p) => (p.id === updated.id ? updated : p)));
            setSampleModalTarget(null);
          }}
          profileId={sampleModalTarget.profile.id}
          defaultName={sampleModalTarget.profile.name}
          currentSampleCount={sampleModalTarget.profile.sample_count}
          targetSampleIndex={sampleModalTarget.targetIndex}
          isSpeakerMode={true}
        />
      )}

      {/* Confirmation modal for delete whole speaker */}
      {confirmDeleteProfile && (
        <div className="dialog-backdrop">
          <div className="dialog" style={{ width: 420, borderRadius: "16px" }}>
            <button
              className="close"
              aria-label="Đóng"
              onClick={() => setConfirmDeleteProfile(null)}
            >
              <Icon name="close" />
            </button>
            <h2 style={{ fontSize: "18px", fontWeight: 700 }}>Xác nhận xóa người nói</h2>
            <p
              style={{
                marginTop: "10px",
                fontSize: "14px",
                color: "var(--muted, #64748b)",
                lineHeight: 1.5,
              }}
            >
              Bạn có chắc chắn muốn xóa toàn bộ hồ sơ mẫu giọng của{" "}
              <strong style={{ color: "var(--text, #0f172a)" }}>
                "{confirmDeleteProfile.name}"
              </strong>
              ? Hệ thống sẽ không thể tự động nhận diện người này trong các cuộc họp tiếp theo.
            </p>
            <div className="form-actions" style={{ marginTop: "24px" }}>
              <Button
                tone="light"
                onClick={() => setConfirmDeleteProfile(null)}
                disabled={deletingId !== null}
              >
                Hủy bỏ
              </Button>
              <Button
                tone="danger"
                onClick={() => void handleDeleteProfile(confirmDeleteProfile)}
                disabled={deletingId !== null}
              >
                {deletingId ? "Đang xóa…" : "Xóa người nói"}
              </Button>
            </div>
          </div>
        </div>
      )}

      {/* Confirmation modal for deleting single sample */}
      {confirmDeleteSample && (
        <div className="dialog-backdrop">
          <div className="dialog" style={{ width: 420, borderRadius: "16px" }}>
            <button
              className="close"
              aria-label="Đóng"
              onClick={() => setConfirmDeleteSample(null)}
            >
              <Icon name="close" />
            </button>
            <h2 style={{ fontSize: "18px", fontWeight: 700 }}>
              Xác nhận xóa Lần thu {confirmDeleteSample.sampleIndex}
            </h2>
            <p
              style={{
                marginTop: "10px",
                fontSize: "14px",
                color: "var(--muted, #64748b)",
                lineHeight: 1.5,
              }}
            >
              {confirmDeleteSample.profile.sample_count <= 1 ? (
                <>
                  Người nói{" "}
                  <strong style={{ color: "var(--text, #0f172a)" }}>
                    "{confirmDeleteSample.profile.name}"
                  </strong>{" "}
                  chỉ còn duy nhất 1 lần thu này. Xóa lần thu này sẽ xóa toàn bộ hồ sơ người nói.
                  Bạn có chắc chắn muốn xóa?
                </>
              ) : (
                <>
                  Bạn có chắc chắn muốn xóa{" "}
                  <strong>Lần thu {confirmDeleteSample.sampleIndex}</strong> của người nói{" "}
                  <strong style={{ color: "var(--text, #0f172a)" }}>
                    "{confirmDeleteSample.profile.name}"
                  </strong>
                  ? Bạn có thể thu âm lại lần thu này bất cứ lúc nào.
                </>
              )}
            </p>
            <div className="form-actions" style={{ marginTop: "24px" }}>
              <Button
                tone="light"
                onClick={() => setConfirmDeleteSample(null)}
                disabled={deletingSample}
              >
                Hủy bỏ
              </Button>
              <Button
                tone="danger"
                onClick={() => void handleConfirmDeleteSample()}
                disabled={deletingSample}
              >
                {deletingSample ? "Đang xóa…" : "Xóa lần thu"}
              </Button>
            </div>
          </div>
        </div>
      )}
    </>
  );
}
