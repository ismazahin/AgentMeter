# Continue starts the preparation service; Other Attack removed (design)

Date: 2026-11-13. Status: approved.

## Why

On the Upload step, Continue stays disabled until the owner presses a separate "Start the
preparation service" button in a large notice. One click should be enough. The "Group other
attacks as Other Attack" option makes a 6-class run, a different setup that the Leaderboard never
ranks with the normal 5-class runs, and the owner does not use it.

## What changes

### Continue starts the service

- Continue is enabled whenever the hosted site (control plane) is in use, running service or not.
- If the preparation service is not running when Continue is clicked, the click opens
  `agentmeter-prep://start` (the same link the old button used, registered by
  `python scripts/prep_local.py install`) and the page shows "Starting the preparation service…".
  The page then checks every 3 seconds, as before. As soon as the service is up, the upload carries
  on by itself.
- The browser asks "Open AgentMeter prep?" the first time. The status line says to tick Always allow.
- The page cannot tell whether the link was installed. If the service is not up after 20 seconds,
  the page shows the hint "First time on this PC? Run `python scripts/prep_local.py install` once"
  and where the log is, and keeps waiting up to 2 minutes. After that it stops and says it did not
  start.
- The large "The preparation service is not running" notice and its Start button go. While the
  service runs, the small "Stop it" line stays.

### Other Attack removed

- The checkbox on the Upload step and the "Add the Other Attack class by default" switch in
  Settings are removed. Uploads always send `other_attack: false`, so attack types outside the 5
  study classes are left out, as the default always did.
- The server keeps accepting the field (old clients), and old 6-class sessions open as before.

## Approaches

Only one sensible way: a click on Continue is a user action, so the browser lets it open the
`agentmeter-prep://` link, which is how the old button worked. Starting the service with no click
(on page load) would be blocked or would prompt unexpectedly, so it was not considered.

## Done when

- With the service off, one click on Continue starts it and the upload proceeds.
- The install hint appears only after 20 seconds without the service.
- No Other Attack option is left on the Upload step or in Settings.
- The browser walk-throughs that cover the upload step pass.
