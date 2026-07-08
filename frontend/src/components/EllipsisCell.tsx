import { Tooltip } from 'antd';
import type { ReactNode } from 'react';

export function cellText(value: unknown): string {
  if (value === null || value === undefined) return '-';
  if (typeof value === 'string') return value || '-';
  if (typeof value === 'number' || typeof value === 'boolean') return String(value);
  try {
    return JSON.stringify(value);
  } catch {
    return String(value);
  }
}

type EllipsisCellProps = {
  value?: unknown;
  children?: ReactNode;
  className?: string;
  align?: 'left' | 'right' | 'center';
};

export function EllipsisCell({ value, children, className, align = 'left' }: EllipsisCellProps) {
  const text = cellText(value ?? children);
  const content = children ?? text;
  const title = text === '-' ? undefined : text;

  return (
    <Tooltip title={title} mouseEnterDelay={0.35} placement="topLeft">
      <span className={`table-cell-ellipsis table-cell-ellipsis-${align}${className ? ` ${className}` : ''}`}>{content}</span>
    </Tooltip>
  );
}
