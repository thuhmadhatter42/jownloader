# Jownloader UI redesign (0.1.45)

The popup and the side panel are one page (`extension/popup.html`, `?panel=1` in the panel). This note is the
design record: what the page is for, the direction it commits to, and the evidence behind it.

## What the page is for, in order of use

1. Save everything on this page: images, videos, documents, collected-as-you-scroll pictures.
2. Save a video-site link as video or audio.
3. Save a music list as tagged audio.
4. Save a site as PDF or Markdown.
5. Pick the destination folder.
6. Watch progress and stop it.
7. See what failed and why.
8. Finishing switches: strip metadata, WebP conversion, MP3, BPM/key, file-name prefix with `[date]` tokens.
9. Install missing dependencies; open the log.

The old layout grouped these by implementation (a "Video / Images" tab, a "PDF" tab, an "Extra tools" tab, a
Spotify block floating above the tabs, the progress bar at the very bottom of whichever tab was open). The new
one groups them by moment: what this page offers, where it goes, what is running.

## Information architecture

```
header      app mark, the page's host, sidebar button (popup only)
destination "Save to" row: the folder every save lands in, always visible, × back to Downloads
alert       only when something blocks saving: no native host, or tools missing (Install button inline)
views       This page | PDF / Markdown | Tools      (segmented control, arrow keys move between views)
  This page   context card (video-site link, or music list) when the page is one
              "Save from this page": Images n, Videos n, Documents n, Collected n — one row each, the row is the button
              File names: the prefix box ([date] tokens)
              While browsing (disclosure): collect as I scroll, auto-download videos, button on videos
              Finishing (disclosure, summary shows what is on): strip metadata, WebP conversion
  PDF / Markdown   this page only / follow links, max pages, Save as PDF, Save as Markdown
  Tools       Rename files, Clean up files (strip / WebP), Dependencies, Log, Saved history (forget)
run dock    sticky at the bottom, appears when a run starts: what is running, n of m, the tally strip,
            the items in flight, failures with reasons, Stop. After the run: the verdict, failures, dismiss.
```

The run dock is the reason for the restructure: progress used to live inside a tab and was wiped by switching
tabs; it now sits outside the views, sticky, so Stop is reachable from every view, in the popup and in the panel.

## Direction contract

THESIS: a native Mac utility panel, the kind that sits beside the work (Activity Monitor's inspector, a Finder
info pane): grouped hairline rows, the system face, one blue from the app icon. It refuses the category default
of a stack of full-width colored buttons and checkbox lines.

