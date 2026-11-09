# Beads in Herdr

User-level native Herdr mods for browsing any project's Beads issues and opening issue-specific omp worktree sessions. This standalone source checkout lives at `~/.local/share/herdr-beads`, outside project repositories. Shipped Herdr layouts and mods are not modified.

## Install and open

Requires Herdr running locally, `hc`, `bd`, `git`, `omp`, and Python 3 on PATH. Node is only needed for the model regression tests.

```sh
bash ~/.local/share/herdr-beads/install.sh
cd /path/to/project
go-bd --dashboard
```

The installer copies `beads-dashboard`, `bead-detail`, and `beads-sidebar` into `${HERDR_APP_HOME:-~/.config/herdr-app}/mods`, installs the personal `beads-workspace` layout, and updates `~/.local/bin/go-bd` together with its `go-bd-session.mjs` startup extension. Keep these two launcher files together. An existing personal layout file is preserved on reinstall. Keep `~/.local/bin` on PATH. `HERDR_APP_HOME` selects a different Herdr window/configuration.

`go-bd --dashboard` opens or focuses the project's Beads tab in its main-checkout space. The dashboard stays pinned to that project when another pane changes directory. Invoking the command from a linked worktree or nested folder resolves the same main checkout and database.

### Sidebar button

Switch to the personal layout once:

```sh
herdr-app do layout.switch --name beads-workspace
```

Click **Beads** above Settings in the sidebar. It opens a dashboard tab inside the currently selected space, using that space's first terminal's live folder. If the space already has a Beads dashboard, the button selects its existing tab and focuses the dashboard pane instead of creating a duplicate. The button remains available as an icon when the sidebar is collapsed and highlights while its dashboard tab is selected.

Unlike the sidebar button, `go-bd --dashboard` opens the dashboard in the project's main-checkout space. Both views use the same shared project database. Herdr remembers the selected layout across restarts; switching back to `main` restores the shipped interface without removing any tabs or sessions.


## Dashboard

- Lists all non-closed issues, including deferred issues, without the CLI's default 50-row limit.
- Uses actual parent IDs for epic/subtask nesting. Epics and sibling issues sort by priority, then ID. Closed ancestors are fetched as structural context where needed.
- Displays issue ID, title, priority, type, durable Beads status, dependency blocking, and associated live sessions. Issues returned by `bd ready --limit 0` show **Ready** in green instead of Open; an unblocked in-progress or deferred issue is not automatically ready.
- Click an issue to open its details on the right. Further selections reuse the same pane. Details have a title/status header, compact metadata and label pills, Markdown narrative sections, and relationship/comment cards. Remaining nonempty `bd show --long` fields stay available under Additional details. Dates are localized with original timestamps in tooltips; Beads' zero timestamps show Not recorded. Ready badges also apply to related issues. Refresh reloads issue data and readiness.
- The left side is the **issue list pane**; the right side is the **issue detail pane** (a master–detail layout). Click a parent or relationship ID in the detail pane to open that bead and highlight/scroll to it in the list. Navigation expands its ancestors and shows filtered-out or closed targets as uncounted Navigation context without changing the active filter.
- **Back** and **Forward** in the detail toolbar revisit selections from either pane. Selecting a different bead after going Back discards the forward branch; selecting the current bead adds no history entry. History lasts for the current detail-pane instance and resets when it closes or reloads.
- **All open** (the initial view) includes every non-closed issue, including blocked, in-progress, and deferred work. **Ready** shows the union of authoritative `bd ready` matches and all `in_progress` issues. In-progress issues keep their In progress status labels. Both buttons show counts without duplicates; ancestor/navigation context does not inflate them. Closed issues stay hidden except when needed as ancestor or selected navigation context.
- Click a disclosure arrow to fold children. Folding and the selected All open / Ready filter are saved per machine/database. The old blocked-filter preference is ignored. There is no search field.
- Beads queries refresh every ten seconds or via Refresh. A query failure is visible and does not replace the last good snapshot with an empty list.

The mod uses Herdr's `exec` API to run the Beads CLI. It does not read exports, query Dolt directly, send issue data over the network, or maintain a second issue database. Live agent data comes from Herdr reactively. Project commands run on the space's machine; starting a remote session requires the launcher and a working Herdr window on that machine. Local worktree sessions are the verified configuration.

## Start and return to work

```sh
go-bd --herdr <issue-id>       # Start, resume saved conversation, or focus live session
go-bd <issue-id>               # Start/resume in the current terminal
```

Start resolves the full issue ID, checks readiness/inherited blockers, claims atomically, and creates or reuses its issue-named omp worktree. A self-owned, unblocked `in_progress` issue can resume after its previous agent ends. Other owners' claims are not overwritten.

Worktree directories, agent tabs, and worktree space names use the full bead ID, such as `myproject-123`, without an additional repository-name prefix.

A live associated Herdr omp session is focused instead of duplicated. Once that terminal ends, an unblocked, self-owned `in_progress` issue with saved session metadata resumes its exact conversation in the original worktree. The dashboard offers **Go to session**, **Resume**, or **Start**, in that order. In-progress issues without saved metadata still offer Start. Each launch pins `BEADS_DIR` to the main checkout's shared database. The launcher respects the repository's omp worktree configuration; configure `worktree.clone: false` when ignored Beads databases must not be copied. Existing foreign-repository/wrong-branch worktrees are refused.

The launcher labels the core terminal with `beads.project=<canonical .beads path>` and `beads.issue=<full issue ID>`. This explicit association survives app restarts. Sessions started before installation are not guessed from titles. To associate a known existing session manually:

