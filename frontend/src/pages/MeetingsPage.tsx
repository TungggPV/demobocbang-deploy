import { useState, useRef, useEffect } from "react";
import { meetingsApi } from "../api/endpoints";
import type { Meeting } from "../types/domain";
import { Icon, Status } from "../components/ui";
import { navigate } from "../app/router";

export function MeetingsPage({
  createOnly = false,
  initialSource,
}: {
  createOnly?: boolean;
  initialSource?: "realtime" | "upload";
}) {
  const isUpload = initialSource === "upload" || new URLSearchParams(window.location.search).get("mode") === "upload";

  if (isUpload) {
    return <UploadScreen />;
  }

  return <StartScreen />;
}

/* ═══════════════════════════════════════════
   START SCREEN - TẠO PHIÊN HỌP MỚI (ẢNH 2)
═══════════════════════════════════════════ */
function StartScreen() {
  const [title, setTitle] = useState("");
  const [creating, setCreating] = useState(false);

  async function handleCreate(e?: React.FormEvent) {
    if (e) e.preventDefault();
    if (!title.trim() || creating) return;
    setCreating(true);
    try {
      const meeting = await meetingsApi.create({
        title: title.trim(),
        source_type: "realtime",
        language_mode: "vi",
        diarization_enabled: true,
      });
      window.dispatchEvent(new CustomEvent("bocbang:refresh-meetings"));
      navigate(`/meetings/${meeting.id}`);
    } catch (cause) {
      alert((cause as Error).message);
      setCreating(false);
    }
  }

  return (
    <div className="start-screen-container">
      <h1 className="start-screen-title">Tạo phiên họp mới</h1>
      <p className="start-screen-subtitle">
        Nhập tên phiên họp để bắt đầu ghi âm và phân tích cuộc họp
      </p>

      <form className="start-screen-input-pill" onSubmit={handleCreate}>
        <input
          type="text"
          placeholder="Ví dụ: Họp team hàng tuần, Thảo luận dự án..."
          value={title}
          onChange={(e) => setTitle(e.target.value)}
          autoFocus
          disabled={creating}
        />
        <button
          type="submit"
          className="start-screen-submit-btn"
          disabled={!title.trim() || creating}
          aria-label="Bắt đầu phiên họp"
        >
          <Icon name="arrow-up" />
        </button>
      </form>
      <div className="start-screen-hint">
        Nhấn Enter để tạo phiên họp và bắt đầu
      </div>
    </div>
  );
}

/* ═══════════════════════════════════════════
   UPLOAD SCREEN - TẢI LÊN BẢN GHI SẴN CÓ (ẢNH 3)
═══════════════════════════════════════════ */
function UploadScreen() {
  const fileInputRef = useRef<HTMLInputElement>(null);
  const [uploading, setUploading] = useState(false);
  const [uploadProgress, setUploadProgress] = useState("");
  const [recentUploads, setRecentUploads] = useState<Meeting[]>([]);

  useEffect(() => {
    meetingsApi.list({ source_type: "upload", limit: 10 }).then(setRecentUploads).catch(() => {});
  }, []);

  async function handleFiles(files: FileList | null) {
    if (!files || files.length === 0) return;
    const file = files[0];
    const meetingTitle = file.name.replace(/\.[^/.]+$/, "");
    setUploading(true);
    setUploadProgress("Đang tạo phiên họp…");
    try {
      const meeting = await meetingsApi.create({
        title: meetingTitle,
        source_type: "upload",
        language_mode: "vi",
        diarization_enabled: true,
      });
      setUploadProgress("Đang tải tệp lên máy chủ…");
      await meetingsApi.uploadMedia(meeting.id, file);
      window.dispatchEvent(new CustomEvent("bocbang:refresh-meetings"));
      navigate(`/meetings/${meeting.id}`);
    } catch (e) {
      alert((e as Error).message);
      setUploading(false);
      setUploadProgress("");
    }
  }

  return (
    <div className="upload-screen-container">
      <h2 className="upload-screen-heading">Trạng thái bản ghi cuộc họp</h2>

      <div
        className="upload-dropzone-blue"
        onClick={() => !uploading && fileInputRef.current?.click()}
        onDragOver={(e) => e.preventDefault()}
        onDrop={(e) => {
          e.preventDefault();
          if (!uploading) handleFiles(e.dataTransfer.files);
        }}
      >
        <input
          ref={fileInputRef}
          type="file"
          accept="audio/*,.mp3,.wav,.m4a,.aac,.webm,.ogg"
          style={{ display: "none" }}
          onChange={(e) => handleFiles(e.target.files)}
        />
        <div className="upload-dropzone-icon">
          <Icon name="upload" />
        </div>
        <div className="upload-dropzone-title">
          {uploading ? uploadProgress : "Tải lên file ghi âm có sẵn"}
        </div>
        <div className="upload-dropzone-detail">
          Hỗ trợ MP3. Kích thước tối đa 1GB.
        </div>
      </div>

      <p className="upload-screen-notice">
        Lưu ý: Phụ thuộc vào kích cỡ của tệp, thời gian xử lý và tạo bóc băng mới sẽ từ 10 tới 15 phút.
      </p>

      <div className="upload-recent-section">
        <h3 style={{ fontSize: 14, fontWeight: 700, margin: 0, color: "var(--text)" }}>
          Tải lên gần đây
        </h3>
        {recentUploads.length === 0 ? (
          <div className="upload-recent-empty">Không có bản ghi</div>
        ) : (
          <div className="meeting-list">
            {recentUploads.map((m) => (
              <article key={m.id} className="meeting-row manageable-meeting-row">
                <a className="meeting-row-link" href={`/meetings/${m.id}`} />
                <div className="avatar">{(m.title || "AU").slice(0, 2).toUpperCase()}</div>
                <div className="meeting-row-main">
                  <strong>{m.title}</strong>
                  <p>
                    {new Intl.DateTimeFormat("vi-VN", {
                      day: "2-digit",
                      month: "2-digit",
                      year: "numeric",
                    }).format(new Date(m.created_at))}
                  </p>
                </div>
                <Status value={m.status} />
              </article>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}
