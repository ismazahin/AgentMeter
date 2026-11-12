# Each user sees their own work, admins see everyone's (with whose it is)

Date: 2026-11-12 · Status: design, waiting for the owner's approval
Mockups: `docs/superpowers/specs/2026-11-12-per-user-separation/` (leaderboard-admin.png,
leaderboard-user.png, sessions-admin.png), drawn inside the real app page with its own styles.

## Why

AgentMeter on the hosted site (control plane) has several user accounts, and today every user can
read every session, data set, ranking and comparison. The owner wants each user to see only the
work they ran, and the admin to see everyone's work while always knowing whose each piece is.
When one user's own sessions are too few to rank, the Leaderboard says so plainly instead of
showing an empty or misleading ranking.

In the owner's words: "each user can only view the session that they run, if not enough, Inform
at leaderboard its not enough", "for admin, he can see all session from all user. make sure the
admin knows which section whose".

## Goals

1. A regular user sees only their own sessions, data sets, traffic analyses, rankings, comparisons
   and repeatability charts, on every page.
2. An admin sees everyone's, with the owner's name next to each session, data set and ranked model
   (the "Run by" column), and can narrow to "Only mine" or to one user.
3. The Leaderboard tells a user when their sessions are not enough to rank, why, and what to run.
4. A filter bar on Sessions, Leaderboard (all its tabs) and the data-set list: GPU, data set, input
   type, status where it applies, rank by, demo runs, and for admins whose sessions.
5. Enforced on the servers (control plane and GPU backend), not only hidden on the page.

## Non-goals

- Sharing a session with another user, or teams. Not asked for.
- Changing how anything is measured or scored. Rankings use exactly today's rules (same flows, GPU
  and settings), only over a different set of sessions.
- The single GPU server without a control plane. It has no accounts; its operator stays the admin
  and sees everything, as today.

## Anti-goals (would make this a failure even if it works)

- A user reaching another user's session, results, flows, prepared set or explanation through any
  address (the page, the control plane API, or the GPU backend API with a read token).
- An admin looking at a ranking without being able to tell whose sessions it is built from.
- A user facing an empty Leaderboard with no reason and no next step.

## Decisions (the owner's)

- Users: own work only. Admins: everyone's, owner shown. (2026-11-11)
- Admin Leaderboard and Compare: everyone's sessions by default, with a switch to only their own
  or one user's. Reason given and accepted: rankings need many sessions on the same setup, the setup
  grouping keeps mixed owners fair, and admins already see everyone's sessions elsewhere. (2026-11-12)
- Users never see other users' results, not even anonymised inside a ranking. (2026-11-12)
- Filters as a row of dropdowns, like the owner's example (GPU, region, price pickers). Design left
  to the builder; the mockups show the chosen look. (2026-11-12)

## Approaches considered

1. **Scope on the server, filter attributes on the page (chosen).** The control plane decides which
   sessions a request may see (owner or admin); the page filters what came back by GPU, data set
   and so on. Whose-sessions is a server parameter, because it is a permission, not a view choice.
   Small, safe, and the page stays fast.
2. **Everything filtered on the page.** Simplest, but every user would still download everyone's
   sessions. Breaks goal 5.
3. **Separate storage per user.** Strongest isolation, but admins need the combined view, data sets
   are shared by hash, and it means migrating stored sessions. Far more work for no gain over 1.

## The rule

One function in the control plane, `visible(user, owner?)`, returns the SQL condition every
session read uses:

- regular user: `owner_id = <their id>` (the `owner` parameter is ignored)
- admin: no condition, or `owner_id = <id>` for `owner=me` or `owner=<username>`

Sessions with no owner (their user was removed, or stored before accounts) are visible to admins
only. A session a user may not see answers 404 (not 403), so its existence is not revealed.

### Where it applies

Control plane (`supabase/functions/agentmeter/src/`):

| Route | Change |
|---|---|
| `GET /api/sessions` | `visible()`; `owner=` for admins; rows carry `owner` (already) |
| `GET /api/sessions/:id`, `/results`, `/files/*`, `/constraints` | 404 unless visible |
| `GET /api/compare`, `/compare/candidates`, `/compare/merged` | only visible sessions |
| `GET /api/leaderboard` (both views) | only visible sessions; `owner=` for admins; each row lists its sessions' owners |
| `GET /api/datasets` | users: sets that one of their sessions or traffic analyses used; admins: all, with owner |
| `GET /api/analyses` | already scoped (prep.ts); unchanged |
| notifications (`notify.ts`, "moved since earlier runs") | a user's run is compared with their own earlier runs only |

