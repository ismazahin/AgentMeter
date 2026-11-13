# Settings, Account and Admin redesign (design)

Date: 2026-11-13. Status: approved (mockups K-settings-notifications, K-settings-account,
K-admin-overview in `docs/superpowers/specs/2026-11-12-per-user-separation/`).

## Why

- Settings had one Save button at the bottom of a long page: changes were lost, and the Telegram
  test failed because the chat ID was never saved.
- Connecting Telegram needed outside tools (@userinfobot, `getUpdates`) and a cryptic hint.
- Default models were empty while the GPU was offline. "SAW weight presets" was jargon. A
  technical Backend block sat in a user page.
- Profile was a thin separate page, so "your stuff" was split across two menus.
- Admin was one long page mixing daily work (users) with rare work (IDS upload, limits, catalog),
  and its credential status read like errors ("unknown (backend never registered)").

## What changes

### Settings (tabs: Notifications, Benchmark defaults, Ranking weights, Appearance, Account)

- **Saves as you go.** Every control saves on change (PUT `/api/settings` already merges a
  partial patch). Each section shows a small "Saved" or the error next to its heading. No Save
  button.
- **Notifications › Telegram alerts** is three steps with a status chip (Not set up / Connected):
  1. *Open in Telegram*: a `t.me/<bot>?start=<code>` link. The code is a one-time link code
     made for this user, so pressing Start tells AgentMeter which chat is theirs.
  2. *Connect your chat*: **Find my chat** asks the control plane to read the bot's recent
     updates and match the `/start <code>` message; on a match it saves that chat ID. A paste box
     stays as the fallback (saved on change).
  3. *Send test*: unchanged route, now always meaningful because the ID is saved.
  A preview of an alert sits beside the steps.
- **When to message you**: three buttons (Never, When one finishes, Finishes or fails), plus a
  switch for the worse-result alert with a one-sentence explanation.
- **Benchmark defaults**: the default models list uses the 5 study models when the GPU is
  offline (from the site's own list), plus default flows and time zone.
- **Ranking weights**: the old SAW weight presets, renamed and explained in plain words.
- **Appearance**: the theme choice.
- **Account**: what the Profile page had (who you are, logged-in devices, change password), plus
  *Log out everywhere else*. The Profile sidebar link goes; `#/profile` opens this tab, and the
  user menu's Profile entry points to it.
- The Backend block moves to Admin.

### Admin (tabs: Overview, Users, Models, Limits, Audit log)

- **Overview**: four tiles (users, benchmarks this week, GPU server status, problems in the last
  24 hours), then a **Setup checklist**: Telegram bot token, control plane, preparation service,
  study models, IDS model (with Upload), Hugging Face token and Vast key ("checked when the GPU
  server starts" while it is offline). Then the System block (moved from Settings).
- **Users**, **Models** (catalog and IDS model), **Limits**, **Audit log**: the existing
  sections, each on its own tab.

## Control-plane changes

- `GET /api/settings/telegram-link`: returns the bot's username (Telegram `getMe`, cached) and a
  fresh one-time link code for the caller (kept in the caller's settings with an expiry of 15
  minutes, never shown to other users).
- `POST /api/settings/telegram-link`: reads the bot's recent updates (`getUpdates`), finds a
  private-chat message `/start <code>` matching the caller's unexpired code, saves that chat ID
  and clears the code. A wrong or expired code changes nothing. Messages from other people never
  match because the code is per user.

## Approaches

1. Tabs and save-as-you-go on the existing pages, with a link-code Telegram flow (chosen).
2. Keep one long page and only fix the Save button and wording. Rejected: the length and mixing
   were half the problem.
3. A Telegram webhook to link chats. Rejected: needs a public webhook and more secrets for the
   same result; polling `getUpdates` on demand is enough for a button press.

## Done when

- No Save button remains in Settings and each change shows "Saved".
- Find my chat connects a chat with no outside tool; the test message then arrives.
- Profile is the Account tab; Admin opens on Overview with the checklist.
- Existing walk-throughs that touch these pages pass, updated for the new layout.
