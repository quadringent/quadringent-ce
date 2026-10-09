/** Keep measured precision; abbreviations belong only on chart axes. */
const THIN = ' ';

export function sequences(value: number): string {
  const sign = value < 0 ? '-' : '';
  const digits = Math.abs(Math.trunc(value)).toString();
  let out = '';
  for (let i = 0; i < digits.length; i += 1) {
    if (i > 0 && (digits.length - i) % 3 === 0) out += ',';
    out += digits[i];
  }
  return sign + out;
}

export function axisTick(value: number): string {
  if (value === 0) return '0';
  if (Math.abs(value) < 1000) return sequences(value);
  if (Math.abs(value) < 1_000_000) return sequences(Math.round(value / 1000)) + THIN + 'k';
  return (value / 1_000_000).toLocaleString('en-US', { maximumFractionDigits: 1 }) + THIN + 'M';
}

export function decimal(value: number, digits: number): string {
  return value.toLocaleString('en-US', {
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
  });
}

export function exact(value: number, maxDigits = 6): string {
  return value.toLocaleString('en-US', {
    minimumFractionDigits: 0,
    maximumFractionDigits: maxDigits,
  });
}

/** One duration format across the product; retain seconds below one hour. */
export function formatDuration(totalSeconds: number): string {
  const s = Math.round(totalSeconds);
  if (s < 60) return sequences(s) + THIN + 's';
  const minutes = Math.floor(s / 60);
  const restSeconds = s % 60;
  if (minutes < 60) {
    return restSeconds
      ? minutes + THIN + 'min' + THIN + pad(restSeconds) + THIN + 's'
      : minutes + THIN + 'min';
  }
  const hours = Math.floor(minutes / 60);
  const restMinutes = minutes % 60;
  if (hours < 48) {
    return restMinutes ? hours + THIN + 'h' + THIN + pad(restMinutes) : hours + THIN + 'h';
  }
  return Math.round(hours / 24) + THIN + 'd';
}

function pad(value: number): string {
  return value < 10 ? '0' + value : String(value);
}

export interface Age {
  readonly seconds: number;
  readonly label: string;
  readonly stale: boolean;
}

export function age(observedAt: string, now: Date, staleAfterS = 120): Age {
  const seconds = Math.max(0, (now.getTime() - new Date(observedAt).getTime()) / 1000);
  return {
    seconds,
    label: seconds < 5 ? 'just now' : formatDuration(seconds) + ' ago',
    stale: seconds > staleAfterS,
  };
}

/** UTC and an unambiguous date keep evidence independent of viewer locale. */
export function timestamp(iso: string): string {
  return new Date(iso).toISOString().replace('T', ' ').replace(/\.\d{3}Z$/, ' UTC');
}

export function position(receiver: string, sequence: number): string {
  return receiver + ' · ' + sequences(sequence);
}
