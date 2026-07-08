import { createContext, useContext, useEffect, useState } from 'react';
import type { ReactNode } from 'react';
import { StreamStatusLight } from './StreamStatusLight';

const HeaderStreamStatusValueContext = createContext<boolean | null>(null);
const HeaderStreamStatusSetterContext = createContext<((online: boolean | null) => void) | null>(null);

export function HeaderStreamStatusProvider({ children }: { children: ReactNode }) {
  const [online, setOnline] = useState<boolean | null>(null);
  return (
    <HeaderStreamStatusSetterContext.Provider value={setOnline}>
      <HeaderStreamStatusValueContext.Provider value={online}>{children}</HeaderStreamStatusValueContext.Provider>
    </HeaderStreamStatusSetterContext.Provider>
  );
}

export function HeaderStreamStatus() {
  const online = useContext(HeaderStreamStatusValueContext);
  if (online === null) return null;
  return <StreamStatusLight online={online} />;
}

export function useHeaderStreamStatus(online: boolean | null) {
  const setOnline = useContext(HeaderStreamStatusSetterContext);

  useEffect(() => {
    if (!setOnline) return;
    setOnline(online);
    return () => setOnline(null);
  }, [setOnline, online]);
}
