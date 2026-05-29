import React, { useEffect, useMemo, useState } from 'react';
import { createRoot } from 'react-dom/client';
import {
  Activity,
  Archive,
  BarChart3,
  Boxes,
  FileKey2,
  Gauge,
  HardDrive,
  History,
  KeyRound,
  ListChecks,
  LogOut,
  LockKeyhole,
  Moon,
  Pause,
  Play,
  RefreshCw,
  RotateCcw,
  Search,
  ServerCog,
  Settings,
  ShieldCheck,
  Sun,
  Trash2,
  UserRound,
  XCircle,
} from 'lucide-react';
import { ApiError, createApiClient } from './api';
import './styles.css';

type AnyRecord = Record<string, any>;
type View =
  | 'dashboard'
  | 'runs'
  | 'mirrors'
  | 'credentials'
  | 'governance'
  | 'observability'
  | 'schedules'
  | 'workers'
  | 'platform'
  | 'storage'
  | 'diagnostics'
  | 'logs'
  | 'audit'
  | 'security'
  | 'settings';

const viewMeta: Record<View, { title: string; subtitle: string; icon: React.ReactNode }> = {
  dashboard: { title: '概览', subtitle: '运行状态、同步心跳和关键操作。', icon: <Gauge size={18} /> },
  runs: { title: '同步任务', subtitle: '查看同步队列、执行历史和失败重试入口。', icon: <History size={18} /> },
  mirrors: { title: '镜像配置', subtitle: '维护、导入和导出上游镜像与目标 Registry。', icon: <Boxes size={18} /> },
  credentials: { title: '仓库凭据', subtitle: '加密保存源仓库和目标仓库认证信息。', icon: <KeyRound size={18} /> },
  governance: { title: '仓库治理', subtitle: '保护关键 tag、执行保留策略 dry-run 和查看恢复清单。', icon: <ShieldCheck size={18} /> },
  observability: { title: '可观测', subtitle: '同步成功率、失败聚合、趋势和告警状态。', icon: <BarChart3 size={18} /> },
  schedules: { title: '计划推送', subtitle: '管理业务镜像的定时推送策略和最近失败原因。', icon: <History size={18} /> },
  workers: { title: 'Worker', subtitle: '查看本地和远程执行节点、心跳和最近领取任务。', icon: <ServerCog size={18} /> },
  platform: { title: '平台配置', subtitle: 'Registry 目标、镜像组和多环境视图。', icon: <Archive size={18} /> },
  storage: { title: '存储管理', subtitle: '仓库 tag、删除标记和垃圾回收指引。', icon: <HardDrive size={18} /> },
  diagnostics: { title: '验证诊断', subtitle: '检查依赖、目录、数据库和同步心跳。', icon: <ListChecks size={18} /> },
  logs: { title: '日志 / 事件', subtitle: '同步日志和结构化事件。', icon: <Activity size={18} /> },
  audit: { title: '审计', subtitle: '面板和同步服务的操作记录。', icon: <ShieldCheck size={18} /> },
  security: { title: '安全', subtitle: '公网暴露边界和反向代理建议。', icon: <FileKey2 size={18} /> },
  settings: { title: '设置', subtitle: '同步间隔、并发、重试、通知和数据库配置。', icon: <Settings size={18} /> },
};

const views = Object.keys(viewMeta) as View[];

function cx(...parts: Array<string | false | null | undefined>) {
  return parts.filter(Boolean).join(' ');
}

function hostFromImage(value: string) {
  const first = value.split('/')[0];
  return first.includes('.') || first.includes(':') || first === 'localhost' ? first : 'docker.io';
}

function formatMB(value: any) {
  const bytes = Number(value);
  if (!Number.isFinite(bytes)) return '-';
  return `${(bytes / 1_000_000).toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })} MB`;
}

function formatRate(value: any) {
  const rate = Number(value);
  if (!Number.isFinite(rate)) return '-';
  return `${(rate * 100).toLocaleString(undefined, { maximumFractionDigits: 1 })}%`;
}

function diagnosticMessage(item: AnyRecord) {
  if (item?.details?.free_bytes !== undefined && item?.details?.total_bytes !== undefined) {
    return `剩余 ${formatMB(item.details.free_bytes)} / 总计 ${formatMB(item.details.total_bytes)}`;
  }
  return item.message;
}

