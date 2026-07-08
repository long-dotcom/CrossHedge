import { useQuery } from '@tanstack/react-query';
import { Card, Empty, Form, InputNumber, Select, Space, Table, Tag } from 'antd';
import type { ColumnsType } from 'antd/es/table';
import ReactECharts from 'echarts-for-react';
import { useMemo, useState } from 'react';
import { api } from '../api/client';
import { EllipsisCell } from '../components/EllipsisCell';
import { useHeaderStreamStatus } from '../components/HeaderStreamStatus';
import { usePageStream } from '../hooks/useLiveStream';
import { fmtAdaptive, fmtChartTime, fmtLocalTime } from '../utils/format';
import { legMeta, venueColor, venueLabel } from '../utils/venues';

function fmtMs(value?: number | null) {
  if (value === undefined || value === null || Number.isNaN(value)) return '-';
  return `${value.toFixed(0)} ms`;
}

function fmtPct(value?: number | null) {
  if (value === undefined || value === null || Number.isNaN(value)) return '-';
  return `${(value * 100).toFixed(1)}%`;
}

function fmtMid(value?: number | null) {
  if (value === undefined || value === null || Number.isNaN(value) || value === 0) return '-';
  return value.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 6 });
}

function DirectionPanel({
  title,
  leader,
  follower,
  data,
}: {
  title: string;
  leader: string;
  follower: string;
  data: any;
}) {
  return (
    <div className="leadlag-direction-card">
      <div className="leadlag-direction-head">
        <strong>{title}</strong>
        <span><Tag color={venueColor(leader)}>{venueLabel(leader)}</Tag><i /> <Tag color={venueColor(follower)}>{venueLabel(follower)}</Tag></span>
      </div>
      <div className="leadlag-direction-metrics">
        <div><span>平均滞后</span><strong>{fmtMs(data.avg_lag_ms)}</strong></div>
        <div><span>P90 滞后</span><strong>{fmtMs(data.p90_lag_ms)}</strong></div>
        <div><span>跟随率</span><strong>{fmtPct(data.follow_rate)}</strong></div>
        <div><span>样本数</span><strong>{data.count ?? '-'}</strong></div>
      </div>
    </div>
  );
}

