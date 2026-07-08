import { useEffect, useRef, useState } from 'react';
import type { MutableRefObject } from 'react';
import { useQueryClient } from '@tanstack/react-query';

type StreamSnapshot = {
  spreads?: { total: number; items: any[] };
  opportunities?: { total: number; items: any[] };
  accounts?: any[];
  orders?: { total: number; page: number; page_size: number; items: any[] };
  fills?: { total: number; page: number; page_size: number; items: any[] };
  logs?: { total: number; page: number; page_size: number; items: any[] };
  alerts?: { total: number; page: number; page_size: number; items: any[] };
  risk_status?: any;
  risk_events?: { total: number; page: number; page_size: number; items: any[] };
  lead_lag?: any;
  dashboard_summary?: any;
  equity_curve?: any[];
  hedge_groups?: { total: number; page: number; page_size: number; items: any[] };
  positions?: any[];
  latest_bucket_id?: number;
  pipeline?: any;
};

function parseUtcTimestamp(value?: string) {
  if (!value) return null;
  const normalized = /(?:Z|[+-]\d{2}:\d{2})$/.test(value) ? value : `${value}Z`;
  const timestamp = new Date(normalized).getTime();
  return Number.isFinite(timestamp) ? timestamp : null;
}

export type StreamStatus = {
  online: boolean;
  lastPushSeconds: number | null;
  latencySeconds: number | null;
};

type PageStreamChannel = 'pipeline' | 'hedge-groups' | 'positions' | 'accounts' | 'execution' | 'dashboard' | 'logs' | 'risk' | 'lead-lag';

type PageStreamOptions = {
  page?: number;
  fillPage?: number;
  alertPage?: number;
  pageSize?: number;
  params?: Record<string, string | number | boolean | undefined>;
  cacheKey?: unknown[];
  enabled?: boolean;
};

function applySnapshot(
  data: StreamSnapshot,
  queryClient: ReturnType<typeof useQueryClient>,
  page: number,
  fillPage: number,
  alertPage: number,
  latestBucketId: MutableRefObject<number>,
  cacheKey?: unknown[],
) {
  if (data.spreads) {
    queryClient.setQueriesData({ queryKey: ['spreads'] }, data.spreads);
  }
  if (data.opportunities) {
    queryClient.setQueriesData({ queryKey: ['opportunities'] }, data.opportunities);
  }
  if (data.accounts) {
    queryClient.setQueryData(['accounts'], data.accounts);
  }
  if (data.orders) {
    queryClient.setQueryData(['orders', data.orders.page || page], data.orders);
  }
  if (data.fills) {
    queryClient.setQueryData(['fills', data.fills.page || fillPage], data.fills);
  }
  if (data.dashboard_summary) {
    queryClient.setQueryData(['dashboard-summary'], data.dashboard_summary);
  }
  if (data.equity_curve) {
    queryClient.setQueryData(['equity-curve'], data.equity_curve);
  }
  if (data.logs) {
    queryClient.setQueryData(['logs', data.logs.page || page], data.logs);
  }
  if (data.alerts) {
    queryClient.setQueryData(['alerts', data.alerts.page || alertPage], data.alerts);
  }
  if (data.risk_status) {
    queryClient.setQueryData(['risk-status'], data.risk_status);
  }
  if (data.risk_events) {
    queryClient.setQueryData(['risk-events', data.risk_events.page || page], data.risk_events);
  }
  if (data.lead_lag && cacheKey) {
    queryClient.setQueryData(cacheKey, data.lead_lag);
  }
  if (data.hedge_groups) {
    queryClient.setQueryData(['hedge-groups', data.hedge_groups.page || page], data.hedge_groups);
  }
  if (data.positions) {
    queryClient.setQueryData(['positions'], data.positions);
  }
  if (data.pipeline) {
    queryClient.setQueryData(['pipeline-diagnostics'], data.pipeline);
  }
  if (data.latest_bucket_id && data.latest_bucket_id !== latestBucketId.current) {
    latestBucketId.current = data.latest_bucket_id;
    queryClient.invalidateQueries({ queryKey: ['spread-analytics'] });
  }
}