function App() {
  const [view, setView] = useState<View>('dashboard');
  const [theme, setTheme] = useState(localStorage.getItem('mirrorRegistryTheme') || 'light');
  const [auth, setAuth] = useState<AnyRecord>({ loading: true, authenticated: false });
  const [status, setStatus] = useState<AnyRecord>({});
  const [opsSummary, setOpsSummary] = useState<AnyRecord>({});
  const [observability, setObservability] = useState<AnyRecord>({});
  const [mirrors, setMirrors] = useState<AnyRecord[]>([]);
  const [runs, setRuns] = useState<AnyRecord[]>([]);
  const [syncQueue, setSyncQueue] = useState<AnyRecord[]>([]);
  const [selectedRun, setSelectedRun] = useState<AnyRecord | null>(null);
  const [platform, setPlatform] = useState<AnyRecord>({});
  const [grouped, setGrouped] = useState<AnyRecord[]>([]);
  const [storage, setStorage] = useState<AnyRecord>({});
  const [diagnostics, setDiagnostics] = useState<AnyRecord>({});
  const [logs, setLogs] = useState('');
  const [events, setEvents] = useState<AnyRecord[]>([]);
  const [audit, setAudit] = useState<AnyRecord[]>([]);
  const [security, setSecurity] = useState<AnyRecord>({});
  const [settings, setSettings] = useState<AnyRecord>({});
  const [credentials, setCredentials] = useState<AnyRecord[]>([]);
  const [governance, setGovernance] = useState<AnyRecord>({ rules: [], policies: [], backup: {}, migration: {} });
  const [schedules, setSchedules] = useState<AnyRecord[]>([]);
  const [workers, setWorkers] = useState<AnyRecord[]>([]);
  const [workerGuide, setWorkerGuide] = useState<AnyRecord>({});
  const [toast, setToast] = useState('');
  const [search, setSearch] = useState('');
  const api = useMemo(() => createApiClient(() => ''), []);

  useEffect(() => {
    document.documentElement.dataset.theme = theme;
    localStorage.setItem('mirrorRegistryTheme', theme);
  }, [theme]);

  function notify(message: string) {
    setToast(message);
    window.setTimeout(() => setToast(''), 2600);
  }

  async function loadAuth() {
    try {
      setAuth({ ...(await api('GET', '/auth/me')), loading: false });
    } catch (error: any) {
      if (error instanceof ApiError && error.status === 401) {
        setAuth({ loading: false, authenticated: false });
        return;
      }
      setAuth({ loading: false, authenticated: false, error: error.message });
    }
  }

  async function login(username: string, password: string) {
    await api('POST', '/auth/login', { username, password });
    await loadAuth();
    await loadStatus();
  }

  async function logout() {
    await api('POST', '/auth/logout', {});
    setAuth({ loading: false, authenticated: false });
  }

  async function loadStatus() {
    const [data, ops] = await Promise.all([
      api('GET', '/status'),
      api('GET', '/ops/summary'),
    ]);
    setStatus(data);
    setOpsSummary(ops);
  }

  async function loadMirrors() {
    setMirrors(await api('GET', '/mirrors'));
  }

  async function loadRuns() {
    const [runRows, queueRows] = await Promise.all([
      api('GET', '/sync-runs?limit=30'),
      api('GET', '/sync-queue?limit=50'),
    ]);
    setRuns(runRows);
    setSyncQueue(queueRows);
  }

  async function loadObservability() {
    setObservability(await api('GET', '/observability/summary'));
  }

  async function loadPlatform() {
    setPlatform(await api('GET', '/platform'));
    setGrouped(await api('GET', '/platform/groups'));
  }

  async function loadStorage() {
    setStorage(await api('GET', '/storage'));
  }

  async function loadDiagnostics() {
    setDiagnostics(await api('POST', '/diagnostics/run'));
  }

  async function loadLogs() {
    setLogs((await api('GET', '/logs?lines=150')).text || '');
    setEvents(await api('GET', '/events?limit=100'));
  }

  async function loadAudit() {
    setAudit(await api('GET', '/audit-logs?limit=100'));
  }

  async function loadSecurity() {
    setSecurity(await api('GET', '/security-guide'));
  }

  async function loadSettings() {
    setSettings(await api('GET', '/settings'));
  }

  async function loadCredentials() {
    setCredentials(await api('GET', '/credentials'));
  }

  async function loadGovernance() {
    const [rules, policies, backup, migration] = await Promise.all([
      api('GET', '/tag-protection'),
      api('GET', '/retention-policies'),
      api('GET', '/backup-restore-guide'),
      api('GET', '/migration/plan'),
    ]);
    setGovernance({ rules, policies, backup, migration });
  }

  async function loadSchedules() {
    setSchedules(await api('GET', '/schedules'));
  }

  async function loadWorkers() {
    const [rows, guide] = await Promise.all([
      api('GET', '/workers'),
      api('GET', '/workers/guide'),
    ]);
    setWorkers(rows);
    setWorkerGuide(guide);
  }

  useEffect(() => {
    loadAuth();
  }, []);

  useEffect(() => {
    if (auth.authenticated) loadStatus().catch((error) => notify(error.message));
  }, [auth.authenticated]);

  useEffect(() => {
    if (!auth.authenticated) return;
    const load = async () => {
      if (view === 'dashboard') await loadStatus();
      if (view === 'runs') await loadRuns();
      if (view === 'mirrors') {
        await loadMirrors();
        await loadCredentials();
      }
      if (view === 'credentials') await loadCredentials();
      if (view === 'governance') await loadGovernance();
      if (view === 'observability') await loadObservability();
      if (view === 'schedules') {
        await loadSchedules();
        await loadCredentials();
      }
      if (view === 'workers') await loadWorkers();
      if (view === 'platform') await loadPlatform();
      if (view === 'storage') await loadStorage();
      if (view === 'diagnostics') await loadDiagnostics();
      if (view === 'logs') await loadLogs();
      if (view === 'audit') await loadAudit();
      if (view === 'security') await loadSecurity();
      if (view === 'settings') await loadSettings();
    };
    load().catch((error) => notify(error.message));
  }, [view, auth.authenticated]);

  const filteredMirrors = useMemo(() => {
    const term = search.trim().toLowerCase();
    if (!term) return mirrors;
    return mirrors.filter((item) => JSON.stringify(item).toLowerCase().includes(term));
  }, [mirrors, search]);

  async function action(label: string, fn: () => Promise<void>) {
    try {
      await fn();
      notify(label);
    } catch (error: any) {
      notify(error.message);
    }
  }

  if (auth.loading) {
    return <div className="auth-page"><div className="login-card"><div className="brand-mark">MR</div><h1>Mirror Registry</h1><p>正在检查登录状态...</p></div></div>;
  }

  if (!auth.authenticated) {
    return <LoginScreen auth={auth} login={login} theme={theme} setTheme={setTheme} />;
  }

  return (
    <div className="shell">
      <aside className="sidebar">
        <div className="brand">
          <div className="brand-mark">MR</div>
          <div>
            <strong>Mirror Registry</strong>
            <span>private image operations</span>
          </div>
        </div>
        <nav>
          {views.map((name) => (
            <button key={name} className={cx('nav-button', view === name && 'active')} onClick={() => setView(name)}>
              {viewMeta[name].icon}
              <span>{viewMeta[name].title}</span>
            </button>
          ))}
        </nav>
        <div className="session-card">
          <span>当前用户</span>
          <strong><UserRound size={15} /> {auth.user?.username || 'admin'}</strong>
          {status.using_default_token && <p className="warn">PANEL_TOKEN 仍为默认值。</p>}
        </div>
      </aside>

      <main>
        <header className="topbar">
          <div>
            <h1>{viewMeta[view].title}</h1>
            <p>{viewMeta[view].subtitle}</p>
          </div>
          <div className="top-actions">
            <span className="user-pill"><UserRound size={15} />{auth.user?.username || 'admin'}</span>
            <button className="ghost" onClick={() => setTheme(theme === 'dark' ? 'light' : 'dark')} title="切换主题">
              {theme === 'dark' ? <Sun size={16} /> : <Moon size={16} />}
            </button>
            <button className="ghost" onClick={() => action('已退出登录', logout)} title="退出登录">
              <LogOut size={16} />
            </button>
            <button className="primary" onClick={() => action('同步已入队', async () => { await api('POST', '/sync'); await loadStatus(); if (view === 'runs') await loadRuns(); })}>
              <Play size={16} />立即同步
            </button>
          </div>
        </header>

        {view === 'dashboard' && <Dashboard status={status} ops={opsSummary} api={api} notify={notify} reload={() => action('已刷新', loadStatus)} />}
        {view === 'runs' && <Runs runs={runs} syncQueue={syncQueue} selectedRun={selectedRun} setSelectedRun={setSelectedRun} api={api} reload={loadRuns} notify={notify} />}
        {view === 'mirrors' && <Mirrors mirrors={filteredMirrors} credentials={credentials} search={search} setSearch={setSearch} api={api} reload={async () => { await loadMirrors(); await loadCredentials(); }} notify={notify} />}
        {view === 'credentials' && <Credentials credentials={credentials} api={api} reload={loadCredentials} notify={notify} />}
        {view === 'governance' && <Governance governance={governance} api={api} reload={loadGovernance} notify={notify} />}
        {view === 'observability' && <Observability data={observability} reload={loadObservability} />}
        {view === 'schedules' && <Schedules schedules={schedules} credentials={credentials} api={api} reload={loadSchedules} notify={notify} />}
        {view === 'workers' && <Workers workers={workers} guide={workerGuide} reload={loadWorkers} />}
        {view === 'platform' && <Platform platform={platform} grouped={grouped} api={api} reload={loadPlatform} notify={notify} />}
        {view === 'storage' && <Storage storage={storage} api={api} reload={loadStorage} notify={notify} />}
        {view === 'diagnostics' && <Diagnostics diagnostics={diagnostics} reload={loadDiagnostics} />}
        {view === 'logs' && <Logs logs={logs} events={events} reload={loadLogs} />}
        {view === 'audit' && <Audit rows={audit} reload={loadAudit} />}
        {view === 'security' && <Security guide={security} />}
        {view === 'settings' && <SettingsView settings={settings} api={api} reload={loadSettings} notify={notify} />}
      </main>

      {toast && <div className="toast">{toast}</div>}
    </div>
  );
}

