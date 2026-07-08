import ReactECharts from 'echarts-for-react';
import { useQuery } from '@tanstack/react-query';
import { Alert, Card, Col, Row, Space } from 'antd';
import { api } from '../api/client';
import { AccountTable } from '../components/AccountTable';
import { DataCard } from '../components/DataCard';
import { useHeaderStreamStatus } from '../components/HeaderStreamStatus';
import { usePageStream } from '../hooks/useLiveStream';
import { fmtChartDateTime, fmtMoney, fmtPnlColor, fmtPnlSigned } from '../utils/format';

export function DashboardPage() {
  const streamStatus = usePageStream('dashboard');
  useHeaderStreamStatus(streamStatus.online);
  const summary = useQuery({ queryKey: ['dashboard-summary'], queryFn: async () => (await api.get('/dashboard/summary')).data });
  const curve = useQuery({ queryKey: ['equity-curve'], queryFn: async () => (await api.get('/dashboard/equity-curve')).data });
  const accounts = useQuery({ queryKey: ['accounts'], queryFn: async () => (await api.get('/accounts')).data });
  const data = summary.data || {};
  const curveData = curve.data || [];

  return (
    <Space direction="vertical" size={16} className="full-width">
      {data.risk_mode === 'emergency_stop' && <Alert type="error" showIcon message="系统处于紧急停止模式" />}
      <Row gutter={[16, 16]}>
        <Col xs={24} md={8} xl={4}><DataCard title="总权益" value={fmtMoney(data.equity)} /></Col>
        <Col xs={24} md={8} xl={4}><DataCard title="今日盈亏" value={fmtPnlSigned(data.today_pnl)} valueStyle={{ color: fmtPnlColor(data.today_pnl) }} /></Col>
        <Col xs={24} md={8} xl={4}><DataCard title="已实现盈亏" value={fmtPnlSigned(data.realized_pnl)} valueStyle={{ color: fmtPnlColor(data.realized_pnl) }} /></Col>
        <Col xs={24} md={8} xl={4}><DataCard title="未实现盈亏" value={fmtPnlSigned(data.unrealized_pnl)} valueStyle={{ color: fmtPnlColor(data.unrealized_pnl) }} /></Col>
        <Col xs={24} md={8} xl={4}><DataCard title="持仓对冲组" value={data.open_hedge_groups ?? 0} /></Col>
        <Col xs={24} md={8} xl={4}><DataCard title="未读告警" value={data.unread_alerts ?? 0} /></Col>
      </Row>
      <Card title="权益曲线 (USD)" className="chart-card">
        <ReactECharts
          style={{ height: 320 }}
          option={{
            tooltip: { trigger: 'axis' },
            xAxis: { type: 'category', data: curveData.map((item: any) => fmtChartDateTime(item.time)) },
            yAxis: { type: 'value', scale: true, axisLabel: { formatter: (v: number) => fmtMoney(v) } },
            series: [{ type: 'line', smooth: true, data: curveData.map((item: any) => item.equity), areaStyle: { opacity: 0.08 } }]
          }}
        />
      </Card>
      <Card title="账户">
        <AccountTable data={accounts.data || []} loading={accounts.isLoading} y={220} />
      </Card>
    </Space>
  );
}
