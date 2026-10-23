import { useState, useEffect, useRef, useMemo } from "react";
import { createPortal } from "react-dom";
import { Icon } from "./ui";
import type { Meeting } from "../types/domain";
import { navigate } from "../app/router";

export function SearchModal({
  isOpen,
  onClose,
  meetings,
}: {
  isOpen: boolean;
  onClose: () => void;
  meetings: Meeting[];
}) {
  const [query, setQuery] = useState("");
  const [selectedIndex, setSelectedIndex] = useState(0);
  const inputRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    if (isOpen) {
      setQuery("");
      setSelectedIndex(0);
      setTimeout(() => inputRef.current?.focus(), 50);
    }
  }, [isOpen]);

  const filtered = useMemo(() => {
    const list = Array.isArray(meetings) ? meetings : [];
    if (!query.trim()) return list;
    const q = query.toLowerCase();
    return list.filter((m) => (m?.title || "").toLowerCase().includes(q));
  }, [meetings, query]);

  useEffect(() => {
    setSelectedIndex(0);
  }, [filtered.length]);

  useEffect(() => {
    if (!isOpen) return;
    function handleKeyDown(e: KeyboardEvent) {
      if (e.key === "Escape") {
        e.preventDefault();
        onClose();
      } else if (e.key === "ArrowDown") {
        e.preventDefault();
        setSelectedIndex((prev) => (filtered.length ? (prev + 1) % filtered.length : 0));
      } else if (e.key === "ArrowUp") {
        e.preventDefault();
        setSelectedIndex((prev) => (filtered.length ? (prev - 1 + filtered.length) % filtered.length : 0));
      } else if (e.key === "Enter" && filtered.length > 0) {
        e.preventDefault();
        const selected = filtered[selectedIndex];
        if (selected) {
          navigate(`/meetings/${selected.id}`);
          onClose();
        }
      }
    }
    window.addEventListener("keydown", handleKeyDown);
    return () => window.removeEventListener("keydown", handleKeyDown);
  }, [isOpen, filtered, selectedIndex, onClose]);

  if (!isOpen) return null;

  return createPortal(
    <div
      className="search-modal-backdrop"
      onClick={(e) => {
        if (e.target === e.currentTarget) onClose();
      }}
    >
      <div className="search-modal-card" role="dialog" aria-modal="true">
        <div className="search-modal-header">
          <Icon name="search" />
          <input
            ref={inputRef}
            type="text"
            placeholder="Tìm kiếm..."
            value={query}
            onChange={(e) => setQuery(e.target.value)}
          />
          <button
            type="button"
            className="search-modal-close"
            onClick={onClose}
            aria-label="Đóng"
          >
            ✕
          </button>
        </div>

        <div className="search-modal-body">
          <div className="search-modal-section-title">Trò chuyện gần đây</div>
          {filtered.length === 0 ? (
            <div style={{ padding: "16px 12px", color: "var(--muted)", fontSize: 13, textAlign: "center" }}>
              {query ? "Không tìm thấy kết quả phù hợp" : "Chưa có bản ghi nào"}
            </div>
          ) : (
            filtered.map((m, idx) => (
              <a
                key={m.id}
                href={`/meetings/${m.id}`}
                className={`search-modal-item ${idx === selectedIndex ? "active" : ""}`}
                style={idx === selectedIndex ? { background: "var(--surface-soft)" } : {}}
                onMouseEnter={() => setSelectedIndex(idx)}
                onClick={onClose}
              >
                <Icon name="message" />
                <span style={{ flex: 1, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                  {m.title || "Cuộc họp không tên"}
                </span>
              </a>
            ))
          )}
        </div>
      </div>
    </div>,
    document.body
  );
}
