// Home cockpit (§6), below the status block: KPI tiles with 6-hour trends, recent conversations, what needs
// attention, service health and grouped activity. Its own chunk, so the first paint (shell + status block)
// stays inside the initial-JS budget (§92). Data: GET /api/home (Ask stats, threads, availability, value,
// trends; refreshed every 60 s while visible and on `thread` events) plus the live status snapshot.
import type { ComponentChildren } from 'preact';
import { get } from '@/api/client';
import type { HomeOverview, ServiceHealth, SystemStatus, Trend } from '@/api/contracts.gen';
import { useResource } from '@/api/store';
import { Badge, Card, EmptyState, HumanErrorCard, Icon, List, ListItem, Skeleton, StatusDot, cx, fmt, safeHref } from '@/ui';
import { NEEDS_LOOK } from '@/pages/system/shared';
import { Attention, needsAttention } from './Attention';
import { RecentActivity } from './RecentActivity';
import { Sparkline } from './Sparkline';

const KEY = 'home/overview';

function num(v: number | null | undefined, unit: string): string {
  if (v == null) return '—';
  const digits = unit === 'GB' || unit === 's' || unit === 'req/min' ? 1 : 0;
  return `${fmt.num(v, v < 10 && digits === 0 ? 1 : digits)}`;
}

interface TileProps {
  label: string;
  value: ComponentChildren;
  unit?: string;
  sub?: ComponentChildren;
  badge?: ComponentChildren;
  trend?: ComponentChildren;
  href?: string;
}

function Tile({ label, value, unit, sub, badge, trend, href }: TileProps) {
  const body = (
    <>
      <span class="lz-tile-label">{label}</span>
      <span class="lz-tile-value num">
        {value}
        {unit && <span class="lz-tile-unit">{unit}</span>}
      </span>
      {trend}
      {(sub || badge) && (
        <span class="lz-tile-sub">
          {badge}
          {sub && <span class="muted">{sub}</span>}
        </span>
      )}
    </>
  );
  const link = safeHref(href);
  return link ? (
    <a class="lz-tile interactive" href={link}>
      {body}
    </a>
  ) : (
    <div class="lz-tile">{body}</div>
  );
}

function trendTile(t: Trend | undefined, hours: number, fallbackLabel: string, extra: { sub?: ComponentChildren; bad?: 'low' | 'high' } = {}) {
  if (!t) return <Tile label={fallbackLabel} value="—" sub="Trend unavailable" />;
  const breach = t.threshold != null && t.latest != null && ((extra.bad ?? 'low') === 'low' ? t.latest < t.threshold : t.latest > t.threshold);
  return (
    <Tile
      label={t.label}
      value={t.latest == null ? <span class="muted">—</span> : num(t.latest, t.unit)}
      unit={t.latest == null ? undefined : t.unit}
      href={t.href || undefined}
      trend={
        <Sparkline values={t.values} label={`${t.label} over the last ${hours} hours`} unit={t.unit} threshold={t.threshold}
          thresholdLabel={t.threshold_label} bad={extra.bad} />
      }
      badge={breach ? <Badge tone="warning" icon="alert" size="sm">{extra.bad === 'high' ? 'Above' : 'Below'} {t.threshold_label}</Badge> : undefined}
      sub={t.latest == null ? `No data in the last ${hours} h` : extra.sub}
    />
  );
}

function Kpis({ h, workload }: { h: HomeOverview; workload: string }) {
  const tr = Object.fromEntries(h.trends.map((t) => [t.key, t]));
  const a = h.ask;
  const av = h.availability;
  const onTarget = av.last_24h != null && av.target != null ? av.last_24h >= av.target : null;
  const v = h.value;
  return (
    <section aria-labelledby="home-kpi-title" class="stack-sm">
      <div class="row-between">
        <h2 id="home-kpi-title" class="section-title">
          Last 24 hours
        </h2>
        <span class="small muted">Trends: last {h.trends_hours} h{h.trends_note ? ` · ${h.trends_note}` : ''}</span>
      </div>
      <div class="lz-tiles">
        <Tile
          label="Answers in Ask"
          value={fmt.num(a.answers)}
          href="/ask"
          sub={a.median_latency_ms != null ? `Typical ${fmt.ms(a.median_latency_ms)} · p90 ${fmt.ms(a.p90_latency_ms)}` : 'No answers in the last 24 h'}
          badge={a.failed > 0 ? <Badge tone="warning" icon="alert" size="sm">{a.failed} failed</Badge> : undefined}
        />
        {trendTile(tr.decode, h.trends_hours, 'Answer speed', { sub: 'How fast the local model writes' })}
        {trendTile(tr.ttft, h.trends_hours, 'First word (p90)', { sub: 'Wait before an answer starts', bad: 'high' })}
        <Tile
          label="Availability"
          value={av.last_24h == null ? '—' : fmt.percent(av.last_24h, { digits: 1 })}
          href="/system/services"
          badge={
            onTarget == null ? undefined : onTarget ? (
              <Badge tone="success" icon="check" size="sm">On target</Badge>
            ) : (
              <Badge tone="warning" icon="alert" size="sm">Below target</Badge>
            )
          }
          sub={
            av.last_24h == null && av.last_7d == null
              ? `Not measured yet · target ${fmt.percent(av.target)}`
              : `7 days ${fmt.percent(av.last_7d, { digits: 1 })} · target ${fmt.percent(av.target)}`
          }
        />
        {trendTile(tr.mem_free, h.trends_hours, 'Free memory', {
          sub: workload ? `Shared by the GPU, ${workload} and Labzilla` : 'Shared by the GPU and Labzilla',
        })}
        {trendTile(tr.gpu, h.trends_hours, 'DGX GPU load', {
          sub: workload ? `${workload} has priority when it is running` : 'GPU load on this machine',
          bad: 'high',
        })}
        {trendTile(tr.requests, h.trends_hours, 'AI requests', {
          sub: v?.requests != null ? `${fmt.num(v.requests)} in 24 h (incl. health checks)` : 'Includes health checks',
          bad: 'high',
        })}
        <Tile
          label="Value (estimate)"
          value={v?.api_equivalent_usd == null ? '—' : `$${v.api_equivalent_usd.toFixed(2)}`}
          sub={
            v
              ? `What a hosted API would charge${v.llm_avoidance != null ? ` · ${fmt.percent(v.llm_avoidance)} of tasks needed no LLM` : ''}`
              : h.value_note ?? 'Not reported'
          }
        />
      </div>
    </section>
  );
}

