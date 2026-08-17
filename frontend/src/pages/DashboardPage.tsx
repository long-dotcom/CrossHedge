import { useState } from 'react';
import ReactECharts from 'echarts-for-react';
import { useQuery } from '@tanstack/react-query';
import { Alert, Card, Col, Row, Segmented, Space } from 'antd';
import { api } from '../api/client';
import { AccountTable } from '../components/AccountTable';
import { DataCard } from '../components/DataCard';
import { useHeaderStreamStatus } from '../components/HeaderStreamStatus';
import { usePageStream } from '../hooks/useLiveStream';
import { fmtChartDateTime, fmtMoney, fmtPnlColor, fmtPnlSigned } from '../utils/format';
import { QueryErrorAlert } from '../components/QueryErrorAlert';

export function DashboardPage() {
  const [equityRange, setEquityRange] = useState<'24h' | '7d' | '30d' | 'all'>('24h');
  const streamStatus = usePageStream('dashboard');
  useHeaderStreamStatus(streamStatus);
  const summary = useQuery({ queryKey: ['dashboard-summary'], queryFn: async () => (await api.get('/dashboard/summary')).data });
  const curve = useQuery({
    queryKey: ['equity-curve', equityRange],
    queryFn: async () => (await api.get('/dashboard/equity-curve', { params: { range: equityRange } })).data,
    staleTime: 30_000,
    refetchInterval: 60_000,
  });
  const accounts = useQuery({ queryKey: ['accounts'], queryFn: async () => (await api.get('/accounts')).data });
  const data = summary.data || {};
  const curveData = curve.data || [];
  const curveHasDegradedData = curveData.some((item: any) => item.quality !== 'complete');

  return (
    <Space direction="vertical" size={16} className="full-width">
      <QueryErrorAlert error={summary.error || curve.error || accounts.error} onRetry={() => { summary.refetch(); curve.refetch(); accounts.refetch(); }} title="仪表盘数据加载失败" />
      {data.risk_mode === 'emergency_stop' && <Alert type="error" showIcon message="系统处于紧急停止模式" />}
      <Row gutter={[16, 16]}>
        <Col xs={24} md={8} xl={4}><DataCard title="总权益" value={fmtMoney(data.equity)} /></Col>
        <Col xs={24} md={8} xl={4}><DataCard title="总盈亏" value={fmtPnlSigned(data.total_pnl)} valueStyle={{ color: fmtPnlColor(data.total_pnl) }} /></Col>
        <Col xs={24} md={8} xl={4}><DataCard title="今日盈亏" value={fmtPnlSigned(data.today_pnl)} valueStyle={{ color: fmtPnlColor(data.today_pnl) }} /></Col>
        <Col xs={24} md={8} xl={4}><DataCard title="今日已实现盈亏" value={fmtPnlSigned(data.today_realized_pnl)} valueStyle={{ color: fmtPnlColor(data.today_realized_pnl) }} /></Col>
        <Col xs={24} md={8} xl={4}><DataCard title="可平仓未实现盈亏" value={fmtPnlSigned(data.unrealized_pnl)} valueStyle={{ color: fmtPnlColor(data.unrealized_pnl) }} /></Col>
        <Col xs={24} md={8} xl={4}><DataCard title="预计待付平仓费" value={fmtMoney(data.remaining_close_fees)} /></Col>
        <Col xs={24} md={8} xl={4}><DataCard title="持仓对冲组" value={data.open_hedge_groups ?? 0} /></Col>
        <Col xs={24} md={8} xl={4}><DataCard title="未读告警" value={data.unread_alerts ?? 0} /></Col>
      </Row>
      <Card
        title="权益曲线 (USD)"
        className="chart-card"
        extra={(
          <Segmented
            size="small"
            value={equityRange}
            onChange={(value) => setEquityRange(value as '24h' | '7d' | '30d' | 'all')}
            options={[
              { label: '24小时', value: '24h' },
              { label: '7天', value: '7d' },
              { label: '30天', value: '30d' },
              { label: '全部', value: 'all' },
            ]}
          />
        )}
      >
        {curveHasDegradedData && (
          <Alert
            type="warning"
            showIcon
            style={{ marginBottom: 12 }}
            message="所选范围包含交易所读取失败时的回退点或数据缺口"
          />
        )}
        <ReactECharts
          style={{ height: 320 }}
          option={{
            tooltip: { trigger: 'axis' },
            xAxis: { type: 'category', data: curveData.map((item: any) => fmtChartDateTime(item.time)) },
            yAxis: { type: 'value', scale: true, axisLabel: { formatter: (v: number) => fmtMoney(v) } },
            dataZoom: curveData.length > 300 ? [{ type: 'inside' }, { type: 'slider', height: 18 }] : undefined,
            series: [{
              type: 'line',
              smooth: true,
              showSymbol: curveData.length < 120,
              connectNulls: false,
              data: curveData.map((item: any) => item.equity),
              areaStyle: { opacity: 0.08 },
            }]
          }}
        />
      </Card>
      <Card title="账户">
        <AccountTable data={accounts.data || []} loading={accounts.isLoading} y={220} />
      </Card>
    </Space>
  );
}
