// Workload timeline (§37): "08:00 inference / 09:00 idle / 10:00 GPU" as an hourly row list
// with a tiny inline SVG strip per hour — no chart library (§56, §93). Its own lazy chunk.
import { get, qs } from '@/api/client';
import type { TimelineBucket, TimelineResponse } from '@/api/contracts.gen';
import { useResource } from '@/api/store';
import { EmptyState, HumanErrorCard, Skeleton, fmt } from '@/ui';

const HOURS = 12;

/** Two stacked shares of the hour, 0..100 each; the text beside it carries the same numbers. */
function Strip({ b }: { b: TimelineBucket }) {
  const external = Math.max(0, Math.min(100, b.blerbz_pct ?? 0));
  const ai = Math.max(0, Math.min(100 - external, b.ai_pct ?? 0));
  return (
    <svg class="lz-sys-strip" viewBox="0 0 100 8" preserveAspectRatio="none" aria-hidden="true" focusable="false">
      <rect x="0" y="0" width="100" height="8" rx="2" class="lz-sys-strip-bg" />
      {external > 0 && <rect x="0" y="0" width={external} height="8" class="lz-sys-strip-primary" />}
      {ai > 0 && <rect x={external} y="0" width={ai} height="8" class="lz-sys-strip-ai" />}
    </svg>
  );
}

function shares(b: TimelineBucket, name: string): string {
  const parts: string[] = [];
  if (b.blerbz_pct != null) parts.push(`${name} ${fmt.percent(b.blerbz_pct, { fromPct: true })}`);
  if (b.ai_pct != null) parts.push(`AI ${fmt.percent(b.ai_pct, { fromPct: true })}`);
  if (b.batch_items) parts.push(`${fmt.num(b.batch_items)} batch items`);
  return parts.join(' · ');
}

export function WorkloadTimeline() {
  const tl = useResource<TimelineResponse>(`system/timeline?hours=${HOURS}`, () => get<TimelineResponse>(`/api/system/timeline${qs({ hours: HOURS })}`), {
    maxAgeMs: 60_000,
  });
  if (tl.loading) return <Skeleton lines={4} />;
  if (!tl.data) return tl.error ? <HumanErrorCard error={tl.error} onRetry={() => void tl.refresh()} compact /> : null;
  const { buckets, available, reason, primary_enabled: on, primary_name: pname } = tl.data;
  const shareName = on && pname ? pname : 'GPU';
  if (!available || !buckets.length)
    return (
      <EmptyState
        icon="history"
        title="No history to show yet"
        body={reason || 'The metrics store has no workload history for the last few hours.'}
        action={{ label: 'Check again', onClick: () => void tl.refresh(), icon: 'refresh' }}
        compact
      />
    );
  return (
    <ol class="lz-sys-timeline" aria-label={`Workload over the last ${HOURS} hours`}>
      {buckets.map((b) => (
        <li key={b.start}>
          <time class="num" dateTime={fmt.iso(b.start)}>
            {fmt.clock(b.start)}
          </time>
          <span class="lz-sys-timeline-label">{b.label}</span>
          <Strip b={b} />
          <span class="muted small num">{shares(b, shareName)}</span>
        </li>
      ))}
    </ol>
  );
}
