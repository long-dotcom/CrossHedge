import { useQuery } from '@tanstack/react-query';
import { Card, Col, Empty, Row, Select, Space, Statistic, Divider } from 'antd';
import ReactECharts from 'echarts-for-react';
import { useMemo, useState } from 'react';
import { api } from '../api/client';
import { fmtAdaptive, fmtChartTime, fmtNum } from '../utils/format';
import { legMeta, venueLabel } from '../utils/venues';

const ranges = [
  { value: '15m', label: '15分钟' },
  { value: '1h', label: '1小时' },
  { value: '4h', label: '4小时' },
  { value: '24h', label: '24小时' },
  { value: '7d', label: '7天' }
];

function fmtChartValue(value: unknown) {
  const numeric = Number(value);
  if (!Number.isFinite(numeric)) return '-';
  return fmtAdaptive(numeric, 2, 6);
}

export function VenueSpreadsPage() {
  const symbols = useQuery({ queryKey: ['symbols'], queryFn: async () => (await api.get('/markets/symbols')).data });
  const defaultSymbol = symbols.data?.[0]?.symbol || 'BTC';
  const [symbol, setSymbol] = useState(defaultSymbol);
  const [range, setRange] = useState('1h');

  const activeSymbol = symbol || defaultSymbol;
  const query = useQuery({
    queryKey: ['venue-spreads', activeSymbol, range],
    enabled: Boolean(activeSymbol),
    queryFn: async () => (await api.get('/analytics/venue-spreads', { params: { symbol: activeSymbol, range } })).data,
    refetchInterval: 5000,
  });

  const summary = query.data?.summary;
  const series = query.data?.series || [];
  const activeMapping = query.data || (symbols.data || []).find((row: any) => row.symbol === activeSymbol);
  const meta = legMeta(activeMapping);
  const legALabel = venueLabel(meta.leg_a_venue);
  const legBLabel = venueLabel(meta.leg_b_venue);

  const option = useMemo(() => {
    const times = series.map((row: any) => fmtChartTime(row.time));
    const legAData = series.map((row: any) => row.leg_a_avg);
    const legBData = series.map((row: any) => row.leg_b_avg);

    const legAMean = summary?.leg_a?.mean ?? 0;
    const legBMean = summary?.leg_b?.mean ?? 0;
    const legAMeanArr = series.map(() => legAMean);
    const legBMeanArr = series.map(() => legBMean);

    const legAStd = summary?.leg_a?.std ?? 0;
    const legBStd = summary?.leg_b?.std ?? 0;
    const legAUpper3 = legAMean + legAStd * 3;
    const legBUpper3 = legBMean + legBStd * 3;
    const legAUpper3Arr = series.map(() => legAUpper3);
    const legBUpper3Arr = series.map(() => legBUpper3);

    // Build markArea data for anomaly regions (value > mean + 3σ)
    const legAAnomalyAreas: any[] = [];
    const legBAnomalyAreas: any[] = [];
    let legAStart: any = null;
    let legBStart: any = null;
    for (let i = 0; i < series.length; i++) {
      const legAExceed = series[i].leg_a_avg > legAUpper3;
      const legBExceed = series[i].leg_b_avg > legBUpper3;
      if (legAExceed && legAStart === null) legAStart = i;
      if (!legAExceed && legAStart !== null) {
        legAAnomalyAreas.push([{ xAxis: legAStart }, { xAxis: i - 1 }]);
        legAStart = null;
      }
      if (legBExceed && legBStart === null) legBStart = i;
      if (!legBExceed && legBStart !== null) {
        legBAnomalyAreas.push([{ xAxis: legBStart }, { xAxis: i - 1 }]);
        legBStart = null;
      }
    }
    if (legAStart !== null) legAAnomalyAreas.push([{ xAxis: legAStart }, { xAxis: series.length - 1 }]);
    if (legBStart !== null) legBAnomalyAreas.push([{ xAxis: legBStart }, { xAxis: series.length - 1 }]);

    return {
      animation: false,
      tooltip: {
        trigger: 'axis',
        valueFormatter: (value: unknown) => fmtChartValue(value)
      },
      legend: { top: 0, data: [`${legALabel} 点差`, `${legBLabel} 点差`, `${legALabel} 均值`, `${legBLabel} 均值`, '+3σ'] },
      grid: { left: 48, right: 28, top: 48, bottom: 42 },
      xAxis: { type: 'category', data: times, boundaryGap: false },
      yAxis: { type: 'value', scale: true, axisLabel: { formatter: (value: number) => fmtChartValue(value) } },
      dataZoom: [{ type: 'inside' }, { type: 'slider', height: 18, bottom: 8 }],
      series: [
        {
          name: `${legALabel} 点差`, type: 'line', data: legAData, showSymbol: false,
          lineStyle: { width: 2, color: '#1677ff' },
          itemStyle: { color: '#1677ff' },
          markArea: legAAnomalyAreas.length ? { silent: true, data: legAAnomalyAreas.map((pair) => pair.map((p: any) => ({ ...p, itemStyle: { color: 'rgba(255,77,79,0.08)' } }))) } : undefined,
        },
        {
          name: `${legBLabel} 点差`, type: 'line', data: legBData, showSymbol: false,
          lineStyle: { width: 2, color: '#52c41a' },
          itemStyle: { color: '#52c41a' },
          markArea: legBAnomalyAreas.length ? { silent: true, data: legBAnomalyAreas.map((pair) => pair.map((p: any) => ({ ...p, itemStyle: { color: 'rgba(255,77,79,0.08)' } }))) } : undefined,
        },
        { name: `${legALabel} 均值`, type: 'line', data: legAMeanArr, showSymbol: false, lineStyle: { width: 1.5, type: 'dashed', color: '#1677ff' } },
        { name: `${legBLabel} 均值`, type: 'line', data: legBMeanArr, showSymbol: false, lineStyle: { width: 1.5, type: 'dashed', color: '#52c41a' } },
        { name: '+3σ', type: 'line', data: legAUpper3Arr, showSymbol: false, lineStyle: { width: 1, type: 'dotted', color: '#ff4d4f' } },
        { name: `+3σ(${legBLabel})`, type: 'line', data: legBUpper3Arr, showSymbol: false, lineStyle: { width: 1, type: 'dotted', color: '#ff4d4f' }, tooltip: { show: false } },
      ]
    };
  }, [series, summary, legALabel, legBLabel]);

  const symbolOptions = (symbols.data || []).map((row: any) => ({ value: row.symbol, label: row.symbol }));

  const renderStats = (stats: any, label: string) => (
    <Space direction="vertical" size={4} className="full-width">
      <div style={{ fontWeight: 600, fontSize: 14, marginBottom: 4 }}>{label}</div>
      <div className="analytics-line"><span>样本数</span><strong>{stats?.sample_count ?? 0}</strong></div>
      <div className="analytics-line"><span>均值 / 标准差</span><strong>{fmtChartValue(stats?.mean)} / {fmtChartValue(stats?.std)}</strong></div>
      <div className="analytics-line"><span>中位数</span><strong>{fmtChartValue(stats?.median)}</strong></div>
      <div className="analytics-line"><span>最小值 / 最大值</span><strong>{fmtChartValue(stats?.min)} / {fmtChartValue(stats?.max)}</strong></div>
      <div className="analytics-line"><span>P95</span><strong>{fmtChartValue(stats?.p95)}</strong></div>
      <div className="analytics-line"><span>变异系数 (CV)</span><strong>{stats?.cv != null ? fmtNum(stats.cv, 4) : '-'}</strong></div>
      <div className="analytics-line"><span>异常占比 (&gt;3σ)</span><strong>{stats?.anomaly_pct != null ? `${fmtNum(stats.anomaly_pct, 2)}%` : '-'}</strong></div>
    </Space>
  );

  return (
    <Space direction="vertical" size={16} className="full-width">
      <Card>
        <Space wrap>
          <Select className="analytics-control" value={activeSymbol} options={symbolOptions} loading={symbols.isLoading} onChange={setSymbol} />
          <Select className="analytics-control" value={range} options={ranges} onChange={setRange} />
        </Space>
      </Card>

      <Row gutter={[16, 16]}>
        <Col xs={24} sm={12} lg={6}>
          <Card className="metric-card">
            <Statistic title={`${legALabel} 当前点差`} value={summary?.leg_a?.current || 0} formatter={(value) => fmtChartValue(value)} />
            <div style={{ marginTop: 4, color: '#888', fontSize: 12 }}>均值: {fmtChartValue(summary?.leg_a?.mean)}</div>
          </Card>
        </Col>
        <Col xs={24} sm={12} lg={6}>
          <Card className="metric-card">
            <Statistic title={`${legBLabel} 当前点差`} value={summary?.leg_b?.current || 0} formatter={(value) => fmtChartValue(value)} />
            <div style={{ marginTop: 4, color: '#888', fontSize: 12 }}>均值: {fmtChartValue(summary?.leg_b?.mean)}</div>
          </Card>
        </Col>
        <Col xs={24} sm={12} lg={6}>
          <Card className="metric-card">
            <Statistic title={`${legALabel} 变异系数 (CV)`} value={summary?.leg_a?.cv || 0} formatter={(value) => fmtNum(value as number, 4)} />
          </Card>
        </Col>
        <Col xs={24} sm={12} lg={6}>
          <Card className="metric-card">
            <Statistic title={`${legBLabel} 变异系数 (CV)`} value={summary?.leg_b?.cv || 0} formatter={(value) => fmtNum(value as number, 4)} />
          </Card>
        </Col>
      </Row>

      <Row gutter={[16, 16]}>
        <Col xs={24} lg={16}>
          <Card className="analytics-chart-card">
            {series.length ? <ReactECharts option={option} style={{ height: 420 }} /> : <Empty description="暂无点差数据" />}
          </Card>
        </Col>
        <Col xs={24} lg={8}>
          <Card className="analytics-side-card">
            {renderStats(summary?.leg_a, `${legALabel} 统计`)}
            <Divider style={{ margin: '12px 0' }} />
            {renderStats(summary?.leg_b, `${legBLabel} 统计`)}
          </Card>
        </Col>
      </Row>
    </Space>
  );
}
