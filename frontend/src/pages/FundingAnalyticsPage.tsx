import { LineChartOutlined } from '@ant-design/icons';
import { useQuery } from '@tanstack/react-query';
import { Alert, Card, Col, Empty, Row, Select, Space, Statistic, Tag } from 'antd';
import ReactECharts from 'echarts-for-react';
import { useMemo, useState } from 'react';
import { api } from '../api/client';
import { EllipsisCell } from '../components/EllipsisCell';
import { fmtChartDate, fmtChartDateTime, fmtChartTime, fmtNum, fmtPct } from '../utils/format';
import { venueLabel } from '../utils/venues';

const ranges = [
  { value: '24h', label: '24小时' },
  { value: '7d', label: '7天' },
  { value: '30d', label: '30天' },
  { value: '90d', label: '90天' }
];

const buckets = [
  { value: 'raw', label: '原始' },
  { value: 'hour', label: '按小时' },
  { value: 'day', label: '按天' }
];

function biasText(value?: string) {
  if (value === 'positive') return '长期偏正';
  if (value === 'negative') return '长期偏负';
  if (value === 'mixed') return '震荡';
  return '暂无数据';
}

function biasColor(value?: string) {
  if (value === 'positive') return 'green';
  if (value === 'negative') return 'red';
  if (value === 'mixed') return 'gold';
  return 'default';
}

function fmtRate(value?: number, digits = 4) {
  if (value === undefined || value === null || Number.isNaN(value)) return '-';
  return `${(value * 100).toFixed(digits)}%`;
}

function fmtChartRate(value: unknown) {
  const numeric = Number(value);
  if (!Number.isFinite(numeric)) return '-';
  return fmtRate(numeric, 4);
}

