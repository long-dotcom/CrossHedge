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
  BarChartOutlined
} from '@ant-design/icons';
import { Button, Layout, Menu, Space, Typography } from 'antd';
import { useState } from 'react';
import { Outlet, useLocation, useNavigate } from 'react-router-dom';
import { HeaderStreamStatus, HeaderStreamStatusProvider } from '../components/HeaderStreamStatus';

const { Header, Sider, Content } = Layout;

const items = [
  { key: '/', icon: <DashboardOutlined />, label: '仪表盘' },
  { key: '/analytics', icon: <ExperimentOutlined />, label: '价差研究' },
  { key: '/venue-spreads', icon: <BarChartOutlined />, label: '点差监控' },
  { key: '/funding', icon: <LineChartOutlined />, label: '资金费研究' },
  { key: '/lead-lag', icon: <NodeIndexOutlined />, label: '报价时差' },
  { key: '/pipeline', icon: <PartitionOutlined />, label: '链路监控' },
  { key: '/hedge-groups', icon: <HistoryOutlined />, label: '对冲组' },
  { key: '/execution', icon: <OrderedListOutlined />, label: '执行记录' },
  { key: '/positions', icon: <StockOutlined />, label: '仓位' },
  { key: '/risk', icon: <SafetyCertificateOutlined />, label: '风控' },
  { key: '/logs', icon: <DatabaseOutlined />, label: '日志' },
  { key: '/settings', icon: <SettingOutlined />, label: '设置' }
];

export function AppLayout() {
  const navigate = useNavigate();
  const location = useLocation();
  const [collapsed, setCollapsed] = useState(false);
  const logout = () => {
    localStorage.removeItem('token');
    localStorage.removeItem('user');
    navigate('/login');
  };

  return (
    <HeaderStreamStatusProvider>
      <Layout className="app-shell">
        <Sider width={224} collapsedWidth={72} collapsed={collapsed} trigger={null} theme="light" className="side-nav">
          <div className={`brand ${collapsed ? 'collapsed' : ''}`}>
            <img className="brand-mark" src="/brand-mark.svg" alt="MT5 Hedge" />
            {!collapsed && <span>MT5 Hedge</span>}
          </div>
          <Menu mode="inline" selectedKeys={[location.pathname]} items={items} onClick={(event) => navigate(event.key)} inlineCollapsed={collapsed} />
        </Sider>
        <Layout>
          <Header className="topbar">
            <Space size={12}>
              <Button
                type="text"
                className="side-collapse-button"
                icon={collapsed ? <MenuUnfoldOutlined /> : <MenuFoldOutlined />}
                onClick={() => setCollapsed((value) => !value)}
              />
              <Typography.Text strong>Hyperliquid + MT5 套利管理台</Typography.Text>
            </Space>
            <Space>
              <HeaderStreamStatus />
              <Typography.Text type="secondary">admin</Typography.Text>
              <Button onClick={logout}>退出</Button>
            </Space>
          </Header>
          <Content className="page-content">
            <Outlet />
          </Content>
        </Layout>
      </Layout>
    </HeaderStreamStatusProvider>
  );
}
