import { BrowserRouter, Navigate, Route, Routes } from 'react-router-dom';
import { AppLayout } from './layouts/AppLayout';
import { LoginPage } from './pages/LoginPage';
import { DashboardPage } from './pages/DashboardPage';
import { SpreadAnalyticsPage } from './pages/SpreadAnalyticsPage';
import { VenueSpreadsPage } from './pages/VenueSpreadsPage';
import { FundingAnalyticsPage } from './pages/FundingAnalyticsPage';
import { LeadLagPage } from './pages/LeadLagPage';
import { PipelinePage } from './pages/PipelinePage';
import { HedgeGroupsPage } from './pages/HedgeGroupsPage';
import { ExecutionPage } from './pages/ExecutionPage';
import { AccountsPage } from './pages/AccountsPage';
import { PositionsPage } from './pages/PositionsPage';
import { RiskPage } from './pages/RiskPage';
import { LogsPage } from './pages/LogsPage';
import { SettingsPage } from './pages/SettingsPage';

function ProtectedRoute() {
  const token = localStorage.getItem('token');
  if (!token) return <Navigate to="/login" replace />;
  return <AppLayout />;
}

export default function App() {
  return (
    <BrowserRouter>
      <Routes>
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
      </Routes>
    </BrowserRouter>
  );
}