export function FundingAnalyticsPage() {
  const symbols = useQuery({ queryKey: ['symbols'], queryFn: async () => (await api.get('/markets/symbols')).data });
  const [symbol, setSymbol] = useState<string>('');
  const [range, setRange] = useState('7d');
  const [bucket, setBucket] = useState('day');
  const activeSymbol = symbol || symbols.data?.[0]?.symbol || '';

  const query = useQuery({
    queryKey: ['funding-analytics', activeSymbol, range, bucket],
    enabled: Boolean(activeSymbol),
    queryFn: async () => (await api.get('/analytics/funding-series', { params: { symbol: activeSymbol, range, bucket } })).data
  });

  const summary = query.data?.summary;
  const items = query.data?.items || [];
  const fundingVenue = query.data?.funding_venue;
  const fundingSymbol = query.data?.funding_symbol;
  const fundingLabel = fundingVenue ? `${venueLabel(fundingVenue)} ${fundingSymbol || ''}`.trim() : '不支持资金费历史';
  const option = useMemo(() => {
    const times = items.map((row: any) => (bucket === 'day' ? fmtChartDate(row.time) : bucket === 'hour' ? fmtChartDateTime(row.time) : fmtChartTime(row.time)));
    const sumRates = items.map((row: any) => row.sum_funding_rate);
    const avgRates = items.map((row: any) => row.avg_funding_rate);
    return {
      animation: false,
      tooltip: {
        trigger: 'axis',
        valueFormatter: (value: unknown) => fmtChartRate(value)
      },
      legend: { top: 0, data: ['累计资金费率', '平均资金费率'] },
      grid: { left: 60, right: 28, top: 48, bottom: 42 },
      xAxis: {
        type: 'category',
        data: times,
        boundaryGap: bucket !== 'raw',
        axisLabel: { hideOverlap: true, rotate: bucket === 'raw' ? 0 : 18 }
      },
      yAxis: { type: 'value', scale: true, axisLabel: { formatter: (value: number) => fmtRate(value, 3) } },
      dataZoom: [{ type: 'inside' }, { type: 'slider', height: 18, bottom: 8 }],
      series: [
        {
          name: '累计资金费率',
          type: bucket === 'raw' ? 'line' : 'bar',
          data: sumRates,
          showSymbol: false,
          itemStyle: { color: (params: any) => (Number(params.value) >= 0 ? '#52c41a' : '#f5222d') },
          lineStyle: { width: 2, color: '#52c41a' }
        },
        {
          name: '平均资金费率',
          type: 'line',
          data: avgRates,
          showSymbol: true,
          symbol: 'circle',
          symbolSize: 5,
          lineStyle: { width: 1.5, type: 'dashed', color: '#1677ff' }
        }
      ]
    };
  }, [items, bucket]);

  const symbolOptions = (symbols.data || []).map((row: any) => ({ value: row.symbol, label: row.symbol }));

  return (
    <Space direction="vertical" size={16} className="full-width">
      <Card>
        <Space wrap>
          <Select className="analytics-control" value={activeSymbol} options={symbolOptions} loading={symbols.isLoading} onChange={setSymbol} />
          <Select className="analytics-control" value={range} options={ranges} onChange={setRange} />
          <Select className="analytics-control" value={bucket} options={buckets} onChange={setBucket} />
          <Tag icon={<LineChartOutlined />} color={biasColor(summary?.bias)}>
            {biasText(summary?.bias)}
          </Tag>
          <EllipsisCell value={fundingLabel} className="analytics-reason" />
        </Space>
      </Card>

      {query.data?.source_error ? <Alert type="warning" showIcon message={query.data?.supported === false ? "资金费历史暂不支持该组合" : "资金费历史读取失败"} description={query.data.source_error} /> : null}

      <Row gutter={[16, 16]}>
        <Col xs={24} sm={12} lg={6}>
          <Card className="metric-card">
            <Statistic title={`${venueLabel(fundingVenue)} 最新资金费率`} value={fmtRate(summary?.latest_funding_rate)} />
          </Card>
        </Col>
        <Col xs={24} sm={12} lg={6}>
          <Card className="metric-card">
            <Statistic title="区间平均" value={fmtRate(summary?.avg_funding_rate)} />
          </Card>
        </Col>
        <Col xs={24} sm={12} lg={6}>
          <Card className="metric-card">
            <Statistic title="区间累计" value={fmtRate(summary?.sum_funding_rate)} />
          </Card>
        </Col>
        <Col xs={24} sm={12} lg={6}>
          <Card className="metric-card">
            <Statistic title="估算年化" value={fmtPct(summary?.annualized_estimate)} />
          </Card>
        </Col>
      </Row>

      <Row gutter={[16, 16]}>
        <Col xs={24} lg={16}>
          <Card className="analytics-chart-card">
            {items.length ? <ReactECharts option={option} style={{ height: 420 }} /> : <Empty description="暂无资金费历史" />}
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
                <span>正 / 负次数</span>
                <strong>{summary?.positive_count || 0} / {summary?.negative_count || 0}</strong>
              </div>
              <div className="analytics-line">
                <span>正资金费占比</span>
                <strong>{fmtPct(summary?.positive_ratio)}</strong>
              </div>
              <div className="analytics-line">
                <span>中位数</span>
                <strong>{fmtRate(summary?.median_funding_rate)}</strong>
              </div>
              <div className="analytics-line">
                <span>最大正值</span>
                <strong>{fmtRate(summary?.max_funding_rate)}</strong>
              </div>
              <div className="analytics-line">
                <span>最大负值</span>
                <strong>{fmtRate(summary?.min_funding_rate)}</strong>
              </div>
              <div className="analytics-line">
                <span>聚合粒度</span>
                <strong>{buckets.find((item) => item.value === bucket)?.label || bucket}</strong>
              </div>
              <div className="analytics-line">
                <span>平均每样本</span>
                <strong>{fmtNum((summary?.sum_funding_rate || 0) / Math.max(summary?.sample_count || 1, 1), 8)}</strong>
              </div>
            </Space>
          </Card>
        </Col>
      </Row>
    </Space>
  );
}
