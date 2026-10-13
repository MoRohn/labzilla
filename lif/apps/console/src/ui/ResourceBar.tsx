import { num } from './format';
import { cx } from './tone';

export type SegmentTone = 'primary' | 'ai' | 'other' | 'free' | 'brand' | 'warning' | 'danger' | 'info' | 'inactive';

export interface ResourceSegment {
  label: string;
  /** In `unit`; null = unknown (rendered in the legend as "—", not drawn). */
  value: number | null;
  tone: SegmentTone;
}

export interface ResourceBarProps {
  /** "Unified memory" */
  label: string;
  segments: ResourceSegment[];
  /** Capacity in `unit` (e.g. 128). */
  total: number;
  unit: string;
  /** Legend under the bar (default true). */
  showLegend?: boolean;
  /** Right-aligned summary (default "<used> / <total> <unit>"). */
  summary?: string;
  class?: string;
}

/** Segmented bar for "Current work: AI Serving 24%, Available 58%" (§36). One bar beats five charts. */
export function ResourceBar({ label, segments, total, unit, showLegend = true, summary, class: cls }: ResourceBarProps) {
  const used = segments.filter((s) => s.tone !== 'free').reduce((a, s) => a + (s.value ?? 0), 0);
  const text = summary ?? `${num(used)} / ${num(total)} ${unit}`;
  const desc = segments.map((s) => `${s.label} ${s.value === null ? 'unknown' : `${num(s.value, 1)} ${unit}`}`).join(', ');
  return (
    <div class={cx('lz-resource', cls)}>
      <div class="row-between">
        <span class="lz-resource-label">{label}</span>
        <span class="lz-resource-summary num">{text}</span>
      </div>
      <div class="lz-resource-track" role="img" aria-label={`${label}: ${text}. ${desc}`}>
        {segments.map((s) =>
          s.value && total > 0 ? <span key={s.label} class={`lz-seg lz-seg-${s.tone}`} style={{ width: `${Math.min(100, (s.value / total) * 100)}%` }} /> : null,
        )}
      </div>
      {showLegend && (
        <ul role="list" class="lz-resource-legend" aria-hidden="true">
          {segments.map((s) => (
            <li key={s.label}>
              <span class={`lz-swatch lz-seg-${s.tone}`} />
              {s.label} <span class="num muted">{s.value === null ? '—' : `${num(s.value, s.value < 10 ? 1 : 0)} ${unit}`}</span>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
