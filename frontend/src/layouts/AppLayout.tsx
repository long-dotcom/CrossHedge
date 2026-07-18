import {
  MenuFoldOutlined,
  MenuUnfoldOutlined,
  DashboardOutlined,
  DatabaseOutlined,
  ExperimentOutlined,
  LineChartOutlined,
  NodeIndexOutlined,
  PartitionOutlined,
  HistoryOutlined,
  OrderedListOutlined,
  SafetyCertificateOutlined,
  SettingOutlined,
  StockOutlined,
  UserOutlined,
  BarChartOutlined
} from '@ant-design/icons';
import { Alert, Button, Drawer, Grid, Layout, Menu, Space, Tag, Typography } from 'antd';
import { useQuery } from '@tanstack/react-query';
import { useState } from 'react';
import { Outlet, useLocation, useNavigate } from 'react-router-dom';
import { HeaderStreamStatus, HeaderStreamStatusProvider } from '../components/HeaderStreamStatus';
import { api } from '../api/client';

const { Header, Sider, Content } = Layout;

const items = [
  { key: '/', icon: <DashboardOutlined />, label: '仪表盘' },
  { key: 'research', label: '研究', children: [
    { key: '/analytics', icon: <ExperimentOutlined />, label: '价差研究' },
    { key: '/venue-spreads', icon: <BarChartOutlined />, label: '点差监控' },
    { key: '/funding', icon: <LineChartOutlined />, label: '资金费研究' },
    { key: '/lead-lag', icon: <NodeIndexOutlined />, label: '报价时差' }
  ] },
  { key: 'trading', label: '交易', children: [
    { key: '/hedge-groups', icon: <HistoryOutlined />, label: '对冲组' },
    { key: '/execution', icon: <OrderedListOutlined />, label: '执行记录' },
    { key: '/positions', icon: <StockOutlined />, label: '仓位' },
    { key: '/accounts', icon: <UserOutlined />, label: '账户' }
  ] },
  { key: 'operations', label: '运维', children: [
    { key: '/pipeline', icon: <PartitionOutlined />, label: '链路监控' },
    { key: '/risk', icon: <SafetyCertificateOutlined />, label: '风控' },
    { key: '/logs', icon: <DatabaseOutlined />, label: '日志' }
  ] },
  { key: '/settings', icon: <SettingOutlined />, label: '设置' }
];

export function AppLayout() {
  const navigate = useNavigate();
  const location = useLocation();
  const [collapsed, setCollapsed] = useState(false);
  const screens = Grid.useBreakpoint();
  const mobile = !screens.md;
  const [drawerOpen, setDrawerOpen] = useState(false);
  const strategy = useQuery({ queryKey: ['settings-strategy'], queryFn: async () => (await api.get('/settings/strategy')).data });
  const risk = useQuery({ queryKey: ['risk-status'], queryFn: async () => (await api.get('/risk/status')).data });
  const user = (() => { try { return JSON.parse(localStorage.getItem('user') || '{}'); } catch { return {}; } })();
  const logout = () => {
    localStorage.removeItem('token');
    localStorage.removeItem('user');
    navigate('/login');
  };

  return (
    <HeaderStreamStatusProvider>
      <Layout className="app-shell">
        {!mobile && <Sider width={224} collapsedWidth={72} collapsed={collapsed} trigger={null} theme="light" className="side-nav">
          <div className={`brand ${collapsed ? 'collapsed' : ''}`}>
            <img className="brand-mark" src="/brand-mark.svg" alt="MT5 Hedge" />
            {!collapsed && <span>MT5 Hedge</span>}
          </div>
          <Menu mode="inline" selectedKeys={[location.pathname]} items={items} onClick={(event) => navigate(event.key)} inlineCollapsed={collapsed} />
        </Sider>}
        <Drawer placement="left" width={260} open={mobile && drawerOpen} onClose={() => setDrawerOpen(false)} styles={{ body: { padding: 0 } }}>
          <div className="brand"><img className="brand-mark" src="/brand-mark.svg" alt="CrossHedge" /><span>CrossHedge</span></div>
          <Menu mode="inline" selectedKeys={[location.pathname]} defaultOpenKeys={['research', 'trading', 'operations']} items={items} onClick={(event) => { navigate(event.key); setDrawerOpen(false); }} />
        </Drawer>
        <Layout>
          <Header className="topbar">
            <Space size={12}>
              <Button
                type="text"
                className="side-collapse-button"
                icon={mobile || collapsed ? <MenuUnfoldOutlined /> : <MenuFoldOutlined />}
                onClick={() => mobile ? setDrawerOpen(true) : setCollapsed((value) => !value)}
              />
              <Typography.Text strong>Hyperliquid + MT5 套利管理台</Typography.Text>
            </Space>
            <Space>
              <HeaderStreamStatus />
              <Tag color={strategy.data?.execution_mode === 'live' ? 'red' : strategy.data?.execution_mode === 'paper' ? 'gold' : 'default'}>{String(strategy.data?.execution_mode || '未知').toUpperCase()}</Tag>
              <Typography.Text type="secondary">{user.username || user.name || '当前用户'}</Typography.Text>
              <Button onClick={logout}>退出</Button>
            </Space>
          </Header>
          {risk.data?.mode === 'emergency_stop' && <Alert banner type="error" showIcon message="系统处于紧急停止模式：禁止新开仓，请检查现有仓位" />}
          <Content className="page-content">
            <Outlet />
          </Content>
        </Layout>
      </Layout>
    </HeaderStreamStatusProvider>
  );
}
