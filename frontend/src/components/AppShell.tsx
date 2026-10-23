import { useState, useRef, useEffect, useCallback, useMemo } from "react";
import type { ReactNode } from "react";
import { useAuth } from "../auth/AuthContext";
import { PasswordModal } from "./PasswordModal";
import { ThemeToggle } from "./ThemeToggle";
import { Icon, Button } from "./ui";
import { SearchModal } from "./SearchModal";
import { meetingsApi } from "../api/endpoints";
import type { Meeting } from "../types/domain";
import { navigate } from "../app/router";

let cachedMeetingsList: Meeting[] | null = null;

function groupMeetingsByDate(meetings: Meeting[]) {
  const now = new Date();
  const startOfToday = new Date(now.getFullYear(), now.getMonth(), now.getDate()).getTime();
  const oneDayMs = 24 * 60 * 60 * 1000;
  const startOfYesterday = startOfToday - oneDayMs;
  const startOf7Days = startOfToday - 6 * oneDayMs;
  const startOf30Days = startOfToday - 29 * oneDayMs;

  const groups: { title: string; items: Meeting[] }[] = [
    { title: "Hôm nay", items: [] },
    { title: "Hôm qua", items: [] },
    { title: "7 ngày trước", items: [] },
    { title: "30 ngày trước", items: [] },
    { title: "Cũ hơn", items: [] },
  ];

  for (const m of meetings) {
    const time = new Date(m.created_at).getTime();
    if (time >= startOfToday) {
      groups[0].items.push(m);
    } else if (time >= startOfYesterday) {
      groups[1].items.push(m);
    } else if (time >= startOf7Days) {
      groups[2].items.push(m);
    } else if (time >= startOf30Days) {
      groups[3].items.push(m);
    } else {
      groups[4].items.push(m);
    }
  }

  return groups.filter((g) => g.items.length > 0);
}

