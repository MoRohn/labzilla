import type { Job, JobAction } from '@/api/contracts.gen';
import { Badge } from './Badge';
import { Button } from './Button';
import type { IconName } from './Icon';
import { Progress } from './Progress';
import { ago, iso, num } from './format';
import { cx, type Tone } from './tone';

export interface JobRowProps {
  job: Job;
  /** Run a safe job action (pause/resume/cancel/retry). Routine safe actions never confirm (§41); cancel is the parent's call. */
  onAction?: (job: Job, action: JobAction) => void;
  /** The action in flight for this job. */
  busyAction?: JobAction | null;
  /** False hides actions (no jobs.control permission). */
  canControl?: boolean;
  /** Detail link (/jobs/:id). */
  href?: string;
}

const STATUS: Record<Job['status'], { tone: Tone; icon: IconName; word: string }> = {
  running: { tone: 'brand', icon: 'play', word: 'Running' },
  queued: { tone: 'inactive', icon: 'clock', word: 'Queued' },
  waiting: { tone: 'warning', icon: 'busy', word: 'Waiting' },
  paused: { tone: 'inactive', icon: 'paused', word: 'Paused' },
  completed: { tone: 'success', icon: 'success', word: 'Completed' },
  failed: { tone: 'danger', icon: 'error', word: 'Failed' },
  cancelled: { tone: 'inactive', icon: 'block', word: 'Cancelled' },
};

const ACTION: Record<JobAction, { label: string; icon: IconName }> = {
  pause: { label: 'Pause', icon: 'pause' },
  resume: { label: 'Resume', icon: 'play' },
  cancel: { label: 'Cancel', icon: 'x' },
  retry: { label: 'Retry', icon: 'refresh' },
};

/** "Paused — Reason: GPU reserved for the primary workload — Resumes automatically" (§31).
 *  Raw scheduler states stay in tech; the row speaks in status_label + reason. Renders an <li>. */
export function JobRow({ job, onAction, busyAction = null, canControl = true, href }: JobRowProps) {
  const st = STATUS[job.status];
  const active = job.status !== 'completed' && job.status !== 'cancelled' && job.status !== 'failed';
  const when: [string, number] | null = job.finished_at ? ['Finished', job.finished_at] : job.started_at ? ['Started', job.started_at] : null;
  const counts = job.counts;
  return (
    <li class={cx('lz-job', `is-${job.status}`)}>
      <div class="lz-job-main">
        <div class="lz-job-head">
          <span class="lz-job-title">{href ? <a href={href}>{job.title}</a> : job.title}</span>
          <Badge tone={st.tone} size="sm" icon={st.icon}>
            {job.status_label || st.word}
          </Badge>
        </div>
        {(job.reason || (job.resumes_automatically && active)) && (
          <p class="lz-job-reason">
            {job.reason && (
              <>
                <span class="muted">Reason: </span>
                {job.reason}
              </>
            )}
            {job.resumes_automatically && active && (
              <span class="lz-job-auto">
                {job.reason ? ' — ' : ''}Resumes automatically
              </span>
            )}
          </p>
        )}
        {typeof job.progress === 'number' && active && <Progress value={job.progress} label={`${job.title} progress`} size="sm" showValue tone={job.status === 'running' ? 'brand' : 'inactive'} />}
        {(counts || when) && (
          <p class="lz-job-meta muted small num">
            {counts && (
              <span>
                {num(counts.done)} / {num(counts.total)} items
                {counts.failed ? ` · ${num(counts.failed)} failed` : ''}
              </span>
            )}
            {counts && when && ' · '}
            {when && (
              <span>
                {when[0]} <time dateTime={iso(when[1])}>{ago(when[1])}</time>
              </span>
            )}
          </p>
        )}
      </div>
      {canControl && onAction && job.actions.length > 0 && (
        <div class="lz-job-actions">
          {job.actions.map((a) => (
            <Button
              key={a}
              size="sm"
              icon={ACTION[a].icon}
              variant={a === 'cancel' ? 'ghost' : 'secondary'}
              loading={busyAction === a}
              disabled={!!busyAction && busyAction !== a}
              aria-label={`${ACTION[a].label}: ${job.title}`}
              onClick={() => onAction(job, a)}
            >
              {ACTION[a].label}
            </Button>
          ))}
        </div>
      )}
    </li>
  );
}
