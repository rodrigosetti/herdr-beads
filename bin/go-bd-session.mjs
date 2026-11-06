import { readFileSync, writeFileSync, renameSync, unlinkSync, realpathSync, openSync, readSync, closeSync } from 'node:fs';
import { spawnSync } from 'node:child_process';

// Explicitly loaded by go-bd. No ambient discovery or filesystem session guesses.
export default function (pi) {
  const configPath = process.env.GO_BD_SESSION_CONFIG;
  if (!configPath) throw new Error('go-bd session configuration is missing');
  const config = JSON.parse(readFileSync(configPath, 'utf8'));
  let previous = config.previous;
  let initialized = false;
  let identity;
  let ownedRelease;

  function receipt(value) {
    const temporary = `${config.ack}.${process.pid}.tmp`;
    writeFileSync(temporary, JSON.stringify(value) + '\n', { mode: 0o600 });
    renameSync(temporary, config.ack);
  }

  function bd(args) {
    const result = spawnSync(config.bd, [...args, '--json'], {
      cwd: config.root, env: { ...process.env, BEADS_DIR: config.beads },
      encoding: 'utf8', timeout: 20000,
    });
    if (result.error || result.status !== 0) {
      throw new Error(result.error?.message || result.stderr?.trim() || result.stdout?.trim() || 'bd failed');
    }
    const value = JSON.parse(result.stdout);
    if (value?.error) throw new Error(String(value.error));
    return value;
  }

  function fail(error, ctx) {
    const message = `go-bd: cannot persist mapped session: ${error.message || error}`;
    try { receipt({ error: message }); } catch { /* stderr remains authoritative */ }
    ctx.ui.notify(message, 'error');
    console.error(message);
    // Hook exceptions are isolated by omp. Exit explicitly rather than leave a
    // runnable, unrecorded session or permit a pending startup prompt to run.
    process.exit(1);
  }

  async function persist(ctx) {
    if (ctx.agent.kind !== 'main') return false;
    if (realpathSync(ctx.cwd) !== config.worktree ||
        realpathSync(ctx.sessionManager.getCwd()) !== config.worktree ||
        realpathSync(ctx.sessionManager.getRecordedCwd()) !== config.worktree) {
      throw new Error('the mapped bead cannot follow a session into another working directory');
    }
    const id = ctx.sessionManager.getSessionId();
    const currentFile = ctx.sessionManager.getSessionFile();
    if (identity && previous.session_id === id && previous.session_file === realpathSync(currentFile)) return true;
    if (!initialized && config.previous && id !== config.previous.session_id) {
      throw new Error('omp resumed a different session; the saved record is preserved');
    }
    // The public v18.16.0 API shares this lease with the session writer. Probe
    // before ensureOnDisk: otherwise omp may fork an owned transcript silently.
    const releaseLease = new pi.pi.FileSessionStorage().claimSession(id, currentFile);
    if (!releaseLease) throw new Error(`session ${id} is already open in another omp process`);
    let retained = false;
    try {
      await ctx.sessionManager.ensureOnDisk();
      const sessionFile = ctx.sessionManager.getSessionFile();
      if (!sessionFile || ctx.sessionManager.getSessionId() !== id) {
        throw new Error('session identity changed while making it persistent');
      }
      const record = {
        session_id: id, session_file: realpathSync(sessionFile),
        machine: config.machine, worktree: config.worktree,
      };
      // Read the real journal, not merely an allocated future path.
      const descriptor = openSync(record.session_file, 'r');
      let header;
      try {
        const buffer = Buffer.alloc(65536);
        const count = readSync(descriptor, buffer, 0, buffer.length, 0);
        header = buffer.toString('utf8', 0, count).split('\n')
          .slice(0, 3).map(line => { try { return JSON.parse(line); } catch { return null; } })
          .find(entry => entry?.type === 'session');
      } finally {
        closeSync(descriptor);
      }
      if (header?.id !== id || realpathSync(header.cwd) !== config.worktree) {
        throw new Error('persistent transcript identity/worktree does not match the session');
      }
      const signature = JSON.stringify(record);
      if (identity === signature) return true;
      const rows = bd(['show', config.issue]);
      const issue = rows?.length === 1 ? rows[0] : null;
      if (!issue || issue.id !== config.issue || issue.status !== 'in_progress') {
        throw new Error('mapped bead is no longer in progress');
      }
      const current = issue.metadata?.omp_session;
      if (JSON.stringify(current ?? null) !== JSON.stringify(previous ?? null)) {
        // Compare fields, not object key ordering in bd's JSON serializer.
        if (!current || !previous || Object.keys(current).length !== Object.keys(previous).length ||
            Object.keys(previous).some(key => current[key] !== previous[key])) {
          throw new Error('saved session metadata changed outside this launcher; refusing to overwrite it');
        }
      }
      bd(['update', config.issue, '--metadata', JSON.stringify({ omp_session: record })]);
      const saved = bd(['show', config.issue])[0]?.metadata?.omp_session;
      if (!saved || Object.keys(record).some(key => saved[key] !== record[key])) {
        throw new Error('bd did not save omp_session as an object');
      }
      previous = record;
      identity = signature;
      receipt({ session: record, pid: process.pid });
      // Keep even a clean, idle resumed session leased. ensureOnDisk need not
      // perform a write (and therefore need not claim the manager's lease).
      ownedRelease?.();
      ownedRelease = releaseLease;
      retained = true;
      return true;
    } finally {
      if (!retained) releaseLease();
    }
  }

  pi.on('session_start', async (_event, ctx) => {
    if (ctx.agent.kind !== 'main') return;
    try {
      await persist(ctx);
      if (!initialized) {
        initialized = true;
        if (config.prompt_file) {
          const task = readFileSync(config.prompt_file, 'utf8');
          unlinkSync(config.prompt_file);
          // No task enters omp until the actual transcript and Beads record
          // have both been durably published and acknowledged to the launcher.
          ctx.setTimeout(() => pi.sendUserMessage(task), 0);
        }
      }
    } catch (error) { fail(error, ctx); }
  });
  for (const event of ['session_switch', 'session_branch', 'session_tree', 'before_agent_start']) {
    pi.on(event, async (_event, ctx) => {
      if (ctx.agent.kind !== 'main') return;
      try { await persist(ctx); } catch (error) { fail(error, ctx); }
    });
  }
  pi.on('session_shutdown', (_event, ctx) => {
    if (ctx.agent.kind === 'main') ownedRelease?.();
  });
}
