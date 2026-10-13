// Home's activity list (§6): the latest human-readable events, with runs of the same event folded into one
// row ("A decision review was answered · 8×"). Seeded from the relevant-events feed (/api/system/logs) and
// kept live by SSE `activity` events, so nothing polls (§61).
import { get, qs } from '@/api/client';
import type { ActivityEvent, LogsResponse } from '@/api/contracts.gen';
import { useEvent } from '@/api/sse';
import { useResource } from '@/api/store';
import { ActivityRow, EmptyState, HumanErrorCard, List, Skeleton } from '@/ui';

const SHOWN = 10;
const FETCHED = 80;            // enough raw events to fill SHOWN rows after folding repeats
const FOLD_SEC = 6 * 3600;     // repeats further apart than this stay separate rows
const KEY = 'home/activity';

export interface FoldedEvent {
  event: ActivityEvent;        // the newest of the run
  count: number;
  oldest: number;
}

/** Fold consecutive events with the same title and category (newest first) into one row with a count. */
export function fold(events: ActivityEvent[], limit = SHOWN): FoldedEvent[] {
  const out: FoldedEvent[] = [];
  for (const e of events) {
    const last = out[out.length - 1];
    if (last && last.event.title === e.title && last.event.category === e.category && last.oldest - e.ts <= FOLD_SEC) {
      last.count += 1;
      last.oldest = e.ts;
    } else {
      if (out.length === limit) break;
      out.push({ event: e, count: 1, oldest: e.ts });
    }
  }
  return out;
}

export function RecentActivity() {
  const feed = useResource<ActivityEvent[]>(KEY, async () => {
    const res = await get<LogsResponse>(`/api/system/logs${qs({ level: 'all', limit: FETCHED })}`);
    return res.events.slice(0, FETCHED);
  });
  useEvent('activity', (ev) =>
    feed.mutate((prev) => [ev, ...(prev ?? []).filter((e) => e.id !== ev.id)].sort((a, b) => b.ts - a.ts).slice(0, FETCHED)),
  );

  return (
    <section class="stack-sm" aria-labelledby="home-activity-title">
      <div class="row-between">
        <h2 id="home-activity-title" class="section-title">
          Recent activity
        </h2>
        <a href="/system/logs" class="small">
          All activity
        </a>
      </div>
      {feed.loading ? (
        <div aria-busy="true" class="stack-sm">
          <span class="sr-only">Loading recent activity</span>
          {Array.from({ length: 4 }, (_, i) => (
            <Skeleton key={i} height="20px" width={`${80 - i * 10}%`} />
          ))}
        </div>
      ) : feed.error ? (
        <HumanErrorCard error={feed.error} onRetry={() => void feed.refresh()} compact />
      ) : feed.data?.length ? (
        <List aria-label="Recent activity">
          {fold(feed.data).map(({ event, count }) => (
            <ActivityRow key={event.id} event={count > 1 ? { ...event, title: `${event.title} · ${count}×` } : event} compact />
          ))}
        </List>
      ) : (
        <EmptyState
          icon="history"
          title="Nothing has happened yet"
          body="Model refreshes and finished agents show up here as they happen."
          action={{ label: 'Ask Labzilla', href: '/ask', icon: 'ask' }}
          compact
        />
      )}
    </section>
  );
}
