/**
 * Lightweight client-side SPA routing utilities.
 * Handles client-side navigation without full page reloads, preserving
 * component state (such as the sidebar meeting list and active connections).
 */

export function navigate(to: string, options?: { replace?: boolean }) {
  if (!to) return;
  const currentUrl = window.location.pathname + window.location.search + window.location.hash;
  if (to === currentUrl) return;

  if (options?.replace) {
    window.history.replaceState(null, "", to);
  } else {
    window.history.pushState(null, "", to);
  }

  // Dispatch popstate event to notify routing listeners
  window.dispatchEvent(new PopStateEvent("popstate"));
}

/**
 * Global click interceptor for internal links.
 * Automatically converts normal <a> tag clicks to client-side SPA navigation
 * without needing to wrap every single anchor tag in a custom Link component.
 */
export function initLinkInterceptor() {
  if (typeof window === "undefined") return;

  const handleClick = (e: MouseEvent) => {
    // Only handle primary left click without modifier keys
    if (e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) {
      return;
    }

    const anchor = (e.target as HTMLElement).closest("a");
    if (!anchor) return;

    // Skip if default was already prevented
    if (e.defaultPrevented) return;

    // Skip external links or links with target="_blank"
    const target = anchor.getAttribute("target");
    if (target && target !== "_self") return;

    // Skip download links
    if (anchor.hasAttribute("download")) return;

    const href = anchor.getAttribute("href");
    if (!href) return;

    // Only intercept local internal paths starting with "/" (but not "//")
    if (href.startsWith("/") && !href.startsWith("//")) {
      // Don't intercept API download/export endpoints
      if (href.startsWith("/api/")) return;

      e.preventDefault();
      navigate(href);
    }
  };

  document.addEventListener("click", handleClick);
  return () => document.removeEventListener("click", handleClick);
}