function LoginScreen({ auth, login, theme, setTheme }: { auth: AnyRecord; login: (username: string, password: string) => Promise<void>; theme: string; setTheme: (theme: string) => void }) {
  const [username, setUsername] = useState('admin');
  const [password, setPassword] = useState('');
  const [error, setError] = useState(auth.error || '');
  const [submitting, setSubmitting] = useState(false);

  async function submit(event: React.FormEvent) {
    event.preventDefault();
    setSubmitting(true);
    setError('');
    try {
      await login(username, password);
    } catch (err: any) {
      setError(err.message || '登录失败');
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <div className="auth-page">
      <section className="login-card">
        <div className="login-head">
          <div className="brand-mark">MR</div>
          <button className="ghost" type="button" onClick={() => setTheme(theme === 'dark' ? 'light' : 'dark')} title="切换主题">
            {theme === 'dark' ? <Sun size={16} /> : <Moon size={16} />}
          </button>
        </div>
        <div>
          <h1>Mirror Registry</h1>
          <p>登录后管理镜像同步、仓库凭据、治理策略和存储统计。</p>
        </div>
        <form className="login-form" onSubmit={submit}>
          <label>
            <span>管理员账号</span>
            <input autoComplete="username" value={username} onChange={(event) => setUsername(event.target.value)} />
          </label>
          <label>
            <span>密码</span>
            <input autoComplete="current-password" type="password" value={password} onChange={(event) => setPassword(event.target.value)} />
          </label>
          {error && <p className="form-error">{error}</p>}
          {auth.admin_initialized === false && <p className="warn">管理员未初始化，请先设置 ADMIN_USERNAME / ADMIN_PASSWORD 并重启 panel。</p>}
          <button className="primary login-submit" disabled={submitting} type="submit">
            <LockKeyhole size={16} />{submitting ? '登录中...' : '登录'}
          </button>
        </form>
      </section>
    </div>
  );
}

function Dashboard({ status, ops, api, notify, reload }: { status: AnyRecord; ops: AnyRecord; api: any; notify: (message: string) => void; reload: () => void }) {
  const failures = ops.sync?.recent_failures || [];
  async function exportBundle() {
    try {
      const bundle = await api('GET', '/ops/diagnostic-bundle');
      const blob = new Blob([JSON.stringify(bundle, null, 2)], { type: 'application/json' });
      const url = URL.createObjectURL(blob);
      const link = document.createElement('a');
      link.href = url;
      link.download = `mirror-registry-diagnostic-${new Date().toISOString().slice(0, 10)}.json`;
      link.click();
      URL.revokeObjectURL(url);
      notify('诊断包已导出');
    } catch (error: any) {
      notify(error.message);
    }
  }
  const cards = [
    ['健康', <Badge value={ops.health || 'ok'} />],
    ['镜像', ops.config?.mirrors ?? status.total ?? 0],
    ['待同步', ops.config?.pending ?? status.pending ?? 0],
    ['失败', failures.length],
    ['删除标记', ops.storage?.deletion_marks ?? 0],
    ['版本', ops.version?.image_tag || status.image_tag || '-'],
  ];
  return (
    <section className="stack">
      <div className="metric-grid">{cards.map(([label, value]) => <Metric key={label} label={label as string} value={value} />)}</div>
      <Panel title="运维摘要" action={<div className="row-actions"><button onClick={reload}><RefreshCw size={16} />刷新</button><button onClick={exportBundle}><FileKey2 size={16} />导出诊断包</button></div>}>
        <dl className="kv">
          <dt>应用版本</dt><dd>{status.app_version || '-'}</dd>
          <dt>镜像 tag</dt><dd>{status.image_tag || '-'}</dd>
          <dt>同步引擎</dt><dd>{status.sync_engine || 'skopeo'}</dd>
          <dt>上次心跳</dt><dd>{status.last_heartbeat || '-'}</dd>
          <dt>健康原因</dt><dd>{(ops.reasons || []).join(', ') || '-'}</dd>
          <dt>最近任务</dt><dd>{ops.sync?.latest_run ? `${ops.sync.latest_run.status} · failed ${ops.sync.latest_run.failed}` : '-'}</dd>
          <dt>磁盘状态</dt><dd>{ops.storage?.disk_low ? '低空间' : '正常'} {ops.storage?.disk_free_bytes || ''}</dd>
        </dl>
      </Panel>
      {failures.length > 0 && <Panel title="最近失败">
        <table><thead><tr><th>镜像</th><th>阶段</th><th>原因</th><th>建议</th></tr></thead>
          <tbody>{failures.map((item: AnyRecord) => <tr key={item.id}><td>{item.source}</td><td>{item.step || '-'}</td><td>{item.explanation?.reason || item.error}</td><td>{item.explanation?.suggestion || '-'}</td></tr>)}</tbody>
        </table>
      </Panel>}
    </section>
  );
}

function Metric({ label, value }: { label: string; value: React.ReactNode }) {
  return <div className="metric"><span>{label}</span><strong>{value}</strong></div>;
}

function Panel({ title, action, children }: { title: string; action?: React.ReactNode; children: React.ReactNode }) {
  return <section className="panel"><div className="panel-head"><h2>{title}</h2>{action}</div>{children}</section>;
}

function Runs({ runs, syncQueue, selectedRun, setSelectedRun, api, reload, notify }: any) {
  async function openRun(id: number) {
    setSelectedRun(await api('GET', `/sync-runs/${id}`));
  }
  async function queueAction(id: number, action: string, label: string) {
    await api('POST', `/sync-queue/${id}/${action}`, {});
    await reload();
    notify(label);
  }
  const queueRows = syncQueue || [];
  return (
    <div className="stack">
      <Panel title="同步队列" action={<button onClick={reload}><RefreshCw size={16} />刷新</button>}>
        {queueRows.length === 0 ? <p className="warn">当前没有同步队列任务。</p> : (
          <table><thead><tr><th>ID</th><th>原因</th><th>状态</th><th>镜像</th><th>优先级</th><th>尝试</th><th>Run</th><th>消息</th><th>时间</th><th>操作</th></tr></thead>
            <tbody>{queueRows.map((task: AnyRecord) => {
              const status = String(task.status || '');
              const terminal = ['completed', 'failed', 'canceled'].includes(status);
              return (
                <tr key={task.id}>
                  <td>{task.id}</td>
                  <td>{task.reason}</td>
                  <td><Badge value={status} /></td>
                  <td className="breakable mono">{(task.sources || []).join(', ') || '全部镜像'}</td>
                  <td>{task.priority}</td>
                  <td>{task.attempts}</td>
                  <td>{task.run_id ? <button onClick={() => openRun(task.run_id)}>#{task.run_id}</button> : '-'}</td>
                  <td>{task.message || '-'}</td>
                  <td>{task.started_at || task.scheduled_at || task.created_at}</td>
                  <td className="row-actions">
                    {status === 'queued' && <button onClick={() => queueAction(task.id, 'pause', '队列任务已暂停')}><Pause size={14} />暂停</button>}
                    {status === 'paused' && <button onClick={() => queueAction(task.id, 'resume', '队列任务已恢复')}><Play size={14} />恢复</button>}
                    {['queued', 'paused', 'running'].includes(status) && <button onClick={() => queueAction(task.id, 'cancel', '队列任务已取消')}><XCircle size={14} />取消</button>}
                    {terminal && <button onClick={() => queueAction(task.id, 'replay', '队列任务已重放')}><RotateCcw size={14} />重放</button>}
                  </td>
                </tr>
              );
            })}</tbody>
          </table>
        )}
      </Panel>
      <Panel title="任务历史" action={<button onClick={reload}><RefreshCw size={16} />刷新</button>}>
        <table><thead><tr><th>ID</th><th>原因</th><th>状态</th><th>更新</th><th>失败</th><th>时间</th><th></th></tr></thead>
          <tbody>{runs.map((run: AnyRecord) => <tr key={run.id}><td>{run.id}</td><td>{run.reason}</td><td><Badge value={run.status} /></td><td>{run.updated}</td><td>{run.failed}</td><td>{run.started_at}</td><td><button onClick={() => openRun(run.id)}>详情</button></td></tr>)}</tbody>
        </table>
      </Panel>
      {selectedRun && <Panel title={`任务 ${selectedRun.run.id}`}>
        <table><thead><tr><th>镜像</th><th>目标</th><th>状态</th><th>阶段</th><th>错误</th><th></th></tr></thead>
          <tbody>{selectedRun.items.map((item: AnyRecord) => <tr key={item.id}><td>{item.source}</td><td>{item.target}</td><td><Badge value={item.status} /></td><td>{item.step}</td><td>{item.error}</td><td>{item.status === 'failed' && <button onClick={() => api('POST', `/sync-run-items/${item.id}/retry`).then(() => { reload(); notify('失败项已入队'); })}>重试</button>}</td></tr>)}</tbody>
        </table>
      </Panel>}
    </div>
  );
}

function Mirrors({ mirrors, credentials, search, setSearch, api, reload, notify }: any) {
  const [form, setForm] = useState({ source: '', target: '', source_credential_id: '', target_credential_id: '' });
  const [preflightRemote, setPreflightRemote] = useState(false);
  const [preflightResult, setPreflightResult] = useState<AnyRecord | null>(null);
  const [discoveryForm, setDiscoveryForm] = useState({
    source_type: 'auto',
    target_registry: 'localhost:5000',
    mode: 'missing_only',
    trigger_sync: false,
    content: '',
  });
  const [discoveryResult, setDiscoveryResult] = useState<AnyRecord | null>(null);
  async function discover() {
    const result = await api('POST', '/mirrors/discover', discoveryForm);
    setDiscoveryResult(result);
    notify(`发现 ${result.summary.importable} 个可导入镜像`);
  }
  async function importDiscovery() {
    const result = await api('POST', '/mirrors/discover/import', discoveryForm);
    setDiscoveryResult({ ...(discoveryResult || {}), summary: result.summary });
    await reload();
    notify(`已导入 ${result.imported} 个镜像`);
  }
  async function preflightDraft() {
    const result = await api('POST', '/mirrors/preflight', { ...form, check_remote: preflightRemote });
    setPreflightResult({ summary: { total: 1, [result.summary.status]: 1 }, items: [result] });
    notify(`预检结果: ${result.summary.status}`);
  }
  async function preflightMirror(mirror: AnyRecord) {
    const result = await api('POST', '/mirrors/preflight', { ...mirror, check_remote: preflightRemote });
    setPreflightResult({ summary: { total: 1, [result.summary.status]: 1 }, items: [result] });
    notify(`预检结果: ${result.summary.status}`);
  }
  async function preflightAll() {
    const result = await api('POST', '/mirrors/preflight/batch', { check_remote: preflightRemote });
    setPreflightResult(result);
    notify(`批量预检: ${result.summary.error} error / ${result.summary.warn} warn`);
  }
  return (
    <div className="stack">
      <Panel title="添加镜像">
        <div className="form-grid">
          <input placeholder="docker.io/library/busybox:latest" value={form.source} onChange={(e) => setForm({ ...form, source: e.target.value })} />
          <input placeholder="localhost:5000/library/busybox:latest" value={form.target} onChange={(e) => setForm({ ...form, target: e.target.value })} />
          <select value={form.source_credential_id} onChange={(e) => setForm({ ...form, source_credential_id: e.target.value })}><option value="">源凭据自动</option>{credentials.map((c: AnyRecord) => <option key={c.id} value={c.id}>{c.name}</option>)}</select>
          <select value={form.target_credential_id} onChange={(e) => setForm({ ...form, target_credential_id: e.target.value })}><option value="">目标凭据自动</option>{credentials.map((c: AnyRecord) => <option key={c.id} value={c.id}>{c.name}</option>)}</select>
          <button className="primary" onClick={() => api('POST', '/mirrors', form).then(() => { setForm({ source: '', target: '', source_credential_id: '', target_credential_id: '' }); reload(); notify('镜像已添加'); })}>添加</button>
        </div>
      </Panel>
      <Panel title="同步预检">
        <div className="form-grid">
          <label className="checkline"><input type="checkbox" checked={preflightRemote} onChange={(e) => setPreflightRemote(e.target.checked)} />远程探测</label>
          <button onClick={preflightDraft}><ListChecks size={16} />预检当前表单</button>
          <button onClick={preflightAll}><ListChecks size={16} />批量预检</button>
        </div>
        {preflightResult && <div className="discovery-result">
          <div className="chip-list">
            <span className="chip">总数 {preflightResult.summary?.total ?? preflightResult.items?.length ?? 0}</span>
            <span className="chip">OK {preflightResult.summary?.ok ?? 0}</span>
            <span className="chip">Warn {preflightResult.summary?.warn ?? 0}</span>
            <span className="chip">Error {preflightResult.summary?.error ?? 0}</span>
            <span className="chip">{preflightRemote ? 'remote' : 'local-only'}</span>
          </div>
          <table><thead><tr><th>源镜像</th><th>目标</th><th>结果</th><th>检查项</th></tr></thead>
            <tbody>{(preflightResult.items || []).map((item: AnyRecord, index: number) => <tr key={`${item.source}-${index}`}><td>{item.source}</td><td>{item.target}</td><td><Badge value={item.summary?.status} /></td><td>{(item.checks || []).map((check: AnyRecord) => `${check.name}:${check.status}`).join(' / ')}</td></tr>)}</tbody>
          </table>
        </div>}
      </Panel>
      <Panel title="镜像发现">
        <div className="form-grid discovery-form">
          <select value={discoveryForm.source_type} onChange={(e) => setDiscoveryForm({ ...discoveryForm, source_type: e.target.value })}>
            <option value="auto">自动识别</option>
            <option value="compose">Docker Compose</option>
            <option value="kubernetes">Kubernetes YAML</option>
            <option value="text">纯文本</option>
          </select>
          <input value={discoveryForm.target_registry} onChange={(e) => setDiscoveryForm({ ...discoveryForm, target_registry: e.target.value })} placeholder="localhost:5000" />
          <select value={discoveryForm.mode} onChange={(e) => setDiscoveryForm({ ...discoveryForm, mode: e.target.value })}>
            <option value="missing_only">只导入缺失项</option>
            <option value="merge">合并导入</option>
            <option value="replace">覆盖导入</option>
          </select>
          <label className="checkline"><input type="checkbox" checked={discoveryForm.trigger_sync} onChange={(e) => setDiscoveryForm({ ...discoveryForm, trigger_sync: e.target.checked })} />导入后同步</label>
          <textarea className="discovery-textarea" value={discoveryForm.content} onChange={(e) => setDiscoveryForm({ ...discoveryForm, content: e.target.value })} placeholder="services:&#10;  web:&#10;    image: nginx:1.27" />
          <button onClick={discover}><Search size={16} />dry-run</button>
          <button className="primary" onClick={importDiscovery}>导入</button>
        </div>
        {discoveryResult && <div className="discovery-result">
          <div className="chip-list">
            <span className="chip">发现 {discoveryResult.summary?.extracted ?? 0}</span>
            <span className="chip">可导入 {discoveryResult.summary?.importable ?? 0}</span>
            <span className="chip">新增 {discoveryResult.summary?.new ?? 0}</span>
            <span className="chip">问题 {discoveryResult.problems?.length ?? discoveryResult.summary?.invalid ?? 0}</span>
          </div>
          <table><thead><tr><th>来源</th><th>源镜像</th><th>目标</th><th>状态</th></tr></thead>
            <tbody>{(discoveryResult.items || []).map((item: AnyRecord, index: number) => <tr key={`${item.location}-${index}`}><td>{item.location || item.source_type}</td><td>{item.source || item.raw}</td><td>{item.target || '-'}</td><td><Badge value={item.action} /></td></tr>)}</tbody>
          </table>
        </div>}
      </Panel>
      <Panel title="镜像列表" action={<div className="search"><Search size={15} /><input value={search} onChange={(e) => setSearch(e.target.value)} placeholder="搜索镜像、tag、环境" /></div>}>
        <table><thead><tr><th>源</th><th>目标</th><th>凭据</th><th>状态</th><th>操作</th></tr></thead>
          <tbody>{mirrors.map((m: AnyRecord) => <tr key={m.index}><td>{m.source}</td><td>{m.target}</td><td>{m.source_credential_id || `host:${hostFromImage(m.source)}`} / {m.target_credential_id || `host:${hostFromImage(m.target)}`}</td><td><Badge value={m.synced ? 'synced' : 'pending'} /></td><td className="row-actions"><button onClick={() => preflightMirror(m)}><ListChecks size={14} />预检</button><button onClick={() => api('POST', `/mirrors/${m.index}/sync`).then(() => notify('单镜像同步已入队'))}>同步</button><button onClick={() => api('POST', `/mirrors/${m.index}/reset`).then(reload)}>重置</button><button className="danger" onClick={() => api('DELETE', `/mirrors/${m.index}`).then(reload)}><Trash2 size={14} /></button></td></tr>)}</tbody>
        </table>
      </Panel>
    </div>
  );
}

function Credentials({ credentials, api, reload, notify }: any) {
  const [form, setForm] = useState({ id: '', name: '', registry_host: '', username: '', secret: '', scope: 'both' });
  async function save() {
    await api('POST', '/credentials', { ...form, id: form.id || undefined });
    setForm({ id: '', name: '', registry_host: '', username: '', secret: '', scope: 'both' });
    await reload();
    notify('凭据已保存');
  }
  return (
    <div className="stack">
      <Panel title="新增凭据">
        <div className="form-grid credentials-form">
          <input placeholder="凭据 ID（可选）" value={form.id} onChange={(e) => setForm({ ...form, id: e.target.value })} />
          <input placeholder="显示名称" value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} />
          <input placeholder="registry host，例如 ghcr.io" value={form.registry_host} onChange={(e) => setForm({ ...form, registry_host: e.target.value })} />
          <input placeholder="用户名" value={form.username} onChange={(e) => setForm({ ...form, username: e.target.value })} />
          <input type="password" placeholder="token/password" value={form.secret} onChange={(e) => setForm({ ...form, secret: e.target.value })} />
          <select value={form.scope} onChange={(e) => setForm({ ...form, scope: e.target.value })}><option value="both">源和目标</option><option value="source">仅源</option><option value="target">仅目标</option></select>
          <button className="primary" onClick={save}><KeyRound size={16} />保存凭据</button>
        </div>
      </Panel>
      <Panel title="已保存凭据">
        <table><thead><tr><th>ID</th><th>名称</th><th>Host</th><th>用户名</th><th>Scope</th><th>状态</th><th>操作</th></tr></thead>
          <tbody>{credentials.map((c: AnyRecord) => <tr key={c.id}><td>{c.id}</td><td>{c.name}</td><td>{c.registry_host}</td><td>{c.username}</td><td>{c.scope}</td><td><Badge value={c.configured ? 'configured' : 'empty'} /></td><td className="row-actions"><button onClick={() => api('POST', `/credentials/${c.id}/test`, {}).then((r: AnyRecord) => notify(`测试结果: ${r.status}`))}>测试</button><button className="danger" onClick={() => api('DELETE', `/credentials/${c.id}`).then(reload)}><Trash2 size={14} /></button></td></tr>)}</tbody>
        </table>
      </Panel>
    </div>
  );
}

function Governance({ governance, api, reload, notify }: any) {
  const [rule, setRule] = useState({ id: '', name: '', repo_pattern: '*', tag_pattern: 'v*', environment: '*', enabled: true, reason: 'release tag' });
  const [policy, setPolicy] = useState({ id: '', name: '', repo_pattern: '*', environment: '*', keep_last: 5, max_age_days: 30, enabled: false });
  const [dryRun, setDryRun] = useState<AnyRecord | null>(null);
  const [restoreDrill, setRestoreDrill] = useState<AnyRecord | null>(null);
  const [migrationPreflight, setMigrationPreflight] = useState<AnyRecord | null>(null);
  async function saveRule() {
    await api('POST', '/tag-protection', { ...rule, id: rule.id || undefined });
    await reload();
    notify('保护规则已保存');
  }
  async function savePolicy() {
    await api('POST', '/retention-policies', { ...policy, id: policy.id || undefined });
    await reload();
    notify('保留策略已保存');
  }
  async function runPolicy(id: string, apply = false) {
    const result = await api('POST', `/retention-policies/${id}/${apply ? 'apply' : 'dry-run'}`, {});
    setDryRun(result);
    await reload();
    notify(apply ? '保留策略已标记候选 tag' : 'dry-run 已完成');
  }
  async function runRestoreDrill() {
    const result = await api('POST', '/backup-restore/drill', { require_credentials_secret: true, verify_registry_sample: false });
    setRestoreDrill(result);
    notify(`恢复演练: ${result.summary.status}`);
  }
  async function runMigrationPreflight() {
    const result = await api('POST', '/migration/preflight', {});
    setMigrationPreflight(result);
    notify(`迁移预检: ${result.summary.status}`);
  }
  return (
    <div className="stack">
      <Panel title="Tag 保护规则">
        <div className="form-grid">
          <input placeholder="id" value={rule.id} onChange={(e) => setRule({ ...rule, id: e.target.value })} />
          <input placeholder="名称" value={rule.name} onChange={(e) => setRule({ ...rule, name: e.target.value })} />
          <input placeholder="repo pattern" value={rule.repo_pattern} onChange={(e) => setRule({ ...rule, repo_pattern: e.target.value })} />
          <input placeholder="tag pattern" value={rule.tag_pattern} onChange={(e) => setRule({ ...rule, tag_pattern: e.target.value })} />
          <input placeholder="environment" value={rule.environment} onChange={(e) => setRule({ ...rule, environment: e.target.value })} />
          <input placeholder="原因" value={rule.reason} onChange={(e) => setRule({ ...rule, reason: e.target.value })} />
          <button className="primary" onClick={saveRule}>保存规则</button>
        </div>
        <table><thead><tr><th>ID</th><th>Repo</th><th>Tag</th><th>环境</th><th>状态</th></tr></thead><tbody>{(governance.rules || []).map((item: AnyRecord) => <tr key={item.id}><td>{item.id}</td><td>{item.repo_pattern}</td><td>{item.tag_pattern}</td><td>{item.environment}</td><td><Badge value={item.enabled ? 'enabled' : 'disabled'} /></td></tr>)}</tbody></table>
      </Panel>
      <Panel title="保留策略">
        <div className="form-grid">
          <input placeholder="id" value={policy.id} onChange={(e) => setPolicy({ ...policy, id: e.target.value })} />
          <input placeholder="名称" value={policy.name} onChange={(e) => setPolicy({ ...policy, name: e.target.value })} />
          <input placeholder="repo pattern" value={policy.repo_pattern} onChange={(e) => setPolicy({ ...policy, repo_pattern: e.target.value })} />
          <input placeholder="environment" value={policy.environment} onChange={(e) => setPolicy({ ...policy, environment: e.target.value })} />
          <input type="number" value={policy.keep_last} onChange={(e) => setPolicy({ ...policy, keep_last: Number(e.target.value) })} />
          <input type="number" value={policy.max_age_days} onChange={(e) => setPolicy({ ...policy, max_age_days: Number(e.target.value) })} />
          <button className="primary" onClick={savePolicy}>保存策略</button>
        </div>
        <table><thead><tr><th>ID</th><th>Repo</th><th>保留</th><th>天数</th><th>状态</th><th>操作</th></tr></thead><tbody>{(governance.policies || []).map((item: AnyRecord) => <tr key={item.id}><td>{item.id}</td><td>{item.repo_pattern}</td><td>{item.keep_last}</td><td>{item.max_age_days || '-'}</td><td><Badge value={item.enabled ? 'enabled' : 'dry-run'} /></td><td className="row-actions"><button onClick={() => runPolicy(item.id)}>dry-run</button><button onClick={() => runPolicy(item.id, true)}>标记</button></td></tr>)}</tbody></table>
        {dryRun && <pre>{JSON.stringify(dryRun, null, 2)}</pre>}
      </Panel>
      <Panel title="备份恢复清单" action={<button onClick={runRestoreDrill}><ListChecks size={16} />恢复演练</button>}>
        <pre>{JSON.stringify(governance.backup || {}, null, 2)}</pre>
        {restoreDrill && <pre>{JSON.stringify(restoreDrill, null, 2)}</pre>}
      </Panel>
      <Panel title="迁移恢复向导" action={<button onClick={runMigrationPreflight}><ListChecks size={16} />迁移预检</button>}>
        <div className="chip-list">
          <span className="chip">配置 {governance.migration?.manifest?.source?.app_version || '-'}</span>
          <span className="chip">镜像 {governance.migration?.manifest?.source?.image_tag || '-'}</span>
          <span className="chip">队列 {governance.migration?.manifest?.queue?.active ?? 0}</span>
        </div>
        {migrationPreflight ? (
          <table><thead><tr><th>检查项</th><th>状态</th><th>结果</th><th>建议</th></tr></thead>
            <tbody>{(migrationPreflight.checks || []).map((item: AnyRecord) => <tr key={item.name}><td>{item.name}</td><td><Badge value={item.status} /></td><td>{item.message}</td><td>{item.suggestion || '-'}</td></tr>)}</tbody>
          </table>
        ) : (
          <table><thead><tr><th>阶段</th><th>步骤</th></tr></thead>
            <tbody>{(governance.migration?.restore_steps || []).map((step: string, index: number) => <tr key={step}><td>{index + 1}</td><td>{step}</td></tr>)}</tbody>
          </table>
        )}
        <pre>{JSON.stringify(governance.migration?.commands || {}, null, 2)}</pre>
      </Panel>
    </div>
  );
}

function Observability({ data, reload }: { data: AnyRecord; reload: () => void }) {
  const window24 = data.windows?.['24h'] || {};
  const window7d = data.windows?.['7d'] || {};
  const alerts = data.alerts || [];
  const failures = data.failure_breakdown || [];
  const trend = data.trend || [];
  const cards = [
    ['健康', <Badge value={data.health || 'ok'} />],
    ['24h 成功率', formatRate(window24.success_rate)],
    ['24h 失败项', window24.failed_items ?? 0],
    ['7d 成功率', formatRate(window7d.success_rate)],
    ['告警', alerts.length],
    ['删除标记', data.storage?.deletion_marks ?? 0],
  ];
  return (
    <div className="stack">
      <div className="metric-grid">{cards.map(([label, value]) => <Metric key={label} label={label as string} value={value} />)}</div>
      <Panel title="告警状态" action={<button onClick={reload}><RefreshCw size={16} />刷新</button>}>
        {alerts.length === 0 ? <p className="warn">当前无活动告警。</p> : (
          <table><thead><tr><th>级别</th><th>告警</th><th>说明</th><th>建议</th><th>指纹</th></tr></thead>
            <tbody>{alerts.map((item: AnyRecord) => <tr key={item.fingerprint}><td><Badge value={item.severity} /></td><td>{item.title}</td><td>{item.message}</td><td>{item.suggestion}</td><td className="mono">{item.fingerprint}</td></tr>)}</tbody>
          </table>
        )}
      </Panel>
      <Panel title="失败聚合">
        {failures.length === 0 ? <p className="warn">最近 7 天无失败聚合。</p> : (
          <table><thead><tr><th>分类</th><th>次数</th><th>源 Registry</th><th>目标 Registry</th><th>镜像组</th><th>建议</th></tr></thead>
            <tbody>{failures.map((item: AnyRecord) => <tr key={`${item.category}-${item.source_registry}-${item.target_registry}-${item.group}`}><td><Badge value={item.category} /></td><td>{item.count}</td><td>{item.source_registry}</td><td>{item.target_registry}</td><td>{item.project}/{item.environment}/{item.group}</td><td>{item.suggestion || item.reason}</td></tr>)}</tbody>
          </table>
        )}
      </Panel>
      <Panel title="同步趋势">
        <table><thead><tr><th>时间桶</th><th>任务数</th><th>成功</th><th>失败</th><th>失败项</th></tr></thead>
          <tbody>{trend.map((item: AnyRecord) => <tr key={item.bucket_start}><td>{item.bucket_start}</td><td>{item.total_runs}</td><td>{item.completed_runs}</td><td>{item.failed_runs}</td><td>{item.failed_items}</td></tr>)}</tbody>
        </table>
      </Panel>
      <Panel title="通知状态">
        <dl className="kv">
          <dt>Webhook</dt><dd>{data.notifications?.webhook_configured ? '已配置' : '未配置'}</dd>
          <dt>去重窗口</dt><dd>{data.notifications?.dedupe_seconds ?? 1800} 秒</dd>
          <dt>上次发送</dt><dd>{data.notifications?.last_sent_at || '-'}</dd>
          <dt>发送事件</dt><dd>{data.notifications?.last_sent_event || '-'}</dd>
          <dt>上次抑制</dt><dd>{data.notifications?.last_suppressed_at || '-'}</dd>
          <dt>抑制事件</dt><dd>{data.notifications?.last_suppressed_event || '-'}</dd>
          <dt>Sync 心跳</dt><dd>{data.runtime?.last_heartbeat || '-'}</dd>
          <dt>磁盘余量</dt><dd>{formatMB(data.storage?.disk_free_bytes)}</dd>
        </dl>
      </Panel>
    </div>
  );
}

function Schedules({ schedules, credentials, api, reload, notify }: any) {
  const [form, setForm] = useState({ id: '', name: '', source: '', target: '', cron: '0 18 * * *', enabled: false, allow_latest: false, source_credential_id: '', target_credential_id: '' });
  async function save() {
    await api('POST', '/schedules', { ...form, id: form.id || undefined });
    setForm({ id: '', name: '', source: '', target: '', cron: '0 18 * * *', enabled: false, allow_latest: false, source_credential_id: '', target_credential_id: '' });
    await reload();
    notify('计划推送已保存');
  }
  async function run(id: string) {
    await api('POST', `/schedules/${id}/run`, {});
    await reload();
    notify('计划推送已入队');
  }
  return (
    <div className="stack">
      <Panel title="新增计划">
        <div className="form-grid">
          <input placeholder="id" value={form.id} onChange={(e) => setForm({ ...form, id: e.target.value })} />
          <input placeholder="名称" value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} />
          <input placeholder="源镜像" value={form.source} onChange={(e) => setForm({ ...form, source: e.target.value })} />
          <input placeholder="目标镜像" value={form.target} onChange={(e) => setForm({ ...form, target: e.target.value })} />
          <input placeholder="UTC cron，例如 0 18 * * *" value={form.cron} onChange={(e) => setForm({ ...form, cron: e.target.value })} />
          <select value={form.source_credential_id} onChange={(e) => setForm({ ...form, source_credential_id: e.target.value })}><option value="">源凭据自动</option>{credentials.map((c: AnyRecord) => <option key={c.id} value={c.id}>{c.name}</option>)}</select>
          <select value={form.target_credential_id} onChange={(e) => setForm({ ...form, target_credential_id: e.target.value })}><option value="">目标凭据自动</option>{credentials.map((c: AnyRecord) => <option key={c.id} value={c.id}>{c.name}</option>)}</select>
          <label className="checkline"><input type="checkbox" checked={form.enabled} onChange={(e) => setForm({ ...form, enabled: e.target.checked })} />启用</label>
          <label className="checkline"><input type="checkbox" checked={form.allow_latest} onChange={(e) => setForm({ ...form, allow_latest: e.target.checked })} />允许 latest</label>
          <button className="primary" onClick={save}>保存计划</button>
        </div>
      </Panel>
      <Panel title="计划列表">
        <table><thead><tr><th>ID</th><th>源</th><th>目标</th><th>Cron</th><th>启用</th><th>上次</th><th>下次</th><th>最近错误</th><th>操作</th></tr></thead><tbody>{schedules.map((item: AnyRecord) => <tr key={item.id}><td>{item.id}</td><td>{item.source}</td><td>{item.target}</td><td>{item.cron}</td><td><Badge value={item.enabled ? 'enabled' : 'disabled'} /></td><td>{item.last_run_at || '-'}</td><td>{item.next_run_at || '-'}</td><td>{item.last_error || '-'}</td><td><button onClick={() => run(item.id)}>运行</button></td></tr>)}</tbody></table>
      </Panel>
    </div>
  );
}

