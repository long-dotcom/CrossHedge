export function fmtMoney(value?: number) {
  if (value === undefined || value === null || Number.isNaN(value)) return '-';
  return value.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 });
}

export function fmtPct(value?: number) {
  if (value === undefined || value === null || Number.isNaN(value)) return '-';
  return `${(value * 100).toFixed(2)}%`;
}

export function fmtNum(value?: number, digits = 4) {
  if (value === undefined || value === null || Number.isNaN(value)) return '-';
  return Number(value).toFixed(digits);
}

export function fmtAdaptive(value?: number, minDigits = 2, maxDigits = 6) {
  if (value === undefined || value === null || Number.isNaN(value)) return '-';
  const numeric = Number(value);
  if (!Number.isFinite(numeric)) return '-';
  const safeMinDigits = Number.isInteger(minDigits) ? Math.min(Math.max(minDigits, 0), 20) : 2;
  const safeMaxDigits = Number.isInteger(maxDigits) ? Math.min(Math.max(maxDigits, safeMinDigits), 20) : 6;
  const abs = Math.abs(numeric);
  if (abs === 0) return safeMinDigits > 0 ? Number(0).toFixed(safeMinDigits) : '0';
  let digits = safeMinDigits;
  if (abs < 0.01) digits = Math.max(digits, 6);
  else if (abs < 0.1) digits = Math.max(digits, 5);
  else if (abs < 1) digits = Math.max(digits, 4);
  else if (abs < 10) digits = Math.max(digits, 3);
  digits = Math.min(Math.max(digits, safeMinDigits), safeMaxDigits);
  return numeric.toLocaleString(undefined, { minimumFractionDigits: 0, maximumFractionDigits: digits });
}

export function fmtSpread(value?: number) {
  return fmtAdaptive(value, 2, 8);
}

export function fmtCompact(value?: number, maxDigits = 6) {
  if (value === undefined || value === null || Number.isNaN(value)) return '-';
  return Number(value).toLocaleString(undefined, { minimumFractionDigits: 0, maximumFractionDigits: maxDigits });
}

export function parseUtcTime(value?: string) {
  if (!value) return null;
  const normalized = /(?:Z|[+-]\d{2}:\d{2})$/.test(value) ? value : `${value}Z`;
  const date = new Date(normalized);
  return Number.isNaN(date.getTime()) ? null : date;
}

export function fmtLocalTime(value?: string, withSeconds = true) {
  const date = parseUtcTime(value);
  if (!date) return '-';
  return date.toLocaleString(undefined, {
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    second: withSeconds ? '2-digit' : undefined,
    hour12: false
  });
}

export function fmtChartTime(value?: string) {
  const date = parseUtcTime(value);
  if (!date) return '-';
  return date.toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false });
}

export function fmtChartDate(value?: string) {
  const date = parseUtcTime(value);
  if (!date) return '-';
  const month = String(date.getMonth() + 1).padStart(2, '0');
  const day = String(date.getDate()).padStart(2, '0');
  return `${month}-${day}`;
}

export function fmtChartDateTime(value?: string) {
  const date = parseUtcTime(value);
  if (!date) return '-';
  const month = String(date.getMonth() + 1).padStart(2, '0');
  const day = String(date.getDate()).padStart(2, '0');
  const time = date.toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit', hour12: false });
  return `${month}-${day} ${time}`;
}

export function ellipsis(text?: string, max = 18) {
  if (!text) return '-';
  return text.length > max ? `${text.slice(0, max)}...` : text;
}

// -------- 共享标签映射 --------

export const RISK_MODE_MAP: Record<string, { label: string; color: string }> = {
  normal: { label: '正常', color: 'green' },
  reduce_only: { label: '仅减仓', color: 'gold' },
  paused: { label: '暂停', color: 'orange' },
  emergency_stop: { label: '紧急停止', color: 'red' },
};

export function riskModeLabel(mode?: string) {
  return RISK_MODE_MAP[mode || '']?.label || mode || '-';
}

export function riskModeColor(mode?: string) {
  return RISK_MODE_MAP[mode || '']?.color || 'default';
}

export const EXECUTION_MODE_MAP: Record<string, string> = {
  dry_run: '干运行',
  paper: '模拟',
  live: '实盘',
};

export function executionModeLabel(mode?: string) {
  return EXECUTION_MODE_MAP[mode || ''] || mode || '-';
}

export function fmtPnlColor(value?: number) {
  if (value === undefined || value === null || Number.isNaN(value)) return undefined;
  return value >= 0 ? '#16a34a' : '#ef4444';
}

export function fmtPnlSigned(value?: number) {
  if (value === undefined || value === null || Number.isNaN(value)) return '-';
  const prefix = value >= 0 ? '+' : '';
  return `${prefix}${fmtMoney(value)}`;
}
