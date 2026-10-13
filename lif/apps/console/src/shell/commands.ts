// Command palette entries (spec §50) and the prompt hand-off from the command bar to Ask.
import type { PromptSuggestion } from '@/api/contracts.gen';
import { observable } from '@/api/observable';
import type { IconName } from '@/ui/Icon';

export interface PaletteCommand {
  id: string;
  label: string;
  hint: string;
  icon: IconName;
  /**
   * navigate → go to href; command → resolve `text` through /api/command (so actions still show their
   * impact first); action → a purely local effect (theme, install, sign out) run by `run`.
   */
  kind: 'navigate' | 'command' | 'action';
  href?: string;
  text?: string;
  run?: () => void;
  keywords?: string;
}

export const PALETTE_COMMANDS: PaletteCommand[] = [
  { id: 'ask', label: 'Ask Labzilla', hint: 'Send a prompt to local AI', icon: 'ask', kind: 'navigate', href: '/ask', keywords: 'prompt chat question' },
  { id: 'run-agent', label: 'Run Agent', hint: 'Start Model Scout or the Evaluator', icon: 'agents', kind: 'navigate', href: '/agents?run=1', keywords: 'agent start' },
  { id: 'search-knowledge', label: 'Search Knowledge', hint: 'Decisions, evidence, assumptions', icon: 'knowledge', kind: 'navigate', href: '/knowledge?search=1', keywords: 'why decision find' },
  { id: 'check-models', label: 'Check Models', hint: 'Look for better models on Hugging Face', icon: 'scout', kind: 'navigate', href: '/models/discovery', keywords: 'discovery upgrade hugging face' },
  { id: 'open-gpu', label: 'Open GPU', hint: 'Compute, memory and GPU load', icon: 'gpu', kind: 'navigate', href: '/system/compute', keywords: 'gpu memory compute dgx primary workload' },
  { id: 'pause-batch', label: 'Pause Batch', hint: 'Pause all batch jobs (shows the impact first)', icon: 'pause', kind: 'command', text: 'Pause batch jobs', keywords: 'stop batch jobs' },
  { id: 'open-settings', label: 'Open Settings', hint: 'Maintenance mode and automation', icon: 'settings', kind: 'navigate', href: '/system/settings', keywords: 'settings maintenance automation' },
  { id: 'connect', label: 'Connect a phone', hint: 'Show a QR code to pair a phone or tablet', icon: 'qr', kind: 'navigate', href: '/connect', keywords: 'phone pair qr device' },
  { id: 'logs', label: 'Open Logs', hint: 'Errors, warnings and relevant events', icon: 'file', kind: 'navigate', href: '/system/logs', keywords: 'logs errors' },
  { id: 'services', label: 'Open Services', hint: 'Health of every platform service', icon: 'server', kind: 'navigate', href: '/system/services', keywords: 'k3s kubernetes services health pods' },
  { id: 'trust', label: 'Trust this device', hint: 'Secure connection, install and voice', icon: 'shield', kind: 'navigate', href: '/trust', keywords: 'https certificate tls install pwa voice' },
  { id: 'decisions', label: 'Open Decisions', hint: 'Jev decisions and reviews', icon: 'decision', kind: 'navigate', href: '/agents?tab=decisions', keywords: 'jev decision fabric review approvals' },
];

/**
 * Prompt handed from the command bar / mobile gateway to Ask without putting the text in the URL
 * (history, logs). Ask reads it once on mount and clears it.
 */
export const promptHandoff = observable<PromptSuggestion | null>(null);

/**
 * Unsent text handed to Ask's composer (the Mobile Gateway prompt is one line; pasted multi-line code
 * goes here so its line breaks survive and the person can finish the message before sending).
 */
export const promptDraft = observable<string | null>(null);

/** Ask the command bar to resolve `text` (palette entries like "Pause Batch", free text from the palette). */
export const commandRequest = observable<{ text: string; n: number } | null>(null);
let n = 0;
export function runCommand(text: string): void {
  commandRequest.set({ text, n: ++n });
}