function Workers({ workers, guide, reload }: any) {
  return (
    <div className="stack">
      <Panel title="Worker 状态" action={<button onClick={reload}><RefreshCw size={16} />刷新</button>}>
        <table><thead><tr><th>ID</th><th>名称</th><th>环境</th><th>状态</th><th>标签</th><th>能力</th><th>心跳</th><th>最近任务</th><th>消息</th></tr></thead>
          <tbody>{workers.map((worker: AnyRecord) => <tr key={worker.worker_id}><td className="mono">{worker.worker_id}</td><td>{worker.name}</td><td>{worker.environment}</td><td><Badge value={worker.status} /></td><td>{(worker.labels || []).join(', ') || '-'}</td><td>{(worker.capabilities || []).join(', ') || '-'}</td><td>{worker.last_heartbeat}</td><td>{worker.latest_claim ? `#${worker.latest_claim.queue_id} ${worker.latest_claim.status}` : '-'}</td><td>{worker.message || '-'}</td></tr>)}</tbody>
        </table>
      </Panel>
      <Panel title="Worker 接入">
        <dl className="kv">
          <dt>WORKER_TOKEN</dt><dd>{guide.enabled ? '已配置' : '未配置'}</dd>
          <dt>心跳</dt><dd>{guide.endpoints?.heartbeat || '-'}</dd>
          <dt>领取</dt><dd>{guide.endpoints?.claim || '-'}</dd>
          <dt>回写</dt><dd>{guide.endpoints?.complete || '-'}</dd>
        </dl>
        <pre>{JSON.stringify(guide.notes || [], null, 2)}</pre>
      </Panel>
    </div>
  );
}

