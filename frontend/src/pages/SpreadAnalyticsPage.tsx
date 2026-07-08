import { ExperimentOutlined } from '@ant-design/icons';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Button, Card, Col, Empty, Popconfirm, Row, Select, Space, Statistic, Tag, message } from 'antd';
import ReactECharts from 'echarts-for-react';
import { useMemo, useState } from 'react';
import { api } from '../api/client';
import { EllipsisCell } from '../components/EllipsisCell';
import { fmtAdaptive, fmtChartTime, fmtNum, fmtPct } from '../utils/format';
import { directionLabel } from '../utils/venues';

const ranges = [
  { value: '15m', label: '15分钟' },
  { value: '1h', label: '1小时' },
  { value: '4h', label: '4小时' },
  { value: '24h', label: '24小时' },
  { value: '7d', label: '7天' }
];

const bases = [
  { value: 'entry', label: '入场价差' },
  { value: 'close', label: '平仓价差' },
  { value: 'mid', label: 'Mid价差' }
];

function statusColor(status?: string) {
  if (status === 'mean_reversion') return 'green';
  if (status === 'too_risky' || status === 'slow_reversion') return 'red';
  if (status === 'watch_only') return 'gold';
  if (status === 'normal_range') return 'blue';
  return 'default';
}

function fmtSeconds(value?: number | null) {
  if (!value) return '-';
  if (value < 60) return `${fmtNum(value, 0)} 秒`;
  if (value < 3600) return `${fmtNum(value / 60, 1)} 分钟`;
  return `${fmtNum(value / 3600, 1)} 小时`;
}

function fmtChartValue(value: unknown) {
  const numeric = Number(value);
  if (!Number.isFinite(numeric)) return '-';
  return fmtAdaptive(numeric, 2, 6);
}