function Conversations({ h }: { h: HomeOverview }) {
  return (
    <Card as="section" title="Recent conversations" level={2}
      actions={
        <a href="/ask?focus=1" class="lz-btn lz-btn-secondary lz-btn-sm">
          <Icon name="plus" size={16} />
          <span class="lz-btn-text">
            <span class="lz-btn-label">New</span>
          </span>
        </a>
      }>
      {h.threads.length ? (
        <List aria-label="Recent conversations">
          {h.threads.map((t) => (
            <ListItem
              key={t.id}
              leading={<Icon name="ask" size={18} />}
              title={<span class="truncate">{t.title || 'Untitled'}</span>}
              subtitle={t.preview ? <span class="truncate">{t.preview}</span> : undefined}
              meta={
                t.active ? (
                  <span class="ask-hrow-live">
                    <span class="ask-live-dot" aria-hidden="true" />
                    Answering…
                  </span>
                ) : (
                  fmt.ago(t.updated_at)
                )
              }
              href={`/ask/${encodeURIComponent(t.id)}`}
            />
          ))}
        </List>
      ) : (
        <EmptyState icon="ask" title="No conversations yet" body="Ask anything; answers stay on this machine." action={{ label: 'Ask Labzilla', href: '/ask', icon: 'ask' }} compact />
      )}
    </Card>
  );
}

function Services({ services }: { services: ServiceHealth[] }) {
  const unwell = services.filter((s) => NEEDS_LOOK.has(s.health));
  return (
    <Card as="section" title="Services" level={2}
      actions={
        <a href="/system/services" class="small">
          All services
        </a>
      }>
      {services.length === 0 ? (
        <p class="muted small">Not reported</p>
      ) : (
        <>
          <p class="row small">
            <StatusDot health={unwell.length ? 'degraded' : 'healthy'} />
            {unwell.length
              ? `${unwell.length} of ${services.length} need a look`
              : services.every((s) => s.health === 'healthy')
                ? `All ${services.length} services healthy`
                : 'No services need a look'}
          </p>
          <ul class="lz-svc-grid" aria-label="Service health">
            {services.map((s) => (
              <li key={s.key} class={cx('lz-svc', NEEDS_LOOK.has(s.health) && 'lz-svc-unwell')} title={s.summary}>
                <StatusDot health={s.health} />
                <span class="truncate">{s.name}</span>
              </li>
            ))}
          </ul>
        </>
      )}
    </Card>
  );
}

export default function Cockpit({ status }: { status: SystemStatus }) {
  const home = useResource<HomeOverview>(KEY, () => get<HomeOverview>('/api/home'), {
    pollMs: 60_000,
    refreshOn: ['thread'],
    maxAgeMs: 15_000,
  });
  const h = home.data;
  return (
    <>
      {h ? (
        <Kpis h={h} workload={status.resource.primary_enabled ? status.resource.primary_name || 'Primary workload' : ''} />
      ) : home.error ? (
        <HumanErrorCard error={home.error} onRetry={() => void home.refresh()} compact />
      ) : (
        <div class="lz-tiles" aria-busy="true">
          <span class="sr-only">Loading metrics</span>
          {Array.from({ length: 8 }, (_, i) => (
            <Skeleton key={i} height="112px" radius="var(--radius)" />
          ))}
        </div>
      )}
      {needsAttention(status) && <Attention status={status} />}
      <div class="lz-home-cols">
        <div class="stack">
          {h ? <Conversations h={h} /> : <Skeleton height="220px" radius="var(--radius)" />}
          <RecentActivity />
        </div>
        <div class="stack">
          {!needsAttention(status) && (
            <Card as="section" title="Needs your attention" level={2}>
              <p class="row small">
                <Icon name="check" size={16} class="lz-tone-success" label="All clear" />
                Nothing needs you right now.
              </p>
            </Card>
          )}
          <Services services={status.services} />
        </div>
      </div>
    </>
  );
}
