# Beads in Herdr

Personal native Herdr mods for browsing project issues and opening issue-specific omp worktree sessions. Shipped Herdr layouts and mods are not modified.

## Install and open

Requires Herdr running locally, `hc`, `bd`, `git`, `omp`, and Python 3 on PATH. Node is only needed for the model regression tests.

```sh
bash integrations/herdr-beads/install.sh
cd /path/to/project
go-bd --dashboard
```

The installer copies `beads-dashboard`, `bead-detail`, and `beads-sidebar` into `${HERDR_APP_HOME:-~/.config/herdr-app}/mods`, installs the personal `beads-workspace` layout, and updates `~/.local/bin/go-bd`. An existing personal layout file is preserved on reinstall. Keep `~/.local/bin` on PATH. `HERDR_APP_HOME` selects a different Herdr window/configuration.

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
- Displays issue ID, title, priority, type, durable Beads status, dependency blocking, and associated live sessions.
- Click an issue to open its details on the right. Further selections reuse the same pane. Details include all nonempty fields returned by `bd show --long`, comments, and dependency identities. Refresh reloads details.
- Search matches title, ID, type, and labels. The blocked filter includes inherited epic blockers, not only stored `status=blocked`.
- Click a disclosure arrow to fold children. Folding and the blocked filter are saved per machine/database.
- Beads queries refresh every ten seconds or via Refresh. A query failure is visible and does not replace the last good snapshot with an empty list.

The mod uses Herdr's `exec` API to run the Beads CLI. It does not read exports, query Dolt directly, send issue data over the network, or maintain a second issue database. Live agent data comes from Herdr reactively. Project commands run on the space's machine; starting a remote session requires the launcher and a working Herdr window on that machine. Local worktree sessions are the verified configuration.

## Start and return to work

```sh
go-bd --herdr <issue-id>       # Same operation as Start in the dashboard
go-bd <issue-id>               # Original direct terminal workflow
```

Start resolves the full issue ID, checks readiness/inherited blockers, claims atomically, and creates or reuses its issue-named omp worktree. A self-owned, unblocked `in_progress` issue can resume after its previous agent ends. Other owners' claims are not overwritten.

A live associated omp session is focused instead of duplicated. If only its old shell remains, a new agent tab is opened in the existing worktree space. Each launch pins `BEADS_DIR` to the main checkout's shared database. The launcher respects the repository's omp worktree configuration; configure `worktree.clone: false` when ignored Beads databases must not be copied. Existing foreign-repository/wrong-branch worktrees are refused.

The launcher labels the core terminal with `beads.project=<canonical .beads path>` and `beads.issue=<full issue ID>`. This explicit association survives app restarts. Sessions started before installation are not guessed from titles. To associate a known existing session manually:

```sh
hc label <terminal-address> beads.project=/absolute/project/.beads beads.issue=<full-issue-id>
```

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

## Verification

```sh
herdr-app check ~/.config/herdr-app/mods/beads-dashboard
herdr-app check ~/.config/herdr-app/mods/bead-detail
herdr-app check ~/.config/herdr-app/mods/beads-sidebar
node --test integrations/herdr-beads/model.test.mjs
```

The native check only covers the initial render. Also open the dashboard in Herdr, select two different issues to confirm detail-pane reuse, toggle dependency blocking, and exercise a real worktree agent. Session attention states must be observed in the running app, not inferred from an issue's `in_progress` field.
