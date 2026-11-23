# Sidebar: Settings and Admin only in the name menu (short design)

Status: proposed. Mockup: `2026-11-23-sidebar-account-mockup/sidebar-options.png` (Option 2, picked by
the owner on 2026-11-23).

## Why
Settings and Admin appear twice: in the sidebar's ACCOUNT section and in the name menu at the
bottom of the sidebar. The owner wants each to appear once.

## Approaches
1. Keep the ACCOUNT section, trim the name menu to Account and Log out. One click to Settings.
2. **Picked.** Remove the ACCOUNT section; Settings and Admin live only in the name menu (Account,
   Settings, Admin for admins, Log out). A shorter sidebar, two clicks to Settings.
3. No name menu; the name opens Settings › Account, Log out on that page. Log out harder to find.

## Design
- The sidebar shows Workspace (Home, New Benchmark, Sessions, Leaderboard, Cost planner), then Help
  (How to use), then the name at the bottom. The ACCOUNT heading, its Settings link and its Admin
  link are removed.
- The name menu is unchanged: Account, Settings, Admin (admins only), Log out.
- While a Settings or Admin page is open, the name button shows as the current item (the same
  highlight a sidebar link gets), so it is clear where you are. (My call.)
- The collapsed sidebar shows the avatar; clicking it opens the same menu. (Already the case.)
- **Without a control plane** (one GPU server, no accounts, no name menu) a Settings link stays in
  the sidebar under Help, because there is nowhere else to reach it. (My call.)
- Tests that click the sidebar's Settings or Admin link open them through the name menu instead.

## Not changing
The pages themselves, the name menu's items, the top-right controls on phones.