export function AppShell({ children, currentPath }: { children: ReactNode; currentPath?: string }) {
  const { user, logout, updateProfile, updatePassword } = useAuth();
  const path = currentPath ?? window.location.pathname;
  const meetingMatch = path.match(/^\/meetings\/([^/]+)/)?.[1];
  const meetingId = meetingMatch === "create" ? undefined : meetingMatch;

  const [collapsed, setCollapsed] = useState(() => {
    try {
      const saved = localStorage.getItem("bocbang_sidebar_collapsed");
      if (saved !== null) return saved === "true";
      return true;
    } catch {
      return true;
    }
  });

  const toggleSidebar = useCallback(() => {
    setCollapsed((prev) => {
      const next = !prev;
      try {
        localStorage.setItem("bocbang_sidebar_collapsed", String(next));
      } catch {
        // ignore
      }
      return next;
    });
  }, []);

  const [menuOpen, setMenuOpen] = useState(false);
  const menuRef = useRef<HTMLDivElement>(null);
  const searchInputRef = useRef<HTMLInputElement>(null);

  const [profileModal, setProfileModal] = useState(false);
  const [passwordModal, setPasswordModal] = useState(false);

  // Chat-like Meeting history in sidebar
  const [meetings, setMeetings] = useState<Meeting[]>(() => cachedMeetingsList ?? []);
  const [loadingMeetings, setLoadingMeetings] = useState(() => !cachedMeetingsList);
  const [searchQuery, setSearchQuery] = useState("");
  const [activeMenuMeetingId, setActiveMenuMeetingId] = useState<string | null>(null);
  const [searchOpen, setSearchOpen] = useState(false);
  const [searchModalOpen, setSearchModalOpen] = useState(false);

  useEffect(() => {
    function handleKeyDown(e: KeyboardEvent) {
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "k") {
        e.preventDefault();
        setSearchModalOpen((prev) => !prev);
      }
      if ((e.metaKey || e.ctrlKey) && e.shiftKey && e.key.toLowerCase() === "s") {
        e.preventDefault();
        toggleSidebar();
      }
    }
    window.addEventListener("keydown", handleKeyDown);
    return () => window.removeEventListener("keydown", handleKeyDown);
  }, [toggleSidebar]);

  const [renameTarget, setRenameTarget] = useState<Meeting | null>(null);
  const [deleteTarget, setDeleteTarget] = useState<Meeting | null>(null);

  const handleRailClick = useCallback(
    (e: React.MouseEvent) => {
      const target = e.target as HTMLElement;
      if (target.closest(".rail-action-btn, .rail-avatar-btn, .rail-search-btn, .account-popup")) {
        return;
      }
      toggleSidebar();
    },
    [toggleSidebar]
  );

  useEffect(() => {
    const handler = () => toggleSidebar();
    window.addEventListener("bocbang:toggle-sidebar", handler);
    return () => window.removeEventListener("bocbang:toggle-sidebar", handler);
  }, [toggleSidebar]);

  const loadMeetings = useCallback(async () => {
    try {
      if (!cachedMeetingsList) {
        setLoadingMeetings(true);
      }
      const data = await meetingsApi.list({ limit: 100 });
      cachedMeetingsList = data;
      setMeetings(data);
    } catch {
      // ignore
    } finally {
      setLoadingMeetings(false);
    }
  }, []);

  useEffect(() => {
    void loadMeetings();
    const refreshHandler = (e?: Event) => {
      const detail = (e as CustomEvent)?.detail as Meeting | undefined;
      if (detail && detail.id) {
        setMeetings((cur) => cur.map((m) => (m.id === detail.id ? { ...m, ...detail } : m)));
        if (cachedMeetingsList) {
          cachedMeetingsList = cachedMeetingsList.map((m) => (m.id === detail.id ? { ...m, ...detail } : m));
        }
      } else {
        void loadMeetings();
      }
    };
    window.addEventListener("bocbang:refresh-meetings", refreshHandler);
    return () => window.removeEventListener("bocbang:refresh-meetings", refreshHandler);
  }, [loadMeetings]);

  useEffect(() => {
    function handleClick(e: MouseEvent) {
      if (menuRef.current && !menuRef.current.contains(e.target as Node)) {
        setMenuOpen(false);
      }
      if (!(e.target as HTMLElement).closest(".chatgpt-item-menu-wrap")) {
        setActiveMenuMeetingId(null);
      }
    }
    document.addEventListener("mousedown", handleClick);
    return () => document.removeEventListener("mousedown", handleClick);
  }, []);

  const filteredMeetings = useMemo(() => {
    if (!searchQuery.trim()) return meetings;
    const q = searchQuery.toLowerCase();
    return meetings.filter((m) => m.title.toLowerCase().includes(q));
  }, [meetings, searchQuery]);

  const groupedMeetings = useMemo(() => groupMeetingsByDate(filteredMeetings), [filteredMeetings]);

  const initials = (user?.full_name || user?.email || "BB")
    .split(/\s+/)
    .map((part) => part[0])
    .join("")
    .slice(0, 2)
    .toUpperCase() || "BB";

  const avatarEl = user?.avatar_url?.trim() ? (
    <img src={user.avatar_url} alt="" className="person-avatar-img" referrerPolicy="no-referrer" />
  ) : (
    <span className="person-avatar">{initials}</span>
  );

  async function handleRenameConfirm(newTitle: string) {
    if (!renameTarget) return;
    try {
      await meetingsApi.update(renameTarget.id, { title: newTitle });
      const nextMeetings = meetings.map((m) => (m.id === renameTarget.id ? { ...m, title: newTitle } : m));
      cachedMeetingsList = nextMeetings;
      setMeetings(nextMeetings);
      setRenameTarget(null);
      window.dispatchEvent(new CustomEvent("bocbang:refresh-meetings"));
    } catch (e) {
      alert((e as Error).message);
    }
  }

  async function handleDeleteConfirm() {
    if (!deleteTarget) return;
    try {
      await meetingsApi.remove(deleteTarget.id);
      const nextMeetings = meetings.filter((m) => m.id !== deleteTarget.id);
      cachedMeetingsList = nextMeetings;
      setMeetings(nextMeetings);
      if (meetingId === deleteTarget.id) {
        navigate("/meetings");
      }
      setDeleteTarget(null);
      window.dispatchEvent(new CustomEvent("bocbang:refresh-meetings"));
    } catch (e) {
      alert((e as Error).message);
    }
  }

  return (
    <div className={`shell ${collapsed ? "sidebar-collapsed" : ""}`}>
      {/* Sidebar (ChatGPT / Gemini style) */}
      <aside className={`sidebar chatgpt-sidebar ${collapsed ? "is-collapsed" : ""}`}>
        {collapsed ? (
          /* Collapsed Rail (54px width like screenshot) */
          <div className="sidebar-rail" onClick={handleRailClick} title="Nhấn để mở sidebar">
            <div className="rail-top">
              <button
                type="button"
                className="rail-toggle-brand-btn"
                onClick={(e) => {
                  e.stopPropagation();
                  toggleSidebar();
                }}
                title="Bật tắt sidebar (Ctrl+Shift+S)"
                aria-label="Bật tắt sidebar"
              >
                <span className="rail-icon-default">
                  <Icon name="brand" />
                </span>
                <span className="rail-icon-hover">
                  <Icon name="sidebar" />
                </span>
                <span className="rail-chatgpt-tooltip">
                  <span>Bật tắt sidebar</span>
                  <kbd>Ctrl+Shift+S</kbd>
                </span>
              </button>

              <a
                className="rail-action-btn rail-new-btn"
                href="/meetings/create"
                title="Tạo phiên họp mới"
                aria-label="Tạo phiên họp mới"
                onClick={(e) => {
                  e.stopPropagation();
                  if (path === "/meetings/create" || path === "/meetings") {
                    e.preventDefault();
                    const input = document.querySelector(".start-screen-input-pill input") as HTMLInputElement | null;
                    if (input) {
                      input.focus();
                      input.select();
                    } else {
                      navigate("/meetings/create");
                    }
                  }
                }}
              >
                <Icon name="plus" />
                <span className="rail-chatgpt-tooltip">
                  <span>Tạo phiên họp mới</span>
                </span>
              </a>

              <a
                className="rail-action-btn rail-upload-btn"
                href="/meetings/create?mode=upload"
                title="Tải tệp âm thanh"
                aria-label="Tải tệp âm thanh"
                onClick={(e) => e.stopPropagation()}
              >
                <Icon name="upload" />
                <span className="rail-chatgpt-tooltip">
                  <span>Tải lên bản ghi</span>
                </span>
              </a>

              <button
                type="button"
                className="rail-icon-btn rail-search-btn"
                title="Tìm kiếm cuộc họp"
                onClick={(e) => {
                  e.stopPropagation();
                  setSearchModalOpen(true);
                }}
                aria-label="Tìm kiếm"
              >
                <Icon name="search" />
                <span className="rail-chatgpt-tooltip">
                  <span>Tìm kiếm</span>
                  <kbd>Ctrl+K</kbd>
                </span>
              </button>
            </div>

            <div className="rail-bottom" ref={menuRef}>
              <button
                className="rail-avatar-btn"
                type="button"
                onClick={() => setMenuOpen(!menuOpen)}
                title={user?.full_name || "Tài khoản"}
              >
                {avatarEl}
              </button>
              {menuOpen && (
                <div className="account-popup rail-account-popup">
                  <div className="account-popup-header">
                    {avatarEl}
                    <div>
                      <strong>{user?.full_name}</strong>
                      <small>{user?.email}</small>
                    </div>
                  </div>
                  <div className="account-popup-divider"></div>
                  <button
                    className="account-popup-item"
                    type="button"
                    onClick={() => {
                      setProfileModal(true);
                      setMenuOpen(false);
                    }}
                  >
                    <Icon name="edit-profile" />
                    <span>Thay đổi thông tin</span>
                  </button>
                  <button
                    className="account-popup-item"
                    type="button"
                    onClick={() => {
                      setPasswordModal(true);
                      setMenuOpen(false);
                    }}
                  >
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
            </div>
          </div>
        ) : (
          /* Expanded Sidebar (ChatGPT style) */
          <div className="sidebar-expanded">
            <div className="sidebar-header">
              <a className="brand-chatgpt" href="/meetings" aria-label="Hệ thống Bóc Băng">
                <span className="brand-mark-chatgpt">
                  <Icon name="brand" />
                </span>
                <div className="sidebar-brand-title">
                  <strong>Hệ thống Bóc Băng</strong>
                  <small>Audio Transcription</small>
                </div>
              </a>
              <button
                type="button"
                className="sidebar-tool-btn"
                onClick={toggleSidebar}
                title="Đóng thanh bên"
                aria-label="Đóng thanh bên"
              >
                <Icon name="sidebar" />
              </button>
            </div>

            <div style={{ display: "flex", flexDirection: "column", gap: 8, padding: "10px 12px 14px" }}>
              <a
                className="sidebar-btn-blue-primary"
                href="/meetings/create"
                onClick={(e) => {
                  if (path === "/meetings/create" || path === "/meetings") {
                    e.preventDefault();
                    const input = document.querySelector(".start-screen-input-pill input") as HTMLInputElement | null;
                    if (input) {
                      input.focus();
                      input.select();
                    } else {
                      navigate("/meetings/create");
                    }
                  }
                }}
              >
                <Icon name="plus" />
                <span>Tạo phiên họp mới</span>
              </a>
              <a className="sidebar-btn-blue-secondary" href="/meetings/create?mode=upload">
                <Icon name="upload" />
                <span>Tải lên bản ghi sẵn có</span>
              </a>
              <button
                type="button"
                className="sidebar-btn-search-trigger"
                onClick={() => setSearchModalOpen(true)}
              >
                <Icon name="search" />
                <span>Tìm kiếm ghi âm</span>
              </button>
            </div>



            {/* Grouped Meeting History */}
            <div className="sidebar-history-container">
              {loadingMeetings && meetings.length === 0 ? (
                <div className="sidebar-history-loading">Đang tải lịch sử…</div>
              ) : groupedMeetings.length === 0 ? (
                <div className="sidebar-history-empty">
                  {searchQuery ? "Không tìm thấy cuộc họp phù hợp." : "Chưa có cuộc họp nào."}
                </div>
              ) : (
                groupedMeetings.map((group) => (
                  <div className="sidebar-group" key={group.title}>
                    <div className="sidebar-group-title">{group.title}</div>
                    <div className="sidebar-group-items">
                      {group.items.map((m) => {
                        const isCurrent = meetingId === m.id;
                        const isMenuOpen = activeMenuMeetingId === m.id;
                        return (
                          <div
                            key={m.id}
                            className={`chatgpt-item ${isCurrent ? "is-active" : ""}`}
                          >
                            <a
                              href={`/meetings/${m.id}`}
                              className="chatgpt-item-link"
                              title={m.title}
                              onClick={(e) => {
                                e.preventDefault();
                                navigate(`/meetings/${m.id}`);
                              }}
                            >
                              <span className="chatgpt-item-title">{m.title || "Cuộc họp không tên"}</span>
                            </a>

                            <div className="chatgpt-item-menu-wrap">
                              <button
                                type="button"
                                className={`chatgpt-item-more-btn ${isMenuOpen ? "active" : ""}`}
                                onClick={(e) => {
                                  e.stopPropagation();
                                  setActiveMenuMeetingId(isMenuOpen ? null : m.id);
                                }}
                                title="Tùy chọn"
                                aria-label="Tùy chọn"
                              >
                                <Icon name="more" />
                              </button>

                              {isMenuOpen && (
                                <div className="chatgpt-item-dropdown">
                                  <button
                                    type="button"
                                    onClick={(e) => {
                                      e.stopPropagation();
                                      setActiveMenuMeetingId(null);
                                      setRenameTarget(m);
                                    }}
                                  >
                                    <Icon name="edit" />
                                    <span>Đổi tên</span>
                                  </button>
                                  <button
                                    type="button"
                                    onClick={(e) => {
                                      e.stopPropagation();
                                      setActiveMenuMeetingId(null);
                                      navigate(`/meetings/${m.id}?modal=share`);
                                    }}
                                  >
                                    <Icon name="export" />
                                    <span>Chia sẻ</span>
                                  </button>
                                  <button
                                    type="button"
                                    className="text-danger"
                                    onClick={(e) => {
                                      e.stopPropagation();
                                      setActiveMenuMeetingId(null);
                                      setDeleteTarget(m);
                                    }}
                                  >
                                    <Icon name="trash" />
                                    <span>Xóa</span>
                                  </button>
                                </div>
                              )}
                            </div>
                          </div>
                        );
                      })}
                    </div>
                  </div>
                ))
              )}
            </div>

            {/* Account Box at Bottom */}
            <div className="account" ref={menuRef}>
              {menuOpen && (
                <div className="account-popup">
                  <div className="account-popup-header">
                    {avatarEl}
                    <div>
                      <strong>{user?.full_name}</strong>
                      <small>{user?.email}</small>
                    </div>
                  </div>
                  <div className="account-popup-divider"></div>
                  <button
                    className="account-popup-item"
                    type="button"
                    onClick={() => {
                      setProfileModal(true);
                      setMenuOpen(false);
                    }}
                  >
                    <Icon name="edit-profile" />
                    <span>Thay đổi thông tin</span>
                  </button>
                  <button
                    className="account-popup-item"
                    type="button"
                    onClick={() => {
                      setPasswordModal(true);
                      setMenuOpen(false);
                    }}
                  >
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
                className={`account-profile ${menuOpen ? "active" : ""}`}
                type="button"
                onClick={() => setMenuOpen(!menuOpen)}
              >
                {avatarEl}
                <span className="account-profile-info">
                  <strong>{user?.full_name}</strong>
                  <small>{user?.email}</small>
                </span>
                <span className="account-profile-chevron">
                  <Icon name="chevron-up" />
                </span>
              </button>
            </div>
          </div>
        )}
      </aside>

      {/* Main Content Area */}
      <main>
        <div className="content">{children}</div>
      </main>

      {/* Mobile Bottom Navigation */}
      <nav className="mobile-bottom-nav" aria-label="Điều hướng chính">
        <a
          className={path.startsWith("/meetings") && path !== "/meetings/create" ? "selected" : ""}
          href="/meetings"
        >
          <span className="mobile-nav-icon">
            <Icon name="meeting" />
          </span>
          <span>Cuộc họp</span>
        </a>
        <a
          className={`mobile-new-action ${path === "/meetings/create" ? "selected" : ""}`}
          href="/meetings/create"
        >
          <span className="mobile-nav-icon">
            <Icon name="plus" />
          </span>
          <span>Tạo mới</span>
        </a>
        <a className={path === "/settings" ? "selected" : ""} href="/settings">
          <span className="mobile-nav-icon">
            {user?.avatar_url?.trim() ? (
              <img
                src={user.avatar_url}
                alt=""
                style={{ width: 20, height: 20, borderRadius: "50%", objectFit: "cover" }}
                referrerPolicy="no-referrer"
              />
            ) : (
              <Icon name="profile" />
            )}
          </span>
          <span>Tài khoản</span>
        </a>
      </nav>

      <ThemeToggle className="shell-theme-toggle" />

      {/* Quick Rename Modal */}
      {renameTarget && (
        <RenameDialog
          initialTitle={renameTarget.title}
          close={() => setRenameTarget(null)}
          onConfirm={handleRenameConfirm}
        />
      )}

      {/* Quick Delete Dialog */}
      {deleteTarget && (
        <DeleteConfirmDialog
          title={deleteTarget.title}
          close={() => setDeleteTarget(null)}
          onConfirm={handleDeleteConfirm}
        />
      )}

      {profileModal && (
        <ProfileModal close={() => setProfileModal(false)} user={user} updateProfile={updateProfile} />
      )}
      {passwordModal && (
        <PasswordModal
          close={() => setPasswordModal(false)}
          updatePassword={updatePassword}
          hasPassword={user?.has_password !== false}
        />
      )}

      <SearchModal
        isOpen={searchModalOpen}
        onClose={() => setSearchModalOpen(false)}
        meetings={meetings}
      />
    </div>
  );
}

function RenameDialog({
  initialTitle,
  close,
  onConfirm,
}: {
  initialTitle: string;
  close: () => void;
  onConfirm: (title: string) => Promise<void>;
}) {
  const [val, setVal] = useState(initialTitle);
  const [saving, setSaving] = useState(false);

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    if (!val.trim()) return;
    setSaving(true);
    await onConfirm(val.trim());
    setSaving(false);
  }

  return (
    <div className="dialog-backdrop">
      <form className="dialog" onSubmit={handleSubmit} style={{ width: 440 }}>
        <button className="close" onClick={close} type="button">
          ×
        </button>
        <h2>Đổi tên cuộc họp</h2>
        <div style={{ marginTop: 16 }}>
          <input
            autoFocus
            maxLength={200}
            value={val}
            onChange={(e) => setVal(e.target.value)}
            style={{ width: "100%", padding: "10px 14px", borderRadius: 8, border: "1px solid var(--border-strong)" }}
          />
        </div>
        <div className="dialog-actions" style={{ marginTop: 20 }}>
          <Button onClick={close} type="button">
            Hủy
          </Button>
          <Button type="submit" tone="dark" disabled={saving || !val.trim()}>
            {saving ? "Đang lưu…" : "Lưu"}
          </Button>
        </div>
      </form>
    </div>
  );
}

function DeleteConfirmDialog({
  title,
  close,
  onConfirm,
}: {
  title: string;
  close: () => void;
  onConfirm: () => Promise<void>;
}) {
  const [deleting, setDeleting] = useState(false);

  async function handleDelete() {
    setDeleting(true);
    await onConfirm();
    setDeleting(false);
  }

  return (
    <div className="dialog-backdrop">
      <div className="dialog" style={{ width: 440 }}>
        <h2>Xóa cuộc họp?</h2>
        <p style={{ marginTop: 12 }}>
          Bạn có chắc muốn xóa cuộc họp <strong>"{title}"</strong>? Mọi dữ liệu bản ghi, âm thanh và phân tích sẽ bị xóa vĩnh viễn.
        </p>
        <div className="dialog-actions" style={{ marginTop: 24 }}>
          <Button onClick={close} type="button">
            Hủy
          </Button>
          <Button tone="danger" onClick={handleDelete} disabled={deleting}>
            {deleting ? "Đang xóa…" : "Xóa vĩnh viễn"}
          </Button>
        </div>
      </div>
    </div>
  );
}

function ProfileModal({
  close,
  user,
  updateProfile,
}: {
  close: () => void;
  user: any;
  updateProfile: (name: string) => Promise<void>;
}) {
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
        <button className="close" aria-label="Đóng" onClick={close}>
          <Icon name="close" />
        </button>
        <h2>Thay đổi thông tin</h2>
        <p>Cập nhật họ và tên hiển thị của bạn.</p>
        <div className="compact-form" style={{ marginTop: 24 }}>
          <label className="field full">
            Tên hiển thị
            <input value={fullName} onChange={(e) => setFullName(e.target.value)} placeholder="Nhập họ và tên" />
          </label>
          <label className="field full">
            Email
            <input value={user?.email ?? ""} readOnly disabled />
          </label>
          {error && <div className="form-error">{error}</div>}
          <div className="form-actions" style={{ marginTop: 24 }}>
            <Button className="btn-ghost" onClick={close}>
              Hủy
            </Button>
            <Button tone="dark" disabled={saving || fullName.trim() === user?.full_name} onClick={() => void save()}>
              {saving ? "Đang lưu…" : "Lưu thay đổi"}
            </Button>
          </div>
        </div>
      </div>
    </div>
  );
}