# Product

<!-- impeccable:product-schema 1 -->
<!-- Inferred from the redesign brief and the codebase; no interview was possible (the redesign ran as a delegated agent). Every fact below is labelled where inferred. -->

## Platform

web (a Chromium MV3 browser extension: toolbar popup + the same page in the browser's side panel)

## Users

One person who saves media from the web all day, on a Mac, in Brave or Chrome (inferred from the brief). Uses it in short bursts while browsing (popup, closes on blur) and in long unattended runs (a Spotify list of 100+ songs, a site crawl) watched from the side panel.

## Product Purpose

Save what is on the page, at the best quality, into the folder you choose, with honest reporting of what failed and why. Frequency of jobs (from the brief): save everything on this page (images/videos/docs) > save a video site link as video or audio > save a Spotify list as tagged audio > save a site as PDF/Markdown > pick the destination > watch progress > see failures and reasons > finishing switches (strip metadata, WebP to JPEG, MP3, BPM/key, prefix with [date] tokens) > install missing dependencies > open the log.

## Positioning

Not a browser download: every file streams through a native host into any folder, never prompts, never overwrites; site engines (video/audio sites, social posts, music lists) run real downloaders behind one button; failures are named per item.

## Operating Context

Popup max 800x600, closes when a Finder dialog takes focus (one dialog at a time). Side panel stays open across tabs and rescans on tab switch. Runs can last many minutes (songs are 20 s apart). The toolbar badge is the only signal when the popup is closed.

## Capabilities and Constraints

- Extension CSP: no external fonts, no CDNs, no inline scripts.
- Element ids are relied on by tests (tests/ext_test.py, tests/spotify_popup_test.js).
- `[hidden]{display:none!important}` must stay.
- Public repository: no personal names, home paths or specific site names in shipped copy beyond generic site categories already present.
- A Stop control is required (brief): stops batch, music-list run, crawl and stream; shows "Stopped — n saved, m not started".

## Brand Commitments

Name: Jownloader. Icon: existing icon128.png. No other binding visual assets (inferred).

## Evidence on Hand

None beyond the product itself; no testimonials or claims to show, and none may be invented.

## Product Principles

1. The file is the deliverable; a clear failure reason is the fallback, never a silent drop.
2. One primary action per page context; everything else is secondary.
3. Running state is always visible and always stoppable.
4. Settings persist and stay out of the way until needed.

## Accessibility & Inclusion

Keyboard reachable, visible focus, light and dark, no layout jump while scanning (from the brief).