function Platform({ platform, grouped, api, reload, notify }: any) {
  const [registry, setRegistry] = useState({ id: '', name: '', url: '', copy_host: '' });
  const [group, setGroup] = useState({ id: '', name: '', project: '', environment: '', namespace: '', registry: 'local' });
  return (
    <div className="stack">
      <Panel title="Registry 目标">
        <div className="chip-list">{(platform.registries || []).map((item: AnyRecord) => <span className="chip" key={item.id}>{item.id} · {item.url}</span>)}</div>
        <div className="form-grid"><input placeholder="id" value={registry.id} onChange={(e) => setRegistry({ ...registry, id: e.target.value })} /><input placeholder="name" value={registry.name} onChange={(e) => setRegistry({ ...registry, name: e.target.value })} /><input placeholder="url" value={registry.url} onChange={(e) => setRegistry({ ...registry, url: e.target.value })} /><input placeholder="copy_host" value={registry.copy_host} onChange={(e) => setRegistry({ ...registry, copy_host: e.target.value })} /><button onClick={() => api('POST', '/registries', registry).then(() => { reload(); notify('Registry 已保存'); })}>保存</button></div>
      </Panel>
      <Panel title="镜像组">
        <div className="chip-list">{(platform.mirror_groups || []).map((item: AnyRecord) => <span className="chip" key={item.id}>{item.project}/{item.environment}/{item.namespace}</span>)}</div>
        <div className="form-grid"><input placeholder="id" value={group.id} onChange={(e) => setGroup({ ...group, id: e.target.value })} /><input placeholder="name" value={group.name} onChange={(e) => setGroup({ ...group, name: e.target.value })} /><input placeholder="project" value={group.project} onChange={(e) => setGroup({ ...group, project: e.target.value })} /><input placeholder="environment" value={group.environment} onChange={(e) => setGroup({ ...group, environment: e.target.value })} /><input placeholder="namespace" value={group.namespace} onChange={(e) => setGroup({ ...group, namespace: e.target.value })} /><button onClick={() => api('POST', '/mirror-groups', group).then(() => { reload(); notify('镜像组已保存'); })}>保存</button></div>
      </Panel>
      <Panel title="分组视图"><pre>{JSON.stringify(grouped, null, 2)}</pre></Panel>
    </div>
  );
}

