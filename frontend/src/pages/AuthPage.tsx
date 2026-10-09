import { useEffect, useState } from "react";
import { ApiError } from "../api/client";
import { authApi } from "../api/endpoints";
import { config } from "../app/config";
import { useAuth } from "../auth/AuthContext";
import { Button, Icon } from "../components/ui";
import { ThemeToggle } from "../components/ThemeToggle";

type Mode = "login" | "register" | "verify" | "forgot" | "reset";

const copy: Record<Mode, { title: string; detail: string; action: string }> = {
  login:    { title: "Chào mừng trở lại", detail: "Đăng nhập để tiếp tục sử dụng Bóc Băng.", action: "Đăng nhập" },
  register: { title: "Tạo tài khoản",     detail: "Bắt đầu bóc băng cuộc họp chỉ trong vài giây.",  action: "Tạo tài khoản" },
  verify:   { title: "Xác minh email",    detail: "Chúng tôi đã gửi mã xác minh tới email của bạn.",    action: "Xác minh" },
  forgot:   { title: "Quên mật khẩu?",   detail: "Nhập email và chúng tôi sẽ gửi mã khôi phục.", action: "Gửi mã" },
  reset:    { title: "Đặt mật khẩu mới", detail: "Nhập mã từ email và mật khẩu mới.",  action: "Đổi mật khẩu" },
};

