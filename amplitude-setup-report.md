<wizard-report>
# Amplitude Setup Report

## Integration Summary

- **SDK:** `@amplitude/analytics-browser` v2.11.12 via CDN snippet (no build pipeline — static HTML site)
- **Init location:** `docs/index.html` — `<head>` section, before stylesheet links
- **API Key:** `13b73947bca78f7cdcf886372210a8c7` (public browser key, inlined — correct for static sites)
- **Server URL:** `https://api2.amplitude.com/2/httpapi`
- **Autocapture enabled:** attribution, pageViews, sessions, formInteractions, fileDownloads, elementInteractions, frustrationInteractions, pageUrlEnrichment, networkTracking, webVitals

## Events Instrumented

| Event Name | Description | Location |
|---|---|---|
| Tab Viewed | Fires when the user switches to a tab | `docs/index.html` → `switchTab()` |
| Cup Fixture Expanded | Fires when a user expands a cup fixture to view squads | `docs/index.html` → `makeSquadsToggle()` btn click |
| League Player Expanded | Fires when a user expands a league table row to view a manager's squad | `docs/index.html` → league `summaryRow` click |
| Live Gameweek Started | Fires when live polling begins because a gameweek is in progress | `docs/index.html` → `startLivePoll()` / `startCupLivePoll()` |
| Cup Settings Opened | Fires when the user opens the Cup Settings & Rules panel | `docs/index.html` → `openCupSettings()` |
| Cup Gameweeks Saved | Fires when the user saves custom cup gameweek configuration | `docs/index.html` → saveBtn click in `openCupSettings()` |
| Data Loaded | Fires on boot when JSON data files resolve | `docs/index.html` → boot `Promise.all().then()` |
| Error Encountered | Fires when a data fetch or render fails | `docs/index.html` → boot `.catch()` |

## Environment Variables

No env file needed — this is a static site with no build pipeline. The public Amplitude API key is inlined in the CDN `<script>` tag in `docs/index.html`. This is the correct pattern for static sites.

## CSP

No Content Security Policy was found in the project. If you add a CSP in the future, include:
- `script-src: https://*.amplitude.com`
- `connect-src: https://*.amplitude.com`

## Existing Analytics Patterns

No prior analytics instrumentation was found in the codebase. The `amplitude` global from the CDN snippet is used directly for all `track()` calls.

## Next Steps

1. Open `docs/index.html` in a browser and navigate between tabs — you should see **Tab Viewed** events in [Amplitude's Event Explorer](https://app.amplitude.com).
2. Expand a league row or cup fixture to verify **League Player Expanded** and **Cup Fixture Expanded** events.
3. Autocapture handles page views, sessions, element clicks, and web vitals automatically — no extra code needed.
4. If you deploy to a domain with ad-blocker concerns, consider adding a Netlify/Cloudflare proxy for `/amplitude-api/*` → `https://api2.amplitude.com/` and updating `serverUrl` accordingly.
</wizard-report>
