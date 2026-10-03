/**
 * 关于页（P0 已真实可用）。
 *
 * 它承担三个职责：
 *   1. **版本与构建信息**：用户报问题时报的就是这一页的内容；
 *   2. **隐私承诺的可视化**：把「本地优先、不上云」写清楚并给出数据目录入口；
 *   3. **诊断入口**：给出可直接复制的自检命令，降低支持成本。
 *
 * 刻意不放"检查更新"按钮：P0 阶段没有更新服务，
 * 放一个点了没反应的按钮比不放更糟。
 */

import { motion } from 'framer-motion'
import { Icon } from '@/components/Icon'
import { Card, Skeleton } from '@/components/ui'
import { staggerContainer, staggerItem } from '@/design/motion'
import { useI18n } from '@/i18n'
import { boot, versionLabel } from '@/lib/boot'
import { formatDuration } from '@/lib/format'
import { useRuntimeInfo } from '@/pages/useRuntimeInfo'

export function AboutPage() {
  const { t } = useI18n()
  const { state } = useRuntimeInfo()

  const rows: { label: string; value: string; mono?: boolean }[] = [
    { label: t('about.version'), value: versionLabel, mono: true },
    { label: t('about.buildType'), value: boot.runtime.frozen ? t('about.buildFrozen') : t('about.buildSource') },
    { label: t('about.python'), value: state.status === 'ready' ? state.info.python_version : '—', mono: true },
    { label: t('about.platform'), value: state.status === 'ready' ? state.info.platform : '—' },
    { label: t('about.port'), value: state.status === 'ready' ? String(state.info.port) : '—', mono: true },
    { label: t('about.pid'), value: state.status === 'ready' ? String(state.info.pid) : '—', mono: true },
    {
      label: t('dashboard.fieldUptime'),
      value: state.status === 'ready' ? formatDuration(state.info.uptime_seconds) : '—',
    },
  ]

  return (
    <motion.div variants={staggerContainer} initial="initial" animate="animate" className="space-y-4">
      {/* ---- 品牌头 ------------------------------------------------------- */}
      <motion.div variants={staggerItem}>
        <Card>
          <div className="flex items-center gap-4">
            <div className="flex h-14 w-14 items-center justify-center rounded-[18px] bg-gradient-to-br from-accent to-indigo text-white shadow-ab-2">
              <Icon name="ledger" size={28} strokeWidth={1.7} />
            </div>
            <div className="min-w-0">
              <h2 className="text-ab-title2 text-label">{t('app.name')}</h2>
              <p className="mt-0.5 text-ab-callout text-label-2">{t('app.tagline')}</p>
              <p className="ab-tnum mt-1 font-mono text-ab-caption text-label-3">{versionLabel}</p>
              <a className="mt-2 inline-block text-ab-footnote text-accent" href="https://github.com/CaiNiaoJian/account-book/releases" target="_blank" rel="noreferrer">{t('about.downloads')}</a>
            </div>
          </div>
        </Card>
      </motion.div>

      {/* ---- 构建信息 ----------------------------------------------------- */}
      <motion.div variants={staggerItem}>
        <Card title={t('about.version')}>
          {state.status === 'loading' ? (
            <div className="space-y-2">
              {[0, 1, 2, 3].map((index) => (
                <Skeleton key={index} className="h-6 w-full" />
              ))}
            </div>
          ) : (
            <dl className="divide-y divide-separator/40">
              {rows.map((row) => (
                <div key={row.label} className="flex items-center justify-between gap-4 py-[7px]">
                  <dt className="text-ab-footnote text-label-2">{row.label}</dt>
                  <dd
                    className={[
                      'ab-selectable truncate text-ab-footnote text-label',
                      row.mono ? 'ab-tnum font-mono text-ab-caption' : '',
                    ]
                      .filter(Boolean)
                      .join(' ')}
                    title={row.value}
                  >
                    {row.value}
                  </dd>
                </div>
              ))}
            </dl>
          )}
        </Card>
      </motion.div>

      {/* ---- 隐私与文档 --------------------------------------------------- */}
      <motion.div variants={staggerItem} className="grid grid-cols-1 gap-3.5 lg:grid-cols-2">
        <Card title={t('about.privacyTitle')}>
          <p className="text-ab-footnote leading-relaxed text-label-2">{t('about.privacyNote')}</p>
          <p className="ab-selectable mt-3 break-all rounded-ab-sm bg-surface-2/80 px-2.5 py-2 font-mono text-ab-caption text-label-2">
            {state.status === 'ready' ? state.info.paths.data_dir : boot.paths.dataDir || '—'}
          </p>
        </Card>

        <Card title={t('about.docsTitle')}>
          <p className="text-ab-footnote leading-relaxed text-label-2">{t('about.docsNote')}</p>
          <ul className="mt-3 space-y-1.5">
            {[
              'docs/PLAN.md',
              'docs/ARCHITECTURE.md',
              'docs/DESIGN_SYSTEM.md',
              'docs/ROADMAP.md',
            ].map((file) => (
              <li key={file} className="flex items-center gap-2 text-ab-footnote text-label-2">
                <Icon name="reports" size={13} className="shrink-0 text-label-3" />
                <span className="ab-selectable font-mono text-ab-caption">{file}</span>
              </li>
            ))}
          </ul>
        </Card>
      </motion.div>

      {/* ---- 诊断 --------------------------------------------------------- */}
      <motion.div variants={staggerItem}>
        <Card title={t('about.diagnosticsTitle')}>
          <p className="text-ab-footnote text-label-2">{t('about.diagnosticsNote')}</p>
          <div className="mt-2.5 space-y-2">
            {['python run.py --doctor', 'python run.py --print-paths'].map((command) => (
              <pre
                key={command}
                className="ab-selectable overflow-x-auto rounded-ab-sm bg-surface-2/80 px-2.5 py-2 font-mono text-ab-caption text-label"
              >
                {command}
              </pre>
            ))}
          </div>
          <p className="mt-3 text-ab-caption text-label-3">{t('about.licenseNote')}</p>
        </Card>
      </motion.div>
    </motion.div>
  )
}
