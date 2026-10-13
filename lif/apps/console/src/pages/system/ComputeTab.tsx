// Compute (§36): "DGX Spark / GPU 42% / Unified memory 61/122 GB / Temperature / Current work" as one
// panel with one segmented bar — direct numbers over five charts (§56). Unknowns stay unknown.
import { get } from '@/api/client';
import type { BlerbzState, ComputeView, ResourceState, Temperature, WorkKey } from '@/api/contracts.gen';
import { useResource } from '@/api/store';
import {
  Card,
  FactList,
  HumanErrorCard,
  ResourceBar,
  Skeleton,
  StatusBadge,
  TechDetails,
  fmt,
  type ResourceSegment,
  type SegmentTone,
} from '@/ui';
import { useLazy } from './shared';

const SEGMENT: Record<WorkKey, SegmentTone> = { blerbz: 'primary', ai_serving: 'ai', other: 'other', available: 'free' };

const TEMPERATURE: Record<Temperature, string> = { normal: 'Normal', warm: 'Warm', hot: 'Hot', unknown: 'Not reported' };

/** What an optional external workload's state means for Labzilla. The raw scheduler reason stays in tech. */
function workloadMeans(name: string, s: BlerbzState): string {
  const n = name || 'Primary workload';
  switch (s) {
    case 'idle':
      return `${n} is quiet, so Labzilla can use the spare capacity.`;
    case 'busy':
      return `${n} has GPU priority. Labzilla steps back while it runs: answers may be shorter and background work waits.`;
    case 'imminent':
      return `${n} is about to need the GPU. Labzilla is stepping back so it can start without waiting.`;
    case 'reserved':
      return `GPU memory is held for ${n}. Background AI work waits until it is released.`;
    default:
      return `The GPU scheduler isn't reporting, so Labzilla runs in fail-safe mode and assumes ${n} may need the GPU.`;
  }
}

function blerbzHealth(s: BlerbzState) {
  return s === 'idle' ? 'healthy' : s === 'unknown' ? 'unknown' : 'busy';
}

function memory(r: ResourceState): string {
  if (r.mem_used_gb == null && r.mem_total_gb == null) return 'Not reported';
  return `${fmt.num(r.mem_used_gb, 1)} / ${fmt.num(r.mem_total_gb, 1)} GB`;
}

function TimelineSkeleton() {
  return (
    <div aria-busy="true" class="stack-sm">
      <span class="sr-only">Loading workload timeline</span>
      <Skeleton lines={4} />
    </div>
  );
}

export function ComputeTab() {
  const compute = useResource<ComputeView>('system/compute', () => get<ComputeView>('/api/system/compute'), { refreshOn: ['status'] });
  // The timeline is the only chart-like view: its own chunk, loaded when Compute is opened (§93).
  const Timeline = useLazy(() => import('./WorkloadTimeline').then((m) => m.WorkloadTimeline));

  if (compute.loading)
    return (
      <Card>
        <div aria-busy="true" class="stack-sm">
          <span class="sr-only">Loading compute</span>
          <Skeleton height="24px" width="30%" />
          <Skeleton lines={4} />
          <Skeleton height="14px" />
        </div>
      </Card>
    );
  if (!compute.data) return compute.error ? <HumanErrorCard error={compute.error} onRetry={() => void compute.refresh()} /> : null;

  const { device, resource: r, work, note, tech } = compute.data;
  const segments: ResourceSegment[] = work.map((w) => ({ label: w.label, value: w.gb ?? null, tone: SEGMENT[w.key] }));
  const total = r.mem_total_gb ?? null;
  const share = work.filter((w) => w.pct != null);

  return (
    <div class="stack">
      <Card
        title={device || 'DGX Spark'}
        subtitle={r.stale ? `Out of date: last update ${fmt.ago(r.updated_at)}` : `Updated ${fmt.ago(r.updated_at)}`}
        actions={<TechDetails items={[...r.tech, ...tech]} title="Compute: technical details" />}
      >
        <div class="stack">
          <FactList
            columns={2}
            items={[
              {
                label: 'GPU load',
                value: r.gpu_util_pct == null ? <span class="muted">Not reported</span> : fmt.percent(r.gpu_util_pct, { fromPct: true }),
                hint: r.gpu_util_pct == null ? 'The GPU scheduler is not reporting utilisation right now.' : undefined,
              },
              {
                label: 'Temperature',
                value: r.temperature === 'unknown' ? <span class="muted">{TEMPERATURE.unknown}</span> : TEMPERATURE[r.temperature],
                hint: r.temperature_note || undefined,
              },
            ]}
          />
          {total != null && segments.some((s) => s.value != null) ? (
            <ResourceBar label="Unified memory" segments={segments} total={total} unit="GB" summary={memory(r)} />
          ) : (
            <p class="muted small">The memory split isn't available right now{note ? `: ${note}` : '.'}</p>
          )}
          {share.length > 0 && (
            <section aria-labelledby="sys-current-work">
              <h3 id="sys-current-work" class="section-title">
                Current work
              </h3>
              <ul role="list" class="lz-sys-work">
                {share.map((w) => (
                  <li key={w.key}>
                    <span class={`lz-swatch lz-seg-${SEGMENT[w.key]}`} aria-hidden="true" />
                    {w.label} <strong class="num">{fmt.percent(w.pct, { fromPct: true })}</strong>
                  </li>
                ))}
              </ul>
            </section>
          )}
          {note && total != null && <p class="muted small">{note}</p>}
        </div>
      </Card>

      {r.primary_enabled && (
        <Card title={r.primary_name || 'Primary workload'} actions={<StatusBadge health={blerbzHealth(r.blerbz)} label={r.blerbz_label || undefined} size="sm" />}>
          <div class="stack-sm">
            {r.blerbz_reason && <p>{r.blerbz_reason}</p>}
            <p class="muted">{workloadMeans(r.primary_name, r.blerbz)}</p>
            <p class="muted small">{r.primary_name || 'The primary workload'} has priority on the GPU while this integration is on. Labzilla borrows only what it leaves free.</p>
          </div>
        </Card>
      )}

      <Card title="Workload timeline" subtitle="What the DGX did, hour by hour">
        {Timeline ? <Timeline /> : <TimelineSkeleton />}
      </Card>
    </div>
  );
}
