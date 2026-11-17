# SSHMC Frontend Architecture & Rendering Guidelines

## 1. Core Principle: One Rendering Model per Area
To avoid synchronization bugs, state drift, and DOM thrashing, each UI area must adhere strictly to **one** rendering model:

### Model A: Server-Rendered HTMX Fragments
- **Where to Use:** List and page content where the server is the single source of truth (e.g., `#profile-list`, `#stats-bar`).
- **Rules:**
  - Rendered by FastAPI + Jinja2 templates (`profile_list.html`).
  - Swapped via HTMX (`hx-trigger="refresh"`, `hx-swap="outerHTML"` or `hx-swap="innerHTML"`).
  - Must NOT contain nested persistent Alpine.js component islands with internal state that would be destroyed during swaps.
  - Profile card actions submit via `hx-post` with `hx-swap="none"`.

### Model B: Client-Side Reactive Islands (Alpine.js)
- **Where to Use:** Interactive islands and rich interactive features that maintain long-lived local state:
  - Mod browser & search (`mods_tab.html` / `modsApp()`)
  - Server log terminal & console streamer (`panel_modals.html` / `panelApp()`)
  - Real-time charts and host telemetry (`monitoring_tab.html` / `monitoringApp()`)
  - Interactive file & folder managers (`storage_tab.html` / `storageApp()`)
  - Complex multi-step wizards & edit dialogs (`formApp()`, `editApp()`, `modPacksApp()`)
- **Rules:**
  - Islands are never swapped or replaced by HTMX.
  - State is strictly encapsulated inside the island's Alpine component scope.
  - Communication across islands occurs **only** through the event bus.

---

## 2. Cross-Component Communication: Event Bus
- **Rule:** Direct DOM reach-ins (such as `Alpine.$data(...)`) and bare `window.*` communication globals are prohibited.
- **Mechanism:** Use `window.sshmc.emit(name, detail)` and `window.sshmc.on(name, handler)`:
  - Standard DOM `CustomEvent` dispatched on `window` with prefix `sshmc:`.
  - In templates, components listen declaratively: `@sshmc:event-name.window="handle($event.detail)"`.
  - In JavaScript, components call `sshmc.on('event-name', handler)` and store the returned unbind callback for cleanup in `destroy()`.

---

## 3. Background Synchronization: Unified Central Poller
- **Rule:** Individual components must never create their own independent `setInterval` or recursive `setTimeout` polling loops.
- **Mechanism:** All background polling is routed through `window.sshmc.poller`:
  - A single timer polls `/api/heartbeat` (3s normal interval).
  - Pauses automatically when `document.hidden` is true; triggers an immediate tick upon tab visibility.
  - Employs exponential backoff on server disconnects and notifies the UI via `sshmc:connection-status`.
  - Active tabs register per-tick callbacks (`registerTabCallback(tab, callback)`) that execute **only** when that specific tab is active.
  - Temporary fast-polling mode (1s) is requested via `requestFastPolling(key)` for in-progress operations (such as mod downloads) and released when complete.
  - HTMX list refreshes are only triggered when the heartbeat payload reveals a status or instance count change.
