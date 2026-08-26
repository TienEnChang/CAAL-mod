'use client';

import { useCallback, useState } from 'react';
import { useTranslations } from 'next-intl';
import { Check, CircleNotch, X } from '@phosphor-icons/react/dist/ssr';
import type { SetupData } from './setup-wizard';

interface IntegrationsStepProps {
  data: SetupData;
  updateData: (updates: Partial<SetupData>) => void;
}

type TestStatus = 'idle' | 'testing' | 'success' | 'error';

interface IntegrationTestState {
  status: TestStatus;
  error: string | null;
  info: string | null;
}

export function IntegrationsStep({ data, updateData }: IntegrationsStepProps) {
  const t = useTranslations('Settings.integrations');
  const tProviders = useTranslations('Settings.providers');
  const tCommon = useTranslations('Common');

  const [n8nTest, setN8nTest] = useState<IntegrationTestState>({
    status: 'idle',
    error: null,
    info: null,
  });

  // Build full n8n MCP URL from host
  const getN8nMcpUrl = (host: string) => {
    if (!host) return '';
    // Remove trailing slash if present
    const baseUrl = host.replace(/\/$/, '');
    // If user already included the path, use as-is
    if (baseUrl.includes('/mcp-server')) return baseUrl;
    // Otherwise append the standard MCP path
    return `${baseUrl}/mcp-server/http`;
  };

  // Test n8n connection
  const testN8n = useCallback(async () => {
    if (!data.n8n_url || !data.n8n_token) return;

    setN8nTest({ status: 'testing', error: null, info: null });

    const mcpUrl = getN8nMcpUrl(data.n8n_url);

    try {
      const response = await fetch('/api/setup/test-n8n', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ url: mcpUrl, token: data.n8n_token }),
      });

      const result = await response.json();

      if (result.success) {
        setN8nTest({
          status: 'success',
          error: null,
          info: t('connected'),
        });
      } else {
        setN8nTest({
          status: 'error',
          error: result.error || 'Connection failed',
          info: null,
        });
      }
    } catch {
      setN8nTest({
        status: 'error',
        error: 'Failed to connect',
        info: null,
      });
    }
  }, [data.n8n_url, data.n8n_token, t]);

  const StatusIcon = ({ status }: { status: TestStatus }) => {
    switch (status) {
      case 'testing':
        return <CircleNotch className="h-4 w-4 animate-spin text-blue-500" />;
      case 'success':
        return <Check className="h-4 w-4 text-green-500" weight="bold" />;
      case 'error':
        return <X className="h-4 w-4 text-red-500" weight="bold" />;
      default:
        return null;
    }
  };

  const Toggle = ({ enabled, onToggle }: { enabled: boolean; onToggle: () => void }) => (
    <button
      type="button"
      role="switch"
      aria-checked={enabled}
      onClick={onToggle}
      className={`relative inline-flex h-6 w-11 shrink-0 cursor-pointer rounded-full border-2 border-transparent transition-colors duration-200 ease-in-out focus:ring-2 focus:ring-offset-2 focus:outline-none ${
        enabled ? 'bg-primary' : 'bg-muted'
      }`}
    >
      <span
        className={`pointer-events-none inline-block h-5 w-5 transform rounded-full bg-white shadow ring-0 transition duration-200 ease-in-out ${
          enabled ? 'translate-x-5' : 'translate-x-0'
        }`}
      />
    </button>
  );

  return (
    <div className="space-y-4">
      <p className="text-muted-foreground text-sm">{t('optionalNote')}</p>

      {/* n8n */}
      <div className="border-input dark:border-muted space-y-3 rounded-lg border p-4">
        <div className="flex items-center justify-between">
          <div>
            <div className="font-medium">{t('n8nWorkflows')}</div>
            <div className="text-muted-foreground text-xs">{t('n8nDesc')}</div>
          </div>
          <Toggle
            enabled={data.n8n_enabled}
            onToggle={() => updateData({ n8n_enabled: !data.n8n_enabled })}
          />
        </div>

        {data.n8n_enabled && (
          <div className="space-y-3 pt-2">
            <div className="space-y-1">
              <label className="text-muted-foreground text-xs">{tProviders('hostUrl')}</label>
              <input
                type="text"
                value={data.n8n_url}
                onChange={(e) => updateData({ n8n_url: e.target.value })}
                placeholder="http://n8n:5678"
                className="border-input bg-background w-full rounded-md border px-3 py-2 text-sm"
              />
              <p className="text-muted-foreground text-xs">{t('mcpNote')}</p>
            </div>
            <div className="space-y-1">
              <label className="text-muted-foreground text-xs">{t('accessToken')}</label>
              <div className="flex gap-2">
                <input
                  type="password"
                  value={data.n8n_token}
                  onChange={(e) => updateData({ n8n_token: e.target.value })}
                  placeholder="n8n_api_..."
                  className="border-input bg-background flex-1 rounded-md border px-3 py-2 text-sm"
                />
                <button
                  onClick={testN8n}
                  disabled={!data.n8n_url || !data.n8n_token || n8nTest.status === 'testing'}
                  className="bg-muted hover:bg-muted/80 flex items-center gap-2 rounded-md px-3 py-2 text-sm disabled:opacity-50"
                >
                  <StatusIcon status={n8nTest.status} />
                  {tCommon('test')}
                </button>
              </div>
              {n8nTest.error && <p className="text-xs text-red-500">{n8nTest.error}</p>}
              {n8nTest.info && <p className="text-xs text-green-500">{n8nTest.info}</p>}
            </div>
          </div>
        )}
      </div>
    </div>
  );
}