async function readSnapshots(response: Response, onSnapshot: (data: StreamSnapshot) => void) {
  if (!response.body) throw new Error('SSE 响应没有可读流');
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = '';
  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    let separator = buffer.indexOf('\n\n');
    while (separator >= 0) {
      const rawEvent = buffer.slice(0, separator);
      buffer = buffer.slice(separator + 2);
      const data = rawEvent
        .split(/\r?\n/)
        .filter((line) => line.startsWith('data:'))
        .map((line) => line.slice(5).trimStart())
        .join('\n');
      if (data) onSnapshot(JSON.parse(data) as StreamSnapshot);
      separator = buffer.indexOf('\n\n');
    }
  }
}

function wait(ms: number, signal: AbortSignal) {
  return new Promise<void>((resolve) => {
    const timer = window.setTimeout(resolve, ms);
    signal.addEventListener('abort', () => {
      window.clearTimeout(timer);
      resolve();
    }, { once: true });
  });
}

export function usePageStream(channel: PageStreamChannel, options: PageStreamOptions = {}) {
  const queryClient = useQueryClient();
  const latestBucketId = useRef<number>(0);
  const lastPushAt = useRef<number | null>(null);
  const [status, setStatus] = useState<StreamStatus>({ online: false, lastPushSeconds: null, latencySeconds: null });
  const page = options.page ?? 1;
  const fillPage = options.fillPage ?? 1;
  const alertPage = options.alertPage ?? 1;
  const pageSize = options.pageSize ?? 20;
  const paramsKey = JSON.stringify(options.params || {});
  const cacheKeyKey = JSON.stringify(options.cacheKey || []);
  const enabled = options.enabled ?? true;

  useEffect(() => {
    if (!enabled) return undefined;
    const token = localStorage.getItem('token');
    if (!token) return undefined;

    const params = new URLSearchParams({
      channel,
      page: String(page),
      page_size: String(pageSize),
      fill_page: String(fillPage),
      alert_page: String(alertPage),
    });
    Object.entries(options.params || {}).forEach(([key, value]) => {
      if (value !== undefined) params.set(key, String(value));
    });
    const controller = new AbortController();
    const heartbeat = window.setInterval(() => {
      if (!lastPushAt.current) return;
      setStatus((current) => ({ ...current, lastPushSeconds: (Date.now() - lastPushAt.current!) / 1000 }));
    }, 1000);

    const connect = async () => {
      while (!controller.signal.aborted) {
        try {
          const response = await fetch(`/api/stream?${params.toString()}`, {
            headers: { Authorization: `Bearer ${token}` },
            signal: controller.signal,
          });
          if (response.status === 401) {
            localStorage.removeItem('token');
            localStorage.removeItem('user');
            if (location.pathname !== '/login') location.href = '/login';
            return;
          }
          if (!response.ok) throw new Error(`SSE 请求失败: ${response.status}`);
          await readSnapshots(response, (data) => {
            const receivedAt = Date.now();
            applySnapshot(data, queryClient, page, fillPage, alertPage, latestBucketId, options.cacheKey);
            const generatedAt = parseUtcTimestamp(data.pipeline?.generated_at) ?? receivedAt;
            lastPushAt.current = receivedAt;
            setStatus({
              online: true,
              lastPushSeconds: 0,
              latencySeconds: Number.isFinite(generatedAt) ? Math.max((receivedAt - generatedAt) / 1000, 0) : null,
            });
          });
        } catch {
          if (controller.signal.aborted) return;
          setStatus((current) => ({ ...current, online: false }));
          await wait(2000, controller.signal);
        }
      }
    };
    void connect();

    return () => {
      controller.abort();
      window.clearInterval(heartbeat);
      setStatus({ online: false, lastPushSeconds: null, latencySeconds: null });
    };
  }, [channel, page, fillPage, alertPage, pageSize, paramsKey, cacheKeyKey, enabled, queryClient]);

  return status;
}
