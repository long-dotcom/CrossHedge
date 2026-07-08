import React from 'react';
import ReactDOM from 'react-dom/client';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { ConfigProvider, App as AntApp } from 'antd';
import zhCN from 'antd/locale/zh_CN';
import App from './App';
import './styles.css';

const queryClient = new QueryClient();

ReactDOM.createRoot(document.getElementById('root')!).render(
  <React.StrictMode>
    <ConfigProvider
      locale={zhCN}
      theme={{
        token: {
          borderRadius: 8,
          colorPrimary: '#0f766e',
          colorInfo: '#2563eb',
          colorSuccess: '#16a34a',
          colorWarning: '#f97316',
          colorError: '#ef4444',
          colorText: '#172033',
          colorTextSecondary: '#66758a',
          colorBgLayout: '#eef4f3',
          colorBorderSecondary: '#e2e8f0'
        }
      }}
    >
      <AntApp>
        <QueryClientProvider client={queryClient}>
          <App />
        </QueryClientProvider>
      </AntApp>
    </ConfigProvider>
  </React.StrictMode>
);
