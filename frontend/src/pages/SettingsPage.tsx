import { useEffect, useState } from "react";
import { useAuth } from "../auth/AuthContext";
import { voiceProfilesApi } from "../api/endpoints";
import { useAsync } from "../hooks/useAsync";
import { PasswordModal } from "../components/PasswordModal";
import { VoiceEnrollmentModal } from "../components/VoiceEnrollmentModal";
import { Button, Icon, PageHeading } from "../components/ui";

export function SettingsPage() {
  const { user, logout, updateProfile, updatePassword } = useAuth();
  const [fullName, setFullName] = useState(user?.full_name ?? "");
  const [message, setMessage] = useState("");
  const [saving, setSaving] = useState(false);
  const [passwordModal, setPasswordModal] = useState(false);
  const [enrollModal, setEnrollModal] = useState(false);
  const [deletingVoice, setDeletingVoice] = useState(false);

  const voiceProfile = useAsync(async () => {
    try {
      return await voiceProfilesApi.getMyProfile();
    } catch {
      return null;
    }
  }, []);

  useEffect(() => setFullName(user?.full_name ?? ""), [user?.full_name]);

  async function save() {
    if (!fullName.trim()) return setMessage("Họ và tên không được để trống.");
    setSaving(true);
    setMessage("");
    try {
      await updateProfile(fullName.trim());
      setMessage("Đã cập nhật hồ sơ thành công.");
    } catch (cause) {
      setMessage((cause as Error).message);
    } finally {
      setSaving(false);
    }
  }

  async function deleteVoice() {
    if (!window.confirm("Bạn có chắc chắn muốn xóa mẫu giọng nói cá nhân không?")) return;
    setDeletingVoice(true);
    try {
      await voiceProfilesApi.deleteMyProfile();
      await voiceProfile.reload();
      setMessage("Đã xóa mẫu giọng nói cá nhân thành công.");
    } catch (cause) {
      setMessage((cause as Error).message);
    } finally {
      setDeletingVoice(false);
    }
  }

  return <>
    <PageHeading 
      eyebrow="TÀI KHOẢN" 
      title="Cài đặt" 
      description="Quản lý thông tin cá nhân, mẫu giọng nói nhận diện và bảo mật tài khoản." 
    />
    
    <div className="settings-container">
      {message && <div className={message.includes("thành công") ? "notice" : "form-error"}>{message}</div>}

      <h3 className="settings-group-title">Thông tin cá nhân</h3>
      <div className="setting-card">
        <div className="setting-row" style={{ alignItems: "center" }}>
          {user?.avatar_url ? (
            <img
              src={user.avatar_url}
              alt={user.full_name}
              style={{ width: 44, height: 44, borderRadius: "50%", objectFit: "cover", border: "1.5px solid var(--border)", flexShrink: 0 }}
              referrerPolicy="no-referrer"
            />
          ) : (
            <span className="person-avatar" style={{ width: 44, height: 44, fontSize: 15, borderRadius: "50%", flexShrink: 0 }}>
              {user?.full_name?.slice(0, 2).toUpperCase() || "BB"}
            </span>
          )}
          <div className="setting-content" style={{ marginLeft: 6 }}>
            <strong>Ảnh đại diện</strong>
            <small>{user?.avatar_url ? "Được tự động đồng bộ từ tài khoản Google của bạn." : "Ảnh mặc định theo chữ cái đầu của tên."}</small>
          </div>
          <div className="setting-action">
            {user?.avatar_url && (
              <span className="status" style={{ borderRadius: "999px", background: "var(--accent-soft)", color: "var(--accent-text)", fontSize: 12 }}>
                ✓ Google Account
              </span>
            )}
          </div>
        </div>
        <div className="setting-row">
          <div className="setting-icon"><Icon name="profile" /></div>
          <div className="setting-content">
            <strong>Họ và tên</strong>
            <small>Tên hiển thị trên hệ thống và các cuộc họp.</small>
          </div>
          <div className="setting-action">
            <input
              className="setting-input"
              value={fullName}
              onChange={(event) => setFullName(event.target.value)}
              placeholder="Nhập họ và tên"
            />
          </div>
        </div>
        <div className="setting-row">
          <div className="setting-icon"><Icon name="translate" /></div>
          <div className="setting-content">
            <strong>Email</strong>
            <small>Địa chỉ email dùng để đăng nhập và nhận thông báo.</small>
          </div>
          <div className="setting-action">
            <input className="setting-input" value={user?.email ?? ""} readOnly disabled />
          </div>
        </div>
        <div className="setting-row" style={{ justifyContent: "flex-end", borderBottom: "none" }}>
           <Button tone="dark" disabled={saving || fullName.trim() === user?.full_name} onClick={() => void save()}>
              {saving ? "Đang lưu…" : "Lưu thay đổi"}
           </Button>
        </div>
      </div>

      <h3 className="settings-group-title">Mẫu giọng nói nhận diện (Voice Profile)</h3>
      <div className="setting-card">
        <div className="setting-row">
          <div className="setting-icon"><Icon name="speaker" /></div>
          <div className="setting-content">
            <div style={{ display: "flex", alignItems: "center", gap: "8px" }}>
              <strong>Mẫu giọng cá nhân của tôi</strong>
              {voiceProfile.data && (
                <span
                  className="status"
                  style={{
                    borderRadius: "999px",
                    background: voiceProfile.data.sample_count >= 3 ? "var(--success-soft)" : "var(--accent-soft)",
                    color: voiceProfile.data.sample_count >= 3 ? "var(--success)" : "var(--accent-text)",
                  }}
                >
                  {voiceProfile.data.sample_count}/3 mẫu
                </span>
              )}
            </div>
            <small>
              {voiceProfile.data
                ? `Đã đăng ký ${voiceProfile.data.sample_count}/3 mẫu giọng (${(voiceProfile.data.sample_duration_ms / 1000).toFixed(1)}s âm thanh, qua ${voiceProfile.data.provider}). ${
                    voiceProfile.data.sample_count < 3
                      ? "Bạn có thể nạp thêm mẫu giọng (tối đa 3 mẫu) ở các môi trường khác nhau để AI nhận diện chuẩn xác nhất."
                      : "Hồ sơ đã đủ 3/3 mẫu tối đa. Hệ thống sẽ tự nhận diện bạn trong các cuộc họp tham gia."
                  }`
                : "Đăng ký từ 1 đến 3 mẫu giọng (10-25s/mẫu) để AI tự động nhận diện giọng nói và gán tên bạn trong mọi cuộc họp tham gia."}
            </small>
          </div>
          <div className="setting-action">
            {voiceProfile.data ? (
              <div style={{ display: "flex", gap: "8px" }}>
                {voiceProfile.data.sample_count < 3 && (
                  <Button onClick={() => setEnrollModal(true)}>
                    + Thêm mẫu ({voiceProfile.data.sample_count}/3)
                  </Button>
                )}
                <Button tone="danger" disabled={deletingVoice} onClick={() => void deleteVoice()}>
                  {deletingVoice ? "Đang xóa…" : "Xóa"}
                </Button>
              </div>
            ) : (
              <Button tone="dark" onClick={() => setEnrollModal(true)}>
                + Đăng ký mẫu giọng
              </Button>
            )}
          </div>
        </div>
      </div>

      <h3 className="settings-group-title">Bảo mật</h3>
      <div className="setting-card">
        <div className="setting-row">
          <div className="setting-icon"><Icon name="lock" /></div>
          <div className="setting-content">
            <strong>{user?.has_password === false ? "Tạo mật khẩu" : "Mật khẩu"}</strong>
            <small>{user?.has_password === false
              ? "Không bắt buộc. Tạo mật khẩu để có thể đăng nhập thêm bằng email."
              : "Thay đổi mật khẩu đăng nhập của bạn."}
            </small>
          </div>
          <div className="setting-action">
            <Button onClick={() => setPasswordModal(true)}>{user?.has_password === false ? "Tạo mật khẩu" : "Đổi mật khẩu"}</Button>
          </div>
        </div>
        <div className="setting-row">
          <div className="setting-icon" style={{ color: "var(--danger)" }}><Icon name="logout" /></div>
          <div className="setting-content">
            <strong style={{ color: "var(--danger)" }}>Đăng xuất</strong>
            <small>Đăng xuất khỏi thiết bị này.</small>
          </div>
          <div className="setting-action">
            <Button tone="danger" onClick={() => void logout()}>Đăng xuất</Button>
          </div>
        </div>
      </div>
    </div>

    {passwordModal && <PasswordModal close={() => setPasswordModal(false)} updatePassword={updatePassword} hasPassword={user?.has_password !== false} />}
    {enrollModal && (
      <VoiceEnrollmentModal
        isOpen={enrollModal}
        defaultName={user?.full_name ?? ""}
        currentSampleCount={voiceProfile.data?.sample_count}
        onClose={() => setEnrollModal(false)}
        onEnrolled={async (profile) => {
          await voiceProfile.reload();
          setEnrollModal(false);
          setMessage(`Đã cập nhật mẫu giọng nói cá nhân (${profile.sample_count}/3 mẫu, ${(profile.sample_duration_ms / 1000).toFixed(1)}s).`);
        }}
      />
    )}
  </>;
}
