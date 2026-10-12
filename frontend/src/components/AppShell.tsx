import { useState, useRef, useEffect } from "react";
import type { ReactNode } from "react";
import { useAuth } from "../auth/AuthContext";
import { PasswordModal } from "./PasswordModal";
import { ThemeToggle } from "./ThemeToggle";
import { Icon, type IconName, Button } from "./ui";

const primaryNav: Array<[string, IconName, string]> = [
  ["/meetings", "meeting", "Cuộc họp"],
];

export function AppShell({ children }: { children: ReactNode }) {
  const { user, logout, updateProfile, updatePassword } = useAuth();
  const path = window.location.pathname;
  const meetingMatch = path.match(/^\/meetings\/([^/]+)/)?.[1];
  const meetingId = meetingMatch === "create" ? undefined : meetingMatch;
  const [menuOpen, setMenuOpen] = useState(false);
  const menuRef = useRef<HTMLDivElement>(null);

  const [profileModal, setProfileModal] = useState(false);
  const [passwordModal, setPasswordModal] = useState(false);

  useEffect(() => {
    function handleClick(e: MouseEvent) {
      if (menuRef.current && !menuRef.current.contains(e.target as Node)) {
        setMenuOpen(false);
      }
    }
    document.addEventListener("mousedown", handleClick);
    return () => document.removeEventListener("mousedown", handleClick);
  }, []);

  const initials = user?.full_name
    .split(/\s+/)
    .map((part) => part[0])
    .join("")
    .slice(0, 2)
    .toUpperCase() || "BB";

  const avatarEl = user?.avatar_url
    ? <img src={user.avatar_url} alt="" className="person-avatar-img" referrerPolicy="no-referrer" />
    : <span className="person-avatar">{initials}</span>;

  const meetingNav: Array<[string, IconName, string]> = meetingId ? [
    [`/meetings/${meetingId}`, "meeting", "Bản ghi"],
    [`/meetings/${meetingId}/summary`, "summary", "Tóm tắt"],
    [`/meetings/${meetingId}/speakers`, "speaker", "Người nói"],
    [`/meetings/${meetingId}/translation`, "translate", "Bản dịch"],
    [`/meetings/${meetingId}/export-share`, "export", "Xuất"],
  ] : [];

  const meetingsSelected = path.startsWith("/meetings") && path !== "/meetings/create";

  return <div className="shell">
    <aside className="sidebar desktop-sidebar">
      <a className="brand" href="/meetings" aria-label="Bóc Băng">
        <span className="brand-mark"><Icon name="brand" /></span>
        <strong>Bóc Băng</strong>
      </a>

      <a className="btn btn-dark side-create" href="/meetings/create">
        <Icon name="plus" /><span className="side-create-label">Cuộc họp mới</span>
      </a>

      <nav className="primary-nav" aria-label="Điều hướng chính">
        {primaryNav.map(([href, icon, label]) => (
          <a
            className={path === href || (href === "/meetings" && path === "/history") ? "selected" : ""}
            href={href}
            key={href}
            aria-label={label}
          >
            <Icon name={icon} />{label}
          </a>
        ))}
      </nav>

      {meetingNav.length > 0 && (
        <nav className="context-nav" aria-label="Công cụ cuộc họp">
          <span className="side-label">Đang mở</span>
          {meetingNav.map(([href, icon, label]) => (
            <a className={path === href ? "selected" : ""} href={href} key={href}>
              <Icon name={icon} />{label}
            </a>
          ))}
        </nav>
      )}

      <div className="account" ref={menuRef}>
        {menuOpen && (
          <div className="account-popup">
            <div className="account-popup-header">
              {avatarEl}
              <div>
                <strong>{user?.full_name}</strong>
              </div>
            </div>
            <div className="account-popup-divider"></div>
            <button className="account-popup-item" type="button" onClick={() => { setProfileModal(true); setMenuOpen(false); }}>
              <Icon name="edit-profile" />
              <span>Thay đổi thông tin</span>
            </button>
            <button className="account-popup-item" type="button" onClick={() => { setPasswordModal(true); setMenuOpen(false); }}>
              <Icon name="lock" />
              <span>{user?.has_password === false ? "Tạo mật khẩu" : "Đổi mật khẩu"}</span>
            </button>
            <div className="account-popup-divider"></div>
            <button className="account-popup-item text-danger" type="button" onClick={() => void logout()}>
              <Icon name="logout" />
              <span>Đăng xuất</span>
            </button>
          </div>
        )}
        <button 
          className={`account-profile ${menuOpen ? 'active' : ''}`} 
          type="button" 
          onClick={() => setMenuOpen(!menuOpen)}
        >
          {avatarEl}
          <span className="account-profile-info">
            <strong>{user?.full_name}</strong>
          </span>
          <span className="account-profile-chevron">
            <Icon name="chevron-up" />
          </span>
        </button>
      </div>
    </aside>

    <main>
      {meetingNav.length > 0 && (
        <nav className="mobile-meeting-nav" aria-label="Công cụ bản ghi">
          {meetingNav.map(([href, icon, label]) => (
            <a
              className={path === href ? "selected" : ""}
              href={href}
              key={href}
              aria-current={path === href ? "page" : undefined}
            >
              <Icon name={icon} />
              <span>{label}</span>
            </a>
          ))}
        </nav>
      )}
      <div className="content">{children}</div>
    </main>

    <nav className="mobile-bottom-nav" aria-label="Điều hướng chính">
      <a className={meetingsSelected ? "selected" : ""} href="/meetings" aria-current={meetingsSelected ? "page" : undefined}>
        <span className="mobile-nav-icon"><Icon name="meeting" /></span>
        <span>Cuộc họp</span>
      </a>
      <a className={`mobile-new-action ${path === "/meetings/create" ? "selected" : ""}`} href="/meetings/create" aria-current={path === "/meetings/create" ? "page" : undefined}>
        <span className="mobile-nav-icon"><Icon name="plus" /></span>
        <span>Tạo mới</span>
      </a>
      <a className={path === "/settings" ? "selected" : ""} href="/settings" aria-current={path === "/settings" ? "page" : undefined}>
        <span className="mobile-nav-icon"><Icon name="profile" /></span>
        <span>Tài khoản</span>
      </a>
    </nav>

    <ThemeToggle className="shell-theme-toggle" />

    {profileModal && <ProfileModal close={() => setProfileModal(false)} user={user} updateProfile={updateProfile} />}
    {passwordModal && <PasswordModal close={() => setPasswordModal(false)} updatePassword={updatePassword} hasPassword={user?.has_password !== false} />}
  </div>;
}
function ProfileModal({ close, user, updateProfile }: { close: () => void, user: any, updateProfile: (name: string) => Promise<void> }) {
  const [fullName, setFullName] = useState(user?.full_name ?? "");
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");

  async function save() {
    if (!fullName.trim()) return setError("Họ và tên không được để trống.");
    setSaving(true);
    setError("");
    try {
      await updateProfile(fullName.trim());
      close();
    } catch (cause) {
      setError((cause as Error).message);
    } finally {
      setSaving(false);
    }
  }

  return (
    <div className="dialog-backdrop">
      <div className="dialog" style={{ width: 400 }}>
        <button className="close" aria-label="Đóng" onClick={close}><Icon name="close" /></button>
        <h2>Thay đổi thông tin</h2>
        <p>Cập nhật họ và tên hiển thị của bạn.</p>
        <div className="compact-form" style={{ marginTop: 24 }}>
          <label className="field full">
            Họ và tên
            <input value={fullName} onChange={(e) => setFullName(e.target.value)} placeholder="Nhập họ và tên" />
          </label>
          <label className="field full">
            Email
            <input value={user?.email ?? ""} readOnly disabled />
          </label>
          {error && <div className="form-error">{error}</div>}
          <div className="form-actions" style={{ marginTop: 24 }}>
            <Button className="btn-ghost" onClick={close}>Hủy</Button>
            <Button tone="dark" disabled={saving || fullName.trim() === user?.full_name} onClick={() => void save()}>
              {saving ? "Đang lưu…" : "Lưu thay đổi"}
            </Button>
          </div>
        </div>
      </div>
    </div>
  );
}
