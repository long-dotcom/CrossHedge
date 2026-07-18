import { lazy, Suspense } from 'react';
import { Spin } from 'antd';
import { BrowserRouter, Navigate, Route, Routes } from 'react-router-dom';
import { AppLayout } from './layouts/AppLayout';

const LoginPage = lazy(() => import('./pages/LoginPage').then((m) => ({ default: m.LoginPage })));
const DashboardPage = lazy(() => import('./pages/DashboardPage').then((m) => ({ default: m.DashboardPage })));
const SpreadAnalyticsPage = lazy(() => import('./pages/SpreadAnalyticsPage').then((m) => ({ default: m.SpreadAnalyticsPage })));
const VenueSpreadsPage = lazy(() => import('./pages/VenueSpreadsPage').then((m) => ({ default: m.VenueSpreadsPage })));
const FundingAnalyticsPage = lazy(() => import('./pages/FundingAnalyticsPage').then((m) => ({ default: m.FundingAnalyticsPage })));
const LeadLagPage = lazy(() => import('./pages/LeadLagPage').then((m) => ({ default: m.LeadLagPage })));
const PipelinePage = lazy(() => import('./pages/PipelinePage').then((m) => ({ default: m.PipelinePage })));
const HedgeGroupsPage = lazy(() => import('./pages/HedgeGroupsPage').then((m) => ({ default: m.HedgeGroupsPage })));
const ExecutionPage = lazy(() => import('./pages/ExecutionPage').then((m) => ({ default: m.ExecutionPage })));
const AccountsPage = lazy(() => import('./pages/AccountsPage').then((m) => ({ default: m.AccountsPage })));
const PositionsPage = lazy(() => import('./pages/PositionsPage').then((m) => ({ default: m.PositionsPage })));
const RiskPage = lazy(() => import('./pages/RiskPage').then((m) => ({ default: m.RiskPage })));
const LogsPage = lazy(() => import('./pages/LogsPage').then((m) => ({ default: m.LogsPage })));
const SettingsPage = lazy(() => import('./pages/SettingsPage').then((m) => ({ default: m.SettingsPage })));

function ProtectedRoute() {
  const token = localStorage.getItem('token');
  if (!token) return <Navigate to="/login" replace />;
  return <AppLayout />;
}

export default function App() {
  return (
    <BrowserRouter>
      <Suspense fallback={<div className="route-loading"><Spin size="large" tip="页面加载中" /></div>}><Routes>
        <Route path="/login" element={<LoginPage />} />
        <Route element={<ProtectedRoute />}>
          <Route path="/" element={<DashboardPage />} />
          <Route path="/analytics" element={<SpreadAnalyticsPage />} />
          <Route path="/venue-spreads" element={<VenueSpreadsPage />} />
          <Route path="/funding" element={<FundingAnalyticsPage />} />
          <Route path="/lead-lag" element={<LeadLagPage />} />
          <Route path="/pipeline" element={<PipelinePage />} />
          <Route path="/hedge-groups" element={<HedgeGroupsPage />} />
          <Route path="/execution" element={<ExecutionPage />} />
          <Route path="/accounts" element={<AccountsPage />} />
          <Route path="/positions" element={<PositionsPage />} />
          <Route path="/risk" element={<RiskPage />} />
          <Route path="/logs" element={<LogsPage />} />
          <Route path="/settings" element={<SettingsPage />} />
        </Route>
        <Route path="*" element={<Navigate to="/" replace />} />
      </Routes></Suspense>
    </BrowserRouter>
  );
}
