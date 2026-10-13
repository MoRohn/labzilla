// One prompt or one answer in an Ask thread (§9, §11, §21, §25, §64, §81). Not chat bubbles: the prompt is
// a quiet header, the answer is the content, and the receipt is one understated line —
// "Handled by local/default · Qwen3 4B · CPU · 1.4 sec" — with the route, decision and raw facts on demand.
import { useEffect, useState } from 'preact/hooks';
import { useLocation } from 'preact-iso/router';
import { post, toHumanError } from '@/api/client';
import type { Message, Receipt, Thread, User } from '@/api/contracts.gen';
import { useCopy } from '@/ui/clipboard';
import { Badge, Button, DecisionBadge, Drawer, FactList, type FactItem, HumanErrorCard, Icon, Markdown, PrivacyBadge, RouteTrail, TechDetails, cx, fmt, toast } from '@/ui';
import { fileSize } from './files';
import { useObservable } from '@/api/observable';
import { type AnswerPhase, agentTaskHandoff, phases } from './state';

/** "Run as Agent" is offered only once an installed agent takes a free-form task (§21). Today's agents
 *  (Model Scout, the Evaluator) run fixed model checks, so the button would always end at "no agent fits". */
const AGENTS_TAKE_TEXT = false as boolean;

/** Device is not a Receipt field; show it only when the server put it in tech (never guess CPU/GPU). */
export function receiptDevice(r: Receipt): string | null {
  return r.tech.find((t) => /^(device|runtime|accelerator)$/i.test(t.label.trim()))?.value ?? null;
}

const PRIVACY_TEXT: Record<Receipt['privacy'], string> = { local_only: 'Local only', local_jev: 'Local + Jev', external: 'External model used' };

