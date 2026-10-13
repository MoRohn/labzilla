// Home's one status block (§6): health, AI, GPU, memory, agents, jobs as a single definition list —
// the optional primary-workload row appears only when that integration is turned on.
// not a card wall (§4). Every value is the server's; unknowns say "Not reported" instead of a fake 0.
import type { ComponentChildren } from 'preact';
import type { ResourceState, SystemStatus } from '@/api/contracts.gen';
import { Card, FactList, Skeleton, StatusDot, fmt, type FactItem } from '@/ui';

const NOT_REPORTED = <span class="muted">Not reported</span>;

/** "61.2 / 121.7 GB": the total is node-exporter's MemTotal (measured), not the nominal 128 GB. */
export function memoryText(r: ResourceState): string | null {
  const { mem_used_gb: used, mem_total_gb: total } = r;
  if (used == null || total == null) return null;     // half a pair isn't a reading: "Not reported"
  return `${fmt.num(used, 1)} / ${fmt.num(total, 1)} GB`;
}

/** The value itself is the drill-down link (the <dt> already names it, so no extra aria-label). */
function linked(href: string, value: ComponentChildren) {
  return (
    <a href={href} class="lz-home-link">
      {value}
    </a>
  );
}

function facts(s: SystemStatus): FactItem[] {
  const r = s.resource;
  const mem = memoryText(r);
  const waiting = s.jobs.waiting;
  const stale = r.stale ? `Out of date (last update ${fmt.ago(r.updated_at)})` : undefined;
  return [
    // Overall health is the headline above (dot + sentence); repeating it as a row only adds noise.
    {
      label: 'Local AI',
      value: (
        <span class="row">
          <StatusDot health={s.local_ai} />
          {s.local_ai_label}
        </span>
      ),
    },
    { label: 'Primary model', value: s.primary_model ? linked('/models', s.primary_model) : NOT_REPORTED },
    { label: 'Fast model', value: s.fast_model ? linked('/models', s.fast_model) : NOT_REPORTED },
    {
      label: 'DGX GPU load',
      value: r.gpu_util_pct == null ? NOT_REPORTED : linked('/system/compute', fmt.percent(r.gpu_util_pct, { fromPct: true })),
      hint: stale,
    },
    {
      label: 'Unified memory',
      value: mem ? linked('/system/compute', mem) : NOT_REPORTED,
      hint: stale,
    },
    ...(r.primary_enabled
      ? [
          {
            label: r.primary_name || 'Primary workload',
            value: r.blerbz_label ? linked('/system/compute', r.blerbz_label) : NOT_REPORTED,
            hint: [r.blerbz_reason, stale].filter(Boolean).join(' · ') || undefined,
          },
        ]
      : []),
    { label: 'Agents running', value: linked('/agents', fmt.num(s.agents_running)) },
    {
      label: 'Queued jobs',
      value: linked('/jobs', fmt.num(s.jobs.queued)),
      hint: waiting > 0 ? `${waiting} waiting or paused` : undefined,
    },
  ];
}

export interface StatusPanelProps {
  status: SystemStatus | undefined;
}

/** Skeleton until the first snapshot; afterwards the cached snapshot renders instantly and SSE keeps it live. */
export function StatusPanel({ status }: StatusPanelProps) {
  if (!status)
    return (
      <Card as="section" class="lz-home-status">
        <div aria-busy="true">
          <span class="sr-only">Loading system status</span>
          <Skeleton height="28px" width="45%" />
          <div class="lz-home-skeleton">
            {Array.from({ length: 8 }, (_, i) => (
              <Skeleton key={i} height="18px" width={i % 2 ? '70%' : '85%'} />
            ))}
          </div>
        </div>
      </Card>
    );
  return (
    <Card as="section" class="lz-home-status" id="home-status">
      <div class="lz-home-headline">
        <StatusDot health={status.health} pulse={status.health === 'busy'} />
        <h1>{status.headline}</h1>
        <time class="lz-home-updated num muted small" dateTime={fmt.iso(status.updated_at)}>
          {status.updated_at ? `Updated ${fmt.ago(status.updated_at)}` : 'Checking…'}
        </time>
      </div>
      <FactList items={facts(status)} columns={2} />
    </Card>
  );
}
