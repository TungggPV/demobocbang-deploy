import { useEffect, useState } from "react";
import { useAuth } from "../auth/AuthContext";
import { PasswordModal } from "../components/PasswordModal";
import { Button, Icon, PageHeading } from "../components/ui";

export function SettingsPage() {
  const { user, logout, updateProfile, updatePassword } = useAuth();
  const [fullName, setFullName] = useState(user?.full_name ?? "");
  const [message, setMessage] = useState("");
  const [saving, setSaving] = useState(false);
  const [passwordModal, setPasswordModal] = useState(false);

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

  return <>
    <PageHeading 
      eyebrow="TÀI KHOẢN" 
      title="Cài đặt" 
      description="Quản lý thông tin cá nhân và bảo mật tài khoản." 
    />
    
    <div className="settings-container">
      {message && <div className={message.includes("thành công") ? "notice" : "form-error"}>{message}</div>}

      <h3 className="settings-group-title">Thông tin cá nhân</h3>
      <div className="setting-card">
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

      <h3 className="settings-group-title">Bảo mật</h3>
      <div className="setting-card">
        <div className="setting-row">
          <div className="setting-icon"><Icon name="lock" /></div>
          <div className="setting-content">
            <strong>Mật khẩu</strong>
            <small>Thay đổi mật khẩu đăng nhập của bạn.</small>
          </div>
          <div className="setting-action">
            <Button onClick={() => setPasswordModal(true)}>Đổi mật khẩu</Button>
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
  </>;
}
