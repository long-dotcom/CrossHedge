import { createContext, useContext, useEffect, useState } from 'react';
import type { ReactNode } from 'react';
import { StreamStatusLight } from './StreamStatusLight';
import type { StreamStatus } from '../hooks/useLiveStream';

const HeaderStreamStatusValueContext = createContext<StreamStatus | null>(null);
const HeaderStreamStatusSetterContext = createContext<((status: StreamStatus | null) => void) | null>(null);

export function HeaderStreamStatusProvider({ children }: { children: ReactNode }) {
  const [online, setOnline] = useState<StreamStatus | null>(null);
  return (
    <HeaderStreamStatusSetterContext.Provider value={setOnline}>
      <HeaderStreamStatusValueContext.Provider value={online}>{children}</HeaderStreamStatusValueContext.Provider>
    </HeaderStreamStatusSetterContext.Provider>
  );
}

export function HeaderStreamStatus() {
  const online = useContext(HeaderStreamStatusValueContext);
  if (online === null) return null;
  return <StreamStatusLight status={online} />;
}

export function useHeaderStreamStatus(status: StreamStatus | boolean | null) {
  const setOnline = useContext(HeaderStreamStatusSetterContext);

  useEffect(() => {
    if (!setOnline) return;
    setOnline(typeof status === 'boolean' ? { online: status, lastPushSeconds: null, latencySeconds: null } : status);
    return () => setOnline(null);
  }, [setOnline, status]);
}
