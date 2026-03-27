/**
 * Detect the app's base path from the current page URL.
 *
 * When the app is mounted at a sub-path (e.g., /severance/ in Docker),
 * WebSocket and asset URLs must include that prefix.
 *
 * Examples:
 *   location.pathname = "/"                → returns ""
 *   location.pathname = "/severance/"      → returns "/severance"
 *   location.pathname = "/severance/index.html" → returns "/severance"
 */
export function getBasePath() {
  let path = location.pathname;
  // Strip filename if present (e.g., /severance/index.html → /severance/)
  if (path.includes('.')) {
    path = path.substring(0, path.lastIndexOf('/'));
  }
  // Remove trailing slash — root "/" becomes ""
  return path.replace(/\/$/, '');
}

/**
 * Build a WebSocket base URL that includes the mount prefix.
 * e.g., "wss://host/severance"
 */
export function getWsBase() {
  const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
  return `${proto}//${location.host}${getBasePath()}`;
}
