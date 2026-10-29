# Changelog

## 0.1.1 — 2026-10-29

- `login --client-secrets FILE` accepts the `client_secret_*.json` downloaded from the Google Cloud console.
- Verified end to end against the live API with a real account.

## 0.1.0 — 2026-10-29

- First release: sync and async clients for the Photos Picker API (sessions, polling, media items).
- Streaming downloads with safe filenames, resize and crop options, and detection of videos that aren't ready.
- OAuth 2.0 with PKCE and refresh.
- `google-photos-picker` CLI with QR-code hand-off.
