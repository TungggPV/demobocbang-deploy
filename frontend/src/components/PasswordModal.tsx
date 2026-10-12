import { useState } from "react";
import { Button, Icon } from "./ui";

type PasswordModalProps = {
  close: () => void;
  updatePassword: (currentPassword: string | undefined, newPassword: string) => Promise<void>;
  hasPassword?: boolean;
};

export function PasswordModal({ close, updatePassword, hasPassword = true }: PasswordModalProps) {
  const [current, setCurrent] = useState("");
  const [newPass, setNewPass] = useState("");
  const [confirm, setConfirm] = useState("");
  const [showCurrent, setShowCurrent] = useState(false);
  const [showNew, setShowNew] = useState(false);
  const [showConfirm, setShowConfirm] = useState(false);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");
  const [success, setSuccess] = useState(false);

  async function save(event: React.FormEvent) {
    event.preventDefault();
    if (hasPassword && !current) return setError("Vui lòng nhập mật khẩu hiện tại.");
    if (newPass.length < 8) return setError("Mật khẩu mới phải có ít nhất 8 ký tự.");
    if (newPass !== confirm) return setError("Mật khẩu xác nhận không khớp.");
    setSaving(true);
    setError("");
    try {
      await updatePassword(hasPassword ? current : undefined, newPass);
      setSuccess(true);
      window.setTimeout(close, 1400);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "Đổi mật khẩu thất bại.");
    } finally {
      setSaving(false);
    }
  }

  return <div className="dialog-backdrop">
    <section className="dialog" style={{ width: 440 }} role="dialog" aria-modal="true" aria-labelledby="password-dialog-title">
      <button type="button" className="close" aria-label="Đóng" onClick={close}><Icon name="close" /></button>
      <h2 id="password-dialog-title">{hasPassword ? "Đổi mật khẩu" : "Tạo mật khẩu"}</h2>
      <p>Bảo mật tài khoản của bạn bằng một mật khẩu mạnh.</p>
      {success ? <div className="notice" style={{ marginTop: 24 }}>Đổi mật khẩu thành công!</div> : (
        <form className="compact-form" style={{ marginTop: 24 }} onSubmit={save}>
          {hasPassword && <label className="field full">Mật khẩu hiện tại
            <div className="input-group">
              <input type={showCurrent ? "text" : "password"} autoComplete="current-password" required value={current} onChange={(event) => setCurrent(event.target.value)} placeholder="Nhập mật khẩu hiện tại" />
              <button type="button" className="btn-icon" aria-label={showCurrent ? "Ẩn mật khẩu" : "Hiện mật khẩu"} onClick={() => setShowCurrent(!showCurrent)}><Icon name={showCurrent ? "eye-off" : "eye"} /></button>
            </div>
          </label>}
          <label className="field full">Mật khẩu mới
            <div className="input-group">
              <input type={showNew ? "text" : "password"} autoComplete="new-password" required value={newPass} onChange={(event) => setNewPass(event.target.value)} placeholder="Tối thiểu 8 ký tự" minLength={8} />
              <button type="button" className="btn-icon" aria-label={showNew ? "Ẩn mật khẩu" : "Hiện mật khẩu"} onClick={() => setShowNew(!showNew)}><Icon name={showNew ? "eye-off" : "eye"} /></button>
            </div>
          </label>
          <label className="field full">Nhập lại mật khẩu mới
            <div className="input-group">
              <input type={showConfirm ? "text" : "password"} autoComplete="new-password" required value={confirm} onChange={(event) => setConfirm(event.target.value)} placeholder="Nhập lại mật khẩu mới" minLength={8} />
              <button type="button" className="btn-icon" aria-label={showConfirm ? "Ẩn mật khẩu" : "Hiện mật khẩu"} onClick={() => setShowConfirm(!showConfirm)}><Icon name={showConfirm ? "eye-off" : "eye"} /></button>
            </div>
          </label>
          {error && <div className="form-error">{error}</div>}
          <div className="form-actions" style={{ marginTop: 24 }}>
            <Button type="button" className="btn-ghost" onClick={close}>Hủy</Button>
            <Button type="submit" tone="dark" disabled={saving}>{saving ? "Đang xử lý…" : "Cập nhật mật khẩu"}</Button>
          </div>
        </form>
      )}
    </section>
  </div>;
}
