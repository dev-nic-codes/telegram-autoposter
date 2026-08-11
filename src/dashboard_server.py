"""
Local dashboard server.
Serves a lightweight read/write dashboard on localhost for runtime status.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Dict, Optional
from urllib.parse import urlparse

from utils import log


DashboardDataCallback = Callable[[], Dict[str, Any]]
DashboardActionCallback = Callable[[str, Dict[str, Any]], Dict[str, Any]]


class LocalDashboardServer:
    """Small local-only HTTP server for the CLI dashboard."""

    def __init__(
        self,
        host: str,
        port: int,
        data_callback: DashboardDataCallback,
        action_callback: DashboardActionCallback,
    ) -> None:
        self.host = host
        self.port = int(port)
        self.data_callback = data_callback
        self.action_callback = action_callback
        self._server: Optional[ThreadingHTTPServer] = None
        self._thread: Optional[threading.Thread] = None

    @property
    def url(self) -> str:
        """Return the dashboard URL."""
        port = self.port
        if self._server:
            port = int(self._server.server_address[1])
        return f"http://{self.host}:{port}/"

    def is_running(self) -> bool:
        """Return True when the server thread is alive."""
        return bool(self._thread and self._thread.is_alive())

    def start(self) -> None:
        """Start the dashboard server in a daemon thread."""
        if self.is_running():
            return

        owner = self

        class Handler(BaseHTTPRequestHandler):
            server_version = "TelegramAutoposterDashboard/1.0"

            def log_message(self, fmt: str, *args: Any) -> None:
                log(f"Dashboard {self.address_string()} {fmt % args}", "DEBUG")

            def _send_bytes(
                self,
                status: int,
                content: bytes,
                content_type: str,
            ) -> None:
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(content)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(content)

            def _send_json(self, status: int, payload: Dict[str, Any]) -> None:
                body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
                self._send_bytes(status, body, "application/json; charset=utf-8")

            def _read_json(self) -> Dict[str, Any]:
                raw_length = self.headers.get("Content-Length", "0")
                try:
                    length = max(0, int(raw_length))
                except ValueError:
                    length = 0
                if length <= 0:
                    return {}
                raw = self.rfile.read(min(length, 64 * 1024))
                try:
                    data = json.loads(raw.decode("utf-8"))
                except json.JSONDecodeError:
                    return {}
                return data if isinstance(data, dict) else {}

            def do_GET(self) -> None:
                path = urlparse(self.path).path
                try:
                    if path in {"/", "/index.html"}:
                        self._send_bytes(
                            200,
                            DASHBOARD_HTML.encode("utf-8"),
                            "text/html; charset=utf-8",
                        )
                        return
                    if path == "/api/status":
                        self._send_json(200, owner.data_callback())
                        return
                    self._send_json(404, {"ok": False, "error": "Not found"})
                except Exception as exc:
                    self._send_json(500, {"ok": False, "error": str(exc)})

            def do_POST(self) -> None:
                path = urlparse(self.path).path
                try:
                    if path != "/api/action":
                        self._send_json(404, {"ok": False, "error": "Not found"})
                        return
                    payload = self._read_json()
                    action = str(payload.get("action") or "").strip()
                    if not action:
                        self._send_json(400, {"ok": False, "error": "Missing action"})
                        return
                    self._send_json(200, owner.action_callback(action, payload))
                except Exception as exc:
                    self._send_json(500, {"ok": False, "error": str(exc)})

        self._server = ThreadingHTTPServer((self.host, self.port), Handler)
        self.port = int(self._server.server_address[1])
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            name="local-dashboard",
            daemon=True,
        )
        self._thread.start()
        log(f"Local dashboard started at {self.url}", "SUCCESS")

    def stop(self) -> None:
        """Stop the dashboard server."""
        if not self._server:
            return
        self._server.shutdown()
        self._server.server_close()
        if self._thread:
            self._thread.join(timeout=3)
        self._server = None
        self._thread = None
        log("Local dashboard stopped", "INFO")


DASHBOARD_HTML = r"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Telegram Autoposter Dashboard</title>
  <style>
    :root {
      --ink: #17202a;
      --muted: #5c6670;
      --line: #d8dde3;
      --panel: #ffffff;
      --page: #f4f7f9;
      --green: #167f5f;
      --red: #b53a33;
      --yellow: #9b6b00;
      --cyan: #116f8a;
      --violet: #6750a4;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      color: var(--ink);
      background: var(--page);
      font-family: Arial, Helvetica, sans-serif;
      letter-spacing: 0;
    }
    header {
      padding: 18px 22px;
      background: #ffffff;
      border-bottom: 1px solid var(--line);
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 16px;
      flex-wrap: wrap;
    }
    h1 { margin: 0; font-size: 22px; }
    h2 { margin: 0 0 10px; font-size: 18px; }
    h3 { margin: 0 0 8px; font-size: 15px; }
    main {
      width: min(1280px, 100%);
      margin: 0 auto;
      padding: 18px;
    }
    .status-line {
      display: flex;
      gap: 10px;
      align-items: center;
      color: var(--muted);
      font-size: 14px;
      flex-wrap: wrap;
    }
    .grid {
      display: grid;
      grid-template-columns: repeat(4, minmax(0, 1fr));
      gap: 12px;
      margin-bottom: 18px;
    }
    .tile, .section, .profile {
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
    }
    .tile { padding: 14px; min-height: 86px; }
    .tile strong { display: block; font-size: 26px; margin-bottom: 6px; }
    .tile span { color: var(--muted); font-size: 13px; }
    .layout {
      display: grid;
      grid-template-columns: 2fr 1fr;
      gap: 14px;
      align-items: start;
    }
    .section { padding: 14px; margin-bottom: 14px; }
    .profile { padding: 14px; margin-bottom: 12px; }
    .profile-head {
      display: flex;
      justify-content: space-between;
      gap: 12px;
      align-items: flex-start;
      flex-wrap: wrap;
    }
    .badge {
      display: inline-block;
      padding: 3px 8px;
      border-radius: 8px;
      border: 1px solid var(--line);
      font-size: 12px;
      color: var(--muted);
      background: #f8fafb;
      margin: 2px 4px 2px 0;
    }
    .ok { color: var(--green); }
    .warn { color: var(--yellow); }
    .bad { color: var(--red); }
    .info { color: var(--cyan); }
    button {
      border: 1px solid #9aa6b2;
      background: #ffffff;
      color: var(--ink);
      border-radius: 8px;
      padding: 7px 10px;
      cursor: pointer;
      margin: 3px 4px 3px 0;
      font-size: 13px;
    }
    button.primary { border-color: var(--green); color: var(--green); }
    button.danger { border-color: var(--red); color: var(--red); }
    button:disabled { opacity: .55; cursor: default; }
    .list {
      display: grid;
      gap: 8px;
      margin-top: 10px;
    }
    .row {
      border-top: 1px solid var(--line);
      padding-top: 8px;
    }
    .row:first-child {
      border-top: 0;
      padding-top: 0;
    }
    .title {
      overflow-wrap: anywhere;
      font-weight: 600;
    }
    .meta {
      color: var(--muted);
      font-size: 13px;
      margin-top: 4px;
      overflow-wrap: anywhere;
    }
    pre {
      white-space: pre-wrap;
      overflow-wrap: anywhere;
      margin: 0;
      font-family: Consolas, Menlo, monospace;
      font-size: 12px;
      line-height: 1.4;
    }
    .settings {
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 8px 14px;
      margin-top: 8px;
      font-size: 13px;
    }
    .empty { color: var(--muted); }
    @media (max-width: 900px) {
      .grid, .layout, .settings { grid-template-columns: 1fr; }
      header { align-items: flex-start; }
    }
  </style>
</head>
<body>
  <header>
    <div>
      <h1>Telegram Autoposter Dashboard</h1>
      <div class="status-line">
        <span id="updated">Loading...</span>
        <span id="api-state"></span>
      </div>
    </div>
    <button onclick="refreshNow()">Refresh</button>
  </header>
  <main>
    <section class="grid" id="summary"></section>
    <div class="layout">
      <section>
        <div id="profiles"></div>
      </section>
      <aside>
        <div class="section">
          <h2>Recent Logs</h2>
          <div id="logs" class="list"></div>
        </div>
      </aside>
    </div>
  </main>
  <script>
    const state = { data: null };

    function esc(value) {
      return String(value ?? '').replace(/[&<>"']/g, ch => ({
        '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
      }[ch]));
    }

    function clsStatus(value) {
      if (value === 'running') return 'ok';
      if (value === 'paused') return 'warn';
      if (value === 'emergency') return 'bad';
      return 'info';
    }

    async function postAction(action, profileKey, extra = {}) {
      const response = await fetch('/api/action', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({action, profile_key: profileKey, ...extra})
      });
      const payload = await response.json();
      if (!payload.ok) alert(payload.error || 'Action failed');
      await refreshNow();
    }

    function renderSummary(data) {
      const s = data.summary || {};
      const items = [
        ['Profiles', s.profile_count ?? 0, 'configured bot profiles'],
        ['Running', s.running_count ?? 0, 'active runtime threads'],
        ['Pending', s.pending_count ?? 0, 'approval previews waiting'],
        ['Queue', s.queue_count ?? 0, 'approved posts ready']
      ];
      document.getElementById('summary').innerHTML = items.map(item => `
        <div class="tile"><strong>${esc(item[1])}</strong><span>${esc(item[0])}: ${esc(item[2])}</span></div>
      `).join('');
    }

    function renderQueue(profile) {
      const queue = profile.queue || [];
      if (!queue.length) return '<div class="empty">Queue is empty.</div>';
      return `<div class="list">${queue.map((item, idx) => `
        <div class="row">
          <div class="title">${idx + 1}. r/${esc(item.subreddit || '?')} · ${esc(item.type || '?')}</div>
          <div class="meta">${esc(item.title || '(untitled)')}</div>
          <button onclick="postAction('queue_up','${esc(profile.key)}',{index:${idx + 1}})">Up</button>
          <button onclick="postAction('queue_down','${esc(profile.key)}',{index:${idx + 1}})">Down</button>
          <button class="danger" onclick="postAction('queue_remove','${esc(profile.key)}',{index:${idx + 1}})">Remove</button>
        </div>
      `).join('')}</div>`;
    }

    function renderHistory(profile) {
      const history = profile.recent_posts || [];
      if (!history.length) return '<div class="empty">No recent posts yet.</div>';
      return `<div class="list">${history.map(item => `
        <div class="row">
          <div class="title">r/${esc(item.subreddit || '?')} · ${esc(item.type || '?')}</div>
          <div class="meta">${esc(item.timestamp || '')} ${esc(item.id || '')}</div>
        </div>
      `).join('')}</div>`;
    }

    function renderProfile(profile) {
      const statusClass = clsStatus(profile.status);
      const pending = profile.pending;
      const settings = profile.settings || {};
      return `
        <article class="profile">
          <div class="profile-head">
            <div>
              <h2>${esc(profile.name)}</h2>
              <span class="badge ${statusClass}">${esc(profile.status)}</span>
              <span class="badge">${esc(profile.channel || 'no channel')}</span>
              <span class="badge">${esc((profile.subreddits || []).length)} subreddits</span>
            </div>
            <div>
              <button class="primary" onclick="postAction('resume','${esc(profile.key)}')">Resume</button>
              <button onclick="postAction('pause','${esc(profile.key)}')">Pause</button>
            </div>
          </div>
          ${profile.emergency_pause ? `<p class="bad"><strong>Emergency:</strong> ${esc(profile.emergency_pause.category)} · ${esc(profile.emergency_pause.reason || '')}</p>` : ''}
          <div class="settings">
            <div><strong>Posting:</strong> every ${esc(settings.post_interval_minutes)} min</div>
            <div><strong>Daily limit:</strong> ${esc(settings.daily_post_limit_text)}</div>
            <div><strong>Timezone:</strong> ${esc(settings.timezone)}</div>
            <div><strong>Caption:</strong> ${esc(settings.caption_mode)}</div>
            <div><strong>Scoring:</strong> ${esc(settings.smart_scoring_enabled ? 'On' : 'Off')}</div>
            <div><strong>Gallery:</strong> ${esc(settings.gallery_posts_enabled ? `${settings.min_gallery_items}-${settings.max_gallery_items} items` : 'Off')}</div>
            <div><strong>Image:</strong> ${esc(settings.image_quality || 'Default')}</div>
            <div><strong>Video:</strong> ${esc(settings.video_rules || 'Default')}</div>
            <div><strong>Domains:</strong> ${esc(settings.domain_downloaders_enabled ? 'On' : 'Off')}</div>
          </div>
          <div class="section">
            <h3>Pending Preview</h3>
            ${pending ? `<div class="title">r/${esc(pending.subreddit)} · ${esc(pending.type)}</div><div class="meta">${esc(pending.title || '')}</div>` : '<div class="empty">No pending preview.</div>'}
          </div>
          <div class="section">
            <h3>Queue <button class="danger" onclick="postAction('queue_clear','${esc(profile.key)}')">Clear Queue</button></h3>
            ${renderQueue(profile)}
          </div>
          <div class="section">
            <h3>Subreddits</h3>
            <div>${(profile.subreddits || []).map(sub => `<span class="badge">r/${esc(sub)}</span>`).join('') || '<span class="empty">None configured.</span>'}</div>
          </div>
          <div class="section">
            <h3>Recent Posts</h3>
            ${renderHistory(profile)}
          </div>
        </article>
      `;
    }

    function renderLogs(data) {
      const logs = data.logs || [];
      document.getElementById('logs').innerHTML = logs.length ? logs.map(item => `
        <div class="row">
          <div><strong class="${esc((item.level || '').toLowerCase())}">${esc(item.level)}</strong> <span class="meta">${esc(item.timestamp)} ${esc(item.thread || '')}</span></div>
          <pre>${esc(item.message)}</pre>
        </div>
      `).join('') : '<div class="empty">No logs captured yet.</div>';
    }

    function render(data) {
      state.data = data;
      document.getElementById('updated').textContent = `Updated ${data.generated_at || ''}`;
      document.getElementById('api-state').textContent = data.ok ? 'Connected' : 'Error';
      renderSummary(data);
      document.getElementById('profiles').innerHTML = (data.profiles || []).map(renderProfile).join('');
      renderLogs(data);
    }

    async function refreshNow() {
      try {
        const response = await fetch('/api/status', {cache: 'no-store'});
        render(await response.json());
      } catch (err) {
        document.getElementById('api-state').textContent = `Error: ${err}`;
      }
    }

    refreshNow();
    setInterval(refreshNow, 5000);
  </script>
</body>
</html>
"""
