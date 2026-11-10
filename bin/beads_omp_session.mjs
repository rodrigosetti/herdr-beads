import { readFileSync, writeFileSync, renameSync, unlinkSync, realpathSync, openSync, readSync, closeSync, fsyncSync, statSync, lstatSync } from 'node:fs';
import { dirname, isAbsolute } from 'node:path';
import { createConnection } from 'node:net';
import { randomUUID } from 'node:crypto';
import { isDeepStrictEqual as same } from 'node:util';

const POLICY = 'Agents do not close Beads, delete workspaces, merge, or push. The trusted controller alone records lifecycle transitions after independent verification. This delivery requires separate owner-reviewed manual closure and cleanup; neither handoff nor release is integration evidence.';
const sameFence = (a, b) => a?.runId === b?.runId && a?.attempt === b?.attempt && a?.generation === b?.generation;

// Explicitly loaded by the concrete omp adapter, never ambient discovery.
export default function (pi) {
  let config;
  const configPath = process.env.BEADS_OMP_SESSION_CONFIG;
  try {
    config = JSON.parse(readFileSync(configPath, 'utf8'));
    if (!config.terminal && process.env.HC_TERMINAL_ID) config.terminal = {
      machineId: config.scope?.machineId, address: process.env.HC_TERMINAL_ID, spaceId: null, tabId: null,
    };
    if (config.schemaVersion !== 1 || !config.scope || !config.fence ||
        !config.bindingId || !config.credential || !config.terminal ||
        !['writable', 'read_only'].includes(config.access) ||
        ![config.controllerSocket, config.worktree, config.ack, configPath].every(value => typeof value === 'string' && isAbsolute(value))) {
      throw new Error('controller v1 configuration is required; legacy direct Beads mutation is unsupported');
    }
    for (const path of [configPath, dirname(configPath), dirname(config.controllerSocket)]) {
      const info = statSync(path);
      if (info.uid !== process.getuid() || (info.mode & 0o077)) throw new Error('controller configuration and socket directories must be owner-only');
    }
    if (realpathSync(config.worktree) !== config.worktree) throw new Error('workspace path must be canonical');
  } catch (error) {
    console.error(`herdr-beads: refusing untracked session: ${error.message}`);
    process.exit(1);
  }

  const statePath = `${configPath}.binding.json`;
  const controlPath = `${configPath}.control.json`;
  let previous = config.previous;
  let initialized = false;
  let acknowledged = false;
  let ownedRelease;
  let binding;
  let lastObservation;
  let sequence = 0;
  let access = config.access;
  let frozen = false;
  let released = false;
  let stopped = false;
  let relinquishing = false;
  let controlId;
  let queue = Promise.resolve();
  let pending = 0;
  let actionDeadline;

  // All session/command/timer effects share one sequence and one local owner gate.
  function serial(action) {
    pending++;
    const result = queue.then(async () => {
      actionDeadline = performance.now() + 25000;
      try { return await action(); }
      finally { actionDeadline = undefined; pending--; }
    });
    queue = result.catch(() => {});
    return result;
  }

  function atomic(path, value) {
    const temporary = `${path}.${process.pid}.tmp`;
    const descriptor = openSync(temporary, 'w', 0o600);
    try {
      writeFileSync(descriptor, JSON.stringify(value) + '\n');
      fsyncSync(descriptor);
    } finally { closeSync(descriptor); }
    renameSync(temporary, path);
    const directory = openSync(dirname(path), 'r');
    try { fsyncSync(directory); } finally { closeSync(directory); }
  }

  function save(observation = lastObservation) {
    atomic(statePath, {
      schemaVersion: 1, fence: config.fence, binding, machineId: config.scope.machineId,
      workspacePath: config.worktree, omp: previous, pid: process.pid, sequence,
      observation, access, released, frozen, acknowledged,
    });
  }


  function request(method, params, timeout = 25000) {
    if (actionDeadline !== undefined) timeout = Math.min(timeout, actionDeadline - performance.now());
    if (timeout <= 0) throw new Error('controller acknowledgement deadline exceeded; inspect/reconcile the retained run');
    const info = lstatSync(config.controllerSocket);
    if (!info.isSocket() || info.uid !== process.getuid() || (info.mode & 0o077)) throw new Error('controller socket must be owner-only and owned by this user');
    const requestId = randomUUID();
    const payload = { apiVersion: 1, requestId, scope: config.scope, method, params };
    return new Promise((resolve, reject) => {
      let data = '';
      let finished = false;
      const socket = createConnection(config.controllerSocket);
      const finish = (error, response) => {
        if (finished) return;
        finished = true;
        clearTimeout(timer);
        socket.destroy();
        if (error) reject(error); else resolve(response);
      };
      const timer = setTimeout(() => finish(new Error('controller acknowledgement timed out; inspect/reconcile the retained run before retrying')), timeout);
      socket.on('error', error => finish(error));
      socket.on('connect', () => socket.end(JSON.stringify(payload) + '\n'));
      socket.on('data', chunk => {
        data += chunk.toString('utf8');
        if (Buffer.byteLength(data) > 4 * 1024 * 1024) return finish(new Error('controller response exceeds limit'));
        if (!data.includes('\n')) return;
        try {
          const response = JSON.parse(data.trim());
          if (response.apiVersion !== 1 || response.requestId !== requestId || !['ok', 'accepted'].includes(response.outcome)) {
            throw new Error(response.error?.message || 'invalid controller acknowledgement');
          }
          if (!sameFence(response.run?.fence, config.fence) || response.run.scope?.issueId !== config.scope.issueId ||
              response.run.scope?.beadsDir !== config.scope.beadsDir || !same(response.run.scope?.repository, config.scope.repository) ||
              response.run.scope?.machineId !== config.scope.machineId) {
            throw new Error('controller acknowledgement belongs to another run/scope');
          }
          finish(null, response);
        } catch (error) { finish(error); }
      });
      socket.on('end', () => { if (!finished) finish(new Error('controller disconnected without acknowledgement')); });
    });
  }

  function fatal(error, ctx) {
    frozen = true;
    const message = `herdr-beads: refusing untracked work: ${error.message || error}`;
    try { if (binding) save(); atomic(config.ack, { error: message }); } catch { /* stderr remains authoritative */ }
    ctx.ui.notify(message, 'error');
    console.error(message);
    // omp isolates most hook exceptions; an exception alone would fail open.
    process.exit(1);
  }

  async function observe(event, timeout) {
    const observation = { fence: config.fence, bindingId: config.bindingId,
      eventId: randomUUID(), sequence: ++sequence, observedAt: new Date().toISOString(), event };
    // The validating controller adapter reads this exact durable receipt first.
    lastObservation = observation;
    save();
    const response = await request('observe', { credential: config.credential, observation }, timeout);
    if (response.outcome !== 'ok' || !same(response.run.execution.session, binding)) {
      throw new Error('controller did not durably acknowledge the exact session');
    }
    return response;
  }

  async function persist(ctx) {
    if (ctx.agent.kind !== 'main') return;
    if (realpathSync(ctx.cwd) !== config.worktree ||
        realpathSync(ctx.sessionManager.getCwd()) !== config.worktree ||
        realpathSync(ctx.sessionManager.getRecordedCwd()) !== config.worktree) {
      throw new Error('the mapped Bead cannot follow a session into another working directory');
    }
    const id = ctx.sessionManager.getSessionId();
    const currentFile = ctx.sessionManager.getSessionFile();
    if (binding && (binding.sessionId !== id || realpathSync(currentFile) !== previous.session_file)) {
      throw new Error('primary session switch/fork requires a new controller binding; saved association is preserved');
    }
    if (binding) return;
    if (previous && (previous.session_id !== id || previous.session_file !== realpathSync(currentFile) ||
        previous.machine !== config.scope.machineId || previous.worktree !== config.worktree)) {
      throw new Error('omp resumed a different session; the saved record is preserved');
    }
    const releaseLease = new pi.pi.FileSessionStorage().claimSession(id, currentFile);
    if (!releaseLease) throw new Error(`session ${id} is already open in another omp process`);
    let retained = false;
    try {
      await ctx.sessionManager.ensureOnDisk();
      const sessionFile = ctx.sessionManager.getSessionFile();
      if (!sessionFile || ctx.sessionManager.getSessionId() !== id) throw new Error('session identity changed while making it persistent');
      const record = { session_id: id, session_file: realpathSync(sessionFile), machine: config.scope.machineId, worktree: config.worktree };
      const descriptor = openSync(record.session_file, 'r');
      let header;
      try {
        const buffer = Buffer.alloc(65536);
        const count = readSync(descriptor, buffer, 0, buffer.length, 0);
        header = buffer.toString('utf8', 0, count).split('\n').slice(0, 3)
          .map(line => { try { return JSON.parse(line); } catch { return null; } })
          .find(entry => entry?.type === 'session');
      } finally { closeSync(descriptor); }
      if (header?.id !== id || realpathSync(header.cwd) !== config.worktree) throw new Error('persistent transcript identity/worktree does not match the session');
      previous = record;
      binding = { harness: 'omp', sessionId: id, bindingId: config.bindingId };
      // Controller acknowledgement includes the execution adapter's metadata
      // publication; the extension must not race that authority with bd writes.
      await observe({ kind: 'session_bound', session: binding, terminal: config.terminal, activity: 'active' });
      acknowledged = true;
      save();
      atomic(config.ack, { session: record, pid: process.pid, observation: lastObservation });
      ownedRelease = releaseLease;
      retained = true;
    } finally { if (!retained) releaseLease(); }
  }

  function idle(ctx) {
    const snapshot = ctx.getAsyncJobSnapshot();
    return ctx.isIdle() && !ctx.hasPendingMessages() && snapshot !== null &&
      snapshot.running.length === 0 && snapshot.delivery.queued === 0 &&
      !snapshot.delivery.delivering && snapshot.delivery.pendingJobIds.length === 0;
  }

  async function current() {
    const response = await request('status', { runId: null });
    if (!same(response.run.execution.session, binding) || response.run.disposition !== 'current') throw new Error('session no longer owns the current controller run');
    return response.run;
  }

  const expected = run => ({ fence: run.fence, revision: run.revision });
  function context() {
    return `Bead ${config.scope.issueId}\nCanonical database: ${config.scope.beadsDir}\nRun: ${config.fence.runId}, attempt ${config.fence.attempt}, generation ${config.fence.generation}\nSession access: ${access}${frozen ? ' (fenced)' : ''}\n${POLICY}\n/bead-context shows current controller state. /bead-handoff <JSON HandoffParams without expected> records an exact mode/target/HEAD and complete requiredSubmissions, summary, nextSteps, risks. local-target needs no PR/network; github-pr requires exact provider repository, PR number/URL, expected base ref and submitted head. /bead-release <reason> is an explicit idle-only owner action, not end-of-turn. Trusted rewritten-history receipts are separate operator actions, never agent merge proof.`;
  }

  function ownerContext(run) {
    const closure = run.closure;
    const lines = [
      context(),
      `Closure state: ${closure.state}`,
      `Closure status: ${closure.reason ?? 'none'}`,
      `Observed Beads status: ${run.issueObservation?.status ?? 'unknown'} (assignee: ${run.issueObservation?.assignee ?? 'unknown'}, observed: ${run.issueObservation?.observedAt ?? 'never'})`,
      `Merge coverage: ${run.coverage.state}, revision ${run.coverage.revision}, workspace HEAD ${run.coverage.workspaceHead ?? 'unknown'}`,
      `Cleanup: ${run.cleanup.state}; retention reasons: ${run.cleanup.blockers.join(', ') || 'none'}`,
      `Retention reason: ${run.cleanup.reason ?? 'Automatic cleanup is disabled; resources require separate operator review.'}`,
      `Workspace observation: ${JSON.stringify(run.cleanup.workspaceObservation ?? { state: 'unknown', reason: 'No provider observation recorded; operator review required.', observedAt: null })}`,
      `Canonical scope: ${JSON.stringify(run.scope)}`,
      `Owner and run: ${JSON.stringify({ owner: run.owner, fence: run.fence, revision: run.revision, disposition: run.disposition })}`,
      `Canonical workspace (recorded identity): ${JSON.stringify(run.workspace)}`,
      `Explicit release: ${JSON.stringify(run.release)}`,
      'Automatic cleanup is disabled even after merge, manual closure or explicit release. Release relinquishes ownership; observed closure does not prove this run owned closure. A missing workspace is observed absence only, not cleanup proof. Registrations, transcripts, sessions, spaces, terminals and other resources remain retained. Unknown/unavailable provider observations require operator review.',
      'The operator may separately review current ownership/activity, tracked, untracked and ignored work, stashes, nested repositories and unpreserved commits, then remove resources externally under the ordinary provider workflow. No cleanup command or action is supplied.',
    ];
    const handoff = closure.state === 'awaiting_manual' ? closure.handoff : null;
    if (handoff) {
      lines.push(
        'Manual closure handoff (review only; no execution performed)',
        `Handoff: ${handoff.handoffId}, created ${handoff.createdAt}`,
        `Canonical database: ${handoff.scope.beadsDir}`,
        `Full issue: ${handoff.scope.issueId}`,
        `Expected owner: ${handoff.owner}`,
        `Expected run: ${handoff.fence.runId}, attempt ${handoff.fence.attempt}, generation ${handoff.fence.generation}`,
        `Coverage revision: ${handoff.coverageRevision}; workspace HEAD: ${handoff.workspaceHead}`,
        `Mode: ${handoff.target.mode}`,
        `Target: ${JSON.stringify(handoff.target)}`,
        'Required integrated submissions:',
      );
      for (const submission of handoff.submissions) {
        const identity = submission.identity;
        lines.push(
          `Submission: ${identity.submissionId}, revision ${identity.submissionRevision}, SHA ${identity.head}; state: ${submission.state}`,
          `Submission target: ${JSON.stringify(identity.target)}; pull request: ${JSON.stringify(identity.pullRequest)}`,
          `Evidence: ${JSON.stringify(submission.evidence)}`,
        );
      }
      lines.push(
        `Evidence reason: ${handoff.evidenceReason}`,
        `Warning: ${handoff.warning}`,
        `Review: ${handoff.review}`,
      );
      if (closure.manualCommand) lines.push('Separate owner-reviewed shell command:', '', closure.manualCommand);
    }
    return lines.join('\n');
  }

  async function release(reason, ctx) {
    if (!reason.trim()) throw new Error('usage: /bead-release <reason>');
    if (released || relinquishing || stopped) throw new Error('session is already relinquishing ownership; inspect controller status');
    if (!idle(ctx)) throw new Error('release refused: owned work, queued messages, or async deliveries are active/unknown');
    const run = await current();
    // Recheck after IO, then close the local gate synchronously before requesting.
    if (!idle(ctx)) throw new Error('release refused: work became active during owner validation');
    frozen = true;
    relinquishing = true;
    save();
    await request('release', { expected: expected(run), reason });
    ctx.ui.notify('Release requested; session fenced. Controller acknowledgement is required; resources remain retained.', 'info');
  }

  async function control(ctx) {
    let command;
    try { command = JSON.parse(readFileSync(controlPath, 'utf8')); }
    catch (error) { if (error.code === 'ENOENT') return; throw error; }
    if (command.requestId === controlId) return;
    if (!sameFence(command.fence, config.fence) || command.bindingId !== config.bindingId || !command.requestId) throw new Error('control request belongs to another incarnation');
    if (command.command === 'read_only') {
      access = 'read_only';
      frozen = true;
      save();
      ctx.abort();
      if (!idle(ctx)) return;
      await observe({ kind: 'read_only_acknowledged', session: binding });
    } else if (command.command === 'release') {
      if (!idle(ctx)) {
        ctx.ui.notify('Release refused: owned work is active; controller request remains pending.', 'warning');
        return;
      }
      frozen = true;
      access = 'read_only';
      save();
      const run = await current();
      if (run.release.state !== 'requested' || !idle(ctx)) throw new Error('release requires a current owner request and inactive owned work');
      released = true;
      await observe({ kind: 'release_acknowledged', receipt: { receiptId: command.requestId,
        fence: config.fence, session: binding, releasedAt: new Date().toISOString(), acknowledgedBy: 'omp' } });
      ctx.ui.notify('Controller acknowledged release. No merge, closure, or cleanup was performed.', 'info');
    } else throw new Error('unsupported controller command');
    controlId = command.requestId;
    save();
  }

  pi.on('session_start', async (_event, ctx) => {
    if (ctx.agent.kind !== 'main') return;
    return serial(async () => {
      try {
        await persist(ctx);
        if (initialized) return;
        initialized = true;
        ctx.setInterval(() => serial(async () => {
          if (stopped || released) return;
          try { await control(ctx); } catch (error) { fatal(error, ctx); }
        }), 250);
        if (config.prompt_file) {
          const task = readFileSync(config.prompt_file, 'utf8');
          unlinkSync(config.prompt_file);
          ctx.setTimeout(() => { if (!frozen && !stopped) pi.sendUserMessage(task); }, 0);
        }
      } catch (error) { fatal(error, ctx); }
    });
  });

  for (const event of ['session_before_switch', 'session_before_branch', 'session_before_tree']) {
    pi.on(event, (_event, ctx) => {
      if (ctx.agent.kind !== 'main') return;
      ctx.ui.notify('Mapped Bead session is pinned. Switch/fork/tree requires a new controller binding; operation refused.', 'warning');
      return { cancel: true };
    });
  }
  for (const event of ['session_switch', 'session_branch', 'session_tree']) {
    pi.on(event, (_event, ctx) => {
      if (ctx.agent.kind !== 'main') return;
      return serial(async () => { try { await persist(ctx); } catch (error) { fatal(error, ctx); } });
    });
  }

  pi.on('before_agent_start', (_event, ctx) => serial(async () => {
    if (ctx.agent.kind === 'main') {
      try {
        if (frozen || released || access !== 'writable') throw new Error('session is fenced/read-only; create an explicitly authorized controller incarnation for new work');
        await persist(ctx);
        const run = await current();
        if (stopped || frozen || run.execution.access !== 'writable' || run.release.state !== 'held') throw new Error('controller no longer permits writable work');
        await observe({ kind: 'activity', activity: 'active', turn: 'working' });
      } catch (error) { fatal(error, ctx); }
    }
    return { message: { customType: 'bead-context', content: context(), display: false } };
  }));

  pi.on('tool_call', (_event, ctx) => {
    // Child factories have separate closure state: consult the primary's durable gate.
    let state;
    try { state = JSON.parse(readFileSync(statePath, 'utf8')); }
    catch { return { block: true, reason: 'Bead owner acknowledgement is unavailable' }; }
    if (!state.acknowledged || frozen || released || access !== 'writable' || state.frozen || state.released || state.access !== 'writable' || !sameFence(state.fence, config.fence)) {
      return { block: true, reason: 'Bead session is fenced/read-only; no new owned work is permitted' };
    }
  });

  pi.on('agent_end', (_event, ctx) => {
    if (ctx.agent.kind !== 'main') return;
    return serial(async () => {
      if (frozen || released) return;
      try { await observe({ kind: 'activity', activity: 'active', turn: 'idle' }); }
      catch (error) { fatal(error, ctx); }
    });
  });

  for (const [name, description, action] of [
    ['bead-context', 'Show Bead ownership policy and current controller state', async (_args, ctx) => {
      const run = await current();
      ctx.ui.notify(ownerContext(run), 'info');
    }],
    ['bead-handoff', 'Record exact integration submission: JSON HandoffParams without expected', async (args, ctx) => {
      if (frozen || released || access !== 'writable') throw new Error('handoff refused: session is fenced/read-only');
      const params = JSON.parse(args);
      const fields = ['previousCoverageRevision', 'target', 'workspaceHead', 'requiredSubmissions', 'summary', 'nextSteps', 'risks'];
      if (!params || typeof params !== 'object' || Object.keys(params).length !== fields.length || fields.some(field => !(field in params))) throw new Error('supply complete HandoffParams without expected; see schemas/v1/controller.schema.json');
      const run = await current();
      const response = await request('handoff', { ...params, expected: expected(run) });
      if (response.outcome !== 'ok') throw new Error('handoff not durably acknowledged');
      ctx.ui.notify(`Exact handoff recorded for ${params.target.mode} at ${params.workspaceHead}. This is not merge evidence or release.`, 'info');
    }],
    ['bead-release', 'Explicitly relinquish this idle session; retains all resources', release],
  ]) {
    pi.registerCommand(name, { description, handler: (args, ctx) => serial(async () => {
      if (ctx.agent.kind !== 'main') return ctx.ui.notify('Only the primary Bead session may perform owner commands', 'error');
      try { await action(args, ctx); }
      catch (error) { ctx.ui.notify(`herdr-beads: ${error.message}; inspect controller state before retrying`, 'error'); }
    }) });
  }

  pi.on('session_shutdown', async (_event, ctx) => {
    if (ctx.agent.kind !== 'main') return;
    stopped = true;
    frozen = true;
    // Never queue behind lifecycle IO or wait for merge/release/deletion here.
    // Durable binding remains for adapter reconciliation after a lost notification.
    try {
      if (binding) save();
      if (initialized && !released && pending === 0) await observe({ kind: 'exited', exitCode: null, activity: 'unknown' }, 500);
    } catch (error) { console.error(`herdr-beads: shutdown notification unacknowledged; reconciliation required: ${error.message}`); }
    finally { ownedRelease?.(); }
  });
}
