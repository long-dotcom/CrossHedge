import { LockOutlined, UserOutlined } from '@ant-design/icons';
import { Button, Card, Form, Input, Typography, message } from 'antd';
import { useNavigate } from 'react-router-dom';
import { api } from '../api/client';

export function LoginPage() {
  const navigate = useNavigate();
  const [messageApi, contextHolder] = message.useMessage();

  const onFinish = async (values: { username: string; password: string }) => {
    try {
      const { data } = await api.post('/auth/login', values);
      localStorage.setItem('token', data.access_token);
      localStorage.setItem('user', JSON.stringify(data.user));
      navigate('/');
    } catch {
      messageApi.error('用户名或密码错误');
    }
  };

  return (
    <div className="login-page">
      {contextHolder}
      <div className="login-background" aria-hidden="true">
        <div className="login-bg-grid" />
        <div className="login-data-streams">
          <span className="login-stream stream-1"><i /><i /><i /></span>
          <span className="login-stream stream-2"><i /><i /></span>
          <span className="login-stream stream-3"><i /><i /><i /><i /></span>
          <span className="login-stream stream-4"><i /><i /></span>
          <span className="login-stream stream-5"><i /><i /><i /></span>
        </div>
        <div className="login-node-field">
          <span className="login-node node-1" />
          <span className="login-node node-2" />
          <span className="login-node node-3" />
          <span className="login-node node-4" />
          <span className="login-node node-5" />
          <span className="login-node node-6" />
        </div>
        <div className="login-hedge-visual">
          <div className="hedge-lane hedge-lane-left">
            <span className="hedge-venue-stack">
              <i />
              <i />
              <i />
            </span>
            <span className="hedge-order sell" />
          </div>
          <div className="hedge-core">
            <span className="hedge-core-ring" />
            <span className="hedge-core-dot" />
          </div>
          <div className="hedge-lane hedge-lane-right">
            <span className="hedge-order buy" />
            <span className="hedge-venue-stack">
              <i />
              <i />
              <i />
            </span>
          </div>
          <div className="hedge-exit-pulse">
            <span />
            <span />
          </div>
        </div>
        <div className="login-signal-panel">
          <div className="signal-row">
            <span />
            <span />
            <span />
            <span />
          </div>
          <div className="signal-row compact">
            <span />
            <span />
            <span />
          </div>
          <div className="signal-row">
            <span />
            <span />
            <span />
            <span />
          </div>
          <div className="signal-meter">
            <span />
            <span />
            <span />
            <span />
            <span />
            <span />
            <span />
            <span />
          </div>
        </div>
        <div className="login-pulse-chain">
          <span />
          <span />
          <span />
          <span />
        </div>
      </div>
      <Card className="login-card">
        <div className="login-brand-head">
          <img src="/brand-mark.svg" alt="" />
          <Typography.Title level={3}>MT5 Hedge</Typography.Title>
        </div>
        <Form layout="vertical" onFinish={onFinish}>
          <Form.Item name="username" label="用户名" rules={[{ required: true }]}>
            <Input prefix={<UserOutlined />} autoComplete="username" />
          </Form.Item>
          <Form.Item name="password" label="密码" rules={[{ required: true }]}>
            <Input.Password prefix={<LockOutlined />} autoComplete="current-password" />
          </Form.Item>
          <Button type="primary" htmlType="submit" block>
            登录
          </Button>
        </Form>
      </Card>
    </div>
  );
}