export function LeadLagPage() {
  const symbols = useQuery({ queryKey: ['symbols'], queryFn: async () => (await api.get('/markets/symbols')).data });
  const [symbol, setSymbol] = useState<string>('');
  const [windowSeconds, setWindowSeconds] = useState(300);
  const [thresholdBps, setThresholdBps] = useState(3);
  const [minMove, setMinMove] = useState(0);
  const [maxLagMs, setMaxLagMs] = useState(2000);
  const activeSymbol = symbol || symbols.data?.[0]?.symbol || '';
  const leadLagQueryKey = ['lead-lag', activeSymbol, windowSeconds, thresholdBps, minMove, maxLagMs];
  const streamStatus = usePageStream('lead-lag', {
    params: {
      symbol: activeSymbol,
      window_seconds: windowSeconds,
      threshold_bps: thresholdBps,
      min_move: minMove,
      max_lag_ms: maxLagMs,
    },
    cacheKey: leadLagQueryKey,
    enabled: Boolean(activeSymbol),
  });
  useHeaderStreamStatus(streamStatus.online);
  const query = useQuery({
    queryKey: leadLagQueryKey,
    enabled: Boolean(activeSymbol),
    queryFn: async () => (await api.get('/analytics/lead-lag', { params: { symbol: activeSymbol, window_seconds: windowSeconds, threshold_bps: thresholdBps, min_move: minMove, max_lag_ms: maxLagMs } })).data
  });
  const series = query.data?.series || [];
  const summary = query.data?.summary || {};
  const latest = query.data?.latest || {};
  const activeMapping = query.data || (symbols.data || []).find((row: any) => row.symbol === activeSymbol);
  const meta = legMeta(activeMapping);
  const legALabel = venueLabel(meta.leg_a_venue);
  const legBLabel = venueLabel(meta.leg_b_venue);
  const option = useMemo(() => {
    const times = series.map((row: any) => fmtChartTime(row.time));
    const hlBase = series.find((row: any) => row.leg_a_mid)?.leg_a_mid || 1;
    const mt5Base = series.find((row: any) => row.leg_b_mid)?.leg_b_mid || 1;
    return {
      tooltip: { trigger: 'axis' },
      legend: { top: 0, data: [`${legALabel} 标准化`, `${legBLabel} 标准化`, 'Mid差'] },
      grid: { left: 48, right: 52, top: 48, bottom: 64, containLabel: true },
      xAxis: {
        type: 'category',
        data: times,
        name: '时间',
        nameLocation: 'middle',
        nameGap: 30,
        axisLabel: { margin: 8 }
      },
      yAxis: [
        { type: 'value', scale: true, name: '标准化价格', nameLocation: 'middle', nameGap: 44, axisLabel: { formatter: (v: number) => v.toFixed(2), margin: 8 } },
        { type: 'value', scale: true, name: 'Mid 差', nameLocation: 'middle', nameGap: 46, axisLabel: { margin: 8 } }
      ],
      dataZoom: [{ type: 'inside' }, { type: 'slider', height: 20, bottom: 10 }],
      series: [
        { name: `${legALabel} 标准化`, type: 'line', showSymbol: false, data: series.map((row: any) => row.leg_a_mid ? row.leg_a_mid / hlBase * 10000 : null), lineStyle: { color: '#1677ff', width: 2 } },
        { name: `${legBLabel} 标准化`, type: 'line', showSymbol: false, data: series.map((row: any) => row.leg_b_mid ? row.leg_b_mid / mt5Base * 10000 : null), lineStyle: { color: '#52c41a', width: 2 } },
        { name: 'Mid差', type: 'line', yAxisIndex: 1, showSymbol: false, data: series.map((row: any) => row.mid_diff), lineStyle: { color: '#fa8c16', width: 1, type: 'dashed' } }
      ]
    };
  }, [series, legALabel, legBLabel]);
  const eventColumns: ColumnsType<any> = [
    { title: '时间', dataIndex: 'leader_time', width: 190, render: fmtLocalTime },
    { title: '领先方', dataIndex: 'leader_platform', width: 120, render: (v) => <Tag color={venueColor(v)}>{venueLabel(v)}</Tag> },
    { title: '跟随方', dataIndex: 'follower_platform', width: 120, render: (v) => <Tag color={venueColor(v)}>{venueLabel(v)}</Tag> },
    { title: '方向', dataIndex: 'direction', width: 80, ellipsis: true, render: (v) => <EllipsisCell value={v} /> },
    { title: '跟随', dataIndex: 'followed', width: 80, render: (v) => <Tag color={v ? 'green' : 'gold'}>{v ? '是' : '否'}</Tag> },
    { title: '滞后', dataIndex: 'lag_ms', width: 100, render: fmtMs },
    { title: '领先跳动', dataIndex: 'leader_move', width: 110, render: (v) => fmtAdaptive(v, 2, 6) },
    { title: '领先bps', dataIndex: 'leader_move_bps', width: 110, render: (v) => fmtAdaptive(v, 2, 6) },
    { title: '跟随跳动', dataIndex: 'follower_move', width: 110, render: (v) => fmtAdaptive(v, 2, 6) },
    { title: '期间最大Mid差', dataIndex: 'max_mid_diff', width: 130, render: (v) => fmtAdaptive(v, 2, 6) }
  ];
  const legAToLegB = summary.leg_a_to_leg_b || {};
  const legBToLegA = summary.leg_b_to_leg_a || {};
  return (
    <div className="leadlag-page">
      <Card className="leadlag-toolbar">
        <Form layout="inline">
          <Form.Item label="品种">
            <Select value={activeSymbol} onChange={setSymbol} className="leadlag-symbol-select" loading={symbols.isLoading} options={(symbols.data || []).map((row: any) => ({ value: row.symbol, label: row.symbol }))} />
          </Form.Item>
          <Form.Item label="窗口">
            <Select value={windowSeconds} onChange={setWindowSeconds} className="leadlag-window-select" options={[{ value: 60, label: '1分钟' }, { value: 300, label: '5分钟' }, { value: 900, label: '15分钟' }, { value: 1800, label: '30分钟' }]} />
          </Form.Item>
          <Form.Item label="阈值 bps">
            <InputNumber min={0.1} step={0.5} value={thresholdBps} onChange={(v) => setThresholdBps(Number(v || 0))} className="leadlag-number-input" />
          </Form.Item>
          <Form.Item label="最小跳动">
            <InputNumber min={0} step={1} value={minMove} onChange={(v) => setMinMove(Number(v || 0))} className="leadlag-number-input" />
          </Form.Item>
          <Form.Item label="最大跟随">
            <InputNumber min={100} step={100} value={maxLagMs} onChange={(v) => setMaxLagMs(Number(v || 0))} className="leadlag-number-input" addonAfter="ms" />
          </Form.Item>
        </Form>
      </Card>

      <div className="leadlag-main-grid">
        <Card className="leadlag-chart-card">
          <div className="leadlag-card-head">
            <strong>{activeSymbol || '-'}</strong>
            <Space size={8}>
              <Tag color={venueColor(meta.leg_a_venue)}>{legALabel} Mid {fmtMid(latest[meta.leg_a_venue]?.mid)}</Tag>
              <Tag color={venueColor(meta.leg_b_venue)}>{legBLabel} Mid {fmtMid(latest[meta.leg_b_venue]?.mid)}</Tag>
            </Space>
          </div>
          {series.length ? <ReactECharts option={option} style={{ height: 430 }} /> : <Empty description="暂无报价历史" />}
        </Card>

        <aside className="leadlag-side">
          <Card className="leadlag-latest-card">
            <div className="leadlag-latest-row">
              <span>{legALabel} 来源</span>
              <strong><EllipsisCell value={latest[meta.leg_a_venue]?.source} /></strong>
            </div>
            <div className="leadlag-latest-row">
              <span>{legBLabel} 来源</span>
              <strong><EllipsisCell value={latest[meta.leg_b_venue]?.source} /></strong>
            </div>
            <div className="leadlag-latest-row">
              <span>样本点</span>
              <strong>{series.length}</strong>
            </div>
          </Card>
          <DirectionPanel title={`${legALabel} 领先 ${legBLabel}`} leader={meta.leg_a_venue} follower={meta.leg_b_venue} data={legAToLegB} />
          <DirectionPanel title={`${legBLabel} 领先 ${legALabel}`} leader={meta.leg_b_venue} follower={meta.leg_a_venue} data={legBToLegA} />
        </aside>
      </div>

      <Card title="跳动事件" className="leadlag-events-card">
        <Table rowKey={(_, index) => String(index)} columns={eventColumns} dataSource={query.data?.items || []} loading={query.isLoading} tableLayout="fixed" scroll={{ x: 1200, y: 260 }} pagination={{ pageSize: 20, size: 'small' }} />
      </Card>
    </div>
  );
}
