/**
 * 通用占位页 —— 未实现模块的统一呈现。
 *
 * 设计立场（需求 10「预留后续开发」「诚实交代」）
 * ---------------------------------------------
 * 与其给一个空白页面或一句"敬请期待"，不如明确告诉用户三件事：
 *   1. **这个模块是做什么的**（scope 文案，来自语言包）；
 *   2. **它计划在哪个阶段实现**（阶段徽标 + 路线图位置）；
 *   3. **当前阶段已经能做什么**（返回概览 / 去设置）。
 *
 * 这样占位页本身就是产品的一部分，而不是"没做完"的证据。
 * 同时它也让开发者在未实现页面上仍能看到本模块的图标与视觉语言，
 * 便于早期评审整体信息架构。
 */

import { motion } from 'framer-motion'
import { Link } from 'react-router-dom'

import { Icon } from '@/components/Icon'
import { Card } from '@/components/ui'
import { staggerContainer, staggerItem } from '@/design/motion'
import { useI18n } from '@/i18n'
import type { NavItem } from '@/app/navigation'

interface PlaceholderProps {
  item: NavItem
}

export function PlaceholderPage({ item }: PlaceholderProps) {
  const { t } = useI18n()

  return (
    <motion.div variants={staggerContainer} initial="initial" animate="animate" className="space-y-5">
      {/* ---- 模块标题与阶段 ----------------------------------------------- */}
      <motion.header variants={staggerItem} className="flex items-start gap-3.5">
        <div className="flex h-11 w-11 shrink-0 items-center justify-center rounded-ab-md bg-surface-3/80 text-label-2">
          <Icon name={item.icon} size={21} />
        </div>
        <div className="min-w-0">
          <div className="flex flex-wrap items-baseline gap-2">
            <h2 className="text-ab-title1 text-label">{t(`phase.title`, { name: t(`nav.${item.id}`) })}</h2>
            <span className="ab-phase-badge">{item.phase}</span>
          </div>
          <p className="mt-1 max-w-2xl text-ab-callout text-label-2">
            {t('phase.badge', { phase: item.phase })}
          </p>
        </div>
      </motion.header>

      {/* ---- 该模块将提供什么 --------------------------------------------- */}
      <motion.div variants={staggerItem}>
        <Card title={t('phase.scopeLabel')}>
          <p className="text-ab-callout leading-relaxed text-label">{t(`scope.${item.id}`)}</p>
        </Card>
      </motion.div>

      {/* ---- 路线图位置与当前可做的事 ------------------------------------- */}
      <motion.div variants={staggerItem} className="grid grid-cols-1 gap-3.5 lg:grid-cols-2">
        <Card title={t('phase.roadmap')}>
          {/* 阶段时间轴：一条横向刻度，标出「当前阶段」与「本模块目标阶段」 */}
          <div className="flex items-end gap-1">
            {['P0', 'P1', 'P2', 'P3', 'P4', 'P5', 'P6', 'P7', 'P8', 'P9', 'P10'].map((phase) => {
              const isCurrent = phase === 'P0'
              const isTarget = phase === item.phase
              return (
                <div key={phase} className="flex flex-1 flex-col items-center gap-1.5">
                  <span
                    className={[
                      'w-full rounded-[4px] transition-colors',
                      isTarget ? 'h-8 bg-accent' : isCurrent ? 'h-5 bg-positive/70' : 'h-3 bg-hairline/10',
                    ].join(' ')}
                  />
                  <span
                    className={[
                      'ab-tnum text-ab-caption2',
                      isTarget ? 'font-semibold text-accent' : isCurrent ? 'text-positive' : 'text-label-3',
                    ].join(' ')}
                  >
                    {phase}
                  </span>
                </div>
              )
            })}
          </div>
          <div className="mt-3 flex flex-wrap items-center gap-x-4 gap-y-1 text-ab-caption text-label-2">
            <span className="inline-flex items-center gap-1.5">
              <span className="h-2 w-2 rounded-full bg-positive/70" />
              {t('phase.current', { phase: 'P0' })}
            </span>
            <span className="inline-flex items-center gap-1.5">
              <span className="h-2 w-2 rounded-full bg-accent" />
              {t('phase.badge', { phase: item.phase })}
            </span>
          </div>
        </Card>

        <Card>
          <p className="text-ab-footnote leading-relaxed text-label-2">
            {t('phase.note', { phase: item.phase })}
          </p>
          <div className="mt-3.5 flex flex-wrap gap-2">
            <Link to="/" className="ab-btn-primary">
              <Icon name="dashboard" size={14} />
              {t('phase.backToDashboard')}
            </Link>
            <Link to="/settings" className="ab-btn-secondary">
              <Icon name="settings" size={14} />
              {t('nav.settings')}
            </Link>
          </div>
        </Card>
      </motion.div>
    </motion.div>
  )
}
