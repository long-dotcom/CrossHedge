export type LegMeta = {
  leg_a_venue?: string;
  leg_a_symbol?: string;
  leg_b_venue?: string;
  leg_b_symbol?: string;
  leg_a_venue_symbol?: string;
  mt5_symbol?: string;
};

const venueNames: Record<string, string> = {
  hyperliquid: 'Hyperliquid',
  mt5: 'MT5',
  binance: 'Binance',
  okx: 'OKX',
  bybit: 'Bybit',
  kraken: 'Kraken',
};

export function venueLabel(venue?: string) {
  const normalized = String(venue || '').trim().toLowerCase();
  if (!normalized) return '-';
  return venueNames[normalized] || normalized.toUpperCase();
}

export function venueColor(venue?: string) {
  const normalized = String(venue || '').trim().toLowerCase();
  if (normalized === 'hyperliquid') return 'cyan';
  if (normalized === 'mt5') return 'geekblue';
  if (normalized === 'binance') return 'gold';
  if (normalized === 'okx') return 'purple';
  if (normalized === 'bybit') return 'orange';
  return 'default';
}

export function legMeta(row?: LegMeta | null): Required<Pick<LegMeta, 'leg_a_venue' | 'leg_a_symbol' | 'leg_b_venue' | 'leg_b_symbol'>> {
  const legA = row?.leg_a_venue || 'hyperliquid';
  const legB = row?.leg_b_venue || 'mt5';
  return {
    leg_a_venue: legA,
    leg_a_symbol: row?.leg_a_symbol || row?.leg_a_venue_symbol || '',
    leg_b_venue: legB,
    leg_b_symbol: row?.leg_b_symbol || row?.mt5_symbol || '',
  };
}

export function legTitle(row: LegMeta | null | undefined, leg: 'a' | 'b') {
  const meta = legMeta(row);
  const venue = leg === 'a' ? meta.leg_a_venue : meta.leg_b_venue;
  const symbol = leg === 'a' ? meta.leg_a_symbol : meta.leg_b_symbol;
  return symbol ? `${venueLabel(venue)} ${symbol}` : venueLabel(venue);
}

export function directionLabel(direction?: string, row?: LegMeta | null) {
  const meta = legMeta(row);
  const a = venueLabel(meta.leg_a_venue);
  const b = venueLabel(meta.leg_b_venue);
  if (direction === 'long_leg_a_short_leg_b') return `多 ${a} / 空 ${b}`;
  if (direction === 'long_leg_b_short_leg_a') return `多 ${b} / 空 ${a}`;
  if (direction === 'long_hyperliquid_short_mt5') return '多 Hyperliquid / 空 MT5';
  if (direction === 'long_mt5_short_hyperliquid') return '多 MT5 / 空 Hyperliquid';
  return direction || '-';
}