function Storage({ storage, api, reload, notify }: any) {
  async function recalculate() {
    await api('POST', '/storage/stats/recalculate', {});
    notify('体积统计重算已排队');
  }
  const rows = (storage.images || []).flatMap((image: AnyRecord) =>
    (image.tags || []).map((tag: AnyRecord) => ({
      image,
      tag,
      logical: tag.stats?.logical_size_bytes,
      deduped: tag.stats?.deduplicated_size_bytes ?? image.deduplicated_size_bytes ?? image.estimated_size_bytes,
    })),
  );
  return (
    <div className="stack">
      <div className="metric-grid storage-summary">
        <Metric label="估算总占用" value={formatMB(storage.estimated_total_bytes)} />
        <Metric label="物理 blob" value={formatMB(storage.physical_blob_bytes)} />
        <Metric label="镜像仓库" value={(storage.images || []).length} />
      </div>
      <Panel title="本地仓库" action={<button onClick={recalculate}>重算体积</button>}>
        <table><thead><tr><th>仓库</th><th>Tag</th><th className="num">逻辑体积</th><th className="num">去重体积</th><th className="num">共享层</th><th>删除标记</th></tr></thead>
          <tbody>{rows.map(({ image, tag, logical, deduped }: AnyRecord) => <tr key={`${image.repo}:${tag.name}`}><td className="breakable mono">{image.repo}</td><td className="breakable mono">{tag.name}</td><td className="num">{formatMB(logical)}</td><td className="num">{formatMB(deduped)}</td><td className="num">{tag.stats?.shared_blob_count ?? '-'}</td><td>{tag.marked_for_deletion ? '已标记' : <button onClick={() => api('POST', '/storage/delete-mark', { repo: image.repo, tag: tag.name, reason: 'manual' }).then(() => { reload(); notify('已标记'); })}>标记</button>}</td></tr>)}</tbody>
        </table>
      </Panel>
      <Panel title="垃圾回收指引"><pre>{(storage.garbage_collection?.commands || []).join('\n')}</pre></Panel>
    </div>
  );
}