export function SpreadAnalyticsPage() {
  const queryClient = useQueryClient();
  const [messageApi, contextHolder] = message.useMessage();
  const symbols = useQuery({ queryKey: ['symbols'], queryFn: async () => (await api.get('/markets/symbols')).data });
  const defaultSymbol = symbols.data?.[0]?.symbol || 'BTC';
  const [symbol, setSymbol] = useState(defaultSymbol);
  const [direction, setDirection] = useState('long_leg_b_short_leg_a');
  const [range, setRange] = useState('1h');
  const [basis, setBasis] = useState('entry');

  const activeSymbol = symbol || defaultSymbol;
  const activeMapping = (symbols.data || []).find((row: any) => row.symbol === activeSymbol);
  const directions = [
    { value: 'long_leg_b_short_leg_a', label: directionLabel('long_leg_b_short_leg_a', activeMapping) },
    { value: 'long_leg_a_short_leg_b', label: directionLabel('long_leg_a_short_leg_b', activeMapping) }
  ];
  const query = useQuery({
    queryKey: ['spread-analytics', activeSymbol, direction, range, basis],
    enabled: Boolean(activeSymbol),
    queryFn: async () => (await api.get('/analytics/spread-series', { params: { symbol: activeSymbol, direction, range, basis } })).data
  });
  const executeCurrent = useMutation({
    mutationFn: async () => (await api.post(`/markets/spreads/${activeSymbol}/execute`, null, { params: { direction, force: true } })).data,
    onSuccess: (data) => {
      messageApi.success(`已提交 ${activeSymbol} 下单，对冲组 #${data.id}`);
      queryClient.invalidateQueries({ queryKey: ['hedge-groups'] });
      queryClient.invalidateQueries({ queryKey: ['orders'] });
      queryClient.invalidateQueries({ queryKey: ['fills'] });
      queryClient.invalidateQueries({ queryKey: ['spread-analytics'] });
    },
    onError: (error: any) => {
      messageApi.error(error?.response?.data?.detail || '下单失败');
    }
  });

  const summary = query.data?.summary;
  const items = query.data?.items || [];
  const option = useMemo(() => {
    const times = items.map((row: any) => fmtChartTime(row.time));
    const close = items.map((row: any) => row.close);
    const cost = items.map((row: any) => row.avg_total_cost);
    const mean = summary ? items.map(() => summary.mean) : [];
    const upper1 = summary ? items.map(() => summary.mean + summary.std) : [];
    const lower1 = summary ? items.map(() => summary.mean - summary.std) : [];
    const upper2 = summary ? items.map(() => summary.mean + summary.std * 2) : [];
    const lower2 = summary ? items.map(() => summary.mean - summary.std * 2) : [];
    return {
      animation: false,
      tooltip: {
        trigger: 'axis',
        valueFormatter: (value: unknown) => fmtChartValue(value)
      },
      legend: { top: 0, data: ['价差', '均值', '+1σ', '-1σ', '+2σ', '-2σ', '平均成本'] },
      grid: { left: 48, right: 28, top: 48, bottom: 42 },
      xAxis: { type: 'category', data: times, boundaryGap: false },
      yAxis: { type: 'value', scale: true, axisLabel: { formatter: (value: number) => fmtChartValue(value) } },
      dataZoom: [{ type: 'inside' }, { type: 'slider', height: 18, bottom: 8 }],
      series: [
        { name: '价差', type: 'line', data: close, showSymbol: false, lineStyle: { width: 2, color: '#1677ff' } },
        { name: '均值', type: 'line', data: mean, showSymbol: false, lineStyle: { width: 1.5, type: 'dashed', color: '#444' } },
        { name: '+1σ', type: 'line', data: upper1, showSymbol: false, lineStyle: { width: 1, type: 'dotted', color: '#52c41a' } },
        { name: '-1σ', type: 'line', data: lower1, showSymbol: false, lineStyle: { width: 1, type: 'dotted', color: '#52c41a' } },
        { name: '+2σ', type: 'line', data: upper2, showSymbol: false, lineStyle: { width: 1, type: 'dashed', color: '#fa8c16' } },
        { name: '-2σ', type: 'line', data: lower2, showSymbol: false, lineStyle: { width: 1, type: 'dashed', color: '#fa8c16' } },
        { name: '平均成本', type: 'line', data: cost, showSymbol: false, lineStyle: { width: 1, type: 'dashed', color: '#f5222d' } }
      ]
    };
  }, [items, summary]);

  const symbolOptions = (symbols.data || []).map((row: any) => ({ value: row.symbol, label: row.symbol }));

  return (
    <Space direction="vertical" size={16} className="full-width">
      {contextHolder}
      <Card>
        <Space wrap>
          <Select className="analytics-control" value={activeSymbol} options={symbolOptions} loading={symbols.isLoading} onChange={setSymbol} />
          <Select className="analytics-control-wide" value={direction} options={directions} onChange={setDirection} />
          <Select className="analytics-control" value={range} options={ranges} onChange={setRange} />
          <Select className="analytics-control" value={basis} options={bases} onChange={setBasis} />
          <Tag icon={<ExperimentOutlined />} color={statusColor(summary?.analytics_status)}>
            {summary?.analytics_status || 'no_data'}
          </Tag>
          <EllipsisCell value={summary?.reason || '等待数据'} className="analytics-reason" />
          <Popconfirm
            title={`确认下单 ${activeSymbol}?`}
            description={`方向：${directions.find((item) => item.value === direction)?.label || direction}。将跳过策略价差/盈利闸门，提交前仍会执行风控、行情同步、MT5 会话和订单预检查。`}
            okText="强制下单"
            cancelText="取消"
            onConfirm={() => executeCurrent.mutate()}
          >
            <Button type="primary" loading={executeCurrent.isPending} disabled={!activeSymbol || query.isLoading}>
              强制下单
            </Button>
          </Popconfirm>
        </Space>
      </Card>

      <Row gutter={[16, 16]}>
        <Col xs={24} sm={12} lg={6}>
          <Card className="metric-card">
            <Statistic title={`当前${bases.find((item) => item.value === basis)?.label || '价差'}`} value={summary?.current_spread || 0} formatter={(value) => fmtChartValue(value)} />
          </Card>
        </Col>
        <Col xs={24} sm={12} lg={6}>
          <Card className="metric-card">
            <Statistic title="Z-Score" value={summary?.z_score || 0} precision={2} />
          </Card>
        </Col>
        <Col xs={24} sm={12} lg={6}>
          <Card className="metric-card">
            <Statistic title="历史分位" value={(summary?.percentile || 0) * 100} precision={1} suffix="%" />
          </Card>
        </Col>
        <Col xs={24} sm={12} lg={6}>
          <Card className="metric-card">
            <Statistic title="机会评分" value={summary?.opportunity_score || 0} precision={1} suffix="/ 100" />
          </Card>
        </Col>
      </Row>

      <Row gutter={[16, 16]}>
        <Col xs={24} lg={16}>
          <Card className="analytics-chart-card">
            {items.length ? <ReactECharts option={option} style={{ height: 420 }} /> : <Empty description="暂无价差快照" />}
          </Card>
        </Col>
        <Col xs={24} lg={8}>
          <Card className="analytics-side-card">
            <Space direction="vertical" size={14} className="full-width">
              <div className="analytics-line">
                <span>样本数</span>
                <strong>{summary?.sample_count || 0}</strong>
              </div>
              <div className="analytics-line">
                <span>均值 / 标准差</span>
                <strong>{fmtChartValue(summary?.mean || 0)} / {fmtChartValue(summary?.std || 0)}</strong>
              </div>
              <div className="analytics-line">
                <span>平均成本线</span>
                <strong>{fmtChartValue(summary?.avg_total_cost || 0)}</strong>
              </div>
              <div className="analytics-line">
                <span>估算半衰期</span>
                <strong>{fmtSeconds(summary?.half_life_seconds)}</strong>
              </div>
              <div className="analytics-line">
                <span>最大不利扩张</span>
                <strong>{fmtChartValue(summary?.max_adverse_spread || 0)}</strong>
              </div>
              <div className="analytics-line">
                <span>5分钟回归概率</span>
                <strong>{summary?.reversion_probability?.['5m'] == null ? '-' : fmtPct(summary.reversion_probability['5m'])}</strong>
              </div>
              <div className="analytics-line">
                <span>15分钟回归概率</span>
                <strong>{summary?.reversion_probability?.['15m'] == null ? '-' : fmtPct(summary.reversion_probability['15m'])}</strong>
              </div>
              <div className="analytics-line">
                <span>60分钟回归概率</span>
                <strong>{summary?.reversion_probability?.['60m'] == null ? '-' : fmtPct(summary.reversion_probability['60m'])}</strong>
              </div>
            </Space>
          </Card>
        </Col>
      </Row>
    </Space>
  );
}