OWN-WORLD: two grays (window and row surface), 1-device-pixel separators, one accent (the icon's blue) reserved
for the primary action of the page context, the selected view, focus and running state. Red only for failure
and Stop. Rows are buttons; switches are switches. Light and dark follow the system.

STORY: open on a page, see what it offers with counts that never jump, press one row, watch the dock count the
files into the folder named above it, stop it if needed, read the failures in plain words.

FIRST VIEWPORT: header and destination at the top, the context card (when the page is a video link or a music
list) as the one accent-filled button, the four "Save from this page" rows right under it. Nothing else competes.

FORM: candidate 4 of 7 on my ordered list (DAW transport bar, mixing-console channel strip, tape-deck front panel,
native Mac utility panel, shipping manifest, contact sheet, library card). Seed key 26582687.

FINISH: unreviewed and undocumented is unfinished; this build ends with the finish review, the verdict, DESIGN.md,
and every shipping raster carrying its provenance. (No rasters ship; the icon is unchanged. The design record is
this file rather than a root DESIGN.md, to keep the public repo's root clean.)

### Raises from the challengers that lost

- From the dance-score notation (declined): state as fill. The signature element is the tally strip, one cell per
  file in the run: solid saved, hatched failed, pulsing in flight, hollow waiting, struck through when stopped.
  It answers "how far, how many failed, what did Stop leave" in one glance, and turns into a proportional
  segmented bar past 120 files.
- From the split-flap departure board (competitive on clarity): rows are live entities in fixed columns. The dock
  restyles in place and never reflows; every number is tabular so counts change without the line shifting.
- From the airport wayfinding signs (competitive on clarity): the destination is always shown, and the accent is
  reserved for the path forward (the primary action) and nothing decorative.
- From the high-density module grid (declined): density courage. No padding inflation; 36 px rows, 8 px rhythm.
- From the centre-rail reference page (declined): one-pixel rules drawn at device resolution, focus as a mark
  (a 2 px ring with offset), never a glow.
- From the streaming title wall (declined): one focused thing. While a run is going the dock carries the weight;
  everything above stays usable but quieter.

### Type and color

- One family: the system UI face (`system-ui, -apple-system, "Segoe UI", sans-serif`), 13 px body (macOS
  control size), 12 px secondary, 15 px for the dock's headline. Weights 400 / 590. Tabular numerals on every
  count and size. No external fonts (extension CSP, and the system face is the native-utility choice).
- Light: window #F3F4F6, rows #FCFCFD, text #1D1F23, secondary #5E636E, hairline #DDE0E5, accent #2563EB.
- Dark: window #1C1D20, rows #26282C, text #ECEDEF, secondary #A1A6B0, hairline #3A3D43, accent #5B8DEF.
- Failure #D23A3A / #FF7A7A, saved #1E9254 / #4FC587, warning #A15C00 / #E9B04A.

### Interaction

- Stop needs no confirmation: finished files are kept and a re-run skips them, so Stop is cheap to undo.
- Views are a WAI-ARIA tablist (Left/Right/Home/End, roving tabindex); the dock is `role=region` with an
  `aria-live=polite` status line that speaks milestones, not every tick.
- One authored motion: a finished cell fills with a 140 ms ease-out; in-flight cells pulse. Reduced motion
  turns both into instant state changes.
- No layout jump while scanning: every row and count exists before the scan answers (counts show a dash), the
  context card is decided from the tab URL before the scan, and the dock overlays the bottom instead of pushing.

## Research used

- Popup size 25x25 to 800x600, sizes itself to its content; closes on blur, no way to keep it open:
  developer.chrome.com/docs/extensions/reference/api/action, developer.chrome.com/docs/extensions/develop/ui/add-popup
- Side panel: `sidePanel.open()` only from a user gesture; no documented width: developer.chrome.com/docs/extensions/reference/api/sidePanel
- Badge: about four characters fit: developer.chrome.com/docs/extensions/reference/api/action
- MV3 CSP `script-src 'self'`: no inline or remote scripts: developer.chrome.com/docs/extensions/reference/manifest/content-security-policy
- Tabs pattern (arrow keys, roving focus, aria-selected): w3.org/WAI/ARIA/apg/patterns/tabs/
- `:focus-visible`, 3:1 focus contrast: developer.mozilla.org/en-US/docs/Web/CSS/:focus-visible
- Live regions, polite by default; announce milestones: developer.mozilla.org/en-US/docs/Web/Accessibility/ARIA/Guides/Live_regions
- `<progress>` / progressbar semantics: developer.mozilla.org/en-US/docs/Web/HTML/Reference/Elements/progress
- `color-scheme: light dark` themes scrollbars and form controls: developer.mozilla.org/en-US/docs/Web/CSS/color-scheme
- `font-variant-numeric: tabular-nums`: developer.mozilla.org/en-US/docs/Web/CSS/font-variant-numeric
- Brave side panel support (fixed in 1.58): github.com/brave/brave-browser/issues/32132
- Progress + cancel: determinate bars, keep them moving, "let people halt processing", confirm when cancel loses
  progress: developer.apple.com/design/human-interface-guidelines/progress-indicators. Stop here has no confirm on
  purpose: what it throws away is only the files in flight, and a re-run skips everything finished.
- Long waits need real progress past 10 s: nngroup.com/articles/progress-indicators/
- Chrome's 2023 downloads bubble (actions and errors inline in the row, a full page for history):
  makeuseof.com/google-chrome-redesigned-downloads/ (secondary source). Failures here are inline in the dock;
  the full history stays in the log.