function Diagnostics({ diagnostics, reload }: any) {
  return <Panel title="诊断结果" action={<button onClick={reload}><RefreshCw size={16} />重新检查</button>}><div className="check-grid">{(diagnostics.checks || []).map((item: AnyRecord) => <div className="check" key={item.name}><div className="check-status"><Badge value={item.status} /></div><strong>{item.name}</strong><span className="breakable">{diagnosticMessage(item)}</span>{item.suggestion && <small className="breakable">{item.suggestion}</small>}</div>)}</div></Panel>;
}

function Logs({ logs, events, reload }: any) {
  return <div className="stack"><Panel title="文本日志" action={<button onClick={reload}><RefreshCw size={16} />刷新</button>}><pre>{logs}</pre></Panel><Panel title="事件"><table><tbody>{events.map((e: AnyRecord) => <tr key={e.id}><td>{e.created_at}</td><td><Badge value={e.level} /></td><td>{e.message}</td></tr>)}</tbody></table></Panel></div>;
}

function Audit({ rows, reload }: any) {
  return <Panel title="审计日志" action={<button onClick={reload}><RefreshCw size={16} />刷新</button>}><table><thead><tr><th>时间</th><th>Actor</th><th>动作</th><th>资源</th><th>详情</th></tr></thead><tbody>{rows.map((row: AnyRecord) => <tr key={row.id}><td>{row.created_at}</td><td>{row.actor}</td><td>{row.action}</td><td>{row.resource_type}:{row.resource_id}</td><td><code>{JSON.stringify(row.detail)}</code></td></tr>)}</tbody></table></Panel>;
}