The Home page, Cost planner and Repeatability read these routes, so they follow without changes of
their own.

GPU backend (`agentmeter/server/`), reached with read tokens that already carry the user (`sub`)
and `admin`:

| Route | Change |
|---|---|
| `GET /api/jobs` | users: only jobs they started; admins: all |
| `GET /api/jobs/<id>/result`, `/flows`, `/telemetry`, `/report.pdf`, `/constraints`, `/explanation` | the job's owner or an admin |
| `GET /api/jobs/<id>` (status only) | unchanged: open for 128-bit ids, shows no results |
| `GET /api/prepared/<kind>/<name>` and its files | users: sets one of their jobs prepared or used; admins: all |
| `GET /api/gpu` (GPU & Queue) | users see their own jobs in full; another user's job shows as "Another user's benchmark" with its status and queue position only |

### Data set names and removals

Today a data set's name and its removal are one record for everyone, owned by whoever benchmarked it
first. With separation, two users who uploaded the same flows would rename or hide each other's set.
**Builder's call (check this, owner):** names and removals become per user, keyed by user and set.
An admin's rename or removal applies to their own view only. Deleting the stored files stays an
admin action.

## The Leaderboard when there is not enough

A setup (same flows, GPU and settings) can be ranked when at least **2 models** have a value for the
chosen measure there. Today's rule, unchanged.

- **By setup:** a setup with fewer shows "Not enough to rank yet", with two dots for the models
  measured out of the 2 needed, the models measured so far (not ranked), and a **Run another model
  on this set** button that opens New Benchmark on that prepared set. See leaderboard-user.png.
- **Overall:** when none of the user's setups has 2 models, the tab says so, with the same button for
  their most recent set.
- **Compare models:** only setups with 2 or more models are offered. With none, the same notice.
- **No sessions at all:** "You have not run a benchmark yet", with a New benchmark button.

Admins see the same notices when their chosen view (Only mine, or one user) is too small.

## The filter bar (see the mockups)

A row of compact dropdown pills under the tabs, each "Label Value ▾": `Sessions Everyone ▾` (admins
only), `GPU`, `Data set`, `Input`, `Rank by`, and a `Demo runs` switch. A pill not on its default
is tinted, a **Clear filters** link appears when any is set, and a count on the right says what is
shown ("2 setups, 9 sessions from 3 people", or "Your sessions only, 2 sessions").

- Sessions page: Search, `Owner` (admins), `Status`, `GPU`, `Input`, plus an **Owner** column
  (admins). A small "Admin · You see everyone's sessions. Users see only their own." line above.
- Leaderboard: the same bar on By setup, Compare models, Overall and Repeatability, so a choice made
  on one tab stays on the others. **Run by** column for admins (owner initials and names).
- Data sets list (New Benchmark › Reuse a prepared set): `Owner` (admins), `GPU`, `Input`.
- The options in each pill come from the sessions the user can see; a GPU nobody ran on is not offered.
- Filters live in the address (`#/leaderboard?gpu=…&set=…`), so Back and shared links keep them,
  and the last choice per page is remembered in the browser.
- On a phone the pills wrap onto more rows; the count moves under them.

## Constraints

- The Python leaderboard on the GPU backend (`agentmeter/server/sessions.py`) mirrors the control
  plane's; its parity tests must keep passing. The backend needs the same owner scoping for its own
  routes only (the hosted site ranks through the control plane).
- On-screen wording follows the house rules: no em dashes, semicolons or clause colons.
- Existing walk-throughs (`tests/e2e/cp_flow.js`) assume one user sees everything; they get a second
  user.

## Testing

- Control plane unit tests (`controlplane/test`): `visible()` for user, admin, `owner=` values,
  ownerless sessions; every route in the table refuses another user's session with 404.
- GPU backend tests: read tokens of user A cannot read user B's job results, flows, explanation or
  prepared set; admins can; `/api/jobs` lists only A's jobs for A.
- `cp_flow.js`: a second user runs a session. Each user sees only their own on Sessions, Home,
  Leaderboard, Compare, Repeatability and Data sets, and gets 404 on the other's session address.
  The admin sees both with owners, narrows with the Sessions pill, and sees "Run by". A user with one
  model sees "Not enough to rank yet" and the button opens New Benchmark on that set.

## Left to the builder

- Exact pill styling within the app's tokens (the mockup's look is the target), keyboard and screen
  reader behaviour of the pills (they are real `<select>` elements styled as pills).
- Owner avatar colours (from the username, stable).
- How the control plane passes owner names to the GPU queue view.
