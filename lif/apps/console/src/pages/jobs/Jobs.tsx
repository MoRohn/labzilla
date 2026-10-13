// Jobs: every kind of background work in one place, grouped by what it is doing (§30), with the
// resource-aware reason in words ("Paused — GPU reserved for the primary workload — resumes
// automatically", §31). Raw scheduler states only appear behind Technical details on the job page.
// Live: the SSE `jobs` event refetches the list; nothing polls.
import { useState } from 'preact/hooks';
import { get, post, toHumanError } from '@/api/client';
import type { Job, JobGroups, JobsResponse, JobsSummary } from '@/api/contracts.gen';
import { can, useMe } from '@/api/session';
import { useResource } from '@/api/store';
import { usePageTitle } from '@/shell/usePageTitle';
import { Button, Card, EmptyState, HumanErrorCard, JobRow, List, Skeleton, Switch, toast } from '@/ui';
import { useJobAction } from './useJobAction';
import './jobs.css';

const JOBS_KEY = 'jobs/list';

const GROUPS: { key: keyof JobGroups; title: string; hint: string }[] = [
  { key: 'running', title: 'Running', hint: 'Working now.' },
  { key: 'queued', title: 'Queued', hint: 'Starts when a slot is free.' },
  { key: 'waiting', title: 'Waiting', hint: 'Held for resources or paused. The reason is on each job.' },
  { key: 'completed', title: 'Completed', hint: 'Finished recently.' },
  { key: 'failed', title: 'Failed', hint: 'Stopped with errors.' },
];

/** Completed work piles up; show the latest few and let the user ask for the rest. */
const COLLAPSE_AFTER = 10;

const jobHref = (job: Job) => `/jobs/${encodeURIComponent(job.id)}`;

function summaryLine(s: JobsSummary): string {
  const parts = [`${s.running} running`, `${s.queued} queued`];
  if (s.waiting) parts.push(`${s.waiting} waiting`);
  parts.push(s.failed_24h ? `${s.failed_24h} failed in the last 24 h` : 'no failures in the last 24 h');
  return parts.join(' · ');
}

function PauseAll({ data, mutate }: { data: JobsResponse; mutate: (fn: (p: JobsResponse | undefined) => JobsResponse) => void }) {
  const [busy, setBusy] = useState(false);
  const paused = data.batch_paused;
  const flip = async (next: boolean) => {
    setBusy(true);
    // Optimistic (safe, reversible action, §60): flip first, put it back if the server refuses; the next
    // revalidation from the server wins either way.
    mutate((p) => ({ ...(p ?? data), batch_paused: next }));
    try {
      await post('/api/jobs/batch/pause-all', { paused: next });
      toast({
        title: next ? 'All batch jobs paused' : 'Batch jobs resumed',
        body: next ? 'Items already running finish; queued work waits until you resume.' : 'Queued work continues when resources allow.',
        tone: 'success',
      });
    } catch (e) {
      // Only undo our own optimistic flip: a refresh that landed meanwhile already holds the server's truth.
      mutate((p) => (p && p.batch_paused !== next ? p : { ...(p ?? data), batch_paused: !next }));
      const err = toHumanError(e);
      toast({ title: err.title, body: err.impact || err.next_step, tone: 'error' });
    } finally {
      setBusy(false);
    }
  };
  return (
    <Switch
      checked={paused}
      busy={busy}
      onChange={(v) => void flip(v)}
      label="Pause all batch jobs"
      description={
        paused
          ? 'On: no batch work starts. Turn off to let queued jobs continue.'
          : 'Stops new batch items from starting. Items already running finish; jobs wait until you turn this off.'
      }
    />
  );
}

function Group({ title, hint, jobs, actions, canControl }: { title: string; hint: string; jobs: Job[]; actions: ReturnType<typeof useJobAction>; canControl: boolean }) {
  const [all, setAll] = useState(false);
  const shown = all ? jobs : jobs.slice(0, COLLAPSE_AFTER);
  return (
    <Card title={`${title} · ${jobs.length}`} subtitle={hint} padded={false}>
      <List aria-label={`${title} jobs`}>
        {shown.map((job) => (
          <JobRow
            key={job.id}
            job={job}
            href={jobHref(job)}
            canControl={canControl}
            onAction={actions.run}
            busyAction={actions.busy?.id === job.id ? actions.busy.action : null}
          />
        ))}
      </List>
      {jobs.length > shown.length && (
        <div class="lz-jobs-more">
          <Button size="sm" variant="ghost" onClick={() => setAll(true)}>
            Show all {jobs.length}
          </Button>
        </div>
      )}
    </Card>
  );
}

export default function Jobs() {
  usePageTitle('Jobs');
  const me = useMe();
  const res = useResource(JOBS_KEY, () => get<JobsResponse>('/api/jobs'), { refreshOn: ['jobs'] });
  const actions = useJobAction();
  const canControl = can(me.data, 'jobs.control');
  const data = res.data;
  const groups = data ? GROUPS.filter((g) => data.groups[g.key].length > 0) : [];

  return (
    <div class="page">
      <header class="page-header">
        <div>
          <h1>Jobs</h1>
          <p class="num">{data ? summaryLine(data.summary) : 'Batch jobs, model checks, downloads and benchmarks.'}</p>
        </div>
      </header>

      {res.error && !data && <HumanErrorCard error={res.error} onRetry={() => void res.refresh()} />}

      {res.loading && (
        <div aria-busy="true" class="stack">
          <Skeleton height="3.5rem" />
          <Skeleton height="1.2rem" lines={4} />
        </div>
      )}

      {data && (
        <>
          {data.note && (
            <p class="lz-jobs-note small" role="status">
              {data.note}
            </p>
          )}

          {canControl ? (
            <Card>
              <PauseAll data={data} mutate={res.mutate} />
            </Card>
          ) : (
            data.batch_paused && (
              <p class="lz-jobs-note small" role="status">
                All batch work is paused by an admin. Queued jobs wait until it is turned back on.
              </p>
            )
          )}

          {groups.length === 0 ? (
            <EmptyState
              icon="jobs"
              title="No background work"
              body="Nothing is running, queued or waiting. Model checks and benchmarks show up here while they run."
              action={{ label: 'Check for better models', href: '/models/discovery', icon: 'scout' }}
            />
          ) : (
            groups.map((g) => <Group key={g.key} title={g.title} hint={g.hint} jobs={data.groups[g.key]} actions={actions} canControl={canControl} />)
          )}
        </>
      )}
      {actions.dialog}
    </div>
  );
}