function Security({ guide }: any) {
  return <div className="stack"><Panel title="公网暴露安全边界"><p>{guide.public_exposure_boundary}</p></Panel><Panel title="Nginx Basic Auth"><pre>{(guide.nginx_basic_auth || []).join('\n')}</pre></Panel></div>;
}

function SettingsView({ settings, api, reload, notify }: any) {
  const [form, setForm] = useState<AnyRecord>({});
  useEffect(() => setForm(settings || {}), [settings]);
  return <Panel title="同步设置"><div className="form-grid"><input type="number" value={form.check_interval_minutes || ''} onChange={(e) => setForm({ ...form, check_interval_minutes: Number(e.target.value) })} placeholder="同步间隔分钟" /><input type="number" value={form.sync_concurrency || ''} onChange={(e) => setForm({ ...form, sync_concurrency: Number(e.target.value) })} placeholder="并发" /><input type="number" value={form.sync_retry_count || ''} onChange={(e) => setForm({ ...form, sync_retry_count: Number(e.target.value) })} placeholder="重试" /><input value={form.notify_webhook_url || ''} onChange={(e) => setForm({ ...form, notify_webhook_url: e.target.value })} placeholder="Webhook URL" /><input value={form.database_url || ''} onChange={(e) => setForm({ ...form, database_url: e.target.value })} placeholder="DATABASE_URL" /><button className="primary" onClick={() => api('PUT', '/settings', form).then(() => { reload(); notify('设置已保存'); })}>保存</button></div></Panel>;
}

function Badge({ value }: { value: any }) {
  return <span className={cx('badge', String(value).toLowerCase())}>{String(value || '-')}</span>;
}

createRoot(document.getElementById('root')!).render(<App />);