export function AuthPage({ initialMode = "login" }: { initialMode?: Exclude<Mode, "verify" | "reset"> }) {
  const [mode, setMode] = useState<Mode>(initialMode);
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [confirmPassword, setConfirmPassword] = useState("");
  const [name, setName] = useState("");
  const [agreed, setAgreed] = useState(false);
  const [code, setCode] = useState("");
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [pending, setPending] = useState(false);
  const [showPassword, setShowPassword] = useState(false);
  const [showConfirmPassword, setShowConfirmPassword] = useState(false);
  const auth = useAuth();
  const requestedNext = new URLSearchParams(window.location.search).get("next");
  const nextPage = requestedNext?.startsWith("/") && !requestedNext.startsWith("//") ? requestedNext : "/meetings";

  useEffect(() => {
    // Start waking the free backend as soon as the auth screen appears instead
    // of waiting for the user to press the Google button.
    void fetch(`${config.apiBaseUrl}/health`).catch(() => undefined);
  }, []);

  async function submit(event: React.FormEvent) {
    event.preventDefault();
    setPending(true);
    setError("");
    setNotice("");
    try {
      if (mode === "login") {
        await auth.login(email, password);
        window.location.assign(nextPage);
      } else if (mode === "register") {
        if (password !== confirmPassword) throw new Error("Mật khẩu xác nhận chưa khớp.");
        if (!agreed) throw new Error("Bạn cần đồng ý với điều khoản sử dụng.");
        await auth.register(email, password, name);
        setNotice("Đã gửi mã xác minh. Hãy kiểm tra hộp thư đến và thư rác.");
        setMode("verify");
      } else if (mode === "verify") {
        await auth.verifyOtp(email, code);
        window.location.assign(nextPage);
      } else if (mode === "forgot") {
        await authApi.forgotPassword(email);
        setNotice("Nếu email đã đăng ký, mã khôi phục đã được gửi. Hãy kiểm tra cả thư rác.");
        setMode("reset");
      } else {
        await authApi.resetPassword(email, code, password);
        setNotice("Đổi mật khẩu thành công. Bạn có thể đăng nhập ngay.");
        setMode("login");
      }
    } catch (cause) {
      const apiError = cause as ApiError;
      if (apiError.code === "EMAIL_NOT_VERIFIED") setMode("verify");
      setError(authErrorMessage(cause));
    } finally {
      setPending(false);
    }
  }

  async function resendVerificationCode() {
    setPending(true);
    setError("");
    setNotice("");
    try {
      await authApi.resendOtp(email);
      setNotice("Đã gửi lại mã xác minh. Hãy kiểm tra hộp thư đến và thư rác.");
    } catch (cause) {
      setError(authErrorMessage(cause));
    } finally {
      setPending(false);
    }
  }

  async function startGoogleLogin() {
    setPending(true);
    setError("");
    try {
      sessionStorage.setItem("bocbang.auth.next", nextPage);
      await auth.googleLogin();
    } catch (cause) {
      setError((cause as Error).message);
      setPending(false);
    }
  }

  const current = copy[mode];
  const googleVisible = mode === "login" || mode === "register";

  return <div className="auth">
    <header>
      <a className="brand" href="/">
        <span className="brand-mark"><Icon name="brand" /></span>
        <strong>Bóc Băng</strong>
      </a>
      <ThemeToggle />
    </header>

    <section className={`auth-card ${mode === "register" ? "auth-card-wide" : ""}`}>
      {/* In-card brand badge */}
      <div className="auth-brand-badge">
        <span className="brand-mark"><Icon name="brand" /></span>
        <span>Bóc Băng</span>
      </div>

      <h1>{current.title}</h1>
      <p>{current.detail}</p>

      {googleVisible && (
        <button className="google" type="button" disabled={pending} onClick={() => void startGoogleLogin()}>
          {pending ? "Đang kết nối…" : "Tiếp tục với Google"}
        </button>
      )}
      {googleVisible && <div className="auth-divider"><span>hoặc</span></div>}

      <form className={mode === "register" ? "register-form" : ""} onSubmit={submit}>
        {mode === "register" && (
          <label>Họ và tên
            <input autoComplete="name" required placeholder="Nguyễn Văn A" value={name} onChange={(event) => setName(event.target.value)} />
          </label>
        )}
        <label>Email
          <input type="email" autoComplete="email" required placeholder="email@congty.com" value={email} onChange={(event) => setEmail(event.target.value)} />
        </label>
        {(mode === "verify" || mode === "reset") && (
          <label>Mã xác minh
            <input inputMode="numeric" autoComplete="one-time-code" required placeholder="123456" value={code} onChange={(event) => setCode(event.target.value)} />
          </label>
        )}
        {mode !== "verify" && mode !== "forgot" && (
          <label>{mode === "reset" ? "Mật khẩu mới" : "Mật khẩu"}
            <div className="input-group">
              <input
                type={showPassword ? "text" : "password"}
                autoComplete={mode === "login" ? "current-password" : "new-password"}
                minLength={8}
                required
                placeholder="Tối thiểu 8 ký tự"
                value={password}
                onChange={(event) => setPassword(event.target.value)}
              />
              <button type="button" className="btn-icon" onClick={() => setShowPassword(!showPassword)} tabIndex={-1}>
                <Icon name={showPassword ? "eye-off" : "eye"} />
              </button>
            </div>
          </label>
        )}
        {mode === "register" && (
          <label>Xác nhận mật khẩu
            <div className="input-group">
              <input 
                type={showConfirmPassword ? "text" : "password"} 
                autoComplete="new-password" 
                minLength={8} 
                required 
                placeholder="Nhập lại mật khẩu" 
                value={confirmPassword} 
                onChange={(event) => setConfirmPassword(event.target.value)} 
              />
              <button type="button" className="btn-icon" onClick={() => setShowConfirmPassword(!showConfirmPassword)} tabIndex={-1}>
                <Icon name={showConfirmPassword ? "eye-off" : "eye"} />
              </button>
            </div>
          </label>
        )}
        {mode === "register" && (
          <label className="auth-consent">
            <input type="checkbox" checked={agreed} onChange={(event) => setAgreed(event.target.checked)} />
            Tôi đồng ý với điều khoản sử dụng dịch vụ.
          </label>
        )}
        {error && <div className="form-error">{error}</div>}
        {notice && <div className="notice">{notice}</div>}
        <Button type="submit" tone="dark" disabled={pending}>
          {pending ? "Đang xử lý…" : current.action}
        </Button>
      </form>

      {mode === "login" && (
        <p className="switch">
          <a href="/forgot-password">Quên mật khẩu?</a>
          <span>·</span>
          <a href="/register">Tạo tài khoản mới</a>
        </p>
      )}
      {mode === "register" && (
        <p className="switch"><a href="/login">Đã có tài khoản? Đăng nhập</a></p>
      )}
      {mode === "verify" && (
        <p className="switch"><button type="button" disabled={pending} onClick={() => void resendVerificationCode()}>{pending ? "Đang gửi…" : "Gửi lại mã"}</button></p>
      )}
      {(mode === "forgot" || mode === "reset") && (
        <p className="switch"><a href="/login">← Quay lại đăng nhập</a></p>
      )}
    </section>
  </div>;
}

function authErrorMessage(cause: unknown): string {
  if (cause instanceof ApiError) {
    if (cause.code === "EMAIL_DELIVERY_UNAVAILABLE") {
      return "Hệ thống gửi email chưa được cấu hình. Vui lòng thử lại sau.";
    }
    if (cause.code === "EMAIL_DELIVERY_FAILED") {
      return "Không thể gửi email lúc này. Vui lòng kiểm tra địa chỉ và thử lại.";
    }
  }
  return cause instanceof Error ? cause.message : "Không thể hoàn tất yêu cầu.";
}