```sh
hc label <terminal-address> beads.project=/absolute/project/.beads beads.issue=<full-issue-id>
```

### Persistent conversations

Both direct and Herdr launches load the explicit startup extension. It obtains the actual persistent session ID and transcript path from omp's lifecycle API, makes the transcript durable, and saves an object in the bead's `metadata.omp_session` with:

- `session_id`: the exact omp session ID, not a guessed newest session or ID prefix.
- `session_file`: the absolute saved transcript path.
- `machine`: a local identity stored in `${XDG_STATE_HOME:-~/.local/state}/go-bd/machine-id`.
- `worktree`: the original absolute worktree directory.

The metadata update merges only this key and preserves other metadata. A fresh task is delivered only after the transcript and metadata are saved; Herdr success also requires the startup acknowledgement. Primary-session switches/forks update the association; subagent sessions do not replace it.

Resume uses `omp --resume <session_file>` with no new startup task and no `--restore`: it reopens the conversation and waits for input rather than automatically restarting interrupted work. The original transcript ID, machine, directory, repository, and worktree branch are checked. Missing transcripts/worktrees, malformed records, foreign-machine records, or conflicting session ownership fail explicitly rather than starting fresh. Recorded history on a non-`in_progress` issue is not automatically replaced or resumed.

The ID is a pointer, not a backup. Keep the transcript and local machine identity; moving to another machine requires deliberate session transfer and association updates. Sessions started before this integration was installed are not retroactively discovered or guessed. The extension lifecycle/storage APIs were exercised with omp 18.17.0.


The dashboard counts actual live agents, not kept shells or finished terminals. Multiple associated sessions remain visible as a count, with the most urgent attention state first. Go to session and the status badge focus that agent.

Session states are independent of issue status:

| Badge | Meaning |
|---|---|
| Working | Agent is working |
| Needs you | Agent requests attention |
| Done · unseen | Agent finished a turn while you were away |
| Idle | Agent is not working; a previously unseen result may have been acknowledged |
| Starting | omp is running but has not reported agent status yet |

`Done · unseen` becomes `Idle` when acknowledged through Herdr. It never means the bead was closed. The launcher does not close issues, merge code, remove worktrees, or push. Follow the repository's merge/close policy.

Per-project launch locks and terminal/dashboard receipts live under `${XDG_STATE_HOME:-~/.local/state}/go-bd`. Concurrent launches are refused rather than duplicated. A failed or unconfirmed launch reports an error and preserves any claimed issue, worktree, and terminal for inspection; retry focuses a live omp session if one exists. Slow launches can exceed Herdr exec's 30-second limit: inspect the associated terminal before retrying.

### Manual completion policy

The approved lifecycle delivery verifies integration and prepares an evidence-backed owner handoff: **Merged — awaiting manual closure**. Both offline local-target and GitHub PR verification remain required. This policy change does not install that workflow; controller, verification, handoff, and UI implementation remain tracked in `herdr-beads-2hv`, `herdr-beads-8vu`, and `herdr-beads-rlm`. The existing launcher does not verify merges or generate closure handoffs.

The owner reviews the current canonical database, full issue ID, ownership/run/submission, target, and merge evidence, then separately executes normal `bd close --reason …` without `--force`. Manual closure retains Beads' existing concurrency limitations: it is not atomically guarded against reopen, reassignment, or a newer run. A Herdr lock or a preceding status check does not remove that race. Observing an externally closed issue is not proof that a particular run owned its closure.

Automatic Bead closure, closure retries, workspace deletion, and destructive registry/terminal cleanup remain disabled, including after manual closure or explicit session release. Workspaces and transcripts stay intact for separate operator review and manual cleanup. The lifecycle delivery must keep retained resources inspectable after their Bead closes. Guarded automation is optional backlog (`herdr-beads-6yd`), not a release prerequisite; no upstream Beads patch or database topology change is required for the manual-completion scope.

## Verification

```sh
cd ~/.local/share/herdr-beads
herdr-app check ~/.config/herdr-app/mods/beads-dashboard
herdr-app check ~/.config/herdr-app/mods/bead-detail
herdr-app check ~/.config/herdr-app/mods/beads-sidebar
node --test model.test.mjs
python3 launcher_test.py
```

The native check only covers the initial render. Also open the dashboard in Herdr, switch between All open and Ready, check Ready against the union of `bd ready --limit 0` and `in_progress` issues, and select two issues to confirm detail-pane reuse and formatted Markdown. Check that blocked, in-progress, and deferred issues remain available in All open and closed issues do not appear as matches. For changes to session launching, also exercise a real worktree agent. Session attention states must be observed in the running app, not inferred from an issue's `in_progress` field.

For list-rendering changes, verify a loaded viewport in the running app, not just an initial-render check: rows must remain visible after refresh and resizing, disclosure arrows must fold/unfold children, and selection/session buttons must still work. The virtual-list callback must only mount retained row views; row handlers belong to those views, and row actions/reconciliation run in the dashboard task. The list height is measured from its viewport.

For navigation, follow parent/dependent/child IDs, including a closed or filtered-out issue under folded ancestors. Verify that the target is highlighted and scrolled into view without inflating filter counts. Use Back/Forward across both list and relationship selections, then go Back and choose a new bead to confirm that Forward becomes unavailable. Re-selecting the current bead must not add a history step.

For persistence changes, use an isolated project to start a real session, remember a unique token, terminate the terminal, and resume. Verify the same session ID and worktree, token recall without resending it, preservation of unrelated metadata, live-session focus without duplication, and visible errors for missing transcripts or a different machine. Exercise both direct and Herdr launches and observe the dashboard's Start → Go to session → Resume transitions.