export function PromptView({ msg }: { msg: Message }) {
  const [open, setOpen] = useState(false);
  const long = msg.content.length > 700 || msg.content.split('\n').length > 12;
  return (
    <div class="ask-prompt">
      <h2 class="sr-only">Prompt</h2>
      <p class={cx('ask-prompt-text', long && !open && 'is-clamped')}>{msg.content}</p>
      {long && (
        <button type="button" class="ask-linkbtn xsmall" aria-expanded={open} onClick={() => setOpen(!open)}>
          {open ? 'Show less' : 'Show the whole prompt'}
        </button>
      )}
      {msg.attachments.length > 0 && (
        <ul role="list" class="ask-prompt-files xsmall muted" aria-label="Files">
          {msg.attachments.map((a, i) => (
            <li key={`${a.name}-${i}`}>
              <Icon name={a.kind === 'image' ? 'image' : 'file'} size={12} /> {a.name} ·{' '}
              {a.kind === 'image' && a.width && a.height ? `${a.width}×${a.height} · ` : ''}
              {fileSize(a.size)} ·{' '}
              {a.kind === 'image' ? (a.included ? 'sent to the vision model · image not kept' : a.note ?? 'not sent') : a.included ? 'text included' : a.note ?? 'not processed'}
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

export interface AnswerViewProps {
  msg: Message;
  thread: Thread;
  /** The prompt this answers (Save to Knowledge, Run as Agent). */
  prompt: string;
  user: User | null | undefined;
  /** Streaming, but started on another device (this tab only follows it). */
  remote: boolean;
  onContinue: () => void;
  onRetry: () => void;
}

/** "Looked up …" with the numbered sources the answer cites as [1], [2]; or why a lookup found nothing. */
export function WebSources({ r }: { r: Receipt }) {
  const w = r.web;
  if (!w) return null;
  if (w.status !== 'ok') {
    const why = w.status === 'blocked' ? 'Not looked up' : 'Couldn’t look this up';
    return (
      <p class="ask-web xsmall muted">
        <Icon name="info" size={12} /> {why}
        {w.note ? ` — ${w.note}` : ''}.
      </p>
    );
  }
  return (
    <section class="ask-web" aria-label="Sources">
      <p class="xsmall muted ask-web-query">
        <Icon name="search" size={12} /> {w.by_guard ? 'Answered again after a live lookup' : 'Looked up'}
        {w.query ? <> “<span class="ask-web-q">{w.query}</span>”</> : null}
        {w.ms != null ? ` · ${fmt.ms(w.ms)}` : ''} · only this query left Labzilla
      </p>
      {w.sources.length > 0 && (
        <ol role="list" class="ask-web-sources xsmall">
          {w.sources.map((s) => (
            <li key={s.url}>
              <span class="ask-web-n num">[{s.n}]</span>{' '}
              <a href={s.url} target="_blank" rel="noopener noreferrer nofollow">
                {s.title || hostOf(s.url)}
              </a>{' '}
              <span class="muted">{hostOf(s.url)}</span>
            </li>
          ))}
        </ol>
      )}
    </section>
  );
}

function hostOf(url: string): string {
  try {
    return new URL(url).hostname.replace(/^www\./, '');
  } catch {
    return '';
  }
}

function Notes({ r }: { r: Receipt }) {
  const notes: { tone: 'warning' | 'info'; title: string; body: string }[] = [];
  if (r.fallback)
    notes.push({
      tone: 'warning',
      title: r.alias === 'local/default' ? 'Default model unavailable' : `${r.role_label || r.alias} model unavailable`,
      body: `Answered by the ${r.model_name ? `fallback (${r.model_name})` : 'fast fallback'}. Responses may be less capable.${r.reason_label ? ` ${r.reason_label}.` : ''}`,
    });
  else if (r.degraded)
    notes.push({
      tone: 'info',
      title: 'Smaller model than this mode prefers',
      body: r.reason_label ?? 'The model meant for this mode isn’t running, so a smaller one answered.',
    });
  if (r.clamped) notes.push({ tone: 'info', title: 'Answer length was capped', body: 'Another workload has the GPU right now, so long answers are cut short.' });
  if (!notes.length) return null;
  return (
    <>
      {notes.map((n) => (
        <p key={n.title} class={cx('ask-note', `lz-tone-${n.tone}`)}>
          <Icon name={n.tone === 'warning' ? 'warning' : 'info'} size={16} />
          <span>
            <strong>{n.title}</strong> — <span class="ask-note-body">{n.body}</span>
          </span>
        </p>
      ))}
    </>
  );
}

function ReceiptLine({ r, onDetails }: { r: Receipt; onDetails: () => void }) {
  const [open, setOpen] = useState(false);
  const device = receiptDevice(r);
  const parts = [r.alias, r.model_name, device, r.latency_ms != null ? fmt.ms(r.latency_ms) : null].filter(Boolean);
  return (
    <div class="ask-receipt">
      <p class="ask-receipt-line xsmall muted num">
        Handled by {parts.join(' · ')}
      </p>
      <div class="ask-receipt-row">
        <RouteTrail steps={r.route} />
        <PrivacyBadge privacy={r.privacy} />
        {r.decision && (
          <button type="button" class="ask-linkbtn xsmall" aria-expanded={open} onClick={() => setOpen(!open)}>
            Why this route?
          </button>
        )}
        <button type="button" class="ask-linkbtn xsmall" onClick={onDetails}>
          Details
        </button>
      </div>
      {open && r.decision && <DecisionBadge decision={r.decision} defaultOpen />}
    </div>
  );
}

function DetailsDrawer({ r, open, onClose }: { r: Receipt; open: boolean; onClose: () => void }) {
  const device = receiptDevice(r);
  const items: (FactItem | null)[] = [
    { label: 'Mode', value: r.role_label ? `${r.role_label} (${r.alias})` : r.alias },
    { label: 'Model', value: r.model_name ?? '—' },
    r.physical_model ? { label: 'Physical model', value: <span class="mono small">{r.physical_model}</span> } : null,
    r.served_by ? { label: 'Served by', value: <span class="mono small">{r.served_by}</span> } : null,
    device ? { label: 'Device', value: device } : null,
    { label: 'Time', value: fmt.ms(r.latency_ms) },
    r.tokens ? { label: 'Tokens', value: `${fmt.num(r.tokens.prompt)} in · ${fmt.num(r.tokens.completion)} out` } : null,
    { label: 'Privacy', value: PRIVACY_TEXT[r.privacy] },
    r.web && r.web.status !== 'not_needed'
      ? { label: 'Web lookup', value: r.web.query ? `“${r.web.query}” (only this query was sent)` : r.web.note ?? r.web.status }
      : null,
    r.fallback || r.degraded ? { label: 'Why not the usual model', value: r.reason_label ?? (r.fallback ? 'Fallback active' : 'Below the quality floor') } : null,
    r.request_id ? { label: 'Request ID', value: <span class="mono small">{r.request_id}</span> } : null,
  ];
  return (
    <Drawer open={open} onClose={onClose} title="Answer details" subtitle="How this request was routed and served">
      <div class="stack">
        <FactList items={items.filter((x): x is FactItem => !!x)} />
        {r.route.length > 0 && (
          <section class="stack-sm">
            <h3 class="section-title">Route</h3>
            <RouteTrail steps={r.route} layout="stacked" />
          </section>
        )}
        {r.decision && <DecisionBadge decision={r.decision} defaultOpen />}
        <TechDetails items={r.tech} inline triggerLabel="Raw values" />
      </div>
    </Drawer>
  );
}

/** "Reading your conversation: 1,700 new tokens (1,200 cached), 40 %, 12 s". Exported for tests. */
export function phaseText(p: AnswerPhase, now: number): string {
  const secs = Math.max(0, Math.round((now - p.since) / 1000));
  const took = secs >= 3 ? ` · ${secs} s` : '';
  if (p.phase === 'waiting') return `Waiting for a free local model…${took}`;
  if (p.total == null) return `Reading your conversation…${took}`;
  const cached = p.cached ?? 0;
  const fresh = Math.max(0, p.total - cached);
  const done = p.processed != null && fresh > 0 ? Math.min(1, Math.max(0, (p.processed - cached) / fresh)) : null;
  const n = (x: number) => x.toLocaleString();
  return (
    `Reading your conversation: ${n(fresh)} new token${fresh === 1 ? '' : 's'}` +
    (cached ? ` (${n(cached)} cached)` : '') +
    (done != null && done > 0 && done < 1 ? `, ${Math.round(done * 100)} %` : '') +
    `…${took}`
  );
}

/** The live pre-answer line, ticking once a second so a long prompt visibly makes progress. */
function PhaseLine({ phase }: { phase: AnswerPhase }) {
  const [now, setNow] = useState(Date.now());
  useEffect(() => {
    const t = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(t);
  }, []);
  return <span class="ask-phase">{phaseText(phase, now)}</span>;
}

export function AnswerView({ msg, thread, prompt, user, remote, onContinue, onRetry }: AnswerViewProps) {
  const { route } = useLocation();
  const [copied, copy] = useCopy();
  const [saving, setSaving] = useState(false);
  const [details, setDetails] = useState(false);
  const r = msg.receipt && msg.receipt.alias ? msg.receipt : null;
  const routeSoFar = msg.receipt?.route ?? [];
  const streaming = msg.status === 'streaming';
  const finished = !streaming && msg.content.length > 0;
  const phase = useObservable(phases).get(thread.id);

  const save = async () => {
    setSaving(true);
    try {
      await post('/api/knowledge/notes', {
        title: prompt.replace(/\s+/g, ' ').trim().slice(0, 80) || thread.title || 'Labzilla answer',
        body: `**Prompt**\n\n${prompt}\n\n**Answer**\n\n${msg.content}`,
        source_thread: thread.id,
      });
      toast({ title: 'Saved to Knowledge', tone: 'success' });
    } catch (e) {
      const err = toHumanError(e);
      toast({ title: err.title, body: err.impact || err.next_step, tone: 'warning' });
    } finally {
      setSaving(false);
    }
  };

  return (
    <article class="ask-answer" aria-busy={streaming || undefined} aria-label="Answer">
      {streaming && !msg.content && (
        <p class="ask-pending small muted">
          <span class="lz-spinner" aria-hidden="true" />
          {routeSoFar.length ? (
            <>
              Answering via <RouteTrail steps={routeSoFar} />
            </>
          ) : (
            'Choosing a local model…'
          )}
          {phase && (
            <>
              <br />
              <PhaseLine phase={phase} />
            </>
          )}
        </p>
      )}
      {streaming && remote && <p class="xsmall muted">Following an answer that started on another device.</p>}
      {msg.content && <Markdown text={msg.content} streaming={streaming} />}
      {msg.status === 'cancelled' && <p class="xsmall muted">Stopped{msg.content ? ' — the answer above is partial.' : ' before an answer arrived.'}</p>}
      {msg.status === 'error' && msg.error && <HumanErrorCard error={msg.error} onRetry={onRetry} onAction={(a) => a === 'details' && r && setDetails(true)} compact={!!msg.content} />}
      {r && <WebSources r={r} />}
      {r && <Notes r={r} />}
      {r && <ReceiptLine r={r} onDetails={() => setDetails(true)} />}
      {!r && routeSoFar.length > 0 && msg.content && (
        <div class="ask-receipt-row">
          <RouteTrail steps={routeSoFar} />
          {msg.receipt && <PrivacyBadge privacy={msg.receipt.privacy} />}
        </div>
      )}

      {finished && (
        <div class="ask-actions" role="group" aria-label="Answer actions">
          <Button size="sm" variant="ghost" icon={copied === 'ok' ? 'check' : 'copy'} onClick={() => copy(msg.content)}>
            {copied === 'ok' ? 'Copied' : copied === 'failed' ? 'Copy failed' : 'Copy'}
          </Button>
          <Button size="sm" variant="ghost" icon="ask" onClick={onContinue}>
            Continue
          </Button>
          {user?.role === 'admin' && (
            <Button size="sm" variant="ghost" icon="save" loading={saving} onClick={() => void save()}>
              Save to Knowledge
            </Button>
          )}
          {AGENTS_TAKE_TEXT && (
            <Button
              size="sm"
              variant="ghost"
              icon="agents"
              onClick={() => {
                agentTaskHandoff.set(prompt);
                route('/agents?run=1');
              }}
            >
              Run as Agent
            </Button>
          )}
          {r && (
            <Button size="sm" variant="ghost" icon="info" onClick={() => setDetails(true)}>
              Open Details
            </Button>
          )}
          <span class="sr-only" aria-live="polite">
            {copied === 'ok' ? 'Answer copied' : copied === 'failed' ? 'Copy failed' : ''}
          </span>
        </div>
      )}
      {r && <DetailsDrawer r={r} open={details} onClose={() => setDetails(false)} />}
      {msg.status === 'error' && !msg.error && <Badge tone="danger">Failed</Badge>}
    </article>
  );
}
