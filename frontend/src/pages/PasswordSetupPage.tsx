import { useState } from "react";
import { useAuth } from "../auth/AuthContext";
import { Button, Icon } from "../components/ui";
import { ThemeToggle } from "../components/ThemeToggle";

export function PasswordSetupPage() {
  const auth = useAuth();
  const [password, setPassword] = useState("");
  const [confirm, setConfirm] = useState("");
  const [showPassword, setShowPassword] = useState(false);
  const [showConfirm, setShowConfirm] = useState(false);
  const [pending, setPending] = useState(false);
  const [error, setError] = useState("");
  const requestedNext = new URLSearchParams(window.location.search).get("next");
  const nextPage = requestedNext?.startsWith("/") && !requestedNext.startsWith("//")
    ? requestedNext
    : "/meetings";

  async function submit(event: React.FormEvent) {
    event.preventDefault();
    setError("");
    if (password.length < 8) return setError("Mật khẩu phải có ít nhất 8 ký tự.");
    if (password !== confirm) return setError("Mật khẩu xác nhận chưa khớp.");
    setPending(true);
    try {
      await auth.updatePassword(undefined, password);
      window.location.assign(nextPage);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "Không thể tạo mật khẩu.");
      setPending(false);
    }
  }

  return <div className="auth">
    <header>
      <span className="brand">
        <span className="brand-mark"><Icon name="brand" /></span>
        <strong>Bóc Băng</strong>
      </span>
      <ThemeToggle />
    </header>
    <section className="auth-card">
      <div className="auth-brand-badge">
        <span className="brand-mark"><Icon name="lock" /></span>
        <span>Bảo mật tài khoản</span>
      </div>
      <h1>Tạo mật khẩu đăng nhập</h1>
      <p>Sau bước này, bạn có thể đăng nhập bằng Google hoặc bằng email <strong>{auth.user?.email}</strong> và mật khẩu.</p>
      <form onSubmit={submit}>
        <label>Mật khẩu mới
          <div className="input-group">
            <input type={showPassword ? "text" : "password"} autoComplete="new-password" minLength={8} required placeholder="Tối thiểu 8 ký tự" value={password} onChange={(event) => setPassword(event.target.value)} />
            <button type="button" className="btn-icon" aria-label={showPassword ? "Ẩn mật khẩu" : "Hiện mật khẩu"} onClick={() => setShowPassword(!showPassword)}><Icon name={showPassword ? "eye-off" : "eye"} /></button>
          </div>
        </label>
        <label>Nhập lại mật khẩu
          <div className="input-group">
            <input type={showConfirm ? "text" : "password"} autoComplete="new-password" minLength={8} required placeholder="Nhập lại mật khẩu" value={confirm} onChange={(event) => setConfirm(event.target.value)} />
            <button type="button" className="btn-icon" aria-label={showConfirm ? "Ẩn mật khẩu" : "Hiện mật khẩu"} onClick={() => setShowConfirm(!showConfirm)}><Icon name={showConfirm ? "eye-off" : "eye"} /></button>
          </div>
        </label>
        {error && <div className="form-error">{error}</div>}
        <Button type="submit" tone="dark" disabled={pending}>{pending ? "Đang lưu…" : "Tạo mật khẩu và tiếp tục"}</Button>
      </form>
      <p className="switch"><button type="button" onClick={() => void auth.logout()}>Đăng xuất</button></p>
    </section>
  </div>;
}
